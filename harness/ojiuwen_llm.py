"""openJiuwen-backed LLM client.

This adapter is the production model layer for the final image.  It keeps the
same synchronous ``chat(...) -> LLMResponse`` contract as
:class:`harness.llm.LLMClient`, while using openJiuwen's async
``Model.invoke`` under the hood.

Design notes
------------
* ``openjiuwen`` is imported lazily, only when the first model is built.  The
  stdlib harness and unit tests never require it.
* A single background event-loop thread is started lazily and reused for every
  model build and every ``invoke``.  The openJiuwen OpenAI client caches an
  ``AsyncOpenAI`` instance bound to the loop that created it, so using one
  persistent loop avoids ``RuntimeError: Event loop is closed`` on the second
  call.
* openJiuwen's global logging is reconfigured before the SDK initializes:
  logs go to ``/tmp/openjiuwen-logs``, console output is suppressed, and
  WARNING+ records are mirrored to stderr.  This keeps stdout clean for the
  final JSON summary and prevents writes to the read-only ``/app/logs``.
* Usage is mapped from ``AssistantMessage.usage_metadata``:
  ``prompt_tokens = input_tokens``, ``completion_tokens = output_tokens``.
  If ``output_tokens`` is zero but ``reasoning_tokens`` is positive, the
  reasoning tokens are used as the completion count.  Reasoning is *not*
  added a second time because OpenAI-style ``output_tokens`` already includes
  it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .llm import (
    DEFAULT_MODEL,
    ESCALATION_MODEL,
    ChatMessage,
    LLMConfig,
    LLMError,
    LLMResponse,
    LLMUsage,
    model_for_attempt,
)

__all__ = [
    "ModelFactory",
    "ModelSpec",
    "OpenJiuwenLLMClient",
]

log = logging.getLogger(__name__)

# The final container rootfs is read-only and has no /app/logs tmpfs.  Keep
# SDK logs on the writable /tmp mount.
OPENJIUWEN_LOG_DIR = "/tmp/openjiuwen-logs"
_LOGGING_CONFIG_LOCK = threading.Lock()
_LOGGING_CONFIGURED = False


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Everything the real openJiuwen ``Model`` needs, decoupled for tests."""

    model_name: str
    api_key: str
    api_base: str
    verify_ssl: bool
    timeout: float
    temperature: float
    max_tokens: int
    extra_body: Mapping[str, Any] | None
    custom_headers: Mapping[str, str]


ModelFactory = Callable[[ModelSpec], Any]


# -- environment helpers ------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("ignoring invalid %s=%r", name, raw)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _parse_extra_body(raw: str | None) -> dict[str, Any] | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError("OJ_EXTRA_BODY must be valid JSON") from exc
    if not isinstance(value, dict):
        raise LLMError("OJ_EXTRA_BODY must be a JSON object")
    return value


def _configure_openjiuwen_logging() -> bool:
    """Point openJiuwen logging at /tmp before the SDK initializes.

    The default SDK config uses ``log_path="./logs/"`` and
    ``log_file="run/jiuwen.log"``, which resolves to ``/app/logs/...`` in the
    final image.  The rootfs is read-only, so configure the global in-memory
    logging config first:

    * files under ``/tmp/openjiuwen-logs``;
    * WARNING level;
    * no console handler on stdout (the harness stdout must stay JSON-only);
    * WARNING+ mirrored to stderr, so real errors remain visible.

    Returns ``True`` when openJiuwen's logging API was found and configured.
    """

    global _LOGGING_CONFIGURED

    with _LOGGING_CONFIG_LOCK:
        if _LOGGING_CONFIGURED:
            return True

        try:
            from openjiuwen.core.common.logging.log_config import (  # type: ignore[import-not-found]
                configure_log_config,
            )
        except ImportError:
            return False

        file_output = True
        try:
            os.makedirs(OPENJIUWEN_LOG_DIR, exist_ok=True)
        except OSError as exc:
            file_output = False
            log.warning("cannot create openJiuwen log dir %s: %s", OPENJIUWEN_LOG_DIR, exc)

        outputs = ["file"] if file_output else []
        config = {
            "backend": "default",
            "level": "WARNING",
            "log_path": OPENJIUWEN_LOG_DIR,
            "output": outputs,
            "interface_output": list(outputs),
            "performance_output": list(outputs),
            "propagate": True,
        }

        try:
            configure_log_config(config)
        except Exception as exc:  # noqa: BLE001 - keep the harness alive
            log.error("failed to configure openJiuwen logging: %s", exc)
            return False

        # Force the built-in loggers to materialize under the new config, then
        # remove any stdout console handlers and add a stderr mirror.
        try:
            from openjiuwen.core.common.logging.manager import (  # type: ignore[import-not-found]
                LogManager,
            )

            wrappers = list(LogManager.get_all_loggers().values())
        except Exception as exc:  # noqa: BLE001 - logging is best-effort
            log.warning("could not inspect openJiuwen loggers: %s", exc)
            wrappers = []

        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

        def mirror_to_stderr(target: logging.Logger | None) -> None:
            if target is None:
                return
            target.setLevel(logging.WARNING)
            target.propagate = False
            for handler in list(target.handlers):
                if getattr(handler, "stream", None) is sys.stdout:
                    target.removeHandler(handler)
            if not any(getattr(handler, "_techlb_stderr", False) for handler in target.handlers):
                handler = logging.StreamHandler(stream=sys.stderr)
                handler.setLevel(logging.WARNING)
                handler.setFormatter(formatter)
                handler._techlb_stderr = True  # type: ignore[attr-defined]
                target.addHandler(handler)

        for wrapper in wrappers:
            mirror_to_stderr(getattr(wrapper, "_logger", None))
        mirror_to_stderr(logging.getLogger("openjiuwen"))

        _LOGGING_CONFIGURED = True
        return True


def _load_model_classes() -> tuple[Any, Any, Any]:
    """Import the real openJiuwen classes lazily.

    Kept as a separate function so tests can force an ``ImportError`` without
    installing the package.
    """

    from openjiuwen.core.foundation.llm import (  # type: ignore[import-not-found]
        Model,
        ModelClientConfig,
        ModelRequestConfig,
    )

    return Model, ModelClientConfig, ModelRequestConfig


def _default_model_factory(spec: ModelSpec) -> Any:
    # Must run before Model/SDK logging initializes.
    _configure_openjiuwen_logging()
    try:
        model_cls, client_config_cls, request_config_cls = _load_model_classes()
    except ImportError as exc:
        raise LLMError(
            "openjiuwen is not installed; install openjiuwen==0.1.20.post1 "
            "(see requirements-openjiuwen.txt) or use --llm-backend stdlib"
        ) from exc

    try:
        client_config = client_config_cls(
            client_provider="OpenAI",
            api_key=spec.api_key,
            api_base=spec.api_base,
            verify_ssl=spec.verify_ssl,
            timeout=spec.timeout,
            custom_headers=dict(spec.custom_headers),
        )
        request_kwargs: dict[str, Any] = {
            "model_name": spec.model_name,
            "temperature": spec.temperature,
            "max_tokens": spec.max_tokens,
        }
        if spec.extra_body is not None:
            request_kwargs["extra_body"] = dict(spec.extra_body)
        request_config = request_config_cls(**request_kwargs)
        # The openJiuwen 0.1.20.post1 signature is
        # Model(model_client_config=..., model_config=...).
        return model_cls(
            model_client_config=client_config,
            model_config=request_config,
        )
    except Exception as exc:
        raise LLMError(
            f"failed to construct openJiuwen Model: {type(exc).__name__}: {exc}"
        ) from exc


# -- message / response helpers ----------------------------------------------
def _normalise_messages(
    messages: Sequence[Mapping[str, Any] | ChatMessage],
) -> list[dict[str, str]]:
    if not messages:
        raise LLMError("messages must not be empty")
    result: list[dict[str, str]] = []
    for message in messages:
        if isinstance(message, ChatMessage):
            role, content = message.role, message.content
        elif isinstance(message, Mapping):
            role = str(message.get("role", ""))
            content = message.get("content", "")
        else:
            raise LLMError(f"unsupported message type: {type(message)!r}")
        if role not in {"system", "user", "assistant", "tool"}:
            raise LLMError(f"unsupported message role: {role!r}")
        result.append({"role": role, "content": "" if content is None else str(content)})
    return result


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _estimate_prompt(messages: Sequence[Mapping[str, str]]) -> int:
    chars = sum(len(m.get("content", "")) for m in messages)
    return max(1, (chars + 3) // 4)


class OpenJiuwenLLMClient:
    """Sync facade over openJiuwen's async ``Model.invoke``."""

    backend_name = "openjiuwen"

    def __init__(
        self,
        config: LLMConfig,
        *,
        model_factory: ModelFactory | None = None,
        custom_headers: Mapping[str, str] | None = None,
        extra_body: Mapping[str, Any] | None = None,
        verify_ssl: bool | None = None,
        timeout: float | None = None,
    ) -> None:
        self.config = config
        self._model_factory: ModelFactory = model_factory or _default_model_factory
        self._custom_headers = dict(custom_headers or {})
        self._extra_body = (
            _parse_extra_body(os.environ.get("OJ_EXTRA_BODY"))
            if extra_body is None
            else dict(extra_body)
        )
        self._verify_ssl = (
            _env_bool("LLM_VERIFY_SSL", True) if verify_ssl is None else bool(verify_ssl)
        )
        self._timeout = (
            _env_float("LLM_TIMEOUT", float(config.timeout))
            if timeout is None
            else float(timeout)
        )
        self._models: dict[str, Any] = {}
        self._models_lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_lock = threading.RLock()
        self._closed = False

    @classmethod
    def from_env(cls, **kwargs: Any) -> "OpenJiuwenLLMClient":
        return cls(LLMConfig.from_env(**kwargs))

    @property
    def available(self) -> bool:
        return bool(self.config.base_url and self.config.api_key)

    @property
    def verify_ssl(self) -> bool:
        return self._verify_ssl

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def extra_body(self) -> Mapping[str, Any] | None:
        return self._extra_body

    @property
    def closed(self) -> bool:
        with self._loop_lock:
            return self._closed

    # -- persistent background event loop ------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Start (once) and return the client's background event loop."""

        with self._loop_lock:
            if self._closed:
                raise LLMError("OpenJiuwenLLMClient is closed")

            loop = self._loop
            if loop is not None and loop.is_running():
                return loop

            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def runner() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                try:
                    loop.run_forever()
                finally:
                    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        try:
                            loop.run_until_complete(
                                asyncio.gather(*pending, return_exceptions=True)
                            )
                        except Exception:  # pragma: no cover - shutdown best effort
                            pass

            thread = threading.Thread(
                target=runner,
                name="openjiuwen-llm-loop",
                daemon=True,
            )
            thread.start()
            if not ready.wait(timeout=10.0):
                try:
                    loop.close()
                except Exception:
                    pass
                raise LLMError("failed to start openJiuwen background event loop")

            self._loop = loop
            self._loop_thread = thread
            return loop

    def _submit(self, coro: Awaitable[Any]) -> Any:
        """Run a coroutine on the persistent loop and wait for its result."""

        try:
            loop = self._ensure_loop()
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except BaseException:
            if inspect.iscoroutine(coro):
                coro.close()
            raise
        return future.result()

    def _build_model(self, spec: ModelSpec) -> Any:
        async def build() -> Any:
            return self._model_factory(spec)

        return self._submit(build())

    def _close_model(self, model: Any, loop: asyncio.AbstractEventLoop) -> None:
        targets: list[Any] = []
        seen: set[int] = set()
        for target in (model, getattr(model, "_client", None)):
            if target is None or id(target) in seen:
                continue
            seen.add(id(target))
            targets.append(target)

        for target in targets:
            closer = getattr(target, "aclose", None) or getattr(target, "close", None)
            if not callable(closer):
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    future = asyncio.run_coroutine_threadsafe(result, loop)
                    future.result(timeout=30.0)
            except Exception as exc:  # noqa: BLE001 - close is best effort
                log.warning("failed to close openJiuwen model resource: %s", exc)

    def close(self) -> None:
        """Stop the background loop and close cached models.  Idempotent."""

        with self._loop_lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._loop_thread
            models = list(self._models.values())
            self._models.clear()
            self._loop = None
            self._loop_thread = None

        if loop is None:
            return

        for model in models:
            self._close_model(model, loop)

        if thread is not None and thread is threading.current_thread():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
            return

        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if thread is not None:
            thread.join(timeout=10.0)
        try:
            loop.close()
        except Exception:
            pass

    def resolve_model(self, attempt: int = 0, override: str | None = None) -> str:
        if override:
            return override
        return model_for_attempt(self.config, attempt)

    def _make_spec(self, model_name: str) -> ModelSpec:
        if not self.config.base_url:
            raise LLMError("LLM_BASE_URL is not configured")
        if not self.config.api_key:
            raise LLMError("LLM_API_KEY is not configured")
        return ModelSpec(
            model_name=model_name,
            api_key=self.config.api_key,
            # The SDK does NOT append /v1; pass the configured base verbatim.
            api_base=self.config.base_url,
            verify_ssl=self._verify_ssl,
            timeout=self._timeout,
            temperature=float(self.config.temperature),
            max_tokens=int(self.config.max_tokens),
            extra_body=self._extra_body,
            custom_headers=self._custom_headers,
        )

    def get_model(self, model_name: str | None = None) -> Any:
        """Build (once) and return the openJiuwen ``Model`` for a tier."""

        name = model_name or self.config.model
        with self._models_lock:
            model = self._models.get(name)
            if model is None:
                model = self._build_model(self._make_spec(name))
                self._models[name] = model
            return model

    def chat(
        self,
        messages: Sequence[Mapping[str, Any] | ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        attempt: int = 0,
    ) -> LLMResponse:
        if not self.config.base_url:
            raise LLMError("LLM_BASE_URL is not configured")
        if not self.config.api_key:
            raise LLMError("LLM_API_KEY is not configured")

        normalised = _normalise_messages(messages)
        chosen_model = self.resolve_model(attempt=attempt, override=model)
        chosen_max_tokens = int(self.config.max_tokens if max_tokens is None else max_tokens)
        chosen_temperature = float(
            self.config.temperature if temperature is None else temperature
        )

        try:
            model_obj = self.get_model(chosen_model)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(
                f"failed to build openJiuwen model {chosen_model!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        async def invoke() -> Any:
            result = model_obj.invoke(
                normalised,
                max_tokens=chosen_max_tokens,
                temperature=chosen_temperature,
            )
            if inspect.isawaitable(result):
                result = await result
            return result

        started = time.perf_counter()
        try:
            assistant = self._submit(invoke())
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(
                f"openJiuwen invoke failed: {type(exc).__name__}: {exc}"
            ) from exc
        latency = time.perf_counter() - started

        content = _get(assistant, "content", "")
        text = "" if content is None else str(content)

        usage_meta = _get(assistant, "usage_metadata", None)
        if usage_meta is None:
            usage_meta = _get(assistant, "usage", None)

        estimated = False
        if usage_meta is None:
            estimated = True
            prompt_tokens = _estimate_prompt(normalised)
            completion_tokens = _estimate_tokens(text)
        else:
            prompt_tokens = _to_int(
                _get(usage_meta, "input_tokens", _get(usage_meta, "prompt_tokens", 0))
            )
            output_tokens = _to_int(
                _get(usage_meta, "output_tokens", _get(usage_meta, "completion_tokens", 0))
            )
            reasoning_tokens = _to_int(_get(usage_meta, "reasoning_tokens", 0))
            # output_tokens already includes reasoning in OpenAI-style usage.
            completion_tokens = output_tokens if output_tokens > 0 else reasoning_tokens

        return LLMResponse(
            text=text,
            model=chosen_model,
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated=estimated,
            ),
            latency=latency,
        )

    def __repr__(self) -> str:  # never include the API key
        return (
            f"OpenJiuwenLLMClient(base_url={self.config.base_url!r}, "
            f"model={self.config.model!r}, available={self.available})"
        )

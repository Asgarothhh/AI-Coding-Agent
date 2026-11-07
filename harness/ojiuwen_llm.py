"""openJiuwen-backed LLM client.

This adapter is the production model layer for the final image.  It keeps the
same synchronous ``chat(...) -> LLMResponse`` contract as
:class:`harness.llm.LLMClient`, while using openJiuwen's async
``Model.invoke`` under the hood.

Design notes
------------
* ``openjiuwen`` is imported lazily, only when the first model is built.  The
  stdlib harness and unit tests never require it.
* ``asyncio.run`` is used for the normal sync path.  If the caller is already
  inside an event loop, the coroutine runs in a short-lived worker thread with
  its own loop so sync callers never deadlock.
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


# -- async bridge -------------------------------------------------------------
def _run_async(factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``factory()`` from sync code, even inside a running event loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["result"] = asyncio.run(factory())
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller thread
            box["error"] = exc

    thread = threading.Thread(target=runner, name="openjiuwen-llm", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


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
                model = self._model_factory(self._make_spec(name))
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
            assistant = _run_async(invoke)
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

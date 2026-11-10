"""Single openJiuwen Core gateway for every production model call.

The gateway implements the contract from ``specs/001-token-estimator/spec.md``
v5.0, sections 5.2, 5.3, 5.4, 4.2 and 11.4:

* only the four allowed proxy models can be requested;
* the SDK is imported lazily from ``openjiuwen.core.foundation.llm`` and a
  missing package is reported as :class:`GatewayUnavailableError` instead of
  falling back to stdlib/HTTP;
* ``LLM_BASE_URL``/``LLM_API_KEY`` are mapped to ``api_base``/``api_key`` and
  TLS verification stays on unless explicitly disabled;
* raw usage is handed to the single ``harness.usage`` normalizer exactly once
  per request, so reasoning tokens cannot be counted twice here;
* an optional budget ledger reserves before the call, settles afterwards and
  releases on a failed call.  Missing ledger methods are a configuration error,
  never a silently skipped accounting step.

The persistent background event loop mirrors
:class:`harness.ojiuwen_llm.OpenJiuwenLLMClient`: the OpenAI async client caches
an ``AsyncOpenAI`` bound to the loop that created it, so every model build and
invoke runs on one long-lived loop.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import inspect
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, Self, runtime_checkable

from .llm import (
    ChatMessage,
    LLMConfig,
    LLMError,
    LLMResponse,
    LLMUsage,
    model_for_attempt,
)
from .usage import TokenUsage

__all__ = [
    "ALLOWED_MODELS",
    "GatewayBudgetError",
    "GatewayChatAdapter",
    "GatewayConfigError",
    "GatewayError",
    "GatewayModelSpec",
    "GatewayUnavailableError",
    "LLMResult",
    "OpenJiuwenGateway",
    "TokenUsageLike",
    "estimate_input_tokens",
]

log = logging.getLogger(__name__)

# Section 5.4: the complete allow-list.  Anything else is a configuration
# error and must be rejected before the SDK is touched.
ALLOWED_MODELS: tuple[str, ...] = (
    "qwen3.8-27b",
    "qwen3.8-27b-noreason",
    "qwen3.6-35b-a3b",
    "qwen3.6-35b-a3b-noreason",
)
ALLOWED_MODEL_SET = frozenset(ALLOWED_MODELS)

DEFAULT_CLIENT_PROVIDER = "OpenAI"
DEFAULT_MODEL_MAX_TOKENS = 1024
SDK_VERSION_HINT = "openjiuwen==0.1.20.post1"
SDK_INSTALL_HINT = (
    f"install {SDK_VERSION_HINT} (see requirements-openjiuwen.txt); "
    "the gateway never falls back to stdlib/HTTP or another endpoint"
)

_MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool"})


class GatewayError(LLMError):
    """Base class for gateway failures."""


class GatewayConfigError(GatewayError):
    """Invalid gateway configuration (model, ledger, usage module, ...)."""


class GatewayUnavailableError(GatewayError):
    """The openJiuwen SDK or another required dependency is missing."""


class GatewayBudgetError(GatewayError):
    """The ledger refused or could not reserve budget for the request."""


@runtime_checkable
class TokenUsageLike(Protocol):
    """Local fallback protocol for the shared ``harness.usage.TokenUsage``.

    The real class is owned by ``harness/usage.py``.  The gateway must not
    duplicate it, so this protocol only documents the attributes that the
    gateway validates before settling a reservation.
    """

    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_cost: float
    source_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LLMResult:
    """Result of one gateway call (spec section 5.2)."""

    text: str
    usage: TokenUsage
    model: str
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True, repr=False)
class GatewayModelSpec:
    """Everything the SDK needs for one model, decoupled for tests."""

    model_name: str
    api_key: str
    api_base: str
    client_provider: str
    verify_ssl: bool
    timeout: float
    temperature: float
    max_tokens: int
    custom_headers: Mapping[str, str]

    def __repr__(self) -> str:  # never include api_key or the proxy URL
        return (
            f"GatewayModelSpec(model_name={self.model_name!r}, "
            f"api_base_configured={bool(self.api_base)}, "
            f"client_provider={self.client_provider!r}, "
            f"verify_ssl={self.verify_ssl}, timeout={self.timeout}, "
            f"temperature={self.temperature}, max_tokens={self.max_tokens})"
        )


ModelFactory = Callable[[GatewayModelSpec], Any]
SyncRunner = Callable[[Coroutine[Any, Any, Any]], Any]


# -- small helpers ------------------------------------------------------------
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


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_mapping(value: Any) -> dict[str, Any] | None:
    """Best-effort conversion of an SDK usage object into a plain dict."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    for name in ("model_dump", "to_dict", "dict"):
        method = getattr(value, name, None)
        if not callable(method):
            continue
        try:
            data = method()
        except TypeError:
            continue
        if isinstance(data, Mapping):
            return dict(data)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, Mapping):
        return {key: item for key, item in raw.items() if not key.startswith("_")}
    return None


def estimate_input_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Conservative local prompt estimate used for the pre-call reservation.

    The authoritative count always comes from the proxy usage; this estimate
    only has to be safe enough to reserve budget before sending.
    """

    chars = 0
    for message in messages:
        content = message.get("content", "") if isinstance(message, Mapping) else ""
        chars += len("" if content is None else str(content))
    return max(1, (chars + 3) // 4)


def _normalise_messages(
    messages: Sequence[Mapping[str, Any] | ChatMessage],
) -> list[dict[str, str]]:
    if not messages:
        raise GatewayConfigError("messages must not be empty")
    result: list[dict[str, str]] = []
    for message in messages:
        if isinstance(message, ChatMessage):
            role, content = message.role, message.content
        elif isinstance(message, Mapping):
            role = str(message.get("role", ""))
            content = message.get("content", "")
        else:
            raise GatewayConfigError(f"unsupported message type: {type(message)!r}")
        if role not in _MESSAGE_ROLES:
            raise GatewayConfigError(f"unsupported message role: {role!r}")
        result.append({"role": role, "content": "" if content is None else str(content)})
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GatewayConfigError(f"{name} must be a positive integer")
    return value


def _request_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GatewayConfigError("request_id must be a non-empty string")
    return value.strip()


def _signature_names(method: Any) -> tuple[list[str], set[str]]:
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return [], set()
    names = list(parameters)
    # A bound method already hides ``self``; an unbound one does not.
    if names and names[0] in {"self", "cls"}:
        names = names[1:]
    return names, set(names)


# -- lazy SDK import ----------------------------------------------------------
def _load_sdk_classes() -> tuple[Any, Any, Any]:
    """Import the openJiuwen Core classes lazily.

    Kept as a function so tests can simulate a missing package and so the
    stdlib harness never imports the SDK at startup.
    """

    try:
        from openjiuwen.core.foundation.llm import (  # type: ignore[import-not-found]
            Model,
            ModelClientConfig,
            ModelRequestConfig,
        )
    except ImportError as exc:
        raise GatewayUnavailableError(
            f"openjiuwen is not installed or cannot be imported ({exc}); {SDK_INSTALL_HINT}"
        ) from exc
    return Model, ModelClientConfig, ModelRequestConfig


def _configure_sdk_logging() -> None:
    """Reuse the SDK logging setup from the existing adapter (best effort)."""

    try:
        from .ojiuwen_llm import _configure_openjiuwen_logging
    except Exception:  # noqa: BLE001 - optional helper, pragma: no cover
        return
    try:
        _configure_openjiuwen_logging()
    except Exception as exc:  # noqa: BLE001 - logging is best effort, pragma: no cover
        log.debug("openJiuwen logging setup failed: %s", exc)


def _default_model_factory(spec: GatewayModelSpec) -> Any:
    _configure_sdk_logging()
    model_cls, client_config_cls, request_config_cls = _load_sdk_classes()
    try:
        client_config = client_config_cls(
            client_provider=spec.client_provider,
            api_key=spec.api_key,
            api_base=spec.api_base,
            verify_ssl=spec.verify_ssl,
            timeout=spec.timeout,
            custom_headers=dict(spec.custom_headers),
        )
        request_config = request_config_cls(
            model_name=spec.model_name,
            temperature=spec.temperature,
            max_tokens=spec.max_tokens,
        )
        return model_cls(
            model_client_config=client_config,
            model_config=request_config,
        )
    except Exception as exc:  # translate any SDK constructor failure
        raise GatewayError(
            f"failed to construct openJiuwen Model for {spec.model_name!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


# -- persistent loop adapter --------------------------------------------------
class _PersistentLoop:
    """Daemon event loop reused for every model build and invoke.

    This mirrors the loop used by :class:`harness.ojiuwen_llm.OpenJiuwenLLMClient`
    (that adapter keeps its loop private, so the gateway carries an equivalent
    implementation instead of reaching into another module's internals).
    """

    def __init__(self, name: str = "openjiuwen-gateway-loop") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._closed = False

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Any:
        loop = self._ensure_loop()
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except BaseException:
            if inspect.iscoroutine(coro):
                coro.close()
            raise
        return future.result()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._closed:
                raise GatewayError("openJiuwen gateway event loop is closed")
            if self._loop is not None and self._loop.is_running():
                return self._loop

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
                        except Exception as exc:  # noqa: BLE001 - shutdown only
                            log.debug("openJiuwen loop shutdown ignored: %s", exc)

            thread = threading.Thread(target=runner, name=self._name, daemon=True)
            thread.start()
            if not ready.wait(timeout=10.0):
                try:
                    loop.close()
                except Exception as exc:  # noqa: BLE001 - cleanup only
                    log.debug("failed to close unused loop: %s", exc)
                raise GatewayError("failed to start openJiuwen gateway event loop")

            self._loop = loop
            self._thread = thread
            return loop

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None

        if loop is None:
            return
        if thread is not None and thread is threading.current_thread():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception as exc:  # noqa: BLE001 - close is best effort
                log.debug("failed to stop gateway loop: %s", exc)
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception as exc:  # noqa: BLE001 - close is best effort
            log.debug("failed to stop gateway loop: %s", exc)
        if thread is not None:
            thread.join(timeout=10.0)
        try:
            loop.close()
        except Exception as exc:  # noqa: BLE001 - close is best effort
            log.debug("failed to close gateway loop: %s", exc)


# -- ledger dispatch ----------------------------------------------------------
def _ledger_reserve(
    ledger: Any,
    request_id: str,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
) -> Any:
    """Call ``reserve`` supporting both the task contract and spec 4.1.

    The gateway contract passes ``estimated_input_tokens``/
    ``estimated_output_tokens``; the older ``BudgetLedger`` protocol in section
    4.1 names the same values ``estimated_input``/``max_output``.  Dispatching
    on the callable's signature keeps both compatible without guessing.
    """

    method = ledger.reserve
    names, name_set = _signature_names(method)
    if not names:  # *args or unknown signature
        return method(request_id, estimated_input_tokens, estimated_output_tokens)

    if "estimated_input_tokens" in name_set or "estimated_output_tokens" in name_set:
        kwargs: dict[str, Any] = {}
        if "request_id" in name_set:
            kwargs["request_id"] = request_id
        if "estimated_input_tokens" in name_set:
            kwargs["estimated_input_tokens"] = estimated_input_tokens
        if "estimated_output_tokens" in name_set:
            kwargs["estimated_output_tokens"] = estimated_output_tokens
        return method(**kwargs)

    if "estimated_input" in name_set or "max_output" in name_set:
        kwargs = {}
        if "request_id" in name_set:
            kwargs["request_id"] = request_id
        if "estimated_input" in name_set:
            kwargs["estimated_input"] = estimated_input_tokens
        if "max_output" in name_set:
            kwargs["max_output"] = estimated_output_tokens
        if "safety_margin" in name_set:
            kwargs["safety_margin"] = 0.0
        return method(**kwargs)

    return method(request_id, estimated_input_tokens, estimated_output_tokens)


def _ledger_settle(ledger: Any, request_id: str, reservation: Any, usage: Any) -> None:
    method = ledger.settle
    names, _ = _signature_names(method)
    first = names[0] if names else ""
    if first in {"request_id", "request", "rid"}:
        method(request_id, usage)
        return
    method(reservation, usage)


def _ledger_release(
    ledger: Any,
    request_id: str,
    reservation: Any,
    *,
    definitely_not_sent: bool,
) -> None:
    method = getattr(ledger, "release", None)
    if callable(method):
        method(reservation)
        return

    method = getattr(ledger, "release_failed", None)
    if callable(method):
        _, name_set = _signature_names(method)
        if "definitely_not_sent" in name_set:
            method(request_id, definitely_not_sent=definitely_not_sent)
        else:  # pragma: no cover - legacy ledger without the flag
            method(request_id)
        return

    raise GatewayConfigError(
        "ledger must provide release(reservation) or release_failed(request_id, "
        "definitely_not_sent=...) so accounting is never skipped"
    )


# -- gateway ------------------------------------------------------------------
class OpenJiuwenGateway:
    """The only production path to the LLM proxy (spec section 5.2)."""

    backend_name = "openjiuwen-gateway"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        client_provider: str | None = None,
        verify_ssl: bool | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
        ledger: Any | None = None,
        model_factory: ModelFactory | None = None,
        normalizer: Any | None = None,
        runner: SyncRunner | None = None,
    ) -> None:
        self._base_url = self._resolve_text(base_url, "LLM_BASE_URL")
        self._api_key = self._resolve_text(api_key, "LLM_API_KEY")
        provider = self._resolve_text(client_provider, "LLM_CLIENT_PROVIDER", "MODEL_PROVIDER")
        self._client_provider = provider or DEFAULT_CLIENT_PROVIDER
        self._verify_ssl = (
            _env_bool("LLM_VERIFY_SSL", True) if verify_ssl is None else bool(verify_ssl)
        )
        self._timeout = float(timeout if timeout is not None else _env_float("LLM_TIMEOUT", 60.0))
        self._temperature = float(
            temperature if temperature is not None else _env_float("LLM_TEMPERATURE", 0.0)
        )
        if not self._client_provider:
            raise GatewayConfigError("client_provider must not be empty")

        self._ledger = ledger
        self._model_factory: ModelFactory = model_factory or _default_model_factory
        self._uses_default_factory = model_factory is None
        self._normalizer = normalizer
        self._normalizer_resolved = normalizer is not None
        self._runner = runner
        self._loop = _PersistentLoop()
        self._models: dict[str, Any] = {}
        self._models_lock = threading.RLock()
        self._closed = False

        if not self._verify_ssl:
            log.warning("LLM_VERIFY_SSL disabled; only do this with confirmed infrastructure")

    @staticmethod
    def _resolve_text(
        explicit: str | None,
        *env_names: str,
    ) -> str:
        if explicit is not None:
            return explicit.strip()
        for name in env_names:
            value = os.environ.get(name)
            if value and value.strip():
                return value.strip()
        return ""

    # -- read-only accessors (never expose the key or the proxy URL) ---------
    @property
    def available(self) -> bool:
        return bool(self._base_url and self._api_key)

    @property
    def client_provider(self) -> str:
        return self._client_provider

    @property
    def verify_ssl(self) -> bool:
        return self._verify_ssl

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def closed(self) -> bool:
        return self._closed

    def __repr__(self) -> str:  # never include api_key or the proxy URL
        return (
            f"OpenJiuwenGateway(client_provider={self._client_provider!r}, "
            f"verify_ssl={self._verify_ssl}, available={self.available}, "
            f"models={len(ALLOWED_MODELS)})"
        )

    # -- validation ----------------------------------------------------------
    def _require_credentials(self) -> None:
        if not self._base_url:
            raise GatewayConfigError("LLM_BASE_URL is not configured")
        if not self._api_key:
            raise GatewayConfigError("LLM_API_KEY is not configured")

    @staticmethod
    def _validate_model(model: Any) -> str:
        if not isinstance(model, str) or not model.strip():
            raise GatewayConfigError("model must be a non-empty string")
        chosen = model.strip()
        if chosen not in ALLOWED_MODEL_SET:
            allowed = ", ".join(ALLOWED_MODELS)
            raise GatewayConfigError(
                f"model {chosen!r} is not allowed; only these proxy models are permitted: {allowed}"
            )
        return chosen

    @staticmethod
    def _validate_ledger(ledger: Any | None) -> None:
        if ledger is None:
            return
        if not callable(getattr(ledger, "reserve", None)) or not callable(
            getattr(ledger, "settle", None)
        ):
            raise GatewayConfigError(
                "ledger must provide callable reserve() and settle(); "
                "refusing to skip budget accounting"
            )
        if not callable(getattr(ledger, "release", None)) and not callable(
            getattr(ledger, "release_failed", None)
        ):
            raise GatewayConfigError(
                "ledger must provide release(reservation) or "
                "release_failed(request_id, definitely_not_sent=...); "
                "refusing to skip budget accounting"
            )

    def _ensure_normalizer(self) -> Any:
        if self._normalizer_resolved:
            return self._normalizer

        package = __package__ or "harness"
        try:
            module = importlib.import_module(".usage", package)
        except ImportError as exc:
            raise GatewayConfigError(
                "usage normalization is unavailable: harness/usage.py could not be "
                "imported, so a paid response could not be accounted for; "
                "the gateway refuses to skip accounting"
            ) from exc

        candidate = None
        for name in ("normalize_usage", "normalize", "UsageNormalizer", "UsageNormaliser"):
            candidate = getattr(module, name, None)
            if candidate is not None:
                break
        if candidate is None:
            raise GatewayConfigError(
                "harness.usage must expose normalize_usage(raw), normalize(raw) or "
                "UsageNormalizer(...); none was found"
            )

        if isinstance(candidate, type):
            try:
                candidate = candidate()
            except Exception as exc:
                raise GatewayConfigError(
                    f"failed to instantiate {candidate.__name__} from harness.usage: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

        self._normalizer = candidate
        self._normalizer_resolved = True
        return candidate

    @staticmethod
    def _normalize_usage(normalizer: Any, raw_usage: Any) -> Any:
        """Call the shared normalizer exactly once and validate its result."""

        method = None
        for name in ("normalize", "normalize_usage", "from_raw"):
            candidate = getattr(normalizer, name, None)
            if callable(candidate):
                method = candidate
                break

        try:
            if method is not None:
                usage = method(raw_usage)
            elif callable(normalizer):
                usage = normalizer(raw_usage)
            else:  # pragma: no cover - defensive
                raise GatewayConfigError(
                    "usage normalizer is not callable and has no normalize() method"
                )
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(
                f"usage normalization failed: {type(exc).__name__}: {exc}"
            ) from exc

        missing = [
            name
            for name in ("input_tokens", "output_tokens", "reasoning_tokens", "total_cost")
            if not hasattr(usage, name)
        ]
        if missing:
            raise GatewayError(
                "usage normalizer returned an object without required fields: "
                + ", ".join(missing)
            )
        return usage

    # -- model construction --------------------------------------------------
    def _make_spec(self, model_name: str) -> GatewayModelSpec:
        return GatewayModelSpec(
            model_name=model_name,
            api_key=self._api_key,
            # The SDK does not append /v1; pass the configured base verbatim.
            api_base=self._base_url,
            client_provider=self._client_provider,
            verify_ssl=self._verify_ssl,
            timeout=self._timeout,
            temperature=self._temperature,
            max_tokens=DEFAULT_MODEL_MAX_TOKENS,
            custom_headers={},
        )

    def _get_model(self, model_name: str) -> Any:
        with self._models_lock:
            if self._closed:
                raise GatewayError("OpenJiuwenGateway is closed")
            model = self._models.get(model_name)
            if model is not None:
                return model
            spec = self._make_spec(model_name)

            async def build() -> Any:
                return self._model_factory(spec)

            try:
                model = self._run(build())
            except GatewayError:
                raise
            except ImportError as exc:
                raise GatewayUnavailableError(
                    f"openjiuwen is not installed or cannot be imported ({exc}); "
                    f"{SDK_INSTALL_HINT}"
                ) from exc
            except Exception as exc:
                raise GatewayError(
                    f"failed to build openJiuwen model {model_name!r}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            self._models[model_name] = model
            return model

    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        if self._runner is not None:
            return self._runner(coro)
        return self._loop.submit(coro)

    def _invoke(
        self,
        model_obj: Any,
        messages: list[dict[str, str]],
        max_output_tokens: int,
    ) -> Any:
        async def call() -> Any:
            result = model_obj.invoke(
                messages,
                max_tokens=max_output_tokens,
                temperature=self._temperature,
            )
            if inspect.isawaitable(result):
                result = await result
            return result

        return self._run(call())

    # -- public API ----------------------------------------------------------
    def preflight(self) -> None:
        """Validate every dependency before a paid request is attempted.

        The smoke script calls this so a missing SDK or ``harness.usage`` is
        reported as ``BLOCKED`` before any model call, not after a paid one.
        """

        self._require_credentials()
        self._validate_ledger(self._ledger)
        self._ensure_normalizer()
        if self._uses_default_factory:
            _configure_sdk_logging()
            _load_sdk_classes()

    def generate(
        self,
        *,
        model: str,
        messages: list[dict],
        max_output_tokens: int,
        request_id: str,
        ledger: Any | None = None,
    ) -> LLMResult:
        """Send exactly one request through the openJiuwen Core SDK.

        ``ledger`` overrides the gateway-level ledger for this call.  When a
        ledger is present, ``reserve`` runs before the request, ``settle`` after
        the normalized usage is available, and ``release`` on a failed call.
        """

        chosen_model = self._validate_model(model)
        normalised_messages = _normalise_messages(messages)
        limit = _positive_int(max_output_tokens, "max_output_tokens")
        request_key = _request_id(request_id)

        self._require_credentials()
        active_ledger = ledger if ledger is not None else self._ledger
        self._validate_ledger(active_ledger)
        normalizer = self._ensure_normalizer()

        # Import/build before reserving: a missing SDK must not lock budget.
        model_obj = self._get_model(chosen_model)

        log.debug(
            "gateway request request_id=%s model=%s max_output_tokens=%d",
            request_key,
            chosen_model,
            limit,
        )

        reservation: Any = None
        if active_ledger is not None:
            estimated_input = estimate_input_tokens(normalised_messages)
            reservation = _ledger_reserve(
                active_ledger,
                request_key,
                estimated_input,
                limit,
            )
            if reservation is None:
                raise GatewayBudgetError(
                    f"budget ledger denied request {request_key!r} for model {chosen_model!r}"
                )

        try:
            assistant = self._invoke(model_obj, normalised_messages, limit)
        except BaseException as exc:
            if active_ledger is not None:
                # The SDK error does not prove the proxy did not charge, but the
                # gateway contract requires the reservation to be released on a
                # failed call; spec-style ledgers receive definitely_not_sent=False.
                _ledger_release(
                    active_ledger,
                    request_key,
                    reservation,
                    definitely_not_sent=False,
                )
            if isinstance(exc, GatewayError):
                raise
            raise GatewayError(
                f"openJiuwen invoke failed for model {chosen_model!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        content = _get(assistant, "content", "")
        text = "" if content is None else str(content)

        usage_meta = _get(assistant, "usage_metadata", None)
        if usage_meta is None:
            usage_meta = _get(assistant, "usage", None)
        usage_mapping = _as_mapping(usage_meta)
        usage_payload: Any = usage_mapping if usage_mapping is not None else usage_meta

        # Exactly one normalization per successful request.  The gateway never
        # adds reasoning on its own: that is the normalizer's single source of
        # truth (spec 4.2, reasoning must not be double-counted).
        usage = self._normalize_usage(normalizer, usage_payload)

        if active_ledger is not None:
            _ledger_settle(active_ledger, request_key, reservation, usage)

        raw: dict[str, Any] = {
            "model": chosen_model,
            "usage": usage_mapping
            if usage_mapping is not None
            else (None if usage_meta is None else {"unsupported_type": type(usage_meta).__name__}),
        }
        return LLMResult(
            text=text,
            usage=usage,
            model=chosen_model,
            raw=raw,
        )

    def close(self) -> None:
        """Stop the background loop.  Idempotent, safe before first request."""

        if self._closed:
            return
        self._closed = True
        with self._models_lock:
            self._models.clear()
        self._loop.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class GatewayChatAdapter:
    """``LLMClient``-compatible facade over :class:`OpenJiuwenGateway`.

    The existing runner talks to a synchronous ``chat(...) -> LLMResponse``
    object.  This adapter is the only wiring needed to route those calls
    through the gateway; it does not implement any transport of its own.
    """

    backend_name = "openjiuwen-gateway"

    def __init__(self, gateway: OpenJiuwenGateway, config: LLMConfig) -> None:
        self.gateway = gateway
        self.config = config

    @property
    def available(self) -> bool:
        return self.gateway.available

    def resolve_model(self, attempt: int = 0, override: str | None = None) -> str:
        if override:
            return override
        return model_for_attempt(self.config, attempt)

    def chat(
        self,
        messages: Sequence[Mapping[str, Any] | ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        attempt: int = 0,
    ) -> LLMResponse:
        chosen_model = self.resolve_model(attempt=attempt, override=model)
        limit = int(self.config.max_tokens if max_tokens is None else max_tokens)
        started = time.perf_counter()
        result = self.gateway.generate(
            model=chosen_model,
            messages=list(messages),  # type: ignore[arg-type]
            max_output_tokens=limit,
            request_id=f"chat-{uuid.uuid4().hex}",
        )
        latency = time.perf_counter() - started
        usage = result.usage
        return LLMResponse(
            text=result.text,
            model=result.model,
            usage=LLMUsage(
                prompt_tokens=int(getattr(usage, "input_tokens", 0)),
                completion_tokens=int(getattr(usage, "output_tokens", 0)),
                estimated=False,
            ),
            latency=latency,
        )

    def close(self) -> None:
        self.gateway.close()

    def __repr__(self) -> str:
        return f"GatewayChatAdapter(gateway={self.gateway!r})"

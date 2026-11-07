"""Minimal OpenAI-compatible chat client built on :mod:`urllib`.

Only the standard library is used.  The client understands the two model
tiers used by the challenge (a cheap default and an escalation model), retries
transient failures with exponential backoff, and always reports usage so the
:class:`harness.budget.Budget` can be charged accurately.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "DEFAULT_MODEL",
    "ESCALATION_MODEL",
    "ChatMessage",
    "LLMClient",
    "LLMConfig",
    "LLMError",
    "LLMResponse",
    "LLMUsage",
    "build_llm_client",
    "chat_endpoint",
    "model_for_attempt",
]

log = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen3.6-35b-a3b-noreason"
ESCALATION_MODEL = "qwen3.8-27b"

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """Raised when a chat request cannot be completed."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class LLMUsage:
    prompt_tokens: int
    completion_tokens: int
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    model: str
    usage: LLMUsage
    latency: float


@dataclass(frozen=True, slots=True)
class LLMConfig:
    base_url: str | None
    api_key: str | None = None
    model: str = DEFAULT_MODEL
    escalation_model: str = ESCALATION_MODEL
    timeout: float = 60.0
    max_retries: int = 3
    backoff_base: float = 0.5
    backoff_max: float = 8.0
    temperature: float = 0.2
    max_tokens: int = 1024
    escalate_after: int = 2
    user_agent: str = "openjiuwen-harness/0.1"

    @classmethod
    def from_env(
        cls,
        *,
        model: str | None = None,
        escalation_model: str | None = None,
        max_tokens: int | None = None,
    ) -> "LLMConfig":
        return cls(
            base_url=os.environ.get("LLM_BASE_URL") or None,
            api_key=os.environ.get("LLM_API_KEY") or None,
            model=model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL,
            escalation_model=escalation_model or os.environ.get("LLM_ESCALATION_MODEL") or ESCALATION_MODEL,
            timeout=_env_float("LLM_TIMEOUT", 60.0),
            max_retries=int(_env_float("LLM_MAX_RETRIES", 3)),
            backoff_base=_env_float("LLM_BACKOFF_BASE", 0.5),
            temperature=_env_float("LLM_TEMPERATURE", 0.2),
            max_tokens=max_tokens or int(_env_float("LLM_MAX_TOKENS", 1024)),
        )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("ignoring invalid %s=%r", name, raw)
        return default


def chat_endpoint(base_url: str) -> str:
    """Normalise a proxy base URL into a chat-completions endpoint."""

    url = base_url.strip().rstrip("/")
    if not url:
        raise LLMError("LLM base URL is empty")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def model_for_attempt(config: LLMConfig, attempt: int) -> str:
    """Pick the default model first, then escalate after N failed attempts."""

    if attempt >= config.escalate_after:
        return config.escalation_model
    return config.model


def _estimate_tokens(text: str) -> int:
    """Rough fallback when a proxy omits usage information."""

    return max(1, (len(text) + 3) // 4)


class LLMClient:
    backend_name = "stdlib"

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls, **kwargs: Any) -> "LLMClient":
        return cls(LLMConfig.from_env(**kwargs))

    @property
    def available(self) -> bool:
        return bool(self.config.base_url)

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
        """Send one chat completion request.

        ``attempt`` controls model escalation (0-based).  Retries are handled
        internally for transient HTTP/network errors.
        """

        if not self.config.base_url:
            raise LLMError("LLM_BASE_URL is not configured")

        normalised = _normalise_messages(messages)
        chosen_model = self.resolve_model(attempt=attempt, override=model)
        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": normalised,
            "temperature": self.config.temperature if temperature is None else float(temperature),
            "max_tokens": int(self.config.max_tokens if max_tokens is None else max_tokens),
            "stream": False,
        }

        started = time.perf_counter()
        data = self._request_with_retries(payload)
        latency = time.perf_counter() - started

        try:
            choices = data["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed chat response: {data!r}") from exc
        text = "" if content is None else str(content)

        usage_obj = data.get("usage") if isinstance(data, dict) else None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        if isinstance(usage_obj, Mapping):
            prompt_tokens = _as_int(usage_obj.get("prompt_tokens"))
            completion_tokens = _as_int(usage_obj.get("completion_tokens"))

        estimated = False
        if prompt_tokens is None or completion_tokens is None:
            estimated = True
            prompt_tokens = _estimate_prompt(normalised)
            completion_tokens = _estimate_tokens(text)

        response_model = str(data.get("model") or chosen_model) if isinstance(data, dict) else chosen_model
        return LLMResponse(
            text=text,
            model=response_model,
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated=estimated,
            ),
            latency=latency,
        )

    # -- internals ------------------------------------------------------------
    def _request_with_retries(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        url = chat_endpoint(self.config.base_url or "")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.config.user_agent,
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        max_attempts = max(1, self.config.max_retries + 1)
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise LLMError("chat response is not a JSON object")
                return parsed
            except urllib.error.HTTPError as exc:
                detail = _read_http_error(exc)
                last_error = LLMError(f"HTTP {exc.code} from LLM proxy: {detail}")
                if exc.code not in _RETRYABLE_STATUS or attempt >= max_attempts - 1:
                    raise last_error from exc
                delay = _retry_delay(self.config, attempt, exc.headers.get("Retry-After"))
                log.warning("LLM HTTP %s; retrying in %.2fs (attempt %d/%d)", exc.code, delay, attempt + 1, max_attempts)
                time.sleep(delay)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                last_error = LLMError(f"LLM network error: {exc}")
                if attempt >= max_attempts - 1:
                    raise last_error from exc
                delay = _retry_delay(self.config, attempt, None)
                log.warning("LLM network error %s; retrying in %.2fs (attempt %d/%d)", exc, delay, attempt + 1, max_attempts)
                time.sleep(delay)
            except json.JSONDecodeError as exc:
                raise LLMError(f"LLM proxy returned invalid JSON: {exc}") from exc

        raise last_error or LLMError("LLM request failed for an unknown reason")


def _openjiuwen_available() -> bool:
    """Return True when the openjiuwen package can be found.

    ``find_spec`` is intentionally used instead of importing the heavy SDK at
    startup.  The actual import happens lazily in
    :class:`harness.ojiuwen_llm.OpenJiuwenLLMClient`.
    """

    try:
        return importlib.util.find_spec("openjiuwen") is not None
    except (ImportError, ValueError):
        return False


def build_llm_client(config: LLMConfig, backend: str = "auto") -> Any:
    """Build the configured LLM client.

    ``backend`` is one of ``auto``, ``stdlib`` or ``openjiuwen``.  ``auto``
    selects openJiuwen when the package is importable and otherwise falls back
    to the stdlib urllib client.  The chosen backend is logged without secrets.
    """

    choice = (backend or "auto").strip().lower()

    if choice == "stdlib":
        log.info("LLM backend: stdlib")
        return LLMClient(config)

    if choice == "openjiuwen":
        if not _openjiuwen_available():
            raise LLMError(
                "openjiuwen backend requested but openjiuwen is not installed; "
                "install openjiuwen==0.1.20.post1 (requirements-openjiuwen.txt) "
                "or use --llm-backend stdlib"
            )
        from .ojiuwen_llm import OpenJiuwenLLMClient

        client = OpenJiuwenLLMClient(config)
        log.info("LLM backend: openjiuwen")
        return client

    if choice == "auto":
        if _openjiuwen_available():
            from .ojiuwen_llm import OpenJiuwenLLMClient

            client = OpenJiuwenLLMClient(config)
            log.info("LLM backend: openjiuwen (auto; openjiuwen is installed)")
            return client
        log.info("LLM backend: stdlib (auto; openjiuwen is not installed)")
        return LLMClient(config)

    raise ValueError(
        f"unknown LLM backend {backend!r}; expected one of auto, stdlib, openjiuwen"
    )


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


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _estimate_prompt(messages: Sequence[Mapping[str, str]]) -> int:
    chars = sum(len(m.get("content", "")) for m in messages)
    return max(1, (chars + 3) // 4)


def _read_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - defensive
        return ""
    return raw[:800]


def _retry_delay(config: LLMConfig, attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return max(0.0, min(config.backoff_max, float(retry_after)))
        except ValueError:
            pass
    base = config.backoff_base * (2**attempt)
    jitter = base * 0.1 * random.random()
    return max(0.0, min(config.backoff_max, base + jitter))

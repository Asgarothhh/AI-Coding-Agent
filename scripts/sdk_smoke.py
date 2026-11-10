#!/usr/bin/env python3
"""Real openJiuwen Core smoke test through the organizer LLM proxy.

The script intentionally performs real, paid model calls and never fabricates a
successful result:

    python3.11 scripts/sdk_smoke.py
    python3.11 scripts/sdk_smoke.py --model qwen3.6-35b-a3b-noreason

It writes a redacted report to ``eval/results/sdk-smoke.json`` (latency, raw
usage, normalized usage, no key and no proxy URL).  When credentials or the SDK
dependencies are missing it prints ``BLOCKED: ...`` and exits with code 2.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.openjiuwen_gateway import (  # path bootstrap above
    ALLOWED_MODELS,
    GatewayError,
    OpenJiuwenGateway,
)

DEFAULT_OUT = ROOT / "eval" / "results" / "sdk-smoke.json"
DEFAULT_PROMPT = "Reply with exactly one word: OK"
DEFAULT_SYSTEM = "You are a minimal connectivity smoke test."
USAGE_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "total_cost", "source_fields")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdk_smoke.py",
        description="Call the openJiuwen Core gateway once per allowed proxy model.",
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        default=None,
        help="model to probe; repeatable (default: all four allowed models)",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=64,
        help="max_output_tokens for the probe (default: 64)",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="user prompt")
    parser.add_argument("--system", default=DEFAULT_SYSTEM, help="system prompt")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"report path (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="SDK timeout in seconds (default: $LLM_TIMEOUT or 60)",
    )
    return parser


def _usage_dict(usage: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(usage) and not isinstance(usage, type):
        data = dataclasses.asdict(usage)
    elif isinstance(usage, Mapping):
        data = dict(usage)
    else:
        data = {name: getattr(usage, name) for name in USAGE_FIELDS if hasattr(usage, name)}
    return data


def _redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "<redacted>")
        return result
    if isinstance(value, Mapping):
        return {str(key): _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    return value


def _write_report(path: Path, payload: Mapping[str, Any], secrets: list[str]) -> None:
    redacted = _redact(dict(payload), secrets)
    text = json.dumps(redacted, ensure_ascii=False, indent=2, default=str)
    for secret in secrets:
        if secret and secret in text:  # pragma: no cover - defensive
            raise SystemExit("refusing to write a report containing a secret")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(path)


def _blocked(reason: str, path: Path, models: list[str], secrets: list[str]) -> int:
    payload = {
        "status": "blocked",
        "reason": reason,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "models": models,
        "results": [],
    }
    try:
        _write_report(path, payload, secrets)
    except OSError as exc:
        print(f"warning: cannot write {path}: {exc}", file=sys.stderr)
    print(f"BLOCKED: {reason}")
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    models = list(args.models) if args.models else list(ALLOWED_MODELS)
    unknown = [model for model in models if model not in ALLOWED_MODELS]
    if unknown:
        print(
            f"error: model(s) not allowed: {', '.join(unknown)}; "
            f"allowed: {', '.join(ALLOWED_MODELS)}",
            file=sys.stderr,
        )
        return 64

    out = Path(args.out)
    base_url = (os.environ.get("LLM_BASE_URL") or "").strip()
    api_key = (os.environ.get("LLM_API_KEY") or "").strip()
    secrets = [api_key, base_url, base_url.rstrip("/")]

    if not base_url or not api_key:
        return _blocked("no LLM_BASE_URL/LLM_API_KEY", out, models, secrets)

    gateway = OpenJiuwenGateway(timeout=args.timeout)
    try:
        gateway.preflight()
    except Exception as exc:  # noqa: BLE001 - any startup failure means BLOCKED
        gateway.close()
        return _blocked(
            f"gateway not ready: {type(exc).__name__}: {exc}",
            out,
            models,
            secrets,
        )

    messages = [
        {"role": "system", "content": args.system},
        {"role": "user", "content": args.prompt},
    ]

    results: list[dict[str, Any]] = []
    try:
        for model in models:
            started = time.perf_counter()
            try:
                result = gateway.generate(
                    model=model,
                    messages=messages,
                    max_output_tokens=args.max_output_tokens,
                    request_id=f"smoke-{model}-{int(time.time() * 1000)}",
                )
            except GatewayError as exc:
                latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
                results.append(
                    {
                        "model": model,
                        "status": "error",
                        "latency_ms": latency_ms,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                print(f"{model}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            except Exception as exc:  # noqa: BLE001 - report, never fake success
                latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
                results.append(
                    {
                        "model": model,
                        "status": "error",
                        "latency_ms": latency_ms,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                print(f"{model}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
                continue

            latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
            usage = _usage_dict(result.usage)
            raw_usage = result.raw.get("usage") if isinstance(result.raw, Mapping) else None
            results.append(
                {
                    "model": model,
                    "status": "ok",
                    "latency_ms": latency_ms,
                    "text_preview": result.text[:200],
                    "usage": usage,
                    "raw_usage": raw_usage,
                    "reasoning_fields_present": _reasoning_fields_present(raw_usage),
                }
            )
            print(
                f"{model}: ok latency={latency_ms}ms "
                f"input={usage.get('input_tokens')} output={usage.get('output_tokens')} "
                f"reasoning={usage.get('reasoning_tokens')}"
            )
    finally:
        gateway.close()

    ok = all(item["status"] == "ok" for item in results)
    payload = {
        "status": "ok" if ok else "failed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sdk": "openjiuwen==0.1.20.post1",
        "client_provider": gateway.client_provider,
        "verify_ssl": gateway.verify_ssl,
        "max_output_tokens": args.max_output_tokens,
        "models": models,
        "results": results,
    }
    _write_report(out, payload, secrets)
    print(f"report: {out}")
    return 0 if ok else 1


def _reasoning_fields_present(raw_usage: Any) -> bool:
    if not isinstance(raw_usage, Mapping):
        return False
    if "reasoning_tokens" in raw_usage:
        return True
    for key in ("completion_tokens_details", "output_tokens_details"):
        details = raw_usage.get(key)
        if isinstance(details, Mapping) and "reasoning_tokens" in details:
            return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())

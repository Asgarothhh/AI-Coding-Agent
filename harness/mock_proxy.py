"""Local OpenAI-compatible mock proxy for offline end-to-end tests.

The server binds to localhost on an ephemeral port by default.  It supports
usage reporting and a configurable number of transient failures so retry and
budget-accounting behaviour can be tested without network access.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence

__all__ = ["MockProxyServer"]


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockLLM/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ARG002
        # Keep test output clean.
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

    def do_GET(self) -> None:  # noqa: N802
        server: MockProxyServer = self.server  # type: ignore[assignment]
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in {"/v1/models", "/models"}:
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": server.model_id, "object": "model"}],
                },
            )
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        server: MockProxyServer = self.server  # type: ignore[assignment]
        path = self.path.split("?", 1)[0].rstrip("/")
        if not path.endswith("/chat/completions"):
            self._send_json(404, {"error": {"message": "not found"}})
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": {"message": "invalid JSON body"}})
            return

        with server._lock:
            server.request_count += 1
            fail = server.remaining_failures > 0
            if fail:
                server.remaining_failures -= 1
                server.failure_count += 1
            if server.response_queue:
                text = server.response_queue.pop(0)
            else:
                text = server.response_text

        if fail:
            self._send_json(
                server.fail_status,
                {"error": {"message": server.fail_message}},
            )
            return

        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            messages = []

        prompt_tokens = server.prompt_tokens_override
        if prompt_tokens is None:
            chars = 0
            for message in messages:
                if isinstance(message, dict):
                    chars += len(str(message.get("content", "")))
            prompt_tokens = max(1, (chars + 3) // 4)

        completion_tokens = server.completion_tokens_override
        if completion_tokens is None:
            completion_tokens = max(1, (len(text) + 3) // 4)

        response = {
            "id": f"mock-{server.request_count}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": str(payload.get("model", server.model_id)) if isinstance(payload, dict) else server.model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        self._send_json(200, response)


class MockProxyServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        response_text: str = "MOCK",
        response_queue: Sequence[str] | None = None,
        fail_first: int = 0,
        fail_status: int = 500,
        fail_message: str = "temporary mock failure",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        model_id: str = "mock-model",
    ) -> None:
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Attributes consumed by _Handler through self.server.
        self._httpd._lock = self._lock  # type: ignore[attr-defined]
        self._httpd.request_count = 0  # type: ignore[attr-defined]
        self._httpd.failure_count = 0  # type: ignore[attr-defined]
        self._httpd.remaining_failures = int(fail_first)  # type: ignore[attr-defined]
        self._httpd.fail_status = int(fail_status)  # type: ignore[attr-defined]
        self._httpd.fail_message = fail_message  # type: ignore[attr-defined]
        self._httpd.response_text = response_text  # type: ignore[attr-defined]
        self._httpd.response_queue = list(response_queue or [])  # type: ignore[attr-defined]
        self._httpd.prompt_tokens_override = prompt_tokens  # type: ignore[attr-defined]
        self._httpd.completion_tokens_override = completion_tokens  # type: ignore[attr-defined]
        self._httpd.model_id = model_id  # type: ignore[attr-defined]

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def request_count(self) -> int:
        with self._lock:
            return int(self._httpd.request_count)  # type: ignore[attr-defined]

    @property
    def failure_count(self) -> int:
        with self._lock:
            return int(self._httpd.failure_count)  # type: ignore[attr-defined]

    def start(self) -> "MockProxyServer":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._httpd.serve_forever,
                name="mock-llm-proxy",
                daemon=True,
            )
            self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def stop(self) -> None:
        if self._thread is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._thread.join(timeout=5.0)
            self._thread = None
        else:
            # Called after a blocking serve_forever() was interrupted; the
            # shutdown event would never be set in that case.
            self._httpd.server_close()

    def __enter__(self) -> "MockProxyServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local OpenAI-compatible mock proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--response", default="MOCK")
    parser.add_argument("--fail-first", type=int, default=0)
    args = parser.parse_args(argv)

    server = MockProxyServer(
        host=args.host,
        port=args.port,
        response_text=args.response,
        fail_first=args.fail_first,
    )
    print(f"mock LLM proxy listening at {server.base_url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

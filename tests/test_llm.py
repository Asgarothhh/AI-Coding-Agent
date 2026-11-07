from __future__ import annotations

import unittest

from harness.llm import (
    DEFAULT_MODEL,
    ESCALATION_MODEL,
    LLMClient,
    LLMConfig,
    LLMError,
    chat_endpoint,
    model_for_attempt,
)
from harness.mock_proxy import MockProxyServer


class LLMClientTests(unittest.TestCase):
    def _client(self, proxy: MockProxyServer, **overrides: object) -> LLMClient:
        config = LLMConfig(
            base_url=proxy.base_url,
            api_key="test-key",
            timeout=5.0,
            max_retries=int(overrides.pop("max_retries", 3)),
            backoff_base=float(overrides.pop("backoff_base", 0.01)),
            **overrides,
        )
        return LLMClient(config)

    def test_chat_parses_content_and_usage(self) -> None:
        with MockProxyServer(
            response_text="hello from mock",
            prompt_tokens=120,
            completion_tokens=45,
        ) as proxy:
            client = self._client(proxy)
            response = client.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(response.text, "hello from mock")
            self.assertEqual(response.usage.prompt_tokens, 120)
            self.assertEqual(response.usage.completion_tokens, 45)
            self.assertEqual(response.usage.total_tokens, 165)
            self.assertFalse(response.usage.estimated)
            self.assertEqual(response.model, DEFAULT_MODEL)
            self.assertEqual(proxy.request_count, 1)

    def test_retries_transient_failures(self) -> None:
        with MockProxyServer(response_text="recovered", fail_first=2) as proxy:
            client = self._client(proxy)
            response = client.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(response.text, "recovered")
            self.assertEqual(proxy.request_count, 3)
            self.assertEqual(proxy.failure_count, 2)

    def test_does_not_retry_client_error(self) -> None:
        with MockProxyServer(response_text="never", fail_first=1, fail_status=400) as proxy:
            client = self._client(proxy)
            with self.assertRaises(LLMError):
                client.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(proxy.request_count, 1)

    def test_missing_base_url(self) -> None:
        client = LLMClient(LLMConfig(base_url=None))
        self.assertFalse(client.available)
        with self.assertRaises(LLMError):
            client.chat([{"role": "user", "content": "hi"}])

    def test_model_escalation(self) -> None:
        config = LLMConfig(base_url="http://example.invalid/v1", escalate_after=2)
        self.assertEqual(model_for_attempt(config, 0), DEFAULT_MODEL)
        self.assertEqual(model_for_attempt(config, 1), DEFAULT_MODEL)
        self.assertEqual(model_for_attempt(config, 2), ESCALATION_MODEL)
        self.assertEqual(model_for_attempt(config, 99), ESCALATION_MODEL)
        client = LLMClient(config)
        self.assertEqual(client.resolve_model(0), DEFAULT_MODEL)
        self.assertEqual(client.resolve_model(3), ESCALATION_MODEL)
        self.assertEqual(client.resolve_model(0, override="custom"), "custom")

    def test_endpoint_normalisation(self) -> None:
        self.assertEqual(chat_endpoint("http://x/v1"), "http://x/v1/chat/completions")
        self.assertEqual(chat_endpoint("http://x/v1/"), "http://x/v1/chat/completions")
        self.assertEqual(chat_endpoint("http://x"), "http://x/v1/chat/completions")
        self.assertEqual(
            chat_endpoint("http://x/v1/chat/completions"),
            "http://x/v1/chat/completions",
        )
        with self.assertRaises(LLMError):
            chat_endpoint("   ")

    def test_message_validation(self) -> None:
        client = LLMClient(LLMConfig(base_url="http://example.invalid/v1"))
        with self.assertRaises(LLMError):
            client.chat([])
        with self.assertRaises(LLMError):
            client.chat([{"role": "hacker", "content": "x"}])


if __name__ == "__main__":
    unittest.main()

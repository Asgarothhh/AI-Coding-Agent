from __future__ import annotations

import asyncio
import importlib.util
import os
import unittest
from dataclasses import dataclass
from typing import Any, Callable
from unittest import mock

from harness.llm import (
    DEFAULT_MODEL,
    ESCALATION_MODEL,
    LLMClient,
    LLMConfig,
    LLMError,
    build_llm_client,
)
from harness.ojiuwen_llm import ModelSpec, OpenJiuwenLLMClient
from harness.ojiuwen_runtime import build_runtime


def _has_openjiuwen() -> bool:
    try:
        return importlib.util.find_spec("openjiuwen") is not None
    except (ImportError, ValueError):
        return False


@dataclass
class FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


@dataclass
class FakeAssistant:
    content: str = ""
    usage_metadata: Any = None


class FakeModel:
    def __init__(self, spec: ModelSpec, responder: Callable[[], FakeAssistant]) -> None:
        self.spec = spec
        self.responder = responder
        self.calls: list[tuple[Any, int | None, float | None]] = []
        self.loop_ids: list[int] = []
        self.closed = False

    async def invoke(
        self,
        messages: Any,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> FakeAssistant:
        self.loop_ids.append(id(asyncio.get_running_loop()))
        self.calls.append((messages, max_tokens, temperature))
        return self.responder()

    async def aclose(self) -> None:
        self.closed = True


class RecordingFactory:
    def __init__(self, responder: Callable[[], FakeAssistant]) -> None:
        self.responder = responder
        self.specs: list[ModelSpec] = []
        self.models: list[FakeModel] = []

    def __call__(self, spec: ModelSpec) -> FakeModel:
        self.specs.append(spec)
        model = FakeModel(spec, self.responder)
        self.models.append(model)
        return model


def make_config(**overrides: Any) -> LLMConfig:
    values: dict[str, Any] = {
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": "not-a-real-key",
        "model": DEFAULT_MODEL,
        "escalation_model": ESCALATION_MODEL,
        "timeout": 12.0,
        "max_tokens": 128,
        "temperature": 0.3,
    }
    values.update(overrides)
    return LLMConfig(**values)


class OpenJiuwenLLMClientTests(unittest.TestCase):
    def test_default_model_is_base_alias(self) -> None:
        self.assertEqual(DEFAULT_MODEL, "qwen3.6-35b-a3b")
        factory = RecordingFactory(
            lambda: FakeAssistant("ok", FakeUsage(1, 1, 0, 2))
        )
        client = self.make_client(factory)
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(factory.specs[0].model_name, "qwen3.6-35b-a3b")

    def make_client(
        self,
        factory: RecordingFactory | None = None,
        *,
        config: LLMConfig | None = None,
    ) -> OpenJiuwenLLMClient:
        client = OpenJiuwenLLMClient(
            config or make_config(),
            model_factory=factory,
        )
        self.addCleanup(client.close)
        return client

    def test_usage_mapping_does_not_double_count_reasoning(self) -> None:
        factory = RecordingFactory(lambda: FakeAssistant("fixed", FakeUsage(11, 22, 7, 33)))
        client = self.make_client(factory)

        response = client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(response.text, "fixed")
        self.assertEqual(response.usage.prompt_tokens, 11)
        self.assertEqual(response.usage.completion_tokens, 22)
        self.assertEqual(response.usage.total_tokens, 33)
        self.assertFalse(response.usage.estimated)

        spec = factory.specs[0]
        self.assertEqual(spec.model_name, DEFAULT_MODEL)
        self.assertEqual(spec.api_base, "http://127.0.0.1:9/v1")
        self.assertEqual(spec.api_key, "not-a-real-key")
        self.assertEqual(spec.max_tokens, 128)
        self.assertEqual(spec.temperature, 0.3)
        self.assertTrue(spec.verify_ssl)
        self.assertEqual(spec.timeout, 12.0)

        messages, max_tokens, temperature = factory.models[0].calls[0]
        self.assertEqual(messages, [{"role": "user", "content": "hi"}])
        self.assertEqual(max_tokens, 128)
        self.assertEqual(temperature, 0.3)

    def test_zero_output_tokens_falls_back_to_reasoning(self) -> None:
        factory = RecordingFactory(lambda: FakeAssistant("thought", FakeUsage(5, 0, 9, 14)))
        client = self.make_client(factory)

        response = client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(response.usage.prompt_tokens, 5)
        self.assertEqual(response.usage.completion_tokens, 9)

    def test_missing_usage_is_estimated(self) -> None:
        factory = RecordingFactory(lambda: FakeAssistant("hello world", None))
        client = self.make_client(factory)

        response = client.chat([{"role": "user", "content": "hi"}])

        self.assertTrue(response.usage.estimated)
        self.assertGreater(response.usage.prompt_tokens, 0)
        self.assertGreater(response.usage.completion_tokens, 0)

    def test_model_escalation_and_cache(self) -> None:
        factory = RecordingFactory(lambda: FakeAssistant("ok", FakeUsage(1, 1, 0, 2)))
        client = self.make_client(factory)

        client.chat([{"role": "user", "content": "a"}], attempt=0)
        client.chat([{"role": "user", "content": "b"}], attempt=1)
        client.chat([{"role": "user", "content": "c"}], attempt=2)
        client.chat([{"role": "user", "content": "d"}], attempt=2)

        self.assertEqual(
            [spec.model_name for spec in factory.specs],
            [DEFAULT_MODEL, ESCALATION_MODEL],
        )
        self.assertEqual(len(factory.models), 2)

    def test_chat_works_inside_running_event_loop(self) -> None:
        factory = RecordingFactory(lambda: FakeAssistant("from-thread", FakeUsage(2, 3, 0, 5)))
        client = self.make_client(factory)

        async def scenario() -> str:
            # This must not raise "asyncio.run() cannot be called from a
            # running event loop"; the client bridges to a worker thread.
            return client.chat([{"role": "user", "content": "hi"}]).text

        self.assertEqual(asyncio.run(scenario()), "from-thread")

    def test_chat_reuses_one_background_loop(self) -> None:
        factory = RecordingFactory(lambda: FakeAssistant("ok", FakeUsage(1, 1, 0, 2)))
        client = self.make_client(factory)

        client.chat([{"role": "user", "content": "first"}])
        client.chat([{"role": "user", "content": "second"}])

        model = factory.models[0]
        self.assertEqual(len(model.loop_ids), 2)
        self.assertEqual(model.loop_ids[0], model.loop_ids[1])

    def test_injected_fake_model_still_works(self) -> None:
        factory = RecordingFactory(lambda: FakeAssistant("injected", FakeUsage(3, 4, 0, 7)))
        client = self.make_client(factory)

        response = client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(response.text, "injected")
        self.assertEqual(len(factory.models), 1)

    def test_close_stops_loop_and_is_idempotent(self) -> None:
        factory = RecordingFactory(lambda: FakeAssistant("ok", FakeUsage(1, 1, 0, 2)))
        client = self.make_client(factory)
        client.chat([{"role": "user", "content": "hi"}])

        model = factory.models[0]
        thread = client._loop_thread
        self.assertIsNotNone(thread)
        assert thread is not None
        self.assertTrue(thread.is_alive())

        client.close()
        self.assertTrue(client.closed)
        self.assertTrue(model.closed)
        self.assertFalse(thread.is_alive())

        # Idempotent second close must not raise.
        client.close()
        self.assertTrue(client.closed)

    def test_import_failure_raises_clear_llm_error(self) -> None:
        client = self.make_client()
        with mock.patch(
            "harness.ojiuwen_llm._load_model_classes",
            side_effect=ImportError("simulated missing openjiuwen"),
        ):
            with self.assertRaises(LLMError) as ctx:
                client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("openjiuwen", str(ctx.exception))

    def test_api_failure_is_wrapped(self) -> None:
        class BrokenModel:
            async def invoke(self, messages: Any, **kwargs: Any) -> Any:  # noqa: ARG002
                raise RuntimeError("boom")

        def factory(spec: ModelSpec) -> BrokenModel:  # noqa: ARG001
            return BrokenModel()

        client = self.make_client(factory)
        with self.assertRaises(LLMError) as ctx:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("boom", str(ctx.exception))

    def test_environment_is_honoured(self) -> None:
        env = {
            "LLM_BASE_URL": "http://env-host/v1",
            "LLM_API_KEY": "env-key",
            "LLM_MODEL": "env-model",
            "LLM_ESCALATION_MODEL": "env-escalation",
            "LLM_MAX_TOKENS": "77",
            "LLM_TEMPERATURE": "0.9",
            "LLM_TIMEOUT": "42.5",
            "LLM_VERIFY_SSL": "false",
            "OJ_EXTRA_BODY": '{"chat_template_kwargs": {"enable_thinking": false}}',
        }
        factory = RecordingFactory(lambda: FakeAssistant("ok", FakeUsage(1, 1, 0, 2)))
        with mock.patch.dict(os.environ, env, clear=False):
            config = LLMConfig.from_env()
            client = self.make_client(factory, config=config)
            client.chat([{"role": "user", "content": "hi"}])

        self.assertFalse(client.verify_ssl)
        self.assertEqual(client.timeout, 42.5)
        self.assertEqual(
            client.extra_body,
            {"chat_template_kwargs": {"enable_thinking": False}},
        )
        spec = factory.specs[0]
        self.assertEqual(spec.model_name, "env-model")
        self.assertEqual(spec.api_base, "http://env-host/v1")
        self.assertEqual(spec.api_key, "env-key")
        self.assertEqual(spec.max_tokens, 77)
        self.assertEqual(spec.temperature, 0.9)
        self.assertEqual(spec.timeout, 42.5)
        self.assertFalse(spec.verify_ssl)

    def test_missing_base_url_or_key(self) -> None:
        factory = RecordingFactory(lambda: FakeAssistant("ok", FakeUsage(1, 1, 0, 2)))
        with self.assertRaises(LLMError):
            self.make_client(factory, config=make_config(base_url=None)).chat(
                [{"role": "user", "content": "hi"}]
            )
        with self.assertRaises(LLMError):
            self.make_client(factory, config=make_config(api_key=None)).chat(
                [{"role": "user", "content": "hi"}]
            )

    def test_repr_does_not_leak_key(self) -> None:
        client = self.make_client(RecordingFactory(lambda: FakeAssistant()))
        self.assertNotIn("not-a-real-key", repr(client))


class BuildLLMClientTests(unittest.TestCase):
    def test_stdlib_backend(self) -> None:
        client = build_llm_client(make_config(), backend="stdlib")
        self.assertIsInstance(client, LLMClient)
        self.assertEqual(client.backend_name, "stdlib")

    def test_auto_falls_back_to_stdlib(self) -> None:
        with mock.patch("harness.llm._openjiuwen_available", return_value=False):
            client = build_llm_client(make_config(), backend="auto")
        self.assertIsInstance(client, LLMClient)

    def test_auto_selects_openjiuwen_when_available(self) -> None:
        with mock.patch("harness.llm._openjiuwen_available", return_value=True):
            client = build_llm_client(make_config(), backend="auto")
        self.assertIsInstance(client, OpenJiuwenLLMClient)
        self.assertEqual(client.backend_name, "openjiuwen")

    def test_explicit_openjiuwen_raises_when_unavailable(self) -> None:
        with mock.patch("harness.llm._openjiuwen_available", return_value=False):
            with self.assertRaises(LLMError) as ctx:
                build_llm_client(make_config(), backend="openjiuwen")
        self.assertIn("not installed", str(ctx.exception))

    def test_unknown_backend(self) -> None:
        with self.assertRaises(ValueError):
            build_llm_client(make_config(), backend="carrier-pigeon")

    def test_build_runtime_none_when_import_fails(self) -> None:
        with mock.patch(
            "harness.ojiuwen_runtime.importlib.import_module",
            side_effect=ImportError("simulated"),
        ):
            self.assertIsNone(build_runtime())


@unittest.skipUnless(_has_openjiuwen(), "openjiuwen is not installed")
class RealOpenJiuwenImportTests(unittest.TestCase):
    def test_explicit_backend_constructs_client(self) -> None:
        client = build_llm_client(make_config(), backend="openjiuwen")
        self.assertIsInstance(client, OpenJiuwenLLMClient)

    def test_build_runtime_returns_adapter(self) -> None:
        runtime = build_runtime()
        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertTrue(runtime.available())


if __name__ == "__main__":
    unittest.main()

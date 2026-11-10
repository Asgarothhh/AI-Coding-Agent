"""Tests for the single openJiuwen gateway (spec sections 5.2-5.4, 4.2, 11.4)."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import types
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from harness.llm import LLMConfig, build_llm_client
from harness.openjiuwen_gateway import (
    ALLOWED_MODELS,
    GatewayBudgetError,
    GatewayChatAdapter,
    GatewayConfigError,
    GatewayError,
    GatewayUnavailableError,
    LLMResult,
    OpenJiuwenGateway,
    estimate_input_tokens,
)

try:  # the shared normalizer is owned by harness/usage.py (another task)
    from harness.usage import TokenUsage as RealTokenUsage
    from harness.usage import normalize_usage as real_normalize_usage
except ImportError:  # pragma: no cover - only before harness/usage.py lands
    RealTokenUsage = None  # type: ignore[assignment,misc]
    real_normalize_usage = None  # type: ignore[assignment]

try:  # production ledger signature must stay compatible with the gateway
    from harness.budget import BudgetLedger
except ImportError:  # pragma: no cover - only before the ledger lands
    BudgetLedger = None  # type: ignore[assignment,misc]

FIXTURE = Path(__file__).parent / "fixtures" / "usage_proxy.json"
SECRET_KEY = "unit-test-secret-value"
BASE_URL = "http://127.0.0.1:9/v1"


@dataclasses.dataclass
class FakeTokenUsage:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_cost: float
    source_fields: tuple[str, ...] = ()


class FakeAssistant:
    def __init__(self, content: str = "OK", usage: Mapping[str, Any] | None = None) -> None:
        self.content = content
        self.usage_metadata = usage


class FakeModel:
    def __init__(
        self,
        spec: Any,
        assistant: FakeAssistant | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.spec = spec
        self.assistant = assistant or FakeAssistant()
        self.error = error
        self.calls: list[tuple[Any, int | None, float | None]] = []

    async def invoke(self, messages: Any, **kwargs: Any) -> FakeAssistant:
        self.calls.append((messages, kwargs.get("max_tokens"), kwargs.get("temperature")))
        if self.error is not None:
            raise self.error
        return self.assistant


class FakeFactory:
    def __init__(
        self,
        assistant: FakeAssistant | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.assistant = assistant
        self.error = error
        self.specs: list[Any] = []
        self.models: list[FakeModel] = []

    def __call__(self, spec: Any) -> FakeModel:
        self.specs.append(spec)
        model = FakeModel(spec, self.assistant, self.error)
        self.models.append(model)
        return model


class FakeNormalizer:
    def __init__(
        self,
        usage: FakeTokenUsage | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.usage = usage or FakeTokenUsage(
            input_tokens=100,
            output_tokens=40,
            reasoning_tokens=10,
            total_cost=50.0,
        )
        self.error = error
        self.calls: list[Any] = []

    def normalize(self, raw: Any) -> FakeTokenUsage:
        self.calls.append(raw)
        if self.error is not None:
            raise self.error
        return self.usage


class FakeLedger:
    """Task-style ledger: reserve(request_id, estimated_input, estimated_output)."""

    def __init__(self, reservation: Any = "reservation") -> None:
        self.reservation = reservation
        self.events: list[tuple[Any, ...]] = []

    def reserve(
        self,
        request_id: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
    ) -> Any:
        self.events.append(
            ("reserve", request_id, estimated_input_tokens, estimated_output_tokens)
        )
        return self.reservation

    def settle(self, reservation: Any, usage: Any) -> None:
        self.events.append(("settle", reservation, usage))

    def release(self, reservation: Any) -> None:
        self.events.append(("release", reservation))


class SpecStyleLedger:
    """Spec 4.1 style ledger with different parameter names."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.reservation = object()

    def reserve(
        self,
        request_id: str,
        estimated_input: int,
        max_output: int,
        safety_margin: float = 0.0,
    ) -> Any:
        self.events.append(
            ("reserve", request_id, estimated_input, max_output, safety_margin)
        )
        return self.reservation

    def settle(self, request_id: str, usage: Any) -> None:
        self.events.append(("settle", request_id, usage))

    def release_failed(self, request_id: str, *, definitely_not_sent: bool) -> None:
        self.events.append(("release_failed", request_id, definitely_not_sent))


class NoReleaseLedger(FakeLedger):
    release = None  # type: ignore[assignment]


_UNSET = object()


def _make_gateway(
    *,
    factory: Any = _UNSET,
    normalizer: Any = _UNSET,
    ledger: Any = None,
    **overrides: Any,
) -> OpenJiuwenGateway:
    if factory is _UNSET:
        factory = FakeFactory()
    if normalizer is _UNSET:
        normalizer = FakeNormalizer()
    kwargs: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": SECRET_KEY,
        "client_provider": "OpenAI",
        "timeout": 5.0,
        "temperature": 0.0,
        "model_factory": factory,
        "normalizer": normalizer,
        "ledger": ledger,
    }
    kwargs.update(overrides)
    return OpenJiuwenGateway(**kwargs)


def _call(
    gateway: OpenJiuwenGateway,
    *,
    model: str = "qwen3.8-27b",
    request_id: str = "req-1",
    max_output_tokens: int = 64,
    messages: list[dict] | None = None,
) -> LLMResult:
    return gateway.generate(
        model=model,
        messages=messages or [{"role": "user", "content": "ping"}],
        max_output_tokens=max_output_tokens,
        request_id=request_id,
    )


class GatewayValidationTests(unittest.TestCase):
    def test_rejects_unknown_model_before_any_call(self) -> None:
        factory = FakeFactory()
        normalizer = FakeNormalizer()
        ledger = FakeLedger()
        gateway = _make_gateway(factory=factory, normalizer=normalizer, ledger=ledger)
        self.addCleanup(gateway.close)

        with self.assertRaises(GatewayConfigError) as ctx:
            _call(gateway, model="gpt-4o")

        self.assertIn("not allowed", str(ctx.exception))
        self.assertEqual(factory.specs, [])
        self.assertEqual(normalizer.calls, [])
        self.assertEqual(ledger.events, [])

    def test_all_four_allowed_models_construct_specs(self) -> None:
        self.assertEqual(len(ALLOWED_MODELS), 4)
        for model in ALLOWED_MODELS:
            factory = FakeFactory()
            gateway = _make_gateway(factory=factory)
            self.addCleanup(gateway.close)
            result = _call(gateway, model=model)
            self.assertEqual(result.model, model)
            self.assertEqual(factory.specs[0].model_name, model)

    def test_missing_package_raises_gateway_unavailable(self) -> None:
        ledger = FakeLedger()
        gateway = _make_gateway(factory=None, ledger=ledger)
        self.addCleanup(gateway.close)
        # Force the lazy import to fail even when openjiuwen is installed.
        with (
            mock.patch.dict(
                sys.modules,
                {
                    "openjiuwen": None,
                    "openjiuwen.core": None,
                    "openjiuwen.core.foundation": None,
                    "openjiuwen.core.foundation.llm": None,
                },
            ),
            self.assertRaises(GatewayUnavailableError) as ctx,
        ):
            _call(gateway)

        self.assertIn("openjiuwen", str(ctx.exception))
        self.assertEqual(ledger.events, [])

    def test_missing_ledger_api_fails_before_call(self) -> None:
        factory = FakeFactory()
        gateway = _make_gateway(factory=factory, ledger=NoReleaseLedger())
        self.addCleanup(gateway.close)
        with self.assertRaises(GatewayConfigError):
            _call(gateway)
        self.assertEqual(factory.specs, [])

    def test_missing_usage_module_fails_before_call(self) -> None:
        factory = FakeFactory()
        ledger = FakeLedger()
        gateway = _make_gateway(factory=factory, normalizer=None, ledger=ledger)
        self.addCleanup(gateway.close)
        with (
            mock.patch.dict(sys.modules, {"harness.usage": None}),
            self.assertRaises(GatewayConfigError) as ctx,
        ):
            _call(gateway)
        self.assertIn("harness/usage.py", str(ctx.exception))
        self.assertEqual(factory.specs, [])
        self.assertEqual(ledger.events, [])

    def test_default_normalizer_is_imported_from_harness_usage(self) -> None:
        calls: list[Any] = []
        usage = FakeTokenUsage(10, 5, 0, 6.0)

        class UsageNormalizer:
            def normalize(self, raw: Any) -> FakeTokenUsage:
                calls.append(raw)
                return usage

        module = types.ModuleType("harness.usage")
        module.UsageNormalizer = UsageNormalizer  # type: ignore[attr-defined]

        factory = FakeFactory(FakeAssistant("pong", {"prompt_tokens": 10, "completion_tokens": 5}))
        gateway = _make_gateway(factory=factory, normalizer=None)
        self.addCleanup(gateway.close)
        with mock.patch.dict(sys.modules, {"harness.usage": module}):
            result = _call(gateway)

        self.assertEqual(len(calls), 1)
        self.assertIs(result.usage, usage)


class UsageNormalizationTests(unittest.TestCase):
    def test_normalizer_called_exactly_once_and_reasoning_not_double_counted(self) -> None:
        raw_usage = {
            "prompt_tokens": 300,
            "completion_tokens": 180,
            "completion_tokens_details": {"reasoning_tokens": 120},
        }
        expected = FakeTokenUsage(
            input_tokens=300,
            output_tokens=180,  # reasoning is already inside output_tokens
            reasoning_tokens=120,
            total_cost=210.0,
            source_fields=("prompt_tokens", "completion_tokens"),
        )
        normalizer = FakeNormalizer(expected)
        factory = FakeFactory(FakeAssistant("done", raw_usage))
        gateway = _make_gateway(factory=factory, normalizer=normalizer)
        self.addCleanup(gateway.close)

        result = _call(gateway)

        self.assertEqual(len(normalizer.calls), 1)
        self.assertIs(result.usage, expected)
        self.assertEqual(result.usage.output_tokens, 180)
        self.assertEqual(result.usage.reasoning_tokens, 120)
        self.assertNotEqual(
            result.usage.output_tokens,
            180 + 120,
            "gateway must not add reasoning tokens again",
        )
        self.assertEqual(result.raw["usage"], raw_usage)

        # One normalization per request, not one per gateway.
        _call(gateway, request_id="req-2")
        self.assertEqual(len(normalizer.calls), 2)

    def test_fixture_payloads_are_passed_to_normalizer_unchanged(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cases = [case for case in fixture["cases"] if case["usage"] is not None]
        self.assertGreaterEqual(len(cases), 4)
        self.assertFalse(fixture["_meta"]["contains_secrets"])

        for case in cases:
            with self.subTest(case=case["id"]):
                normalizer = FakeNormalizer()
                factory = FakeFactory(FakeAssistant("ok", case["usage"]))
                gateway = _make_gateway(factory=factory, normalizer=normalizer)
                try:
                    result = _call(gateway)
                finally:
                    gateway.close()

                self.assertEqual(normalizer.calls, [case["usage"]])
                self.assertEqual(result.raw["usage"], case["usage"])
                self.assertIs(result.usage, normalizer.usage)

    def test_normalizer_failure_is_reported(self) -> None:
        normalizer = FakeNormalizer(error=ValueError("bad usage shape"))
        factory = FakeFactory(FakeAssistant("ok", {"prompt_tokens": 1}))
        gateway = _make_gateway(factory=factory, normalizer=normalizer)
        self.addCleanup(gateway.close)
        with self.assertRaises(GatewayError) as ctx:
            _call(gateway)
        self.assertIn("usage normalization failed", str(ctx.exception))
        self.assertEqual(len(normalizer.calls), 1)


@unittest.skipUnless(
    real_normalize_usage is not None,
    "harness.usage is not available",
)
class RealUsageNormalizerIntegrationTests(unittest.TestCase):
    def _fixture_case(self, case_id: str) -> dict[str, Any]:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for case in fixture["cases"]:
            if case["id"] == case_id:
                return case
        raise AssertionError(f"fixture case {case_id!r} not found")

    def _gateway_for(self, usage: Mapping[str, Any] | None) -> OpenJiuwenGateway:
        return _make_gateway(
            factory=FakeFactory(FakeAssistant("ok", usage)),
            normalizer=None,
        )

    def test_reasoning_already_in_output_is_not_double_counted(self) -> None:
        case = self._fixture_case("reasoning_included_in_completion")
        gateway = self._gateway_for(case["usage"])
        self.addCleanup(gateway.close)

        result = _call(gateway)

        self.assertIsInstance(result.usage, RealTokenUsage)
        self.assertEqual(result.usage.input_tokens, 300)
        self.assertEqual(result.usage.output_tokens, 180)
        self.assertEqual(result.usage.reasoning_tokens, 120)
        self.assertEqual(result.usage.total_cost, 210.0)
        self.assertNotEqual(result.usage.output_tokens, 180 + 120)

    def test_every_fixture_payload_flows_through_the_real_normalizer(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for case in fixture["cases"]:
            if case["usage"] is None:
                continue
            with self.subTest(case=case["id"]):
                gateway = self._gateway_for(case["usage"])
                try:
                    result = _call(gateway)
                finally:
                    gateway.close()
                self.assertIsInstance(result.usage, RealTokenUsage)
                expected = case["expected"]
                self.assertEqual(result.usage.input_tokens, expected["input_tokens"])
                self.assertEqual(result.usage.output_tokens, expected["output_tokens"])
                self.assertEqual(result.usage.reasoning_tokens, expected["reasoning_tokens"])
                self.assertEqual(result.usage.total_cost, expected["total_cost"])
                self.assertEqual(result.raw["usage"], case["usage"])

    def test_missing_usage_is_preserved_in_raw(self) -> None:
        gateway = self._gateway_for(None)
        self.addCleanup(gateway.close)

        result = _call(gateway)

        self.assertIsInstance(result.usage, RealTokenUsage)
        self.assertEqual(result.usage.input_tokens, 0)
        self.assertEqual(result.usage.output_tokens, 0)
        self.assertEqual(result.usage.source_fields, ())
        self.assertIsNone(result.raw["usage"])


class LedgerTests(unittest.TestCase):
    def test_reserve_then_settle_with_actual_usage(self) -> None:
        ledger = FakeLedger()
        usage = FakeTokenUsage(120, 30, 0, 42.0)
        normalizer = FakeNormalizer(usage)
        gateway = _make_gateway(normalizer=normalizer, ledger=ledger)
        self.addCleanup(gateway.close)

        result = _call(gateway, request_id="req-ledger", max_output_tokens=77)

        names = [event[0] for event in ledger.events]
        self.assertEqual(names, ["reserve", "settle"])
        reserve = ledger.events[0]
        self.assertEqual(reserve[1], "req-ledger")
        expected_input = estimate_input_tokens(
            [{"role": "user", "content": "ping"}]
        )
        self.assertEqual(reserve[2], expected_input)
        self.assertEqual(reserve[3], 77)
        settle = ledger.events[1]
        self.assertIs(settle[1], ledger.reservation)
        self.assertIs(settle[2], result.usage)

    def test_release_on_invoke_failure(self) -> None:
        ledger = FakeLedger()
        factory = FakeFactory(error=RuntimeError("proxy exploded"))
        gateway = _make_gateway(factory=factory, ledger=ledger)
        self.addCleanup(gateway.close)

        with self.assertRaises(GatewayError) as ctx:
            _call(gateway)

        self.assertIn("invoke failed", str(ctx.exception))
        self.assertEqual([event[0] for event in ledger.events], ["reserve", "release"])
        self.assertIs(ledger.events[1][1], ledger.reservation)

    def test_denied_reservation_prevents_invoke(self) -> None:
        ledger = FakeLedger(reservation=None)
        factory = FakeFactory()
        gateway = _make_gateway(factory=factory, ledger=ledger)
        self.addCleanup(gateway.close)

        with self.assertRaises(GatewayBudgetError):
            _call(gateway)

        self.assertEqual([event[0] for event in ledger.events], ["reserve"])
        self.assertEqual(factory.models[0].calls, [])

    @unittest.skipUnless(BudgetLedger is not None, "harness.budget is not available")
    def test_real_budget_ledger_round_trip(self) -> None:
        ledger = BudgetLedger(1000.0)
        usage = FakeTokenUsage(input_tokens=100, output_tokens=40, reasoning_tokens=10, total_cost=50.0)
        gateway = _make_gateway(normalizer=FakeNormalizer(usage), ledger=ledger)
        self.addCleanup(gateway.close)

        result = _call(gateway, request_id="budget-round-trip", max_output_tokens=64)

        self.assertIs(result.usage, usage)
        self.assertEqual(ledger.reserved, 0.0)
        self.assertEqual(ledger.spent, 50.0)
        self.assertEqual(ledger.remaining, 950.0)
        self.assertEqual(ledger.prompt_tokens, 100)
        self.assertEqual(ledger.completion_tokens, 40)

    def test_spec_style_ledger_reserve_settle(self) -> None:
        ledger = SpecStyleLedger()
        gateway = _make_gateway(ledger=ledger)
        self.addCleanup(gateway.close)

        _call(gateway, request_id="req-spec")

        self.assertEqual([event[0] for event in ledger.events], ["reserve", "settle"])
        self.assertEqual(ledger.events[0][1], "req-spec")
        self.assertEqual(ledger.events[1][1], "req-spec")

    def test_spec_style_ledger_release_on_failure(self) -> None:
        ledger = SpecStyleLedger()
        gateway = _make_gateway(factory=FakeFactory(error=RuntimeError("boom")), ledger=ledger)
        self.addCleanup(gateway.close)

        with self.assertRaises(GatewayError):
            _call(gateway, request_id="req-spec-fail")

        self.assertEqual(
            [event[0] for event in ledger.events],
            ["reserve", "release_failed"],
        )
        self.assertEqual(ledger.events[1][1], "req-spec-fail")
        self.assertFalse(ledger.events[1][2])


class ConfigAndSecretTests(unittest.TestCase):
    def test_env_mapping_and_tls_default(self) -> None:
        env = {
            "LLM_BASE_URL": BASE_URL,
            "LLM_API_KEY": SECRET_KEY,
            "LLM_CLIENT_PROVIDER": "DashScope",
            "LLM_VERIFY_SSL": "1",
        }
        factory = FakeFactory()
        with mock.patch.dict(os.environ, env, clear=False):
            gateway = OpenJiuwenGateway(
                model_factory=factory,
                normalizer=FakeNormalizer(),
            )
        self.addCleanup(gateway.close)

        _call(gateway)

        spec = factory.specs[0]
        self.assertEqual(spec.api_base, BASE_URL)
        self.assertEqual(spec.api_key, SECRET_KEY)
        self.assertEqual(spec.client_provider, "DashScope")
        self.assertTrue(spec.verify_ssl)
        self.assertEqual(gateway.client_provider, "DashScope")
        self.assertTrue(gateway.verify_ssl)

    def test_verify_ssl_can_only_be_disabled_explicitly(self) -> None:
        with mock.patch.dict(os.environ, {"LLM_VERIFY_SSL": "0"}, clear=False):
            gateway = _make_gateway()
        self.addCleanup(gateway.close)
        self.assertFalse(gateway.verify_ssl)

    def test_no_key_in_repr_or_logs(self) -> None:
        gateway = _make_gateway()
        self.addCleanup(gateway.close)

        with self.assertLogs("harness.openjiuwen_gateway", level="DEBUG") as captured:
            _call(gateway, request_id="req-secret")

        text = "\n".join(captured.output)
        self.assertNotIn(SECRET_KEY, repr(gateway))
        self.assertNotIn(SECRET_KEY, text)
        self.assertNotIn(SECRET_KEY, repr(gateway._make_spec("qwen3.8-27b")))

    def test_missing_credentials_is_configuration_error(self) -> None:
        gateway = OpenJiuwenGateway(
            base_url="",
            api_key="",
            model_factory=FakeFactory(),
            normalizer=FakeNormalizer(),
        )
        self.addCleanup(gateway.close)
        with self.assertRaises(GatewayConfigError) as ctx:
            _call(gateway)
        self.assertIn("LLM_BASE_URL", str(ctx.exception))


class ChatAdapterTests(unittest.TestCase):
    class StubGateway:
        available = True

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def generate(self, **kwargs: Any) -> LLMResult:
            self.calls.append(kwargs)
            return LLMResult(
                text="adapter-ok",
                usage=FakeTokenUsage(11, 7, 3, 8.1),
                model=kwargs["model"],
                raw={"usage": {"prompt_tokens": 11, "completion_tokens": 7}},
            )

        def close(self) -> None:
            pass

    def test_adapter_maps_gateway_result_to_llm_response(self) -> None:
        gateway = self.StubGateway()
        config = LLMConfig(
            base_url=BASE_URL,
            api_key=SECRET_KEY,
            model="qwen3.8-27b",
            max_tokens=99,
        )
        adapter = GatewayChatAdapter(gateway, config)
        self.assertEqual(adapter.backend_name, "openjiuwen-gateway")

        response = adapter.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(response.text, "adapter-ok")
        self.assertEqual(response.model, "qwen3.8-27b")
        self.assertEqual(response.usage.prompt_tokens, 11)
        self.assertEqual(response.usage.completion_tokens, 7)
        self.assertGreaterEqual(response.latency, 0.0)
        self.assertEqual(gateway.calls[0]["max_output_tokens"], 99)
        self.assertTrue(gateway.calls[0]["request_id"].startswith("chat-"))

    def test_build_llm_client_gateway_backend_wires_adapter(self) -> None:
        config = LLMConfig(base_url=BASE_URL, api_key=SECRET_KEY, model="qwen3.6-35b-a3b")
        client = build_llm_client(config, backend="gateway")
        try:
            self.assertIsInstance(client, GatewayChatAdapter)
            self.assertIsInstance(client.gateway, OpenJiuwenGateway)
            self.assertEqual(client.backend_name, "openjiuwen-gateway")
            self.assertNotIn(SECRET_KEY, repr(client))
        finally:
            client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()

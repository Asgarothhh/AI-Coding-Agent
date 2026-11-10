"""Tests for the single openJiuwen gateway (spec sections 5.2-5.4, 4.2, 11.4)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sys
import threading
import time
import types
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from harness.llm import LLMConfig, build_llm_client
from harness.openjiuwen_gateway import (
    ALLOWED_MODELS,
    DEFAULT_LLM_MAX_RETRIES,
    DEFAULT_MAX_CONCURRENT_LLM,
    GatewayBudgetError,
    GatewayChatAdapter,
    GatewayConfigError,
    GatewayError,
    GatewayRateLimitError,
    GatewayUnavailableError,
    LLMResult,
    OpenJiuwenGateway,
    classify_error,
    concurrency_limit,
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


# -- retry, rate-limit and concurrency tests ---------------------------------
class FakeRateLimitError(Exception):
    """Mirrors the SDK's RateLimitError: class name and HTTP 429."""

    def __init__(self, message: str = "429 rate limit exceeded", status_code: int = 429) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeInternalServerError(Exception):
    status_code = 503


class FakeAuthenticationError(Exception):
    def __init__(self, message: str = "401 Unauthorized", status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeErrorWithRetryAfter(Exception):
    def __init__(self, retry_after: str, message: str = "429 too many requests") -> None:
        super().__init__(message)
        self.status_code = 429
        self.headers = {"retry-after": retry_after}


class FlakyModel:
    """Raises a scripted exception per call, then succeeds."""

    def __init__(self, errors: list[BaseException | None], assistant: FakeAssistant | None = None) -> None:
        self.errors = list(errors)
        self.assistant = assistant or FakeAssistant()
        self.calls = 0
        self._lock = threading.Lock()

    async def invoke(self, messages: Any, **kwargs: Any) -> FakeAssistant:
        with self._lock:
            index = self.calls
            self.calls += 1
        if index < len(self.errors) and self.errors[index] is not None:
            raise self.errors[index]
        return self.assistant


class GatedModel:
    """Blocks inside ``invoke`` until the test opens the gate.

    The wait is an async poll so one persistent event loop can host several
    concurrent invokes (a blocking ``Event.wait`` would serialise the fake and
    hide the semaphore under test).
    """

    def __init__(self) -> None:
        self.gate = threading.Event()
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls = 0

    async def invoke(self, messages: Any, **kwargs: Any) -> FakeAssistant:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            deadline = time.monotonic() + 10.0
            while not self.gate.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
        finally:
            with self._lock:
                self.active -= 1
        return FakeAssistant()

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self.active, self.peak


class RetryConfigTests(unittest.TestCase):
    def test_defaults_are_three_retries_and_three_concurrent_calls(self) -> None:
        self.assertEqual(DEFAULT_MAX_CONCURRENT_LLM, 3)
        self.assertEqual(DEFAULT_LLM_MAX_RETRIES, 3)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OJ_MAX_CONCURRENT_LLM", None)
            os.environ.pop("OJ_LLM_MAX_RETRIES", None)
            self.assertEqual(concurrency_limit(), 3)
            gateway = _make_gateway()
            try:
                self.assertEqual(gateway.max_concurrent, 3)
                self.assertEqual(gateway.max_retries, 3)
            finally:
                gateway.close()

    def test_concurrency_env_is_clamped_to_one_through_ten(self) -> None:
        cases = {"99": 10, "10": 10, "4": 4, "1": 1, "0": 1, "-5": 1, "abc": 3}
        for raw, expected in cases.items():
            with self.subTest(raw=raw), mock.patch.dict(
                os.environ, {"OJ_MAX_CONCURRENT_LLM": raw}, clear=False
            ):
                self.assertEqual(concurrency_limit(), expected)

    def test_retry_env_is_clamped_and_invalid_falls_back(self) -> None:
        cases = {"0": 0, "1": 1, "99": 10, "abc": 3}
        for raw, expected in cases.items():
            with self.subTest(raw=raw), mock.patch.dict(
                os.environ, {"OJ_LLM_MAX_RETRIES": raw}, clear=False
            ):
                gateway = _make_gateway()
                try:
                    self.assertEqual(gateway.max_retries, expected)
                finally:
                    gateway.close()


class ClassifyErrorTests(unittest.TestCase):
    def test_transient_statuses_are_retryable(self) -> None:
        for status in (429, 500, 502, 503, 504, 408, 409, 425):
            with self.subTest(status=status):
                info = classify_error(FakeRateLimitError("boom", status_code=status))
                self.assertTrue(info.retryable)
                self.assertEqual(info.status, status)
                self.assertTrue(info.definitely_not_sent)

    def test_auth_and_client_errors_are_not_retryable(self) -> None:
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                info = classify_error(FakeAuthenticationError("nope", status_code=status))
                self.assertFalse(info.retryable)
                self.assertFalse(info.definitely_not_sent)

    def test_message_only_rate_limit_is_detected_in_a_cause_chain(self) -> None:
        try:
            try:
                raise RuntimeError("HTTP 429: потолок тарифа 3")
            except RuntimeError as inner:
                raise ValueError("request failed") from inner
        except ValueError as exc:
            info = classify_error(exc)
            self.assertTrue(info.retryable)
            self.assertIsNone(info.status)


class RateLimitRetryTests(unittest.TestCase):
    def test_429_then_success_settles_exactly_once(self) -> None:
        ledger = FakeLedger()
        sleeps: list[float] = []
        model = FlakyModel([FakeRateLimitError()])
        gateway = _make_gateway(
            factory=lambda spec: model,
            ledger=ledger,
            sleep=sleeps.append,
        )
        self.addCleanup(gateway.close)

        result = _call(gateway, request_id="req-429-ok")

        self.assertEqual(result.text, "OK")
        self.assertEqual(model.calls, 2)
        self.assertEqual([event[0] for event in ledger.events], ["reserve", "settle"])
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 1.0)
        self.assertLess(sleeps[0], 1.0 * 1.25)

    def test_429_exhausted_releases_and_raises_clear_error(self) -> None:
        ledger = SpecStyleLedger()
        sleeps: list[float] = []
        model = FlakyModel([FakeRateLimitError()] * 4)
        gateway = _make_gateway(
            factory=lambda spec: model,
            ledger=ledger,
            sleep=sleeps.append,
        )
        self.addCleanup(gateway.close)

        with self.assertRaises(GatewayRateLimitError) as ctx:
            _call(gateway, request_id="req-429-dead")

        message = str(ctx.exception)
        self.assertIn("429", message)
        self.assertIn("retries exhausted", message)
        self.assertEqual(model.calls, 4)  # 1 initial + 3 retries
        self.assertEqual(
            [event[0] for event in ledger.events], ["reserve", "release_failed"]
        )
        self.assertEqual(ledger.events[1][1], "req-429-dead")
        self.assertTrue(ledger.events[1][2])
        for delay, base in zip(sleeps, (1.0, 2.0, 4.0)):
            self.assertGreaterEqual(delay, base)
            self.assertLess(delay, base * 1.25)

    def test_500_is_retried_and_then_settles(self) -> None:
        ledger = FakeLedger()
        sleeps: list[float] = []
        model = FlakyModel([FakeInternalServerError("503 Service Unavailable")])
        gateway = _make_gateway(
            factory=lambda spec: model,
            ledger=ledger,
            sleep=sleeps.append,
        )
        self.addCleanup(gateway.close)

        _call(gateway, request_id="req-500")

        self.assertEqual(model.calls, 2)
        self.assertEqual([event[0] for event in ledger.events], ["reserve", "settle"])
        self.assertEqual(len(sleeps), 1)

    def test_auth_error_is_not_retried(self) -> None:
        ledger = SpecStyleLedger()
        sleeps: list[float] = []
        model = FlakyModel([FakeAuthenticationError()])
        gateway = _make_gateway(
            factory=lambda spec: model,
            ledger=ledger,
            sleep=sleeps.append,
        )
        self.addCleanup(gateway.close)

        with self.assertRaises(GatewayError) as ctx:
            _call(gateway, request_id="req-401")

        self.assertNotIsInstance(ctx.exception, GatewayRateLimitError)
        self.assertIn("AuthenticationError", str(ctx.exception))
        self.assertEqual(model.calls, 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(
            [event[0] for event in ledger.events], ["reserve", "release_failed"]
        )
        self.assertFalse(ledger.events[1][2])

    def test_retry_after_header_is_honoured(self) -> None:
        sleeps: list[float] = []
        model = FlakyModel([FakeErrorWithRetryAfter("5")])
        gateway = _make_gateway(factory=lambda spec: model, sleep=sleeps.append)
        self.addCleanup(gateway.close)

        _call(gateway, request_id="req-retry-after")

        self.assertEqual(model.calls, 2)
        self.assertEqual(sleeps, [5.0])

    def test_retry_after_caps_absorbing_backoff(self) -> None:
        sleeps: list[float] = []
        model = FlakyModel(
            [FakeErrorWithRetryAfter("0.25"), FakeRateLimitError()]
        )
        gateway = _make_gateway(factory=lambda spec: model, sleep=sleeps.append)
        self.addCleanup(gateway.close)

        _call(gateway, request_id="req-retry-after-then-default")

        self.assertEqual(sleeps[0], 0.25)
        self.assertGreaterEqual(sleeps[1], 2.0)  # second retry uses 2 s base
        self.assertLess(sleeps[1], 2.0 * 1.25)

    def test_transient_log_does_not_leak_key_or_base_url(self) -> None:
        sleeps: list[float] = []
        leaky = FakeRateLimitError(f"429 key={SECRET_KEY} url={BASE_URL}")
        model = FlakyModel([leaky])
        gateway = _make_gateway(factory=lambda spec: model, sleep=sleeps.append)
        self.addCleanup(gateway.close)

        with self.assertLogs("harness.openjiuwen_gateway", level="WARNING") as captured:
            _call(gateway, request_id="req-leaky")

        text = "\n".join(captured.output)
        self.assertNotIn(SECRET_KEY, text)
        self.assertNotIn(BASE_URL, text)
        self.assertIn("429", text)


class ConcurrencyCapTests(unittest.TestCase):
    def test_semaphore_blocks_the_fourth_concurrent_call_across_gateways(self) -> None:
        tracker = GatedModel()

        def factory(spec: Any) -> GatedModel:
            return tracker

        first = _make_gateway(factory=factory, max_concurrent=3)
        second = _make_gateway(factory=factory, max_concurrent=3)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertIs(first._semaphore, second._semaphore)

        results: list[Any] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(index: int, gateway: OpenJiuwenGateway) -> None:
            try:
                result = _call(gateway, request_id=f"conc-{index}")
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    results.append(result)

        threads = [
            threading.Thread(
                target=worker,
                args=(index, first if index < 3 else second),
            )
            for index in range(4)
        ]
        for thread in threads:
            thread.start()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            active, peak = tracker.snapshot()
            if peak >= 3:
                break
            time.sleep(0.01)

        time.sleep(0.1)  # give a broken cap time to admit the 4th call
        active, peak = tracker.snapshot()
        self.assertEqual(peak, 3, "the 4th simultaneous SDK call must wait")
        self.assertEqual(active, 3)

        tracker.gate.set()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(tracker.calls, 4)
        self.assertEqual(tracker.snapshot()[1], 3)

    def test_semaphore_released_after_a_failed_call(self) -> None:
        sleeps: list[float] = []
        model = FlakyModel([FakeRateLimitError()] * 4)
        gateway = _make_gateway(
            factory=lambda spec: model,
            sleep=sleeps.append,
            max_concurrent=2,
        )
        self.addCleanup(gateway.close)

        with self.assertRaises(GatewayRateLimitError):
            _call(gateway, request_id="req-release-slot")

        # Both slots are free again, so two more calls can run concurrently.
        semaphore = gateway._semaphore
        self.assertTrue(semaphore.acquire(blocking=False))
        self.assertTrue(semaphore.acquire(blocking=False))
        semaphore.release()
        semaphore.release()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()

"""Optional openJiuwen DeepAgent runtime.

This module is the next-iteration agent path.  It is import-guarded: when
``openjiuwen`` is not installed, importing :mod:`harness.ojiuwen_runtime` is
safe and :func:`build_runtime` returns ``None``.  The default runner still uses
the deterministic/stdlib solvers; DeepAgent is available for experiments and a
future integration.

The adapter targets openJiuwen 0.1.20.post1:

* ``openjiuwen.harness.factory.create_deep_agent``
* ``openjiuwen.core.single_agent.schema.agent_card.AgentCard``
* ``openjiuwen.core.session.agent.create_agent_session``
* ``openjiuwen.harness.rails.task_completion_rail.TaskCompletionRail``
* ``openjiuwen.harness.schema.stop_condition.MaxRoundsEvaluator`` /
  ``TimeoutEvaluator``
* Rails receive ``AgentCallbackEvent.AFTER_MODEL_CALL`` and expose model usage
  through the response's ``usage_metadata``.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from .llm import LLMConfig, LLMError
from .task import Task

__all__ = [
    "AgentRuntime",
    "DeepAgentRuntime",
    "build_agent",
    "build_runtime",
    "run_query",
]

log = logging.getLogger(__name__)


@runtime_checkable
class AgentRuntime(Protocol):
    name: str

    def available(self) -> bool: ...

    def solve(self, task: Task, workdir: Path) -> str | None: ...


@dataclass(frozen=True)
class _Api:
    create_deep_agent: Any
    AgentCard: Any
    create_agent_session: Any
    TaskCompletionRail: Any
    MaxRoundsEvaluator: Any
    TimeoutEvaluator: Any
    AgentRail: Any
    AgentCallbackEvent: Any


def _load_api() -> _Api:
    """Import the heavy openJiuwen DeepAgent API only when needed."""

    try:
        from openjiuwen.core.session.agent import create_agent_session  # type: ignore[import-not-found]
        from openjiuwen.core.single_agent.rail.base import (  # type: ignore[import-not-found]
            AgentCallbackEvent,
            AgentRail,
        )
        from openjiuwen.core.single_agent.schema.agent_card import (  # type: ignore[import-not-found]
            AgentCard,
        )
        from openjiuwen.harness.factory import create_deep_agent  # type: ignore[import-not-found]
        from openjiuwen.harness.rails.task_completion_rail import (  # type: ignore[import-not-found]
            TaskCompletionRail,
        )
        from openjiuwen.harness.schema.stop_condition import (  # type: ignore[import-not-found]
            MaxRoundsEvaluator,
            TimeoutEvaluator,
        )
    except ImportError as exc:
        raise LLMError(
            "openjiuwen is not installed; install openjiuwen==0.1.20.post1 "
            "(requirements-openjiuwen.txt) to use the DeepAgent runtime"
        ) from exc
    except Exception as exc:
        raise LLMError(
            f"failed to import openJiuwen DeepAgent API: {type(exc).__name__}: {exc}"
        ) from exc

    return _Api(
        create_deep_agent=create_deep_agent,
        AgentCard=AgentCard,
        create_agent_session=create_agent_session,
        TaskCompletionRail=TaskCompletionRail,
        MaxRoundsEvaluator=MaxRoundsEvaluator,
        TimeoutEvaluator=TimeoutEvaluator,
        AgentRail=AgentRail,
        AgentCallbackEvent=AgentCallbackEvent,
    )


def _make_usage_rail(api: _Api) -> Any:
    """Create a rail that accumulates usage on AFTER_MODEL_CALL."""

    class UsageRail(api.AgentRail):
        priority = 10
        usage_event = api.AgentCallbackEvent.AFTER_MODEL_CALL
        _techlb_usage_rail = True

        def __init__(self) -> None:
            super().__init__()
            self.input_tokens = 0
            self.output_tokens = 0
            self.reasoning_tokens = 0
            self.model_calls = 0

        async def after_model_call(self, ctx: Any) -> None:
            self.model_calls += 1
            inputs = getattr(ctx, "inputs", None)
            response = getattr(inputs, "response", None)
            if response is None:
                return
            meta = getattr(response, "usage_metadata", None)
            if meta is None:
                meta = getattr(response, "usage", None)
            if meta is None:
                return

            def read(name: str, default: int = 0) -> Any:
                if isinstance(meta, MappingABC):
                    return meta.get(name, default)
                return getattr(meta, name, default)

            input_tokens = int(read("input_tokens", read("prompt_tokens", 0)) or 0)
            output_tokens = int(read("output_tokens", read("completion_tokens", 0)) or 0)
            reasoning_tokens = int(read("reasoning_tokens", 0) or 0)

            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.reasoning_tokens += reasoning_tokens

        def reset(self) -> None:
            self.input_tokens = 0
            self.output_tokens = 0
            self.reasoning_tokens = 0
            self.model_calls = 0

        def summary(self) -> dict[str, int]:
            # Mirror the model-layer fallback: if the provider reports zero
            # output tokens but reasoning tokens exist, charge reasoning.
            completion = self.output_tokens if self.output_tokens > 0 else self.reasoning_tokens
            return {
                "input_tokens": self.input_tokens,
                "output_tokens": completion,
                "prompt_tokens": self.input_tokens,
                "completion_tokens": completion,
                "reasoning_tokens": self.reasoning_tokens,
                "total_tokens": self.input_tokens + completion,
                "model_calls": self.model_calls,
            }

    return UsageRail()


def _configured_rails(agent: Any) -> list[Any]:
    getter = getattr(agent, "configured_rails", None)
    if callable(getter):
        try:
            return list(getter())
        except Exception:  # pragma: no cover - defensive across SDK versions
            pass
    rails: list[Any] = []
    for attribute in ("_pending_rails", "_registered_rails"):
        rails.extend(getattr(agent, attribute, []) or [])
    return rails


def _find_usage_rail(agent: Any) -> Any:
    for rail in _configured_rails(agent):
        if getattr(rail, "_techlb_usage_rail", False):
            return rail
    raise RuntimeError(
        "usage rail not found; build the agent with harness.ojiuwen_runtime.build_agent()"
    )


def build_agent(
    system_prompt: str,
    tools: Sequence[Any] | None = None,
    *,
    model: Any | None = None,
    model_config: LLMConfig | None = None,
    name: str = "techlb-deep-agent",
    description: str = "Budget-aware DeepAgent for the openJiuwen Challenge",
    card: Any | None = None,
    max_rounds: int | None = 20,
    timeout_seconds: float | None = 900.0,
    completion_promise: str | None = None,
    task_instruction: str | None = None,
    workspace: str | Path | None = None,
    enable_task_loop: bool = True,
    extra_rails: Sequence[Any] | None = None,
    **agent_kwargs: Any,
) -> Any:
    """Build a configured DeepAgent.

    ``tools`` are openJiuwen ``Tool``/``ToolCard`` instances.  When ``model`` is
    omitted, an ``OpenJiuwenLLMClient`` is built from ``model_config`` (or
    environment) and used to construct the SDK model.
    """

    api = _load_api()

    if model is None:
        from .ojiuwen_llm import OpenJiuwenLLMClient

        client = OpenJiuwenLLMClient(model_config or LLMConfig.from_env())
        model = client.get_model()

    if card is None:
        card = api.AgentCard(name=name, description=description)

    evaluators: list[Any] = []
    if max_rounds is not None:
        evaluators.append(api.MaxRoundsEvaluator(int(max_rounds)))
    if timeout_seconds is not None:
        evaluators.append(api.TimeoutEvaluator(float(timeout_seconds)))

    completion_rail = api.TaskCompletionRail(
        task_instruction=task_instruction,
        completion_promise=completion_promise,
        evaluators=evaluators or None,
    )
    usage_rail = _make_usage_rail(api)
    rails = [completion_rail, usage_rail, *list(extra_rails or [])]

    workspace_arg: Any = str(workspace) if isinstance(workspace, Path) else workspace
    return api.create_deep_agent(
        model,
        card=card,
        system_prompt=system_prompt,
        tools=list(tools or []),
        rails=rails,
        enable_task_loop=enable_task_loop,
        max_iterations=max_rounds,
        workspace=workspace_arg,
        **agent_kwargs,
    )


async def run_query(
    query: str,
    system_prompt: str,
    tools: Sequence[Any] | None = None,
    run_id: str = "run",
    *,
    agent: Any | None = None,
    usage_rail: Any | None = None,
    **agent_kwargs: Any,
) -> tuple[str, dict[str, int]]:
    """Run one query through a DeepAgent and return ``(output, usage)``.

    Usage is accumulated by a rail bound to
    ``AgentCallbackEvent.AFTER_MODEL_CALL``.  The returned dict contains
    ``input_tokens``/``output_tokens`` aliases plus ``prompt_tokens`` /
    ``completion_tokens`` for direct budget charging.
    """

    api = _load_api()
    if agent is None:
        agent = build_agent(system_prompt, tools, **agent_kwargs)

    rail = usage_rail if usage_rail is not None else _find_usage_rail(agent)
    rail.reset()

    session = api.create_agent_session(
        session_id=run_id,
        card=getattr(agent, "card", None),
    )
    await session.pre_run(inputs={"query": query})
    try:
        result = await agent.invoke({"query": query}, session=session)
    finally:
        await session.post_run()

    if isinstance(result, MappingABC):
        output = str(result.get("output", "") or "")
    else:
        output = str(getattr(result, "output", result) or "")
    return output, rail.summary()


@dataclass
class DeepAgentRuntime:
    """Lightweight object returned by :func:`build_runtime`."""

    module: Any
    name: str = "openjiuwen-deep-agent"

    def available(self) -> bool:
        return True

    def build_agent(self, *args: Any, **kwargs: Any) -> Any:
        return build_agent(*args, **kwargs)

    async def run_query(self, *args: Any, **kwargs: Any) -> tuple[str, dict[str, int]]:
        return await run_query(*args, **kwargs)

    def solve(self, task: Task, workdir: Path) -> str | None:
        # Deliberately not wired into the default runner yet.  A future
        # iteration can implement this by calling run_query() and writing the
        # answer through harness.store.
        raise NotImplementedError(
            "DeepAgent solve() is not wired into the MVP runner yet; "
            f"use run_query()/build_agent() for task {task.task_id} in {workdir}"
        )


def build_runtime() -> AgentRuntime | None:
    """Return a DeepAgent runtime when openjiuwen is importable, else ``None``."""

    try:
        module = importlib.import_module("openjiuwen")
    except ImportError:
        return None
    except Exception as exc:  # pragma: no cover - broken installs
        log.warning("found openjiuwen but importing it failed: %s", exc)
        return None
    log.info("openjiuwen is installed; DeepAgent runtime available (not wired into runner)")
    return DeepAgentRuntime(module=module)

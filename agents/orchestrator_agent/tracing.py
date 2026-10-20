# agents/orchestrator_agent/tracing.py — what the manager writes to the run
# log, and nothing else: what a helper was handed, what came back, and the
# state before and after a step. Every LLM call and tool call is already
# recorded by observability/agent_run.py; this adds the manager's own view.

from __future__ import annotations

from agents.orchestrator_agent.state import Stage
from observability.agent_run import EventType, emit_event


def snapshot(state) -> dict:
    """The state without the chat (the chat is already recorded with every LLM call)."""
    return {k: v for k, v in state.items() if k != "messages"}


def log_handover(stage: Stage, inputs: tuple) -> None:
    emit_event(EventType.AGENT_INPUT, None, {"stage": stage.value, "input": list(inputs)})


def log_answer(stage: Stage, result: dict) -> None:
    emit_event(EventType.AGENT_OUTPUT, result.get("status"), {"stage": stage.value, "output": result})


def log_state(stage: str, before: dict | None, after: dict) -> None:
    emit_event(EventType.STATE, None, {"stage": stage, "before": before, "after": after})

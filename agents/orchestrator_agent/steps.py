# agents/orchestrator_agent/steps.py — one step of the manager: hand the
# inputs to a helper, take its answer, write it into the state (the artifact
# in its field, and in `stages` how the helper went), and give the answer to
# the LLM. Logging is delegated to tracing.py; which helper runs and with
# what is decided in tools.py.
#
# Retry limit: guard.py. run_stage asks it before each run and, when a helper
# has used up its calls, hands the step over to it instead of running the helper.

from __future__ import annotations

import json

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agents.orchestrator_agent.blame import apply_blame, second_opinion
from agents.orchestrator_agent.guard import is_over_limit, retry_limit_reached
from agents.orchestrator_agent.state import Stage
from agents.orchestrator_agent.tracing import log_answer, log_handover, log_state, snapshot


def attempt_of(state, stage: Stage) -> int:
    """Which call this is for `stage` in this run: 1 for the first, 2 for a retry, ..."""
    return state["stages"].get(stage.value, {}).get("attempts", 0) + 1


def attempt_history_of(state, stage: Stage) -> list[dict]:
    """The helper's earlier attempts in this run, oldest first: {attempt, status, error, issues}."""
    return state["stages"].get(stage.value, {}).get("attempt_history", [])


def record(runtime: ToolRuntime, stage: Stage, result: dict, part: str | None = None) -> Command:
    """Write one helper's answer into the state: its artifact (the whole answer,
    or the `part` of it), its record in `stages` (status, attempts, error and the
    attempt_history of every attempt), and the answer as the tool message the
    LLM reads. Blame on an earlier stage is added by blame.apply_blame; when
    the blame keeps repeating, the message also carries a "second_opinion"."""
    state = runtime.state
    analysis = result.get("analysis") or {}
    attempt = {"attempt": attempt_of(state, stage), "status": result.get("status"), "error": result.get("error"),
               "issues": result.get("issues") or (result.get("lint") or {}).get("issues") or analysis.get("failures"),
               "likely_cause": analysis.get("likely_cause")}
    entry = state["stages"].get(stage.value, {})
    stages = {**state["stages"], stage.value: {
        **({"retry_limit": entry["retry_limit"]} if "retry_limit" in entry else {}),
        "status": attempt["status"], "attempts": attempt["attempt"], "error": attempt["error"],
        "attempt_history": [*attempt_history_of(state, stage), attempt]}}
    opinion = second_opinion(stages, result.get("analysis"))
    message = {**result, "second_opinion": opinion} if opinion else result
    return Command(update={
        stage.produces: result if part is None else result.get(part),
        "stages": apply_blame(stages, stage, result.get("analysis")),
        "messages": [ToolMessage(content=json.dumps(message, default=str), name=stage.value,
                                 tool_call_id=runtime.tool_call_id)],
    })


def run_stage(runtime: ToolRuntime, stage: Stage, agent, *inputs, part: str | None = None) -> Command:
    """Run one helper on `inputs`, record its answer, and log the whole step.
    A retry-limited stage that has used up its calls is not run (see guard.py)."""
    if is_over_limit(runtime.state, stage):
        return retry_limit_reached(runtime, stage)
    before = snapshot(runtime.state)
    log_handover(stage, inputs)
    result = agent(*inputs)
    log_answer(stage, result)
    command = record(runtime, stage, result, part)
    log_state(stage.value, before, {**before, **{k: v for k, v in command.update.items() if k != "messages"}})
    return command

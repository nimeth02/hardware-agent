# agents/orchestrator_agent/steps.py — one step of the manager: hand the
# inputs to a helper, take its answer, write it into the state (the artifact
# in its field, and in `stages` how the helper went), and give the answer to
# the LLM. Logging is delegated to tracing.py; which helper runs and with
# what is decided in tools.py.

from __future__ import annotations

import json

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agents.orchestrator_agent.state import Stage
from agents.orchestrator_agent.tracing import log_answer, log_handover, log_state, snapshot


def record(runtime: ToolRuntime, stage: Stage, result: dict, part: str | None = None) -> Command:
    """Write one helper's answer into the state: its artifact (the whole answer,
    or the `part` of it), its record in `stages` (status, attempts, error), and
    the answer as the tool message the LLM reads."""
    stages = runtime.state["stages"]
    entry = {"status": result.get("status"), "attempts": stages.get(stage.value, {}).get("attempts", 0) + 1,
             "error": result.get("error")}
    return Command(update={
        stage.produces: result if part is None else result.get(part),
        "stages": {**stages, stage.value: entry},
        "messages": [ToolMessage(content=json.dumps(result, default=str), name=stage.value,
                                 tool_call_id=runtime.tool_call_id)],
    })


def run_stage(runtime: ToolRuntime, stage: Stage, agent, *inputs, part: str | None = None) -> Command:
    """Run one helper on `inputs`, record its answer, and log the whole step."""
    before = snapshot(runtime.state)
    log_handover(stage, inputs)
    result = agent(*inputs)
    log_answer(stage, result)
    command = record(runtime, stage, result, part)
    log_state(stage.value, before, {**before, **{k: v for k, v in command.update.items() if k != "messages"}})
    return command

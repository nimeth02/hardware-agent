# agents/orchestrator_agent/orchestrator_agent.py — simple, fully autonomous
# orchestrator.
#
# One LLM in a tool-calling loop. The existing agents are its tools
# (tools.py); it reads their answers in the chat and decides on its own what
# to run next, what to redo and when to stop. No fixed order, no decision
# node, no guard rules: the only bound is the runtime's step limit.
#
# The run's state (state.py) is the manager's notebook: the requirement goes
# in there first, the LLM is shown a message built from it, and the tools
# read what they need from it.

from __future__ import annotations

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from agents.json_utils import extract_json
from agents.orchestrator_agent.requirement_id import new_requirement_id
from agents.orchestrator_agent.state import AGENT_NAME, PRODUCES, OrchestratorState, initial_state
from agents.orchestrator_agent.tools import make_tools
from agents.orchestrator_agent.tracing import log_state, snapshot
from observability.callback import ObservabilityCallbackHandler
from prompts.orchestrator_prompt import ORCHESTRATOR_SYSTEM_PROMPT
from services.llm_client import get_llm

FINAL_STATUSES = ("COMPLETE", "NEEDS_REVIEW", "FAILED")


def build_manager():
    """Job 1: create the manager AI with its helpers (the other agents) attached
    and the run's state as its notebook: the tools read from it and record into it."""
    return create_agent(get_llm(), make_tools(), system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
                        state_schema=OrchestratorState)


def requirement_message(state: OrchestratorState) -> HumanMessage:
    """The first thing the LLM reads: the requirement as recorded in the state,
    not the user's raw text."""
    return HumanMessage(content="\n".join([
        f"REQUIREMENT (run {state['requirement_id']})",
        state["requirement_text"],
        "",
        "Start the design flow for this requirement.",
    ]))


def ask_manager(agent, requirement_text: str, requirement_id: str) -> tuple[dict, dict]:
    """Job 2: record the requirement in the state, hand the state to the manager
    and return its final answer as a dict plus the final state. Raises on step
    limit, LLM unreachable, final answer not JSON, ..."""
    state = initial_state(requirement_text, requirement_id)
    state["messages"] = [requirement_message(state)]
    log_state("start", None, snapshot(state))
    handler = ObservabilityCallbackHandler(source=AGENT_NAME)
    out = agent.invoke(state, config={"callbacks": [handler]})
    return extract_json(out["messages"][-1].content), out


def make_report(final: dict, requirement_id: str, state: dict, error: str | None) -> dict:
    """Job 3: turn the manager's raw answer and the final state into the fixed report
    the caller expects: {"status", "summary", "next_steps", "requirement_id", "results",
    "stages", "error"}. The status is only trusted when the run finished normally and
    the manager used a known word."""
    status = final.get("status") if error is None and final.get("status") in FINAL_STATUSES else "FAILED"
    return {"status": status, "summary": final.get("summary"), "next_steps": final.get("next_steps"),
            "requirement_id": requirement_id,
            "results": {field: state.get(field) for field in PRODUCES.values()},   # what the helpers produced
            "stages": state.get("stages", {}),                                      # how each helper went
            "error": error}


def run_orchestration(requirement_text: str) -> dict:
    """Returns the report described in make_report, as far as the run got."""
    requirement_id = new_requirement_id(requirement_text)
    final, state, error = {}, {}, None
    try:
        final, state = ask_manager(build_manager(), requirement_text, requirement_id)
    except Exception as e:
        error = f"orchestration failed: {e}"
    return make_report(final, requirement_id, state, error)

# agents/rtl_agent/dto.py — the RTL agent's data transfer objects: plain
# dicts built from the state to hand data across a boundary. No logic here.
#
#   decider_input_dto(state)  what RTLDecider (the LLM that picks the next
#                             action) decides from: a compact picture of the
#                             run, never the code itself.
#   agent_result_dto(state)   what the caller gets back from run_rtl_generation
#                             (the orchestrator, the API, the tests).

from __future__ import annotations

from agents.rtl_agent.state import MAX_ITERATIONS, RTLAgentState, RTLStatus


def decider_input_dto(state: RTLAgentState) -> dict:
    """Compact picture handed to RTLDecider: where the run is, not the code."""
    return {
        "spec": state.get("spec"),
        "skills_loaded": state.get("skills_loaded", False),
        "skill_names": list((state.get("skills") or {}).keys()),
        "has_rtl": state.get("rtl") is not None,
        "lint": state.get("lint"),
        "iteration": state.get("iteration", 0),
        "max_iterations": MAX_ITERATIONS,
    }


def agent_result_dto(state: dict) -> dict:
    """The agent's answer to its caller: status, the orchestrator's note
    (requirement_id, attempt), the design, the lint result and the run facts."""
    return {
        "status": state.get("status", RTLStatus.RUNNING.value),
        "requirement_id": state.get("requirement_id"),
        "attempt": state.get("attempt", 1),
        "rtl": state.get("rtl"),
        "lint": state.get("lint"),
        "skills_used": list((state.get("skills") or {}).keys()),
        "iteration": state.get("iteration", 0),
        "decision_reason": state.get("decision_reason"),
        "warnings": state.get("warnings", []),
        "error": state.get("error"),
    }

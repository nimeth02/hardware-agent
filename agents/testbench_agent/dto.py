# agents/testbench_agent/dto.py — the testbench agent's data transfer
# objects: plain dicts built from the state to hand data across a boundary.
# No logic here.
#
#   decider_input_dto(state)  what TestbenchDecider (the LLM that picks the
#                             next action) decides from: a compact picture of
#                             the run, never the code itself.
#   agent_result_dto(state)   what the caller gets back from
#                             run_testbench_generation (the orchestrator, the
#                             API, the tests).

from __future__ import annotations

from agents.testbench_agent.state import MAX_ITERATIONS, TestbenchAgentState, TestbenchStatus


def decider_input_dto(state: TestbenchAgentState) -> dict:
    """Compact picture handed to TestbenchDecider: where the run is, not the code."""
    return {
        "spec": state.get("spec"),
        "rtl_entity": (state.get("rtl") or {}).get("entity_name"),
        "skills_loaded": state.get("skills_loaded", False),
        "skill_names": list((state.get("skills") or {}).keys()),
        "has_testbench": state.get("testbench") is not None,
        "lint": state.get("lint"),
        "iteration": state.get("iteration", 0),
        "max_iterations": MAX_ITERATIONS,
    }


def agent_result_dto(state: dict) -> dict:
    """The agent's answer to its caller: status, the orchestrator's note
    (requirement_id, attempt), the testbench, the lint result and the run facts."""
    return {
        "status": state.get("status", TestbenchStatus.RUNNING.value),
        "requirement_id": state.get("requirement_id"),
        "attempt": state.get("attempt", 1),
        "testbench": state.get("testbench"),
        "lint": state.get("lint"),
        "skills_used": list((state.get("skills") or {}).keys()),
        "iteration": state.get("iteration", 0),
        "decision_reason": state.get("decision_reason"),
        "warnings": state.get("warnings", []),
        "error": state.get("error"),
    }

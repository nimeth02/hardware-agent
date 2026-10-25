# agents/test_vector_agent/dto.py — the test vector agent's data transfer
# objects: plain dicts built from the state to hand data across a boundary.
# No logic here.
#
#   decider_input_dto(state)  what TestVectorDecider (the LLM that picks the
#                             next action) decides from: a compact picture of
#                             the run, never the script or the CSVs.
#   agent_result_dto(state)   what the caller gets back from
#                             run_test_vector_generation (the orchestrator,
#                             the API, the tests).

from __future__ import annotations

from agents.test_vector_agent.state import MAX_ITERATIONS, TestVectorAgentState, TestVectorStatus


def decider_input_dto(state: TestVectorAgentState) -> dict:
    """Compact picture handed to TestVectorDecider: where the run is, not the data."""
    return {
        "spec": state.get("spec"),
        "skills_loaded": state.get("skills_loaded", False),
        "skill_names": list((state.get("skills") or {}).keys()),
        "has_result": state.get("result") is not None,
        "validation": state.get("validation"),
        "iteration": state.get("iteration", 0),
        "max_iterations": MAX_ITERATIONS,
    }


def agent_result_dto(state: dict) -> dict:
    """The agent's answer to its caller: status, the orchestrator's note
    (requirement_id, attempt), the run folder, the generator's summary, the
    validation result and the run facts."""
    return {
        "status": state.get("status", TestVectorStatus.RUNNING.value),
        "requirement_id": state.get("requirement_id"),
        "attempt": state.get("attempt", 1),
        "run_dir": state.get("run_dir"),
        "result": state.get("result"),
        "validation": state.get("validation"),
        "skills_used": list((state.get("skills") or {}).keys()),
        "iteration": state.get("iteration", 0),
        "decision_reason": state.get("decision_reason"),
        "warnings": state.get("warnings", []),
        "error": state.get("error"),
    }

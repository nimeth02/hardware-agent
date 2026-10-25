# agents/test_vector_agent/test_vector_guard.py — deterministic guardrails
# applied to every decider decision before the graph acts on it. The
# decider (LLM) suggests; these rules, using only facts from the state,
# have the final say. Rules run in order; the first one that objects
# replaces the decision.

from __future__ import annotations

from typing import Callable

from agents.test_vector_agent.state import Action, TestVectorAgentState
from agents.test_vector_agent.test_vector_decider import Decision

Rule = Callable[[Decision, TestVectorAgentState], Decision | None]


def must_load_skills_first(decision: Decision, state: TestVectorAgentState) -> Decision | None:
    """Skills are loaded exactly once, before anything else."""
    if not state.get("skills_loaded"):
        if decision.action is Action.LOAD_SKILLS:
            return decision
        return Decision(Action.LOAD_SKILLS, "guard: skills must be loaded before generating")
    if decision.action is Action.LOAD_SKILLS:
        next_action = Action.GENERATE_VECTORS if state.get("result") is None else Action.VALIDATE_VECTORS
        return Decision(next_action, "guard: skills already loaded")
    return None


def must_generate_first(decision: Decision, state: TestVectorAgentState) -> Decision | None:
    if state.get("result") is None and decision.action is not Action.GENERATE_VECTORS:
        return Decision(Action.GENERATE_VECTORS, "guard: no test vectors yet")
    return None


def validate_before_finalize(decision: Decision, state: TestVectorAgentState) -> Decision | None:
    if decision.action is Action.FINALIZE_VECTORS and state.get("validation") is None:
        return Decision(Action.VALIDATE_VECTORS, "guard: vectors have not been validated")
    return None


def no_finalize_with_issues(decision: Decision, state: TestVectorAgentState) -> Decision | None:
    """The validator decides when the vectors are done, not the decider."""
    if decision.action is Action.FINALIZE_VECTORS and (state.get("validation") or {}).get("issues"):
        return Decision(Action.GENERATE_VECTORS, "guard: cannot finalize while the validator reports issues; fixing")
    return None


RULES: tuple[Rule, ...] = (
    must_load_skills_first,
    must_generate_first,
    validate_before_finalize,
    no_finalize_with_issues,
)


def guard(decision: Decision, state: TestVectorAgentState) -> Decision:
    for rule in RULES:
        corrected = rule(decision, state)
        if corrected is not None:
            return corrected
    return decision

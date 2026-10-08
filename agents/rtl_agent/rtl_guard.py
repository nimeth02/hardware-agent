# agents/rtl_agent/rtl_guard.py — deterministic guardrails applied to every
# planner decision before the graph acts on it. The planner (LLM) suggests;
# these rules, using only facts from the state, have the final say.
#
# Rules run in order; the first one that objects replaces the decision.

from __future__ import annotations

from typing import Callable

from agents.rtl_agent.rtl_decider import Decision
from agents.rtl_agent.state import Action, RTLAgentState

Rule = Callable[[Decision, RTLAgentState], Decision | None]


def must_load_skills_first(decision: Decision, state: RTLAgentState) -> Decision | None:
    """Skills are loaded exactly once, before anything else."""
    if not state.get("skills_loaded"):
        if decision.action is Action.LOAD_SKILLS:
            return decision
        return Decision(Action.LOAD_SKILLS, "guard: skills must be loaded before generating")
    if decision.action is Action.LOAD_SKILLS:
        next_action = Action.GENERATE_RTL if state.get("rtl") is None else Action.LINT_RTL
        return Decision(next_action, "guard: skills already loaded")
    return None


def must_generate_first(decision: Decision, state: RTLAgentState) -> Decision | None:
    if state.get("rtl") is None and decision.action is not Action.GENERATE_RTL:
        return Decision(Action.GENERATE_RTL, "guard: no design yet")
    return None


def lint_before_finalize(decision: Decision, state: RTLAgentState) -> Decision | None:
    if decision.action is Action.FINALIZE_RTL and state.get("lint") is None:
        return Decision(Action.LINT_RTL, "guard: design has not been linted")
    return None


def no_finalize_with_issues(decision: Decision, state: RTLAgentState) -> Decision | None:
    """The linter decides when the design is done, not the planner."""
    if decision.action is Action.FINALIZE_RTL and (state.get("lint") or {}).get("issues"):
        return Decision(Action.GENERATE_RTL, "guard: cannot finalize while the linter reports issues; fixing")
    return None


RULES: tuple[Rule, ...] = (
    must_load_skills_first,
    must_generate_first,
    lint_before_finalize,
    no_finalize_with_issues,
)


def guard(decision: Decision, state: RTLAgentState) -> Decision:
    for rule in RULES:
        corrected = rule(decision, state)
        if corrected is not None:
            return corrected
    return decision

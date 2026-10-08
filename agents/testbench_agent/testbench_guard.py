# agents/testbench_agent/testbench_guard.py — deterministic guardrails
# applied to every planner decision before the graph acts on it. The
# planner (LLM) suggests; these rules, using only facts from the state,
# have the final say. Rules run in order; the first one that objects
# replaces the decision.

from __future__ import annotations

from typing import Callable

from agents.testbench_agent.state import Action, TestbenchAgentState
from agents.testbench_agent.testbench_decider import Decision

Rule = Callable[[Decision, TestbenchAgentState], Decision | None]


def must_load_skills_first(decision: Decision, state: TestbenchAgentState) -> Decision | None:
    """Skills are loaded exactly once, before anything else."""
    if not state.get("skills_loaded"):
        if decision.action is Action.LOAD_SKILLS:
            return decision
        return Decision(Action.LOAD_SKILLS, "guard: skills must be loaded before generating")
    if decision.action is Action.LOAD_SKILLS:
        next_action = Action.GENERATE_TESTBENCH if state.get("testbench") is None else Action.LINT_TESTBENCH
        return Decision(next_action, "guard: skills already loaded")
    return None


def must_generate_first(decision: Decision, state: TestbenchAgentState) -> Decision | None:
    if state.get("testbench") is None and decision.action is not Action.GENERATE_TESTBENCH:
        return Decision(Action.GENERATE_TESTBENCH, "guard: no testbench yet")
    return None


def lint_before_finalize(decision: Decision, state: TestbenchAgentState) -> Decision | None:
    if decision.action is Action.FINALIZE_TESTBENCH and state.get("lint") is None:
        return Decision(Action.LINT_TESTBENCH, "guard: testbench has not been linted")
    return None


def no_finalize_with_issues(decision: Decision, state: TestbenchAgentState) -> Decision | None:
    """The linter decides when the testbench is done, not the planner."""
    if decision.action is Action.FINALIZE_TESTBENCH and (state.get("lint") or {}).get("issues"):
        return Decision(Action.GENERATE_TESTBENCH, "guard: cannot finalize while the linter reports issues; fixing")
    return None


RULES: tuple[Rule, ...] = (
    must_load_skills_first,
    must_generate_first,
    lint_before_finalize,
    no_finalize_with_issues,
)


def guard(decision: Decision, state: TestbenchAgentState) -> Decision:
    for rule in RULES:
        corrected = rule(decision, state)
        if corrected is not None:
            return corrected
    return decision

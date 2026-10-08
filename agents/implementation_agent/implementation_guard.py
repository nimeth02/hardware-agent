# agents/implementation_agent/implementation_guard.py — deterministic
# guardrails applied to every planner decision before the graph acts on it.
# The planner (LLM) suggests; these rules, using only facts from the state,
# have the final say. Rules run in order; the first one that objects
# replaces the decision.

from __future__ import annotations

from typing import Callable

from agents.implementation_agent.implementation_decider import Decision
from agents.implementation_agent.state import Action, ImplementationAgentState

Rule = Callable[[Decision, ImplementationAgentState], Decision | None]


def next_step(state: ImplementationAgentState) -> Action:
    """The first action whose result the state does not have yet."""
    if state.get("scripts") is None:
        return Action.WRITE_SCRIPTS
    if state.get("batch") is None:
        return Action.RUN_IMPLEMENTATION
    if state.get("analysis") is None:
        return Action.ANALYZE_RESULT
    return Action.FINALIZE_IMPLEMENTATION


def must_load_project_first(decision: Decision, state: ImplementationAgentState) -> Decision | None:
    """The project is read exactly once, before anything else."""
    if state.get("project") is None:
        if decision.action is Action.LOAD_PROJECT:
            return decision
        return Decision(Action.LOAD_PROJECT, "guard: project must be loaded first")
    if decision.action is Action.LOAD_PROJECT:
        return Decision(next_step(state), "guard: project already loaded")
    return None


def write_scripts_once_before_running(decision: Decision, state: ImplementationAgentState) -> Decision | None:
    if state.get("scripts") is None:
        if decision.action is not Action.WRITE_SCRIPTS:
            return Decision(Action.WRITE_SCRIPTS, "guard: constraints and TCL have not been written")
        return None
    if decision.action is Action.WRITE_SCRIPTS:
        return Decision(next_step(state), "guard: scripts already written")
    return None


def run_once(decision: Decision, state: ImplementationAgentState) -> Decision | None:
    """A Vivado run is minutes of work and deterministic: never repeat it."""
    if decision.action is Action.RUN_IMPLEMENTATION and state.get("batch") is not None:
        return Decision(next_step(state), "guard: implementation already ran; not rerunning")
    return None


def run_before_analyze(decision: Decision, state: ImplementationAgentState) -> Decision | None:
    if state.get("batch") is None and decision.action in (Action.ANALYZE_RESULT, Action.FINALIZE_IMPLEMENTATION):
        return Decision(Action.RUN_IMPLEMENTATION, "guard: nothing has been implemented yet")
    return None


def analyze_before_finalize(decision: Decision, state: ImplementationAgentState) -> Decision | None:
    """The analyst decides the verdict, not the planner."""
    if decision.action is Action.FINALIZE_IMPLEMENTATION and state.get("analysis") is None:
        return Decision(Action.ANALYZE_RESULT, "guard: no verdict yet")
    return None


RULES: tuple[Rule, ...] = (
    must_load_project_first,
    write_scripts_once_before_running,
    run_once,
    run_before_analyze,
    analyze_before_finalize,
)


def guard(decision: Decision, state: ImplementationAgentState) -> Decision:
    for rule in RULES:
        corrected = rule(decision, state)
        if corrected is not None:
            return corrected
    return decision

# agents/simulation_agent/simulation_guard.py — deterministic guardrails
# applied to every planner decision before the graph acts on it. The
# planner (LLM) suggests; these rules, using only facts from the state,
# have the final say. Rules run in order; the first one that objects
# replaces the decision.

from __future__ import annotations

from typing import Callable

from agents.simulation_agent.simulation_decider import Decision
from agents.simulation_agent.state import MAX_SIM_RERUNS, Action, SimulationAgentState

Rule = Callable[[Decision, SimulationAgentState], Decision | None]


def next_step(state: SimulationAgentState) -> Action:
    """The first action whose result the state does not have yet."""
    compile_result = state.get("compile")
    if compile_result is None:
        return Action.COMPILE_DESIGN
    if compile_result.get("status") == "ok" and state.get("sim") is None:
        return Action.RUN_SIMULATION
    if state.get("analysis") is None:
        return Action.ANALYZE_RESULT
    return Action.FINALIZE_SIMULATION


def must_load_project_first(decision: Decision, state: SimulationAgentState) -> Decision | None:
    """The project is read exactly once, before anything else."""
    if state.get("project") is None:
        if decision.action is Action.LOAD_PROJECT:
            return decision
        return Decision(Action.LOAD_PROJECT, "guard: project must be loaded first")
    if decision.action is Action.LOAD_PROJECT:
        return Decision(next_step(state), "guard: project already loaded")
    return None


def compile_once_before_anything(decision: Decision, state: SimulationAgentState) -> Decision | None:
    if state.get("compile") is None:
        if decision.action is not Action.COMPILE_DESIGN:
            return Decision(Action.COMPILE_DESIGN, "guard: design has not been compiled")
        return None
    if decision.action is Action.COMPILE_DESIGN:
        return Decision(next_step(state), "guard: design already compiled")
    return None


def no_simulation_after_compile_error(decision: Decision, state: SimulationAgentState) -> Decision | None:
    if decision.action is Action.RUN_SIMULATION and (state.get("compile") or {}).get("status") != "ok":
        return Decision(Action.ANALYZE_RESULT, "guard: design failed to compile; analyzing instead of simulating")
    return None


def simulate_before_analyze(decision: Decision, state: SimulationAgentState) -> Decision | None:
    compiled_ok = (state.get("compile") or {}).get("status") == "ok"
    if compiled_ok and state.get("sim") is None and decision.action in (Action.ANALYZE_RESULT, Action.FINALIZE_SIMULATION):
        return Decision(Action.RUN_SIMULATION, "guard: nothing simulated yet")
    return None


def rerun_only_without_marker(decision: Decision, state: SimulationAgentState) -> Decision | None:
    """A second run only makes sense when the first ended without a verdict
    marker (sim_time too short), and only MAX_SIM_RERUNS times."""
    sim = state.get("sim")
    if decision.action is not Action.RUN_SIMULATION or sim is None:
        return None
    if sim.get("outcome") == "no_marker" and state.get("sim_runs", 0) <= MAX_SIM_RERUNS:
        return None
    return Decision(next_step(state), "guard: simulation already has a result; not rerunning")


def analyze_before_finalize(decision: Decision, state: SimulationAgentState) -> Decision | None:
    """The analyst decides the verdict, not the planner."""
    if decision.action is Action.FINALIZE_SIMULATION and state.get("analysis") is None:
        return Decision(Action.ANALYZE_RESULT, "guard: no verdict yet")
    return None


RULES: tuple[Rule, ...] = (
    must_load_project_first,
    compile_once_before_anything,
    no_simulation_after_compile_error,
    simulate_before_analyze,
    rerun_only_without_marker,
    analyze_before_finalize,
)


def guard(decision: Decision, state: SimulationAgentState) -> Decision:
    for rule in RULES:
        corrected = rule(decision, state)
        if corrected is not None:
            return corrected
    return decision

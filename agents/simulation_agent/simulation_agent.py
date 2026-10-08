# agents/simulation_agent/simulation_agent.py — bounded planning agent that
# simulates a project folder (src/, tb/, test_vectors.csv) through the
# Vivado MCP server and delivers a verdict.
#
#   START -> decide -+- load_project --------------------------------+
#                    +- compile_design ------------------------------+
#                    +- run_simulation (rerun w/ longer sim_time) ---+-> decide ...
#                    +- analyze_result ------------------------------+
#                    +- finalize_simulation -> END (COMPLETE | NEEDS_REVIEW)
#                    +- limit -> END (NEEDS_REVIEW | FAILED)
#
# `decide` asks the LLM policy (SimulationDecider) for the next action, then
# applies deterministic guardrails (simulation_guard): load before compile,
# compile before simulate, no simulate after a compile error, analyze
# before finalize, reruns only when no result marker appeared. The agent
# never edits RTL or testbench files — it reports.

from __future__ import annotations

import threading
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from agents.simulation_agent.simulation_guard import guard
from agents.simulation_agent.simulation_runner import double_sim_time
from agents.simulation_agent.state import (
    DEFAULT_SIM_TIME, MAX_ITERATIONS, PROJECTS_DIR, Action, SimulationAgentState, SimulationStatus, initial_state,
)
from agents.simulation_agent.tools import SimulationAgentTools
from services.config import REPO_ROOT


def resolve_project_dir(project: str) -> str:
    """A name under projects/, a repo-relative path, or an absolute path."""
    path = Path(project)
    if not path.is_absolute():
        candidate = REPO_ROOT / path
        path = candidate if candidate.is_dir() else REPO_ROOT / PROJECTS_DIR / project
    return str(path.resolve())


class SimulationAgent:
    def __init__(self, tools: SimulationAgentTools | None = None, max_iterations: int = MAX_ITERATIONS):
        self.tools = tools or SimulationAgentTools()
        self.max_iterations = max_iterations
        self.graph = self._build().compile()

    # ── nodes ────────────────────────────────────────────────────────────
    def decide_node(self, state: SimulationAgentState) -> dict:
        iteration = state.get("iteration", 0)
        if iteration >= self.max_iterations:
            has_verdict = state.get("analysis") is not None
            return {
                "status": (SimulationStatus.NEEDS_REVIEW if has_verdict else SimulationStatus.FAILED).value,
                "last_action": None,
                "error": f"Iteration limit ({self.max_iterations}) reached before the simulation was finalized.",
            }
        decision = guard(self.tools.decider.decide(self.summary(state)), state)
        return {
            "iteration": iteration + 1,
            "last_action": decision.action.value,
            "decision_reason": decision.reason,
        }

    def load_project_node(self, state: SimulationAgentState) -> dict:
        try:
            project = self.tools.project_loader.load(state["project_dir"])
        except Exception as e:  # unusable folder or file server down: nothing to simulate
            return {"status": SimulationStatus.FAILED.value, "error": f"project load failed: {e}"}
        warnings = list(state.get("warnings", []))
        if project.get("spec") is None:
            warnings.append("no spec.json in the project; the analyst will judge from the testbench alone")
        if project.get("test_vectors") is None:
            warnings.append("no test-vector CSV in the project")
        return {"project": project, "warnings": warnings}

    def compile_node(self, state: SimulationAgentState) -> dict:
        try:
            return {"compile": self.tools.compiler(state["project"])}
        except Exception as e:  # MCP/transport failure: end the run, don't crash the caller
            return {"status": SimulationStatus.FAILED.value, "error": f"compile failed: {e}"}

    def simulate_node(self, state: SimulationAgentState) -> dict:
        sim_time = state["sim_time"] if state.get("sim") is None else double_sim_time(state["sim_time"])
        try:
            sim = self.tools.simulator(state["project"], sim_time)
        except Exception as e:
            return {"status": SimulationStatus.FAILED.value, "error": f"simulation failed: {e}"}
        return {"sim": sim, "sim_time": sim_time, "sim_runs": state.get("sim_runs", 0) + 1, "analysis": None}

    def analyze_node(self, state: SimulationAgentState) -> dict:
        try:
            analysis = self.tools.analyst.analyze(state["project"], state.get("compile"), state.get("sim"))
        except Exception as e:  # LLM unreachable: the raw tool output is still in the state
            return {"status": SimulationStatus.FAILED.value, "error": f"analysis failed: {e}"}
        return {"analysis": analysis}

    def finalize_node(self, state: SimulationAgentState) -> dict:
        analysis = state.get("analysis") or {}
        if analysis.get("verdict") == "pass":
            return {"status": SimulationStatus.COMPLETE.value, "error": None}
        return {"status": SimulationStatus.NEEDS_REVIEW.value,
                "error": f"simulation {analysis.get('verdict', 'error')}: {analysis.get('summary') or 'see analysis'}"}

    # ── graph ────────────────────────────────────────────────────────────
    @staticmethod
    def _route(state: SimulationAgentState) -> str:
        if SimulationStatus(state.get("status", SimulationStatus.RUNNING.value)).terminal:
            return END
        return state.get("last_action") or END

    @staticmethod
    def _after_step(state: SimulationAgentState) -> str:
        return END if SimulationStatus(state["status"]).terminal else "decide"

    def _build(self) -> StateGraph:
        g = StateGraph(SimulationAgentState)
        g.add_node("decide", self.decide_node)
        g.add_node(Action.LOAD_PROJECT.value, self.load_project_node)
        g.add_node(Action.COMPILE_DESIGN.value, self.compile_node)
        g.add_node(Action.RUN_SIMULATION.value, self.simulate_node)
        g.add_node(Action.ANALYZE_RESULT.value, self.analyze_node)
        g.add_node(Action.FINALIZE_SIMULATION.value, self.finalize_node)
        g.add_edge(START, "decide")
        g.add_conditional_edges("decide", self._route, {a.value: a.value for a in Action} | {END: END})
        for action in (Action.LOAD_PROJECT, Action.COMPILE_DESIGN, Action.RUN_SIMULATION, Action.ANALYZE_RESULT):
            g.add_conditional_edges(action.value, self._after_step, {"decide": "decide", END: END})
        g.add_edge(Action.FINALIZE_SIMULATION.value, END)
        return g

    # ── public API ───────────────────────────────────────────────────────
    def run(self, project: str, sim_time: str = DEFAULT_SIM_TIME) -> dict:
        return self.summarize(self.graph.invoke(initial_state(resolve_project_dir(project), sim_time)))

    @staticmethod
    def summary(state: SimulationAgentState) -> dict:
        """Compact view handed to the planner."""
        project = state.get("project") or {}
        compile_result = state.get("compile")
        sim = state.get("sim")
        return {
            "project_loaded": state.get("project") is not None,
            "project": {k: project.get(k) for k in ("name", "src_files", "tb_file", "top_entity", "test_vectors")} if project else None,
            "compile": {"status": compile_result.get("status"), "error_class": compile_result.get("error_class")} if compile_result else None,
            "sim": {"status": sim.get("status"), "outcome": sim.get("outcome"), "sim_time": sim.get("sim_time")} if sim else None,
            "sim_runs": state.get("sim_runs", 0),
            "analysis": {"verdict": (state.get("analysis") or {}).get("verdict")} if state.get("analysis") else None,
            "iteration": state.get("iteration", 0),
            "max_iterations": MAX_ITERATIONS,
        }

    @staticmethod
    def summarize(state: dict) -> dict:
        project = state.get("project") or {}
        return {
            "status": state.get("status", SimulationStatus.RUNNING.value),
            "project_dir": state.get("project_dir"),
            "project": {k: project.get(k) for k in ("name", "src_files", "tb_file", "top_entity", "test_vectors")} if project else None,
            "compile": state.get("compile"),
            "sim": state.get("sim"),
            "analysis": state.get("analysis"),
            "verdict": (state.get("analysis") or {}).get("verdict"),
            "iteration": state.get("iteration", 0),
            "decision_reason": state.get("decision_reason"),
            "warnings": state.get("warnings", []),
            "error": state.get("error"),
        }


# ── module-level API (kept for existing callers) ───────────────────────────

_default_agent: SimulationAgent | None = None
_default_lock = threading.Lock()


def get_agent() -> SimulationAgent:
    global _default_agent
    with _default_lock:
        if _default_agent is None:
            _default_agent = SimulationAgent()
        return _default_agent


def run_simulation(project: str, sim_time: str = DEFAULT_SIM_TIME) -> dict:
    """Returns {"status", "project_dir", "project", "compile", "sim", "analysis", "verdict",
    "iteration", "decision_reason", "warnings", "error"}."""
    return get_agent().run(project, sim_time)

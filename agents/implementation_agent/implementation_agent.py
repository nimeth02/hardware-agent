# agents/implementation_agent/implementation_agent.py — bounded planning
# agent that synthesises and implements a project folder (src/, spec.json,
# optional constraints/) through the Vivado MCP server and delivers a
# timing/utilization verdict.
#
#   START -> decide -+- load_project -----------------------------+
#                    +- write_scripts (clock XDC + synth->route TCL)+
#                    +- run_implementation (run_vivado_batch) ------+-> decide ...
#                    +- analyze_result ------------------------------+
#                    +- finalize_implementation -> END (COMPLETE | NEEDS_REVIEW)
#                    +- limit -> END (NEEDS_REVIEW | FAILED)
#
# `decide` asks the LLM policy (ImplementationDecider) for the next action,
# then applies deterministic guardrails (implementation_guard): load before
# write, write before run, run exactly once, analyze before finalize. The
# agent never edits the design sources — it reports.

from __future__ import annotations

import threading
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from agents.implementation_agent.implementation_guard import guard
from agents.implementation_agent.state import (
    MAX_ITERATIONS, PROJECTS_DIR, Action, ImplementationAgentState, ImplementationStatus, initial_state,
)
from agents.implementation_agent.tools import ImplementationAgentTools
from services.config import REPO_ROOT, VIVADO_PART


def resolve_project_dir(project: str) -> str:
    """A name under projects/, a repo-relative path, or an absolute path."""
    path = Path(project)
    if not path.is_absolute():
        candidate = REPO_ROOT / path
        path = candidate if candidate.is_dir() else REPO_ROOT / PROJECTS_DIR / project
    return str(path.resolve())


class ImplementationAgent:
    def __init__(self, tools: ImplementationAgentTools | None = None, max_iterations: int = MAX_ITERATIONS):
        self.tools = tools or ImplementationAgentTools()
        self.max_iterations = max_iterations
        self.graph = self._build().compile()

    # ── nodes ────────────────────────────────────────────────────────────
    def decide_node(self, state: ImplementationAgentState) -> dict:
        iteration = state.get("iteration", 0)
        if iteration >= self.max_iterations:
            has_verdict = state.get("analysis") is not None
            return {
                "status": (ImplementationStatus.NEEDS_REVIEW if has_verdict else ImplementationStatus.FAILED).value,
                "last_action": None,
                "error": f"Iteration limit ({self.max_iterations}) reached before the implementation was finalized.",
            }
        decision = guard(self.tools.decider.decide(self.summary(state)), state)
        return {
            "iteration": iteration + 1,
            "last_action": decision.action.value,
            "decision_reason": decision.reason,
        }

    def load_project_node(self, state: ImplementationAgentState) -> dict:
        try:
            project = self.tools.project_loader.load(state["project_dir"])
        except Exception as e:  # unusable folder or file server down: nothing to implement
            return {"status": ImplementationStatus.FAILED.value, "error": f"project load failed: {e}"}
        warnings = list(state.get("warnings", []))
        if project.get("spec") is None and project.get("xdc_file") is None:
            warnings.append("no spec.json and no constraints/*.xdc in the project; timing will be unconstrained")
        return {"project": project, "warnings": warnings}

    def write_scripts_node(self, state: ImplementationAgentState) -> dict:
        try:
            return {"scripts": self.tools.script_writer(state["project"], state["part"])}
        except Exception as e:  # file server failure: end the run, don't crash the caller
            return {"status": ImplementationStatus.FAILED.value, "error": f"writing scripts failed: {e}"}

    def run_node(self, state: ImplementationAgentState) -> dict:
        try:
            batch = self.tools.runner(state["project"], state["scripts"])
            metrics = self.tools.metrics(state["project"]) if batch.get("reports_dir") else {}
        except Exception as e:
            return {"status": ImplementationStatus.FAILED.value, "error": f"implementation run failed: {e}"}
        return {"batch": batch, "metrics": metrics, "analysis": None}

    def analyze_node(self, state: ImplementationAgentState) -> dict:
        try:
            analysis = self.tools.analyst.analyze(
                state["project"], state.get("scripts"), state.get("batch"), state.get("metrics"))
        except Exception as e:  # LLM unreachable: the raw tool output is still in the state
            return {"status": ImplementationStatus.FAILED.value, "error": f"analysis failed: {e}"}
        return {"analysis": analysis}

    def finalize_node(self, state: ImplementationAgentState) -> dict:
        analysis = state.get("analysis") or {}
        if analysis.get("verdict") == "pass":
            return {"status": ImplementationStatus.COMPLETE.value, "error": None}
        return {"status": ImplementationStatus.NEEDS_REVIEW.value,
                "error": f"implementation {analysis.get('verdict', 'error')}: {analysis.get('summary') or 'see analysis'}"}

    # ── graph ────────────────────────────────────────────────────────────
    @staticmethod
    def _route(state: ImplementationAgentState) -> str:
        if ImplementationStatus(state.get("status", ImplementationStatus.RUNNING.value)).terminal:
            return END
        return state.get("last_action") or END

    @staticmethod
    def _after_step(state: ImplementationAgentState) -> str:
        return END if ImplementationStatus(state["status"]).terminal else "decide"

    def _build(self) -> StateGraph:
        g = StateGraph(ImplementationAgentState)
        g.add_node("decide", self.decide_node)
        g.add_node(Action.LOAD_PROJECT.value, self.load_project_node)
        g.add_node(Action.WRITE_SCRIPTS.value, self.write_scripts_node)
        g.add_node(Action.RUN_IMPLEMENTATION.value, self.run_node)
        g.add_node(Action.ANALYZE_RESULT.value, self.analyze_node)
        g.add_node(Action.FINALIZE_IMPLEMENTATION.value, self.finalize_node)
        g.add_edge(START, "decide")
        g.add_conditional_edges("decide", self._route, {a.value: a.value for a in Action} | {END: END})
        for action in (Action.LOAD_PROJECT, Action.WRITE_SCRIPTS, Action.RUN_IMPLEMENTATION, Action.ANALYZE_RESULT):
            g.add_conditional_edges(action.value, self._after_step, {"decide": "decide", END: END})
        g.add_edge(Action.FINALIZE_IMPLEMENTATION.value, END)
        return g

    # ── public API ───────────────────────────────────────────────────────
    def run(self, project: str, part: str = VIVADO_PART) -> dict:
        return self.summarize(self.graph.invoke(initial_state(resolve_project_dir(project), part or VIVADO_PART)))

    @staticmethod
    def summary(state: ImplementationAgentState) -> dict:
        """Compact view handed to the planner."""
        project = state.get("project") or {}
        batch = state.get("batch")
        metrics = state.get("metrics") or {}
        return {
            "project_loaded": state.get("project") is not None,
            "project": {k: project.get(k) for k in ("name", "src_files", "top_entity", "xdc_file")} if project else None,
            "part": state.get("part"),
            "scripts_written": state.get("scripts") is not None,
            "batch": {"status": batch.get("status"), "error_class": batch.get("error_class"),
                      "has_reports": bool(batch.get("reports_dir"))} if batch else None,
            "metrics": {k: metrics.get(k) for k in ("wns_ns", "lut_pct", "ff_pct", "unconstrained")} if metrics else None,
            "analysis": {"verdict": (state.get("analysis") or {}).get("verdict")} if state.get("analysis") else None,
            "iteration": state.get("iteration", 0),
            "max_iterations": MAX_ITERATIONS,
        }

    @staticmethod
    def summarize(state: dict) -> dict:
        project = state.get("project") or {}
        return {
            "status": state.get("status", ImplementationStatus.RUNNING.value),
            "project_dir": state.get("project_dir"),
            "part": state.get("part"),
            "project": {k: project.get(k) for k in ("name", "src_files", "top_entity", "xdc_file")} if project else None,
            "scripts": state.get("scripts"),
            "batch": state.get("batch"),
            "metrics": state.get("metrics"),
            "analysis": state.get("analysis"),
            "verdict": (state.get("analysis") or {}).get("verdict"),
            "iteration": state.get("iteration", 0),
            "decision_reason": state.get("decision_reason"),
            "warnings": state.get("warnings", []),
            "error": state.get("error"),
        }


# ── module-level API (kept for existing callers) ───────────────────────────

_default_agent: ImplementationAgent | None = None
_default_lock = threading.Lock()


def get_agent() -> ImplementationAgent:
    global _default_agent
    with _default_lock:
        if _default_agent is None:
            _default_agent = ImplementationAgent()
        return _default_agent


def run_implementation(project: str, part: str = VIVADO_PART) -> dict:
    """Returns {"status", "project_dir", "part", "project", "scripts", "batch", "metrics", "analysis",
    "verdict", "iteration", "decision_reason", "warnings", "error"}."""
    return get_agent().run(project, part)

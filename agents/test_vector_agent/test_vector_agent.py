# agents/test_vector_agent/test_vector_agent.py — bounded planning agent that
# turns an approved spec into a validated golden test-vector CSV, using the
# project's test-vector skills and the MATLAB MCP tools.
#
#   START -> decide -+- load_skills -----------------------------------+
#                    +- generate_vectors -> validate_vectors -----------+-> decide ...
#                    +- validate_vectors ---------------------------------+
#                    +- finalize_vectors -> END (COMPLETE)
#                    +- limit / fixes not converging -> END (NEEDS_REVIEW | FAILED)
#
# `decide` asks the LLM policy (TestVectorDecider) for the next action, then
# applies deterministic guardrails (test_vector_guard). Every generation is
# followed by the validator; validation issues feed the next generation as
# fix instructions. No RTL is needed: the vectors come from the spec alone.

from __future__ import annotations

import threading
import time

from langgraph.graph import END, START, StateGraph

from agents.test_vector_agent.state import (
    MAX_ITERATIONS, MAX_STALLED_FIXES, Action, TestVectorAgentState, TestVectorStatus, initial_state,
)
from agents.test_vector_agent.test_vector_guard import guard
from agents.test_vector_agent.tools import TestVectorAgentTools
from services.config import REPO_ROOT

RUNS_DIR = REPO_ROOT / "runs" / "test_vectors"


def new_run_dir(spec: dict) -> str:
    """Path only: the MATLAB MCP's generate_matlab_script creates it on the
    first write, so no local file-system call is needed here."""
    entity = spec.get("entity_name") or "design"
    return str(RUNS_DIR / f"{entity}_{time.strftime('%Y%m%d_%H%M%S')}")


class TestVectorAgent:
    def __init__(self, tools: TestVectorAgentTools | None = None, max_iterations: int = MAX_ITERATIONS):
        self.tools = tools or TestVectorAgentTools()
        self.max_iterations = max_iterations
        self.graph = self._build().compile()

    # ── nodes ────────────────────────────────────────────────────────────
    def decide_node(self, state: TestVectorAgentState) -> dict:
        iteration = state.get("iteration", 0)
        has_issues = bool((state.get("validation") or {}).get("issues"))
        stop_reason = None
        if iteration >= self.max_iterations:
            stop_reason = f"Iteration limit ({self.max_iterations}) reached before the test vectors were valid."
        elif has_issues and state.get("stall_count", 0) >= MAX_STALLED_FIXES:
            stop_reason = f"{MAX_STALLED_FIXES} fix attempts left the validation issues unchanged."
        if stop_reason:
            has_result = state.get("result") is not None
            return {
                "status": (TestVectorStatus.NEEDS_REVIEW if has_result else TestVectorStatus.FAILED).value,
                "last_action": None,
                "error": stop_reason,
            }
        summary = self.summary(state)
        try:
            decision = self.tools.decider.decide(summary)
        except Exception as e:  # planner LLM unreachable: use the deterministic fallback policy
            decision = self.tools.decider.parse("", summary)
            decision.reason = f"planner unavailable ({e}); {decision.reason}"
        decision = guard(decision, state)
        return {
            "iteration": iteration + 1,
            "last_action": decision.action.value,
            "decision_reason": decision.reason,
        }

    def load_skills_node(self, state: TestVectorAgentState) -> dict:
        try:
            skills = self.tools.skill_loader.load()
        except Exception as e:  # skills improve quality but their absence must not kill the run
            return {"skills": {}, "skills_loaded": True,
                    "warnings": [*state.get("warnings", []), f"skills not loaded: {e}"]}
        return {"skills": skills, "skills_loaded": True}

    def generate_node(self, state: TestVectorAgentState) -> dict:
        issues = (state.get("validation") or {}).get("issues") or []
        try:
            result = self.tools.generator.generate(
                state["spec"], state["run_dir"], state.get("skills"), state.get("result"), issues)
        except Exception as e:  # LLM/MCP/transport failure: end the run, don't crash the caller
            return {"status": TestVectorStatus.FAILED.value, "error": f"test vector generation failed: {e}"}
        return {"result": result, "validation": None, "error": None}

    def validate_node(self, state: TestVectorAgentState) -> dict:
        try:
            validation = self.tools.validator(state["spec"], state["run_dir"], state.get("result") or {})
        except Exception as e:
            return {"status": TestVectorStatus.FAILED.value, "error": f"validation failed: {e}"}
        issues = validation.get("issues") or []
        signature = "|".join(f"{i.get('code')}:{i.get('detail')}" for i in issues)
        stalled = bool(issues) and signature == state.get("validation_signature")
        return {
            "validation": validation,
            "validation_signature": signature,
            "stall_count": state.get("stall_count", 0) + 1 if stalled else 0,
        }

    def finalize_node(self, state: TestVectorAgentState) -> dict:
        return {"status": TestVectorStatus.COMPLETE.value}

    # ── graph ────────────────────────────────────────────────────────────
    @staticmethod
    def _route(state: TestVectorAgentState) -> str:
        if TestVectorStatus(state.get("status", TestVectorStatus.RUNNING.value)).terminal:
            return END
        return state.get("last_action") or END

    @staticmethod
    def _after_step(state: TestVectorAgentState) -> str:
        return END if TestVectorStatus(state["status"]).terminal else "decide"

    def _build(self) -> StateGraph:
        g = StateGraph(TestVectorAgentState)
        g.add_node("decide", self.decide_node)
        g.add_node(Action.LOAD_SKILLS.value, self.load_skills_node)
        g.add_node(Action.GENERATE_VECTORS.value, self.generate_node)
        g.add_node(Action.VALIDATE_VECTORS.value, self.validate_node)
        g.add_node(Action.FINALIZE_VECTORS.value, self.finalize_node)
        g.add_edge(START, "decide")
        g.add_conditional_edges("decide", self._route, {a.value: a.value for a in Action} | {END: END})
        g.add_edge(Action.LOAD_SKILLS.value, "decide")
        g.add_conditional_edges(Action.GENERATE_VECTORS.value, self._after_step,
                                {"decide": Action.VALIDATE_VECTORS.value, END: END})  # LLM -> CSV -> validator, always
        g.add_conditional_edges(Action.VALIDATE_VECTORS.value, self._after_step, {"decide": "decide", END: END})
        g.add_edge(Action.FINALIZE_VECTORS.value, END)
        return g

    # ── public API ───────────────────────────────────────────────────────
    def run(self, spec: dict, run_dir: str | None = None) -> dict:
        run_dir = run_dir or new_run_dir(spec)
        return self.summarize(self.graph.invoke(initial_state(spec, run_dir)))

    @staticmethod
    def summary(state: TestVectorAgentState) -> dict:
        """Compact view handed to the planner."""
        return {
            "spec": state.get("spec"),
            "skills_loaded": state.get("skills_loaded", False),
            "skill_names": list((state.get("skills") or {}).keys()),
            "has_result": state.get("result") is not None,
            "validation": state.get("validation"),
            "iteration": state.get("iteration", 0),
            "max_iterations": MAX_ITERATIONS,
        }

    @staticmethod
    def summarize(state: dict) -> dict:
        return {
            "status": state.get("status", TestVectorStatus.RUNNING.value),
            "run_dir": state.get("run_dir"),
            "result": state.get("result"),
            "validation": state.get("validation"),
            "skills_used": list((state.get("skills") or {}).keys()),
            "iteration": state.get("iteration", 0),
            "decision_reason": state.get("decision_reason"),
            "warnings": state.get("warnings", []),
            "error": state.get("error"),
        }


# ── module-level API (kept for existing callers) ───────────────────────────

_default_agent: TestVectorAgent | None = None
_default_lock = threading.Lock()


def get_agent() -> TestVectorAgent:
    global _default_agent
    with _default_lock:
        if _default_agent is None:
            _default_agent = TestVectorAgent()
        return _default_agent


def run_test_vector_generation(spec: dict, run_dir: str | None = None) -> dict:
    """Returns {"status", "run_dir", "result", "validation", "skills_used", "iteration",
    "decision_reason", "warnings", "error"}."""
    return get_agent().run(spec, run_dir)

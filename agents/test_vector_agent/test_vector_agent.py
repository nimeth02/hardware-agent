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
# `decide` asks the decider (TestVectorDecider, an LLM) for the next action, then
# applies deterministic guardrails (test_vector_guard). Every generation is
# followed by the validator; validation issues feed the next generation as
# fix instructions. No RTL is needed: the vectors come from the spec alone.

from __future__ import annotations

import threading
import time

from langgraph.graph import END, START, StateGraph

from agents.test_vector_agent.dto import agent_result_dto, decider_input_dto
from agents.test_vector_agent.state import (
    MAX_ITERATIONS, MAX_STALLED_FIXES, Action, TestVectorAgentState, TestVectorStatus, initial_state,
)
from agents.test_vector_agent.test_vector_generator import shared_matlab_session
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
        decider_input = decider_input_dto(state)
        try:
            decision = self.tools.decider.decide(decider_input)
        except Exception as e:  # decider LLM unreachable: use the deterministic fallback policy
            decision = self.tools.decider.parse("", decider_input)
            decision.reason = f"decider unavailable ({e}); {decision.reason}"
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
                state["spec"], state["run_dir"], state.get("skills"), state.get("result"), issues,
                attempt=state.get("attempt", 1), attempt_history=state.get("attempt_history"))
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
    def run(self, spec: dict, run_dir: str | None = None, requirement_id: str | None = None, attempt: int = 1,
            attempt_history: list[dict] | None = None) -> dict:
        """requirement_id / attempt / attempt_history: the orchestrator's note saying
        which run this call belongs to, whether it is the 1st, 2nd, ... call for it,
        and how the earlier calls went ({attempt, status, error, issues} each)."""
        run_dir = run_dir or new_run_dir(spec)
        # One MATLAB for the whole run: every generate_vectors attempt inside
        # the block reuses the same MATLAB MCP server instead of booting a
        # fresh MATLAB (1–2 min) per attempt. Closed when the run ends.
        with shared_matlab_session() as matlab:
            # TODO(human): when should MATLAB be switched on? Leave this empty
            # and it starts on the first generate_vectors attempt (nothing is
            # booted if the run stops before that). Or call `matlab.start()`
            # here so MATLAB boots while skills are still being loaded.
            return agent_result_dto(self.graph.invoke(
                initial_state(spec, run_dir, requirement_id, attempt, attempt_history)))


# ── module-level API (kept for existing callers) ───────────────────────────

_default_agent: TestVectorAgent | None = None
_default_lock = threading.Lock()


def get_agent() -> TestVectorAgent:
    global _default_agent
    with _default_lock:
        if _default_agent is None:
            _default_agent = TestVectorAgent()
        return _default_agent


def run_test_vector_generation(spec: dict, run_dir: str | None = None, requirement_id: str | None = None,
                               attempt: int = 1, attempt_history: list[dict] | None = None) -> dict:
    """Returns dto.agent_result_dto: {"status", "requirement_id", "attempt", "run_dir", "result",
    "validation", "skills_used", "iteration", "decision_reason", "warnings", "error"}."""
    return get_agent().run(spec, run_dir, requirement_id, attempt, attempt_history)

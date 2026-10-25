# agents/testbench_agent/testbench_agent.py — bounded planning agent that
# turns an approved spec + its RTL into a lint-clean testbench that replays
# the golden test vectors at simulation time, using the project's
# testbench-rule skills. The testbench never receives the vectors: it reads
# projects/<id>/test_vectors_in.csv (cycle + the spec's input ports) and
# test_vectors_out.csv (cycle + its output ports), whose layout is fixed by
# convention, so the spec alone tells it what to drive and what to compare.
#
#   START -> decide -+- load_skills ------------------------------+
#                    +- generate_testbench -> lint_testbench ------+-> decide ...
#                    +- lint_testbench -----------------------------+
#                    +- finalize_testbench -> END (COMPLETE)
#                    +- limit / fixes not converging -> END (NEEDS_REVIEW | FAILED)
#
# `decide` asks the decider (TestbenchDecider, an LLM) for the next action, then
# applies deterministic guardrails (testbench_guard). Every generation is
# followed by the linter (static checks + DUT/testbench compile); lint
# issues feed the next generation as fix instructions.

from __future__ import annotations

import re
import threading

from langgraph.graph import END, START, StateGraph

from agents.testbench_agent.dto import agent_result_dto, decider_input_dto
from agents.testbench_agent.state import (
    MAX_ITERATIONS, MAX_STALLED_FIXES, Action, TestbenchAgentState, TestbenchStatus, initial_state,
)
from agents.testbench_agent.testbench_guard import guard
from agents.testbench_agent.tools import TestbenchAgentTools


class TestbenchAgent:
    def __init__(self, tools: TestbenchAgentTools | None = None, max_iterations: int = MAX_ITERATIONS):
        self.tools = tools or TestbenchAgentTools()
        self.max_iterations = max_iterations
        self.graph = self._build().compile()

    # ── nodes ────────────────────────────────────────────────────────────
    def decide_node(self, state: TestbenchAgentState) -> dict:
        iteration = state.get("iteration", 0)
        has_issues = bool((state.get("lint") or {}).get("issues"))
        stop_reason = None
        if iteration >= self.max_iterations:
            stop_reason = f"Iteration limit ({self.max_iterations}) reached before the testbench was lint-clean."
        elif has_issues and state.get("stall_count", 0) >= MAX_STALLED_FIXES:
            stop_reason = f"{MAX_STALLED_FIXES} fix attempts left the lint issues unchanged."
        if stop_reason:
            has_tb = state.get("testbench") is not None
            return {
                "status": (TestbenchStatus.NEEDS_REVIEW if has_tb else TestbenchStatus.FAILED).value,
                "last_action": None,
                "error": stop_reason,
            }
        decision = guard(self.tools.decider.decide(decider_input_dto(state)), state)
        return {
            "iteration": iteration + 1,
            "last_action": decision.action.value,
            "decision_reason": decision.reason,
        }

    def load_skills_node(self, state: TestbenchAgentState) -> dict:
        try:
            skills = self.tools.skill_loader.load()
        except Exception as e:  # skills improve quality but their absence must not kill the run
            return {"skills": {}, "skills_loaded": True,
                    "warnings": [*state.get("warnings", []), f"skills not loaded: {e}"]}
        return {"skills": skills, "skills_loaded": True}

    def generate_node(self, state: TestbenchAgentState) -> dict:
        lint_issues = (state.get("lint") or {}).get("issues") or []
        try:
            testbench = self.tools.generator.generate(
                state["spec"], state["rtl"], state.get("skills"), state.get("testbench"), lint_issues,
                attempt=state.get("attempt", 1), attempt_history=state.get("attempt_history"))
        except Exception as e:  # LLM/transport failure: end the run, don't crash the caller
            return {"status": TestbenchStatus.FAILED.value, "error": f"testbench generation failed: {e}"}
        return {"testbench": testbench, "lint": None, "error": None}

    def lint_node(self, state: TestbenchAgentState) -> dict:
        try:
            lint = self.tools.linter(state["spec"], state["rtl"], state.get("testbench") or {})
        except Exception as e:
            return {"status": TestbenchStatus.FAILED.value, "error": f"lint failed: {e}"}
        issues = lint.get("issues") or []
        signature = "|".join(f"{i.get('code')}:{i.get('detail')}" for i in issues)
        stalled = bool(issues) and signature == state.get("lint_signature")
        return {
            "lint": lint,
            "lint_signature": signature,
            "stall_count": state.get("stall_count", 0) + 1 if stalled else 0,
        }

    def finalize_node(self, state: TestbenchAgentState) -> dict:
        return {"status": TestbenchStatus.COMPLETE.value}

    # ── graph ────────────────────────────────────────────────────────────
    @staticmethod
    def _route(state: TestbenchAgentState) -> str:
        if TestbenchStatus(state.get("status", TestbenchStatus.RUNNING.value)).terminal:
            return END
        return state.get("last_action") or END

    @staticmethod
    def _after_step(state: TestbenchAgentState) -> str:
        return END if TestbenchStatus(state["status"]).terminal else "decide"

    def _build(self) -> StateGraph:
        g = StateGraph(TestbenchAgentState)
        g.add_node("decide", self.decide_node)
        g.add_node(Action.LOAD_SKILLS.value, self.load_skills_node)
        g.add_node(Action.GENERATE_TESTBENCH.value, self.generate_node)
        g.add_node(Action.LINT_TESTBENCH.value, self.lint_node)
        g.add_node(Action.FINALIZE_TESTBENCH.value, self.finalize_node)
        g.add_edge(START, "decide")
        g.add_conditional_edges("decide", self._route, {a.value: a.value for a in Action} | {END: END})
        g.add_edge(Action.LOAD_SKILLS.value, "decide")
        g.add_conditional_edges(Action.GENERATE_TESTBENCH.value, self._after_step,
                                {"decide": Action.LINT_TESTBENCH.value, END: END})  # LLM -> testbench -> linter, always
        g.add_conditional_edges(Action.LINT_TESTBENCH.value, self._after_step, {"decide": "decide", END: END})
        g.add_edge(Action.FINALIZE_TESTBENCH.value, END)
        return g

    # ── public API ───────────────────────────────────────────────────────
    def run(self, spec: dict, rtl: dict, requirement_id: str | None = None, attempt: int = 1,
            attempt_history: list[dict] | None = None) -> dict:
        """requirement_id / attempt / attempt_history: the orchestrator's note saying
        which run this call belongs to, whether it is the 1st, 2nd, ... call for it,
        and how the earlier calls went ({attempt, status, error, issues, downstream?} each)."""
        start = initial_state(spec, rtl, requirement_id, attempt, attempt_history)
        problem = self.check_inputs(rtl)
        if problem:
            return agent_result_dto({**start, "status": TestbenchStatus.FAILED.value, "error": problem})
        return agent_result_dto(self.graph.invoke(start))

    @staticmethod
    def check_inputs(rtl: dict) -> str | None:
        """A testbench cannot compile against a DUT that is not real RTL —
        fail fast instead of burning iterations (e.g. the UI placeholder)."""
        code = (rtl or {}).get("code") or ""
        if not re.search(r"\bentity\s+\w+\s+is", code, re.IGNORECASE):
            return "rtl.code contains no entity declaration — provide the RTL agent's output, not a placeholder."
        return None



# ── module-level API (kept for existing callers) ───────────────────────────

_default_agent: TestbenchAgent | None = None
_default_lock = threading.Lock()


def get_agent() -> TestbenchAgent:
    global _default_agent
    with _default_lock:
        if _default_agent is None:
            _default_agent = TestbenchAgent()
        return _default_agent


def run_testbench_generation(spec: dict, rtl: dict, requirement_id: str | None = None, attempt: int = 1,
                             attempt_history: list[dict] | None = None) -> dict:
    """Returns dto.agent_result_dto: {"status", "requirement_id", "attempt", "testbench", "lint",
    "skills_used", "iteration", "decision_reason", "warnings", "error"}."""
    return get_agent().run(spec, rtl, requirement_id, attempt, attempt_history)

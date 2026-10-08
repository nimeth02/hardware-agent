# agents/rtl_agent/rtl_agent.py — bounded planning agent that turns an
# approved spec into lint-clean VHDL, using the project's design-rule skills.
#
#   START -> decide -+- load_skills ------------------------+
#                    +- generate_rtl -> lint_rtl -----------+-> decide ...
#                    +- lint_rtl ---------------------------+
#                    +- finalize_rtl -> END (COMPLETE)
#                    +- limit / fixes not converging -> END (NEEDS_REVIEW | FAILED)
#
# `decide` asks the LLM policy (RTLDecider) for the next action, then applies
# deterministic guardrails (rtl_guard): skills first, no finalize while the
# linter reports issues, etc. Every generation is followed by the linter;
# lint issues feed the next generation as fix instructions.

from __future__ import annotations

import threading

from langgraph.graph import END, START, StateGraph

from agents.rtl_agent.rtl_guard import guard
from agents.rtl_agent.state import (
    MAX_ITERATIONS, MAX_STALLED_FIXES, Action, RTLAgentState, RTLStatus, initial_state,
)
from agents.rtl_agent.tools import RTLAgentTools


class RTLAgent:
    def __init__(self, tools: RTLAgentTools | None = None, max_iterations: int = MAX_ITERATIONS):
        self.tools = tools or RTLAgentTools()
        self.max_iterations = max_iterations
        self.graph = self._build().compile()

    # ── nodes ────────────────────────────────────────────────────────────
    def decide_node(self, state: RTLAgentState) -> dict:
        iteration = state.get("iteration", 0)
        has_issues = bool((state.get("lint") or {}).get("issues"))
        stop_reason = None
        if iteration >= self.max_iterations:
            stop_reason = f"Iteration limit ({self.max_iterations}) reached before the design was lint-clean."
        elif has_issues and state.get("stall_count", 0) >= MAX_STALLED_FIXES:
            stop_reason = f"{MAX_STALLED_FIXES} fix attempts left the lint issues unchanged."
        if stop_reason:
            has_rtl = state.get("rtl") is not None
            return {
                "status": (RTLStatus.NEEDS_REVIEW if has_rtl else RTLStatus.FAILED).value,
                "last_action": None,
                "error": stop_reason,
            }
        decision = guard(self.tools.decider.decide(self.summary(state)), state)
        return {
            "iteration": iteration + 1,
            "last_action": decision.action.value,
            "decision_reason": decision.reason,
        }

    def load_skills_node(self, state: RTLAgentState) -> dict:
        try:
            skills = self.tools.skill_loader.load()
        except Exception as e:  # skills improve quality but their absence must not kill the run
            return {"skills": {}, "skills_loaded": True,
                    "warnings": [*state.get("warnings", []), f"skills not loaded: {e}"]}
        return {"skills": skills, "skills_loaded": True}

    def generate_node(self, state: RTLAgentState) -> dict:
        lint_issues = (state.get("lint") or {}).get("issues") or []
        try:
            rtl = self.tools.generator.generate(state["spec"], state.get("skills"), state.get("rtl"), lint_issues)
        except Exception as e:  # LLM/transport failure: end the run, don't crash the caller
            return {"status": RTLStatus.FAILED.value, "error": f"RTL generation failed: {e}"}
        return {"rtl": rtl, "lint": None, "error": None}

    def lint_node(self, state: RTLAgentState) -> dict:
        try:
            lint = self.tools.linter(state["spec"], state.get("rtl") or {})
        except Exception as e:
            return {"status": RTLStatus.FAILED.value, "error": f"lint failed: {e}"}
        issues = lint.get("issues") or []
        signature = "|".join(f"{i.get('code')}:{i.get('detail')}" for i in issues)
        stalled = bool(issues) and signature == state.get("lint_signature")
        return {
            "lint": lint,
            "lint_signature": signature,
            "stall_count": state.get("stall_count", 0) + 1 if stalled else 0,
        }

    def finalize_node(self, state: RTLAgentState) -> dict:
        return {"status": RTLStatus.COMPLETE.value}

    # ── graph ────────────────────────────────────────────────────────────
    @staticmethod
    def _route(state: RTLAgentState) -> str:
        if RTLStatus(state.get("status", RTLStatus.RUNNING.value)).terminal:
            return END
        return state.get("last_action") or END

    @staticmethod
    def _after_step(state: RTLAgentState) -> str:
        return END if RTLStatus(state["status"]).terminal else "decide"

    def _build(self) -> StateGraph:
        g = StateGraph(RTLAgentState)
        g.add_node("decide", self.decide_node)
        g.add_node(Action.LOAD_SKILLS.value, self.load_skills_node)
        g.add_node(Action.GENERATE_RTL.value, self.generate_node)
        g.add_node(Action.LINT_RTL.value, self.lint_node)
        g.add_node(Action.FINALIZE_RTL.value, self.finalize_node)
        g.add_edge(START, "decide")
        g.add_conditional_edges("decide", self._route, {a.value: a.value for a in Action} | {END: END})
        g.add_edge(Action.LOAD_SKILLS.value, "decide")
        g.add_conditional_edges(Action.GENERATE_RTL.value, self._after_step,
                                {"decide": Action.LINT_RTL.value, END: END})  # LLM -> design -> linter, always
        g.add_conditional_edges(Action.LINT_RTL.value, self._after_step, {"decide": "decide", END: END})
        g.add_edge(Action.FINALIZE_RTL.value, END)
        return g

    # ── public API ───────────────────────────────────────────────────────
    def run(self, spec: dict) -> dict:
        return self.summarize(self.graph.invoke(initial_state(spec)))

    @staticmethod
    def summary(state: RTLAgentState) -> dict:
        """Compact view handed to the planner."""
        return {
            "spec": state.get("spec"),
            "skills_loaded": state.get("skills_loaded", False),
            "skill_names": list((state.get("skills") or {}).keys()),
            "has_rtl": state.get("rtl") is not None,
            "lint": state.get("lint"),
            "iteration": state.get("iteration", 0),
            "max_iterations": MAX_ITERATIONS,
        }

    @staticmethod
    def summarize(state: dict) -> dict:
        return {
            "status": state.get("status", RTLStatus.RUNNING.value),
            "rtl": state.get("rtl"),
            "lint": state.get("lint"),
            "skills_used": list((state.get("skills") or {}).keys()),
            "iteration": state.get("iteration", 0),
            "decision_reason": state.get("decision_reason"),
            "warnings": state.get("warnings", []),
            "error": state.get("error"),
        }


# ── module-level API (kept for existing callers) ───────────────────────────

_default_agent: RTLAgent | None = None
_default_lock = threading.Lock()


def get_agent() -> RTLAgent:
    global _default_agent
    with _default_lock:
        if _default_agent is None:
            _default_agent = RTLAgent()
        return _default_agent


def run_rtl_generation(spec: dict) -> dict:
    """Returns {"status", "rtl", "lint", "skills_used", "iteration", "decision_reason", "warnings", "error"}."""
    return get_agent().run(spec)


def call_rtl_agent(spec: dict) -> str:
    """One-shot generation, raw LLM text, with the skills applied (used by agents/pipeline.py)."""
    tools = get_agent().tools
    try:
        skills = tools.skill_loader.load()
    except Exception:
        skills = {}
    return tools.generator.generate_raw(spec, skills)


from agents.rtl_agent.rtl_generator import parse_rtl_response  # noqa: E402,F401  re-export for agents/pipeline.py

# agents/rtl_agent/rtl_agent.py — bounded planning agent that turns an
# approved spec into lint-clean VHDL, using the project's design-rule skills.
#
#   START -> decide -+- load_skills ------------------------+
#                    +- generate_rtl -> lint_rtl -----------+-> decide ...
#                    +- lint_rtl ---------------------------+
#                    +- finalize_rtl -> END (COMPLETE)
#                    +- limit / fixes not converging -> END (NEEDS_REVIEW | FAILED)
#
# `decide` asks the decider (RTLDecider, an LLM) for the next action, then applies
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
from agents.rtl_agent.dto import agent_result_dto, decider_input_dto
from agents.rtl_agent.tools import RTLAgentTools
from agents.vhdl_version import resolve_vhdl_version, skill_matches_version


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
        decision = guard(self.tools.decider.decide(decider_input_dto(state)), state)
        return {
            "iteration": iteration + 1,
            "last_action": decision.action.value,
            "decision_reason": decision.reason,
        }

    def load_skills_node(self, state: RTLAgentState) -> dict:
        # skills/RTL holds one rulebook per VHDL edition; read only the one
        # matching the spec so the two can never contradict each other.
        version = resolve_vhdl_version(state["spec"])
        try:
            skills = self.tools.skill_loader.load(keep=lambda name: skill_matches_version(name, version))
        except Exception as e:  # skills improve quality but their absence must not kill the run
            return {"skills": {}, "skills_loaded": True,
                    "warnings": [*state.get("warnings", []), f"skills not loaded: {e}"]}
        return {"skills": skills, "skills_loaded": True}

    def generate_node(self, state: RTLAgentState) -> dict:
        lint_issues = (state.get("lint") or {}).get("issues") or []
        try:
            rtl = self.tools.generator.generate(state["spec"], state.get("skills"), state.get("rtl"), lint_issues,
                                                attempt=state.get("attempt", 1),
                                                attempt_history=state.get("attempt_history"),
                                                starting_rtl=state.get("previous_rtl"))
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
    def run(self, spec: dict, requirement_id: str | None = None, attempt: int = 1,
            attempt_history: list[dict] | None = None, previous_rtl: dict | None = None) -> dict:
        """requirement_id / attempt / attempt_history: the orchestrator's note saying
        which run this call belongs to, whether it is the 1st, 2nd, ... call for it,
        and how the earlier calls went ({attempt, status, error, issues} each).
        previous_rtl: the design the earlier call delivered; a retry revises it
        instead of starting from the spec alone. It is NOT seeded as `rtl`:
        the planner would lint and finalize it as-is instead of regenerating."""
        return agent_result_dto(self.graph.invoke(
            initial_state(spec, requirement_id, attempt, attempt_history, previous_rtl)))


# ── module-level API (kept for existing callers) ───────────────────────────

_default_agent: RTLAgent | None = None
_default_lock = threading.Lock()


def get_agent() -> RTLAgent:
    global _default_agent
    with _default_lock:
        if _default_agent is None:
            _default_agent = RTLAgent()
        return _default_agent


def run_rtl_generation(spec: dict, requirement_id: str | None = None, attempt: int = 1,
                       attempt_history: list[dict] | None = None, previous_rtl: dict | None = None) -> dict:
    """Returns {"status", "requirement_id", "attempt", "rtl", "lint", "skills_used", "iteration",
    "decision_reason", "warnings", "error"}."""
    return get_agent().run(spec, requirement_id, attempt, attempt_history, previous_rtl)


def call_rtl_agent(spec: dict) -> str:
    """One-shot generation, raw LLM text, with the skills applied (used by agents/pipeline_agent/pipeline.py)."""
    tools = get_agent().tools
    version = resolve_vhdl_version(spec)
    try:
        skills = tools.skill_loader.load(keep=lambda name: skill_matches_version(name, version))
    except Exception:
        skills = {}
    return tools.generator.generate_raw(spec, skills)


from agents.rtl_agent.rtl_generator import parse_rtl_response  # noqa: E402,F401  re-export for agents/pipeline_agent/pipeline.py

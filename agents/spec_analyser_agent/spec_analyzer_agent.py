# agents/spec_analyser_agent/spec_analyzer_agent.py — bounded planning agent
# that turns a hardware requirement into a structured spec, asking the user
# for clarification when the deterministic evaluator finds gaps.
#
#   START -> decide -+- generate_spec -> evaluate_spec -+
#                    +- evaluate_spec ------------------+-> decide ...
#                    +- ask_user  (interrupt: WAITING_FOR_USER) --+
#                    +- finalize_spec -> END (COMPLETE)
#                    +- limit reached -> END (NEEDS_REVIEW | FAILED)
#
# `decide` asks the LLM policy (SpecDecider) for the next action and then
# applies deterministic guardrails: no finalize while issues exist, a
# question is always present for ask_user, and MAX_ITERATIONS ends the run.
# Human input never blocks: ask_user calls interrupt(); resume(session_id,
# answer) continues the same checkpointed thread.

from __future__ import annotations

import threading
import uuid

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agents.spec_analyser_agent.spec_guard import guard
from agents.spec_analyser_agent.spec_suggester import generate_suggestions
from agents.spec_analyser_agent.state import MAX_ITERATIONS, Action, SpecAnalyzerState, SpecStatus, initial_state
from agents.spec_analyser_agent.tools import SpecAnalyzerTools
from services.checkpointer import checkpoints

class SpecAnalyzerAgent:
    def __init__(self, tools: SpecAnalyzerTools | None = None,
                 checkpointer: BaseCheckpointSaver | None = None,
                 max_iterations: int = MAX_ITERATIONS):
        self.tools = tools or SpecAnalyzerTools()
        self.max_iterations = max_iterations
        self._lock = threading.Lock()
        self.graph = self._build().compile(checkpointer=checkpointer or checkpoints.sqlite("spec_analyzer"))

    # ── nodes ────────────────────────────────────────────────────────────
    def decide_node(self, state: SpecAnalyzerState) -> dict:
        iteration = state.get("iteration", 0)
        if iteration >= self.max_iterations:
            has_spec = state.get("spec") is not None
            return {
                "status": (SpecStatus.NEEDS_REVIEW if has_spec else SpecStatus.FAILED).value,
                "last_action": None,
                "error": f"Iteration limit ({self.max_iterations}) reached before the spec was complete.",
            }
        decision = self.tools.decider.decide(self.summary(state))
        decision = guard(decision, state)
        state_update = {
            "iteration": iteration + 1,
            "last_action": decision.action.value,
            "decision_reason": decision.reason,
            "pending_question": None,
        }
        if decision.action is Action.ASK_USER:
            state_update["status"] = SpecStatus.WAITING_FOR_USER.value
            state_update["pending_question"] = decision.question
        return state_update

    def generate_node(self, state: SpecAnalyzerState) -> dict:
        feedback = state.get("user_feedback") or []
        try:
            spec = self.tools.generator.generate(state["original_request"], feedback, state.get("spec"))
        except Exception as e:  # LLM/transport failure: end the run, don't crash the caller
            return {"status": SpecStatus.FAILED.value, "error": f"spec generation failed: {e}"}
        return {"spec": spec, "answers_applied": len(feedback), "error": None}

    def evaluate_node(self, state: SpecAnalyzerState) -> dict:
        try:
            issues = self.tools.evaluator(state.get("spec") or {})
            suggestions = generate_suggestions(issues)
        except Exception as e:
            return {"status": SpecStatus.FAILED.value, "error": f"spec evaluation failed: {e}"}
        signature = "|".join(f"{i.get('code')}:{i.get('detail')}" for i in issues)
        stalled = bool(issues) and signature == state.get("issue_signature")
        return {
            "issues": issues,
            "suggestions": suggestions,
            "issue_signature": signature,
            "stall_count": state.get("stall_count", 0) + 1 if stalled else 0,
        }

    def ask_user_node(self, state: SpecAnalyzerState) -> dict:
        answer = interrupt({
            "type": "clarification",
            "question": state.get("pending_question"),
            "spec": state.get("spec"),
            "issues": state.get("issues", []),
            "suggestions": state.get("suggestions", []),
        })
        return {
            "user_feedback": [*(state.get("user_feedback") or []), str(answer)],
            "status": SpecStatus.RUNNING.value,
            "pending_question": None,
        }

    def finalize_node(self, state: SpecAnalyzerState) -> dict:
        return {"status": SpecStatus.COMPLETE.value, "pending_question": None}

    # ── graph ────────────────────────────────────────────────────────────
    @staticmethod
    def _route(state: SpecAnalyzerState) -> str:
        if SpecStatus(state.get("status", SpecStatus.RUNNING.value)).terminal:
            return END
        return state.get("last_action") or END

    @staticmethod
    def _after_step(state: SpecAnalyzerState) -> str:
        return END if SpecStatus(state["status"]).terminal else "decide"

    def _build(self) -> StateGraph:
        g = StateGraph(SpecAnalyzerState)
        g.add_node("decide", self.decide_node)
        g.add_node(Action.GENERATE_SPEC.value, self.generate_node)
        g.add_node(Action.EVALUATE_SPEC.value, self.evaluate_node)
        g.add_node(Action.ASK_USER.value, self.ask_user_node)
        g.add_node(Action.FINALIZE_SPEC.value, self.finalize_node)
        g.add_edge(START, "decide")
        g.add_conditional_edges("decide", self._route, {a.value: a.value for a in Action} | {END: END})
        g.add_conditional_edges(Action.GENERATE_SPEC.value, self._after_step,
                                {"decide": Action.EVALUATE_SPEC.value, END: END})  # LLM -> spec -> evaluator, always
        g.add_conditional_edges(Action.EVALUATE_SPEC.value, self._after_step, {"decide": "decide", END: END})
        g.add_edge(Action.ASK_USER.value, "decide")
        g.add_edge(Action.FINALIZE_SPEC.value, END)
        return g

    # ── public API ───────────────────────────────────────────────────────
    def start(self, request: str, session_id: str | None = None) -> dict:
        session_id = session_id or str(uuid.uuid4())
        with self._lock:
            result = self.graph.invoke(initial_state(request), config=self._config(session_id))
        return self.summarize(session_id, result)

    def resume(self, session_id: str, answer: str) -> dict:
        """Continue a WAITING_FOR_USER session with the user's answer.
        Raises ValueError for an unknown session or one that is not waiting."""
        current = self.get(session_id)
        if current is None:
            raise ValueError(f"unknown session '{session_id}'")
        if current["status"] != SpecStatus.WAITING_FOR_USER.value:
            raise ValueError(f"session '{session_id}' is {current['status']}, not WAITING_FOR_USER")
        with self._lock:
            result = self.graph.invoke(Command(resume=answer), config=self._config(session_id))
        return self.summarize(session_id, result)

    def get(self, session_id: str) -> dict | None:
        snapshot = self.graph.get_state(self._config(session_id))
        return self.summarize(session_id, snapshot.values) if snapshot and snapshot.values else None

    @staticmethod
    def _config(session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id}}

    @staticmethod
    def summary(state: SpecAnalyzerState) -> dict:
        """Compact view handed to the planner."""
        feedback = state.get("user_feedback") or []
        return {
            "original_request": state.get("original_request"),
            "user_answers": feedback,
            "spec": state.get("spec"),
            "issues": state.get("issues", []),
            "suggestions": state.get("suggestions", []),
            "answers_applied": len(feedback) <= state.get("answers_applied", 0),
            "iteration": state.get("iteration", 0),
            "max_iterations": MAX_ITERATIONS,
        }

    @staticmethod
    def summarize(session_id: str, state: dict) -> dict:
        return {
            "session_id": session_id,
            "status": state.get("status", SpecStatus.RUNNING.value),
            "pending_question": state.get("pending_question"),
            "spec": state.get("spec"),
            "issues": state.get("issues", []),
            "suggestions": state.get("suggestions", []),
            "user_feedback": state.get("user_feedback", []),
            "iteration": state.get("iteration", 0),
            "decision_reason": state.get("decision_reason"),
            "error": state.get("error"),
        }


# ── module-level API (kept for existing callers) ───────────────────────────

_default_agent: SpecAnalyzerAgent | None = None
_default_lock = threading.Lock()


def get_agent() -> SpecAnalyzerAgent:
    global _default_agent
    with _default_lock:
        if _default_agent is None:
            _default_agent = SpecAnalyzerAgent()
        return _default_agent


def build_graph():
    return get_agent().graph


def run_spec_analysis(spec_text: str) -> dict:
    """Start a new session. Runs until COMPLETE, WAITING_FOR_USER (see
    pending_question / session_id), NEEDS_REVIEW or FAILED."""
    return get_agent().start(spec_text)


def resume_spec_analysis(session_id: str, answer: str) -> dict:
    """Feed the user's answer to a WAITING_FOR_USER session and continue it."""
    return get_agent().resume(session_id, answer)


def call_spec_analyzer(spec_text: str) -> str:
    """One-shot spec generation, raw LLM text (used by agents/pipeline.py)."""
    return get_agent().tools.generator.generate_raw(spec_text)

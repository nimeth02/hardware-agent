# agents/spec_analyser_agent/state.py — state, statuses and actions of the
# Spec Analyzer agent.
#
# This file holds only data definitions (no logic) so that the agent, its
# tools, the API and the tests all share one vocabulary.

from __future__ import annotations

from enum import Enum
from typing import TypedDict


MAX_ITERATIONS = 8

AGENT_NAME = "spec_analyzer"   # observability source tag for this agent's LLM calls


class SpecStatus(str, Enum):
    """Where a session is. Set by the agent's nodes, read by the API/UI to
    decide whether to show a question, a result, or an error."""

    RUNNING = "RUNNING"                     # graph is executing (never seen by the caller)
    WAITING_FOR_USER = "WAITING_FOR_USER"   # paused at ask_user; caller must POST an answer to resume
    COMPLETE = "COMPLETE"                   # evaluator found no issues; spec is final
    NEEDS_REVIEW = "NEEDS_REVIEW"           # iteration limit hit but a spec exists — a human should look
    FAILED = "FAILED"                       # LLM/evaluator error, or limit hit with no spec at all

    @property
    def terminal(self) -> bool:
        """True when the graph must stop (used by the routing functions)."""
        return self in (SpecStatus.COMPLETE, SpecStatus.NEEDS_REVIEW, SpecStatus.FAILED)


class Action(str, Enum):
    """The only things the agent can do. Chosen by the planner LLM
    (SpecDecider), validated by the guardrails, and each value is also the
    name of the graph node that executes it."""

    GENERATE_SPEC = "generate_spec"   # LLM writes/refines the spec (SpecGenerator)
    EVALUATE_SPEC = "evaluate_spec"   # deterministic checks (evaluate_spec + generate_suggestions)
    ASK_USER = "ask_user"             # pause with a question (interrupt)
    FINALIZE_SPEC = "finalize_spec"   # mark COMPLETE — allowed only with zero issues


class SpecAnalyzerState(TypedDict, total=False):
    """Everything LangGraph checkpoints for one session (thread_id). Nodes
    return partial updates to it; SpecAnalyzerAgent.summarize() turns it
    into the API response."""

    # --- inputs ---
    original_request: str        # the user's requirement text, given once at start()
    user_feedback: list[str]     # clarification answers in order; appended by ask_user on resume

    # --- working data ---
    spec: dict | None            # latest structured spec from the LLM (None before the first generation)
    issues: list[dict]           # evaluator findings {code, detail} on `spec`; empty = clean
    suggestions: list[str]       # human-readable hints derived from `issues` (shown in the UI)
    answers_applied: int         # len(user_feedback) when `spec` was generated; if it is smaller than
                                 # len(user_feedback) the guard forces a regeneration with the new answers

    # --- stall detection (guardrail) ---
    issue_signature: str | None  # fingerprint of `issues` after the last evaluation
    stall_count: int             # regenerations in a row that left the issues unchanged;
                                 # >= 1 makes the guard turn "generate again" into ask_user

    # --- control / reporting ---
    status: str                  # a SpecStatus value (stored as str so it serialises cleanly)
    pending_question: str | None # the question for the user while WAITING_FOR_USER, else None
    iteration: int               # planner decisions taken so far; compared with MAX_ITERATIONS
    last_action: str | None      # the Action just chosen — routes decide -> that node
    decision_reason: str | None  # planner's (or guard's) one-line justification, for the UI/observability
    error: str | None            # why the run ended in FAILED / NEEDS_REVIEW


def initial_state(request: str) -> SpecAnalyzerState:
    """Fresh state for a new session (SpecAnalyzerAgent.start)."""
    return {
        "original_request": request,
        "user_feedback": [],
        "spec": None,
        "issues": [],
        "suggestions": [],
        "answers_applied": 0,
        "issue_signature": None,
        "stall_count": 0,
        "status": SpecStatus.RUNNING.value,
        "pending_question": None,
        "iteration": 0,
        "last_action": None,
        "decision_reason": None,
        "error": None,
    }

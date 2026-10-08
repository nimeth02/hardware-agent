# agents/spec_analyser_agent/spec_guard.py — deterministic guardrails applied
# to every planner decision before the graph acts on it. The planner (LLM)
# suggests; these rules, using only facts from the state, have the final say.
#
# Rules run in order; the first one that objects replaces the decision.

from __future__ import annotations

from typing import Callable

from agents.spec_analyser_agent.spec_decider import Decision
from agents.spec_analyser_agent.state import Action, SpecAnalyzerState

Rule = Callable[[Decision, SpecAnalyzerState], Decision | None]


def question_from_issues(state: SpecAnalyzerState) -> str:
    hints = state.get("suggestions") or [i.get("detail", "") for i in state.get("issues", [])]
    return "Please clarify the following so the specification can be completed:\n- " + "\n- ".join(hints)


def must_generate_first(decision: Decision, state: SpecAnalyzerState) -> Decision | None:
    """No spec yet, or the user answered since the spec was made -> generate."""
    no_spec = state.get("spec") is None
    answers_pending = len(state.get("user_feedback") or []) > state.get("answers_applied", 0)
    if not (no_spec or answers_pending):
        return None
    if decision.action is Action.GENERATE_SPEC:
        return decision
    return Decision(Action.GENERATE_SPEC, reason=f"guard: {'no spec yet' if no_spec else 'new answers to apply'}")


def no_finalize_with_issues(decision: Decision, state: SpecAnalyzerState) -> Decision | None:
    """The evaluator decides validity, not the planner."""
    if decision.action is Action.FINALIZE_SPEC and state.get("issues"):
        return Decision(Action.ASK_USER, question_from_issues(state),
                        reason="guard: cannot finalize while the evaluator reports issues")
    return None


def stalled_regeneration_asks_user(decision: Decision, state: SpecAnalyzerState) -> Decision | None:
    """Regenerating already changed nothing once: the missing information is
    not in the request, so stop burning iterations and ask."""
    if decision.action is Action.GENERATE_SPEC and state.get("issues") and state.get("stall_count", 0) >= 1:
        return Decision(Action.ASK_USER, decision.question or question_from_issues(state),
                        reason="guard: regeneration is not resolving the issues; asking the user")
    return None


def ask_needs_a_question(decision: Decision, state: SpecAnalyzerState) -> Decision | None:
    if decision.action is Action.ASK_USER and not decision.question:
        return Decision(Action.ASK_USER, question_from_issues(state), reason=decision.reason or "guard: question added")
    return None


RULES: tuple[Rule, ...] = (
    must_generate_first,
    no_finalize_with_issues,
    stalled_regeneration_asks_user,
    ask_needs_a_question,
)


def guard(decision: Decision, state: SpecAnalyzerState) -> Decision:
    for rule in RULES:
        corrected = rule(decision, state)
        if corrected is not None:
            return corrected
    return decision

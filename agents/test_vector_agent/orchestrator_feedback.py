# agents/test_vector_agent/orchestrator_feedback.py — how the test vector
# agent uses the feedback the orchestrator sends about its earlier calls.
#
# The orchestrator calls run_test_vector_generation(spec, run_dir,
# requirement_id, attempt, attempt_history). Attempt 1 is a fresh call with
# no feedback. On attempt 2, 3, ... the attempt_history is the feedback: how
# each earlier call went (status, error, validation issues).
#
# This module decides whether there is feedback to use and, if so, what to
# add to the generator's prompt so the LLM does not repeat the earlier
# mistakes. It is separate from the validator fix loop inside one call
# (test_vector_generator's "fixing" path): that reacts to this run's own
# validator, this reacts to what the orchestrator learned after the previous
# call ended.

from __future__ import annotations

from prompts.test_vector_agent_prompt import TEST_VECTOR_RETRY_ADDENDUM


def has_feedback(attempt: int, attempt_history: list[dict] | None) -> bool:
    """True when the orchestrator has called the test vector agent before for
    this requirement and sent back how it went. Attempt 1, or a later attempt
    with an empty history, means no feedback."""
    return attempt > 1 and bool(attempt_history)


def feedback_rules(attempt: int) -> str:
    """The extra rules text for the system prompt when there is feedback:
    which attempt this is and how to read the attempt_history."""
    return TEST_VECTOR_RETRY_ADDENDUM.replace("{ATTEMPT}", str(attempt))


def feedback_inputs(attempt_history: list[dict]) -> dict:
    """What is added to the user message when there is feedback, next to the spec."""
    return {"attempt_history": attempt_history}

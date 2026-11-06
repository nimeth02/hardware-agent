# agents/testbench_agent/orchestrator_feedback.py — how the testbench agent
# uses the feedback the orchestrator sends about its earlier calls.
#
# The orchestrator calls run_testbench_generation(spec, rtl, requirement_id,
# attempt, attempt_history, previous_testbench). Attempt 1 is a fresh call
# with no feedback. On attempt 2, 3, ... the attempt_history is the feedback:
# how each earlier call went (status, error, lint issues) and any
# "downstream" note, i.e. a simulation that failed and blamed the testbench;
# previous_testbench is the testbench that call delivered, to be revised.
#
# This module decides whether there is feedback to use and, if so, what to
# add to the generator's prompt so the LLM does not repeat the earlier
# mistakes. It is separate from the linter fix loop inside one call
# (testbench_generator's "fixing" path): that reacts to this run's own
# linter, this reacts to what the orchestrator learned after the previous
# call ended.

from __future__ import annotations

from prompts.testbench_agent_prompt import TESTBENCH_RETRY_ADDENDUM, TESTBENCH_REVISE_ADDENDUM


def has_feedback(attempt: int, attempt_history: list[dict] | None) -> bool:
    """True when the orchestrator has called the testbench agent before for
    this requirement and sent back how it went. Attempt 1, or a later
    attempt with an empty history, means no feedback."""
    return attempt > 1 and bool(attempt_history)


def feedback_rules(attempt: int) -> str:
    """The extra rules text for the system prompt when there is feedback:
    which attempt this is and how to read the attempt_history."""
    return TESTBENCH_RETRY_ADDENDUM.replace("{ATTEMPT}", str(attempt))


def revise_rules() -> str:
    """The extra rules text when the previous call's testbench is handed over too:
    revise that testbench in place instead of writing a new one."""
    return TESTBENCH_REVISE_ADDENDUM


def feedback_inputs(attempt_history: list[dict], starting_testbench: dict | None = None) -> dict:
    """What is added to the user message when there is feedback, next to the spec
    and RTL: the history, and the previous call's code when the retry should revise it."""
    inputs = {"attempt_history": attempt_history}
    if starting_testbench and starting_testbench.get("code"):
        inputs["previous_code"] = starting_testbench["code"]
    return inputs

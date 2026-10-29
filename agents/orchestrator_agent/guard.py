# agents/orchestrator_agent/guard.py — the safety rule that stops the manager
# from running the same helper again and again.
#
# A helper that makes an artifact (spec, RTL, testbench, test vectors) may be
# called 1 + MAX_RETRIES_PER_STAGE times (3) per run. A call beyond that does
# not run the helper: the person watching the run is shown each of those
# attempts with the reason it failed, and asked what to do. Their answer is
# kept on the stage's latest attempt, the stage gets another round of retries,
# and the manager reads the answer as a RETRY_LIMIT result. Without a person to
# ask, the manager gets RETRY_LIMIT with no answer and must stop.
#
# Only the rule lives here. Running helpers and recording their answers stays
# in steps.py, which calls is_over_limit() before each run.

from __future__ import annotations

import json

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agents.orchestrator_agent import human_input
from agents.orchestrator_agent.blame import SECOND_OPINION
from agents.orchestrator_agent.state import MAX_RETRIES_PER_STAGE, Stage
from agents.orchestrator_agent.tracing import log_answer

# the stages whose re-runs are counted against the limit (write/simulate/implement
# are re-run whenever an input changed, so they are bounded by these instead)
RETRY_LIMITED = (Stage.ANALYZE_SPEC, Stage.GENERATE_RTL, Stage.GENERATE_TESTBENCH, Stage.GENERATE_TEST_VECTORS)

REASON_MAX_CHARS = 300    # one attempt's reason is cut to this length in the question


def retry_limit_of(state, stage: Stage) -> int:
    """How many calls `stage` may get in this run: 1 + MAX_RETRIES_PER_STAGE,
    plus any extra round the person granted after an earlier limit."""
    return state["stages"].get(stage.value, {}).get("retry_limit", 1 + MAX_RETRIES_PER_STAGE)


def is_over_limit(state, stage: Stage) -> bool:
    """True when the next call to `stage` would go past its allowed number of calls."""
    entry = state["stages"].get(stage.value, {})
    return stage in RETRY_LIMITED and entry.get("attempts", 0) + 1 > retry_limit_of(state, stage)


def reason_of(attempt: dict) -> str:
    """Why one attempt did not succeed, in one line: the helper's error text, else
    its first remaining issue, else the cause the analysis blamed."""
    reason = attempt.get("error")
    if not reason:
        issues = attempt.get("issues") or []
        first = issues[0] if issues else None
        reason = first.get("detail") if isinstance(first, dict) else first
    reason = str(reason or attempt.get("likely_cause") or "no reason recorded")
    reason = " ".join(reason.split())
    return reason if len(reason) <= REASON_MAX_CHARS else reason[:REASON_MAX_CHARS - 1] + "…"


def attempt_reasons(entry: dict) -> list[str]:
    """One line per earlier attempt, oldest first: 'Attempt 2 (NEEDS_REVIEW): <reason>'."""
    return [f"Attempt {a.get('attempt')} ({a.get('status')}): {reason_of(a)}"
            for a in entry.get("attempt_history", [])]


def retry_limit_question(stage: Stage, reasons: list[str]) -> tuple[str, list[str]]:
    """What the person is asked when `stage` has used up its retries.

    `reasons` has one line per earlier attempt (see attempt_reasons). Returns
    (question, suggested answers). The manager AI reads the person's answer
    word for word and acts on it, so the suggestions should say plainly which
    helper to run next or that the run should stop."""
    listed = "\n".join(f"  - {line}" for line in reasons) or "  - (no attempt recorded)"
    question = (f"{stage.value} has been run {len(reasons)} times and still did not succeed:\n{listed}\n"
                f"What should happen next?")
    suggestions = [f"Run {stage.value} once more",
                   *(f"Re-run {other.value} instead; the fault may be there" for other in SECOND_OPINION.get(stage, ())),
                   "Stop the run so I can review it (NEEDS_REVIEW)"]
    return question, suggestions


def retry_limit_reached(runtime: ToolRuntime, stage: Stage) -> Command:
    """The step taken instead of running the helper once its retries are used up."""
    state = runtime.state
    entry = state["stages"].get(stage.value, {})
    reasons = attempt_reasons(entry)
    question, suggestions = retry_limit_question(stage, reasons)
    run_id = human_input.current_run_id()
    answer = human_input.ask(run_id, question, suggestions) if run_id else None
    result = {"status": "RETRY_LIMIT", "stage": stage.value, "attempts": entry.get("attempts", 0),
              "retry_limit": retry_limit_of(state, stage), "attempt_reasons": reasons,
              "question": question, "human_answer": answer}
    update = {"messages": [ToolMessage(content=json.dumps(result, default=str), name=stage.value,
                                       tool_call_id=runtime.tool_call_id)]}
    if answer:
        history = list(entry.get("attempt_history", []))
        if history:
            history[-1] = {**history[-1], "human_note": answer}
        update["stages"] = {**state["stages"], stage.value: {
            **entry, "attempt_history": history,
            "retry_limit": retry_limit_of(state, stage) + MAX_RETRIES_PER_STAGE}}
    log_answer(stage, result)
    return Command(update=update)

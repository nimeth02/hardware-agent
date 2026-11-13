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
#
# Two more safety rules live here, each one function that tools.py asks before
# a helper runs and that answers BLOCKED with the reason instead of running it:
#   rtl_not_finished     no simulation while the RTL helper ended unfinished
#   blamed_helper_first  no re-making of another artifact while the helper the
#                        last failure blames still has a call left

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


# the helpers a failed simulation / implementation can send the manager back to
BLAME_FIRST = (Stage.GENERATE_RTL, Stage.GENERATE_TESTBENCH, Stage.GENERATE_TEST_VECTORS)


def latest_attempt(state, stage: Stage) -> dict:
    """`stage`'s newest attempt record in this run; {} when it never ran."""
    history = state["stages"].get(stage.value, {}).get("attempt_history", [])
    return history[-1] if history else {}


def open_blame(state, stage: Stage) -> dict | None:
    """The newest blame note on `stage` that no re-run has answered yet, or None.

    blame.apply_blame writes a failed simulation onto the blamed helper's LATEST
    attempt; a re-run adds a new attempt without notes. So a note on the latest
    attempt means: blamed, and not redone since. Second-opinion notes ("the
    fault may be on your side") are not counted: they blame nobody."""
    notes = [n for n in latest_attempt(state, stage).get("downstream", []) if not n.get("second_opinion")]
    return notes[-1] if notes else None


def was_told_to_recheck(state, stage: Stage) -> bool:
    """True when `stage` itself has a reason to run now: a blame or a second-opinion
    note on its latest attempt, or the person's instruction after a retry limit."""
    latest = latest_attempt(state, stage)
    return bool(latest.get("downstream") or latest.get("human_note"))


def blamed_helper_first(runtime: ToolRuntime, stage: Stage) -> Command | None:
    """The step taken instead of `stage` (a generate_* helper) while the last
    failure blames a DIFFERENT helper that has not been re-run and still has a
    call left; None when `stage` may run.

    Run d4fa0f77 (2026-11-11 13:01, timing_recovery): all four simulations
    blamed the RTL, and the RTL was the only wrong part (the same test vectors
    and testbench pass with a corrected RTL). After the second failure the
    manager AI re-made the test vectors three times and the testbench once;
    the RTL helper's third call was never used and the run ended on its step
    budget. Not blocked: the blamed helper itself; a helper that was itself
    told to re-check (second opinion, or the person's answer); and anything
    once the blamed helper has used up its calls or the person has answered
    for it. Nothing is written to the state: the manager only reads why."""
    state = runtime.state
    if was_told_to_recheck(state, stage):
        return None
    for blamed in BLAME_FIRST:
        note = open_blame(state, blamed)
        if blamed is stage or not note or is_over_limit(state, blamed) or latest_attempt(state, blamed).get("human_note"):
            continue
        entry = state["stages"].get(blamed.value, {})
        result = {"status": "BLOCKED", "stage": stage.value, "blamed": blamed.value,
                  "blamed_calls_used": entry.get("attempts", 0), "blamed_calls_allowed": retry_limit_of(state, blamed),
                  "failure": note.get("summary"),
                  "reason": f"{stage.value} was not run: the last {note.get('stage') or 'check'} failure blames "
                            f"{blamed.value}, which has not been re-run since and still has a call left. "
                            f"Call {blamed.value} now (it is handed that failure), then write_project and "
                            f"simulate. Other helpers may be re-run once {blamed.value} has used its calls."}
        log_answer(stage, result)
        return Command(update={"messages": [ToolMessage(content=json.dumps(result, default=str), name=stage.value,
                                                        tool_call_id=runtime.tool_call_id)]})
    return None


def open_rtl_findings(state) -> list[str]:
    """The names of the findings the RTL helper's latest attempt left open."""
    history = state["stages"].get(Stage.GENERATE_RTL.value, {}).get("attempt_history", [])
    issues = (history[-1].get("issues") if history else None) or []
    return [i.get("code") if isinstance(i, dict) else str(i) for i in issues]


def rtl_not_finished(runtime: ToolRuntime, stage: Stage) -> Command | None:
    """The step taken instead of `stage` (simulate) while the RTL helper's latest
    result is not COMPLETE; None when the RTL is finished or was never run.

    Run be4b20ac (2026-11-11): the RTL helper stopped with NEEDS_REVIEW and
    smoke_output_stuck_at_zero open, then with 12 width findings open; both
    times the manager AI simulated anyway (output 0 on every beat, then a
    crash). 19 of the last 73 simulations ran on unfinished RTL and none
    passed. Nothing is written to the state: the manager only reads why."""
    entry = runtime.state["stages"].get(Stage.GENERATE_RTL.value, {})
    status = entry.get("status")
    if status in (None, "COMPLETE"):
        return None
    result = {"status": "BLOCKED", "stage": stage.value, "rtl_status": status, "rtl_error": entry.get("error"),
              "open_rtl_findings": open_rtl_findings(runtime.state),
              "reason": f"{stage.value} was not run: the RTL is not finished (generate_rtl ended {status}). "
                        f"Simulating it cannot pass. Call generate_rtl again; do not call generate_testbench, "
                        f"write_project or {stage.value} until generate_rtl is COMPLETE."}
    log_answer(stage, result)
    return Command(update={"messages": [ToolMessage(content=json.dumps(result, default=str), name=stage.value,
                                                    tool_call_id=runtime.tool_call_id)]})

# agents/orchestrator_agent/blame.py — when a simulation or implementation
# fails, write that failure onto the stage that caused it.
#
# How it works:
#   1. The simulation / implementation analysis has a "likely_cause":
#      "rtl", "testbench" or "spec" (or "environment" / "constraints" / None,
#      which blame nobody).
#   2. We look up which stage made that artifact (BLAMED_STAGE in state.py).
#   3. We add a "downstream" note to that stage's latest attempt in
#      attempt_history: which stage failed, its verdict, failures, summary
#      and suggested fix.
#   4. The next time the orchestrator calls that stage, steps.run_stage hands
#      the attempt_history over, so the agent sees why its last result was
#      rejected even though its own checks had passed.
#   5. Second opinion: the analysis is an LLM's guess, and it can guess the
#      same wrong stage every time. Once a stage has already been blamed
#      MAX_RETRIES_PER_STAGE times (so it was redone that often and the
#      failure stayed), the stages that could equally be at fault get a
#      "second opinion" note too, and the orchestrator is told which tools
#      to re-run instead. This is a fixed rule, not something the LLM decides.

from __future__ import annotations

from agents.orchestrator_agent.state import BLAMED_STAGE, MAX_RETRIES_PER_STAGE, Stage

# blamed stage -> the stages that get a second-opinion note once that blame keeps repeating
SECOND_OPINION = {
    Stage.GENERATE_RTL: (Stage.GENERATE_TESTBENCH, Stage.GENERATE_TEST_VECTORS),
    Stage.GENERATE_TESTBENCH: (Stage.GENERATE_RTL, Stage.GENERATE_TEST_VECTORS),
    Stage.GENERATE_TEST_VECTORS: (Stage.GENERATE_TESTBENCH, Stage.GENERATE_RTL),
}

# what each stage should re-check when it receives a second-opinion note
SECOND_OPINION_CHECK = {
    Stage.GENERATE_TESTBENCH: "that outputs are compared in the order the design presents them (when its valid "
                              "signal is set), not at a fixed cycle; that every input, including ready/handshake "
                              "signals, is driven from test_vectors_in.csv; and that the compare cycle matches the "
                              "design's real latency",
    Stage.GENERATE_TEST_VECTORS: "that the expected values, their order and the latency/handshake assumptions "
                                 "follow the spec exactly, and that no row expects an output before the design "
                                 "can produce it",
    Stage.GENERATE_RTL: "that output timing, the valid/ready handshake and signed/unsigned arithmetic follow the "
                        "spec exactly",
}


def blamed_stage(analysis: dict | None) -> Stage | None:
    """Which stage does this analysis blame?

    Reads analysis["likely_cause"] and returns the matching Stage
    (rtl -> GENERATE_RTL, testbench -> GENERATE_TESTBENCH, spec -> ANALYZE_SPEC).
    Returns None when nobody is blamed (no analysis, no cause, "environment",
    "constraints")."""
    return BLAMED_STAGE.get((analysis or {}).get("likely_cause"))


def downstream_note(by: Stage, analysis: dict) -> dict:
    """The note written onto the blamed attempt.

    `by` is the stage that failed (simulate or implement). The note keeps the
    parts of its analysis the blamed agent needs: verdict, failures (the exact
    mismatches), summary and suggested_fix."""
    return {"stage": by.value, "verdict": analysis.get("verdict"), "failures": analysis.get("failures"),
            "summary": analysis.get("summary"), "suggested_fix": analysis.get("suggested_fix")}


def times_blamed(stages: dict, stage: Stage) -> int:
    """How many downstream failures have already been written onto `stage` in this run."""
    history = stages.get(stage.value, {}).get("attempt_history", [])
    return sum(len(attempt.get("downstream", [])) for attempt in history)


def second_opinion_note(by: Stage, blamed: Stage, times: int, other: Stage, analysis: dict) -> dict:
    """The note written onto `other` when `blamed` keeps being blamed without effect."""
    return {**downstream_note(by, analysis), "second_opinion": True,
            "summary": f"SECOND OPINION: {blamed.value} has been blamed {times} times and redone, and {by.value} "
                       f"still fails the same way. The fault may be on your side instead. Check "
                       f"{SECOND_OPINION_CHECK.get(other, 'your result against the spec')}. "
                       f"The analyst said: {analysis.get('summary')}"}


def second_opinion(stages: dict, analysis: dict | None) -> str | None:
    """What the orchestrator is told when the blame keeps repeating, or None.

    Computed before apply_blame on the same `stages`: a text naming the tools to
    re-run instead of the blamed one, once the blamed stage has already been
    blamed MAX_RETRIES_PER_STAGE times."""
    blamed = blamed_stage(analysis)
    if not blamed or blamed not in SECOND_OPINION or times_blamed(stages, blamed) < MAX_RETRIES_PER_STAGE:
        return None
    others = [s.value for s in SECOND_OPINION[blamed] if stages.get(s.value, {}).get("attempt_history")]
    if not others:
        return None
    return (f"{blamed.value} has already been blamed and redone {times_blamed(stages, blamed)} times with the same "
            f"result, so the fault may be elsewhere. Re-run {' and '.join(others)} (they have been told what to "
            f"check) and simulate again before calling {blamed.value} again.")


def add_note(stages: dict, stage: Stage, note: dict) -> dict:
    """Append `note` under "downstream" of `stage`'s latest attempt; unchanged if it never ran."""
    entry = stages.get(stage.value)
    if not entry or not entry.get("attempt_history"):
        return stages
    history = list(entry["attempt_history"])
    latest = history[-1]
    history[-1] = {**latest, "downstream": [*latest.get("downstream", []), note]}
    return {**stages, stage.value: {**entry, "attempt_history": history}}


def apply_blame(stages: dict, by: Stage, analysis: dict | None) -> dict:
    """Add the blame to `stages` and return the updated copy.

    `stages` is the orchestrator's per-stage records, `by` is the stage that
    just reported `analysis`. If the analysis blames a stage that has already
    run, that stage's latest attempt_history entry gets a downstream_note
    appended under "downstream". If that stage had already been blamed
    MAX_RETRIES_PER_STAGE times, the stages in SECOND_OPINION get a
    second-opinion note too. Otherwise `stages` is returned unchanged.
    The input dict is never modified in place."""
    blamed = blamed_stage(analysis)
    if not blamed:
        return stages
    times = times_blamed(stages, blamed)
    updated = add_note(stages, blamed, downstream_note(by, analysis))
    if updated is stages or times < MAX_RETRIES_PER_STAGE:
        return updated
    for other in SECOND_OPINION.get(blamed, ()):
        updated = add_note(updated, other, second_opinion_note(by, blamed, times + 1, other, analysis))
    return updated

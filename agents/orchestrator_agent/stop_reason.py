# agents/orchestrator_agent/stop_reason.py — says, in plain words, why a run
# ended. Only the wording lives here; orchestrator_agent.py puts the result in
# the final report as "stop_reason".
#
# Order of trust: a crash or the step limit is explained by the code (the
# manager AI never got to answer); otherwise the manager's own "stop_reason"
# is used; if it gave none (or an unknown status), the reason is worked out
# from how the helpers went.

from __future__ import annotations

from agents.orchestrator_agent.guard import reason_of
from agents.orchestrator_agent.state import MAX_ITERATIONS

FINAL_STATUSES = ("COMPLETE", "NEEDS_REVIEW", "FAILED")


def _last_unfinished(stages: dict) -> str | None:
    """'<stage> attempt N ended <status>: <reason>' for the latest helper that did not end COMPLETE."""
    for name, entry in reversed(list(stages.items())):
        history = entry.get("attempt_history") or []
        if history and entry.get("status") != "COMPLETE":
            last = history[-1]
            return f"{name} attempt {last.get('attempt')} ended {last.get('status')}: {reason_of(last)}"
    return None


def stop_reason(final: dict, state: dict, error: str | None) -> str:
    """One or two plain sentences: why the run stopped."""
    if error and "Recursion limit" in error:
        return f"The manager AI used up all {MAX_ITERATIONS} steps allowed for one run before it finished."
    if error:
        return f"The run crashed before it finished: {error.removeprefix('orchestration failed: ')}"
    status = final.get("status")
    if status not in FINAL_STATUSES:
        return f"The manager AI ended with an unknown status ({status!r}), so the run is counted as gave up."
    given = (final.get("stop_reason") or "").strip()
    if given:
        return given
    if status == "COMPLETE":
        return "Simulation passed and the implementation check passed."
    return _last_unfinished(state.get("stages") or {}) or "The manager AI stopped but did not say why."

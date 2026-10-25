# agents/orchestrator_agent/human_input.py — lets a running orchestrator wait
# for a person's answer. The run (a background thread) calls ask(), which
# blocks; the web server (another thread) shows get_pending() to the user and
# hands the reply over with submit_answer(), which wakes the run up.
# Pending questions live in memory only: if the server restarts, the run is
# gone too.

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from observability.agent_run import EventType, emit_event, _current_recorder

POLL_SECONDS = 0.5


@dataclass
class Pending:
    question: str
    suggestions: list[str]
    answered: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None


_pending: dict[str, Pending] = {}
_lock = threading.Lock()


def current_run_id() -> str | None:
    """The id of the run this thread is part of, or None outside a recorded run."""
    recorder = _current_recorder.get()
    return recorder.run_id if recorder is not None else None


def ask(run_id: str, question: str, suggestions: list[str] | None = None) -> str | None:
    """Show `question` to the user and wait for the answer. Returns the answer,
    or None if the run was stopped while waiting."""
    pending = Pending(question, list(suggestions or []))
    with _lock:
        _pending[run_id] = pending
    emit_event(EventType.QUESTION, "waiting", {"question": question, "suggestions": pending.suggestions})
    recorder = _current_recorder.get()
    try:
        while not pending.answered.wait(POLL_SECONDS):
            if recorder is not None and recorder.stop.is_set():
                return None
    finally:
        with _lock:
            _pending.pop(run_id, None)
    emit_event(EventType.ANSWER, "ok", {"answer": pending.answer})
    return pending.answer


def get_pending(run_id: str) -> dict | None:
    """{"question", "suggestions"} while the run is waiting for the user, else None."""
    with _lock:
        pending = _pending.get(run_id)
    return None if pending is None else {"question": pending.question, "suggestions": pending.suggestions}


def submit_answer(run_id: str, answer: str) -> bool:
    """Give the waiting run its answer. False if that run is not waiting."""
    with _lock:
        pending = _pending.get(run_id)
    if pending is None:
        return False
    pending.answer = answer
    pending.answered.set()
    return True

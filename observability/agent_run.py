# observability/agent_run.py — record one agent run end to end: graph steps,
# every LLM call (input/"thinking"/output/tokens), every tool call, timing
# and errors — all captured from outside the agent.
#
# Like step_tracer, the recorder is injected through LangChain's
# configure-hook context var, so wrapping a call in `observe_run(...)` is
# the only integration point; agents need no changes.

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from typing import Any, Iterator
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.tracers.context import register_configure_hook
from langgraph.errors import GraphInterrupt

from observability.run_store import AgentRunStore, store as default_store


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    RUN_RESUMED = "run_resumed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_STOPPED = "run_stopped"        # the run ended because the user asked it to
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    AGENT_STEP = "agent_step"
    ERROR = "error"
    STOP_REQUESTED = "stop_requested"  # user asked a running orchestrator to stop
    AGENT_INPUT = "agent_input"        # orchestrator -> agent: exactly what it handed over
    AGENT_OUTPUT = "agent_output"      # agent -> orchestrator: exactly what came back
    STATE = "state"                    # orchestrator's state (notebook) before and after a step
    QUESTION = "question"              # a running orchestrator is waiting for the user's answer
    ANSWER = "answer"                  # the user's answer reached the waiting orchestrator


class RunFinished(Exception):
    """A tool raises a subclass of this to end an agent loop on purpose
    (the work is done). The recorder logs it as a finished result, not an
    error."""


class RunStopped(BaseException):
    """Raised by the recorder at the next step boundary (LLM call, tool call
    or graph node) once the user asked the run to stop. A BaseException on
    purpose: the agents' own `except Exception` fall-backs (which turn any
    error into a FAILED result and carry on) must not swallow it, so the
    whole run — orchestrator and nested agents — unwinds within one step."""


STOP_MESSAGE = "stopped by user"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


# Runs currently inside an observe_run block in this process, by run id, so a
# web request (another thread) can flip their stop flag.
_live_runs: dict[str, "AgentRunRecorder"] = {}
_live_lock = threading.Lock()


def request_stop(run_id: str) -> bool:
    """Ask a live run to stop at its next step boundary. Python threads
    cannot be killed, so this is cooperative: the recorder raises RunStopped
    from the next LLM/tool/node start it sees; a tool already in flight
    (e.g. a simulator run inside an MCP server) finishes first. Returns
    False when no run with this id is live in this process."""
    with _live_lock:
        recorder = _live_runs.get(run_id)
    if recorder is None:
        return False
    recorder.stop.set()
    recorder.store.add_event(run_id, EventType.STOP_REQUESTED, recorder.step, None, "requested", {"by": "user"})
    return True


def is_live(run_id: str) -> bool:
    with _live_lock:
        return run_id in _live_runs


def _message_to_dict(msg: BaseMessage) -> dict:
    d: dict[str, Any] = {"role": msg.type, "content": msg.content}
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        d["tool_calls"] = tool_calls
    return d


def _prompt_version(messages: list[BaseMessage]) -> str | None:
    # Short hash of the system prompt: lets runs be compared across prompt edits.
    for m in messages:
        if m.type == "system":
            return hashlib.sha1(str(m.content).encode("utf-8")).hexdigest()[:8]
    return None


# Full payloads (tool inputs/outputs) are kept up to this size so the UI can
# show the complete xsim/xvhdl log; the short *_preview fields stay small.
FULL_PAYLOAD_LIMIT = 200_000


def _preview(value: Any, limit: int = FULL_PAYLOAD_LIMIT) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... [truncated {len(value) - limit} chars]"
    return value


class AgentRunRecorder(BaseCallbackHandler):
    """Callback handler that writes one run's steps, LLM calls and tool calls."""

    def __init__(self, run_id: str, store: AgentRunStore):
        self.run_id = run_id
        self.store = store
        self.step = 0
        self.stop = threading.Event()  # set by request_stop(); checked at every step boundary
        self._open: dict[str, dict] = {}

    def _next_step(self) -> int:
        self.step += 1
        return self.step

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise RunStopped(STOP_MESSAGE)

    # ── graph nodes → AGENT_STEP ─────────────────────────────────────────
    def on_chain_start(self, serialized: dict[str, Any], inputs: dict[str, Any], *, run_id: UUID,
                       parent_run_id: UUID | None = None, metadata: dict[str, Any] | None = None,
                       **kwargs: Any) -> None:
        if not metadata or "langgraph_node" not in metadata:
            return
        self._check_stop()
        parent = self._open.get(str(parent_run_id)) if parent_run_id else None
        if parent and parent.get("kind") == "step" and parent["name"] == metadata["langgraph_node"]:
            return  # LangGraph wraps a node's callable in a second run with the same metadata
        self._open[str(run_id)] = {"kind": "step", "name": metadata["langgraph_node"],
                                   "step": self._next_step(), "t0": time.perf_counter()}

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        o = self._open.pop(str(run_id), None)
        if o:
            self.store.add_event(self.run_id, EventType.AGENT_STEP, o["step"], self._ms(o), "ok", {"node": o["name"]})

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        o = self._open.pop(str(run_id), None)
        if not o:
            return
        if isinstance(error, GraphInterrupt):
            self.store.add_event(self.run_id, EventType.AGENT_STEP, o["step"], self._ms(o), "interrupted", {"node": o["name"]})
        elif isinstance(error, RunStopped):
            self.store.add_event(self.run_id, EventType.AGENT_STEP, o["step"], self._ms(o), "stopped", {"node": o["name"]})
        else:
            self.store.add_event(self.run_id, EventType.ERROR, o["step"], self._ms(o), "error",
                                 {"node": o["name"], "error": str(error)})

    # ── LLM calls ────────────────────────────────────────────────────────
    def on_chat_model_start(self, serialized: dict[str, Any], messages: list[list[BaseMessage]], *,
                            run_id: UUID, **kwargs: Any) -> None:
        self._check_stop()
        kw = (serialized or {}).get("kwargs") or {}
        msgs = messages[0] if messages else []
        self._open[str(run_id)] = {"kind": "llm", "step": self._next_step(), "t0": time.perf_counter(),
                                   "model": kw.get("model") or kw.get("model_name"),
                                   "input": [_message_to_dict(m) for m in msgs],
                                   "prompt_version": _prompt_version(msgs)}

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        o = self._open.pop(str(run_id), None)
        if not o:
            return
        gen = response.generations[0][0]
        message = getattr(gen, "message", None)
        output = _message_to_dict(message) if message is not None else {"content": gen.text}
        usage = (response.llm_output or {}).get("token_usage") or {}
        ms = self._ms(o)
        self.store.add_llm_call(self.run_id, o["step"], o["model"], o["prompt_version"], o["input"], output,
                                usage.get("prompt_tokens"), usage.get("completion_tokens"), ms, "ok", None)
        # Token counts go on the event too, so the live timeline can show the
        # context size of every call without waiting for the full llm_calls record.
        self.store.add_event(self.run_id, EventType.LLM_CALL, o["step"], ms, "ok",
                             {"model": o["model"], "tool_calls": [t.get("name") for t in output.get("tool_calls", [])],
                              "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                              "messages": len(o["input"]),
                              "output_preview": _preview(str(output.get("content", "")), 200)})

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        o = self._open.pop(str(run_id), None)
        if not o:
            return
        ms = self._ms(o)
        self.store.add_llm_call(self.run_id, o["step"], o["model"], o["prompt_version"], o["input"], None,
                                None, None, ms, "error", str(error))
        self.store.add_event(self.run_id, EventType.ERROR, o["step"], ms, "error", {"llm_error": str(error)})

    # ── tool calls ───────────────────────────────────────────────────────
    def on_tool_start(self, serialized: dict[str, Any], input_str: str, *, run_id: UUID,
                      inputs: dict[str, Any] | None = None, **kwargs: Any) -> None:
        self._check_stop()
        name = (serialized or {}).get("name") or "tool"
        o = {"kind": "tool", "name": name, "step": self._next_step(), "t0": time.perf_counter(),
             "input": inputs if inputs is not None else input_str}
        self._open[str(run_id)] = o
        self.store.add_event(self.run_id, EventType.TOOL_CALL, o["step"], None, "started",
                             {"tool": name, "input": _preview(o["input"])})

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        o = self._open.pop(str(run_id), None)
        if not o:
            return
        content = getattr(output, "content", output)
        ms = self._ms(o)
        self.store.add_tool_call(self.run_id, o["step"], o["name"], o["input"], _preview(content), ms, "ok", None)
        self.store.add_event(self.run_id, EventType.TOOL_RESULT, o["step"], ms, "ok",
                             {"tool": o["name"], "output_preview": _preview(str(content), 300)})

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        o = self._open.pop(str(run_id), None)
        if not o:
            return
        ms = self._ms(o)
        if isinstance(error, RunFinished):
            self.store.add_tool_call(self.run_id, o["step"], o["name"], o["input"], str(error), ms, "finished", None)
            self.store.add_event(self.run_id, EventType.TOOL_RESULT, o["step"], ms, "finished",
                                 {"tool": o["name"], "output_preview": str(error)})
            return
        if isinstance(error, RunStopped):
            self._close_stopped(o)
            return
        self.store.add_tool_call(self.run_id, o["step"], o["name"], o["input"], None, ms, "error", str(error))
        self.store.add_event(self.run_id, EventType.ERROR, o["step"], ms, "error", {"tool": o["name"], "error": str(error)})

    def _close_stopped(self, o: dict) -> None:
        """Record a tool call / LLM call / node that was open when the run stopped."""
        ms = self._ms(o)
        if o["kind"] == "tool":
            self.store.add_tool_call(self.run_id, o["step"], o["name"], o["input"], None, ms, "stopped", STOP_MESSAGE)
            self.store.add_event(self.run_id, EventType.TOOL_RESULT, o["step"], ms, "stopped",
                                 {"tool": o["name"], "output_preview": STOP_MESSAGE})
        elif o["kind"] == "llm":
            self.store.add_llm_call(self.run_id, o["step"], o["model"], o["prompt_version"], o["input"], None,
                                    None, None, ms, "stopped", STOP_MESSAGE)
        else:
            self.store.add_event(self.run_id, EventType.AGENT_STEP, o["step"], ms, "stopped", {"node": o["name"]})

    def close_all_stopped(self) -> None:
        """RunStopped is a BaseException, which some LangChain wrappers do not
        report through on_*_error: close whatever is still open so the UI does
        not show it as running forever."""
        for o in list(self._open.values()):
            self._close_stopped(o)
        self._open.clear()

    @staticmethod
    def _ms(o: dict) -> float:
        return round((time.perf_counter() - o["t0"]) * 1000, 1)


_current_recorder: ContextVar[AgentRunRecorder | None] = ContextVar("agent_run_recorder", default=None)
register_configure_hook(_current_recorder, inheritable=True)


def emit_event(event_type: EventType, status: str | None, data: Any) -> None:
    """Record a custom event against the run currently being observed, if
    any — for agent code with something to report outside the automatic
    LLM/tool-call callbacks. A no-op outside an observe_run block."""
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder.store.add_event(recorder.run_id, event_type, recorder.step, None, status, data)


class RunHandle:
    def __init__(self, run_id: str, recorder: AgentRunRecorder):
        self.run_id = run_id
        self.recorder = recorder
        self.result: Any = None

    def set_result(self, result: Any) -> None:
        self.result = result


@contextmanager
def observe_run(agent_name: str, input: Any, *, agent_version: str | None = None,
                model: str | None = None, store: AgentRunStore = default_store,
                run_id: str | None = None, resume: bool = False) -> Iterator[RunHandle]:
    """Record everything an agent does inside the block as one run.
    Call handle.set_result(...) before leaving the block to store the result.

    run_id: use an existing agent_runs row (already created/reopened by the
            caller, e.g. so a client can subscribe before the work starts).
    resume: the row belongs to a paused session; keep its history and
            continue the step numbering instead of starting a new run.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())
        store.create_run(run_id, agent_name, agent_version, model, input)
    recorder = AgentRunRecorder(run_id, store)
    handle = RunHandle(run_id, recorder)
    if resume:
        recorder.step = store.last_step(run_id)
        store.add_event(run_id, EventType.RUN_RESUMED, recorder.step, None, None, {"agent": agent_name, "input": input})
    else:
        store.add_event(run_id, EventType.RUN_STARTED, 0, None, None, {"agent": agent_name})
    token = _current_recorder.set(recorder)
    with _live_lock:
        _live_runs[run_id] = recorder
    try:
        yield handle
    except RunStopped:
        _finish_stopped(store, run_id, recorder, handle)
        raise
    except Exception as e:
        if recorder.stop.is_set():  # the stop surfaced as some other error: still "stopped", not "failed"
            _finish_stopped(store, run_id, recorder, handle)
            raise
        store.add_event(run_id, EventType.RUN_FAILED, recorder.step, None, "error", {"error": str(e)})
        store.finish_run(run_id, RunStatus.FAILED, str(e), handle.result)
        raise
    else:
        store.add_event(run_id, EventType.RUN_COMPLETED, recorder.step, None, "ok", None)
        store.finish_run(run_id, RunStatus.COMPLETED, None, handle.result)
    finally:
        _current_recorder.reset(token)
        with _live_lock:
            _live_runs.pop(run_id, None)


def _finish_stopped(store: AgentRunStore, run_id: str, recorder: AgentRunRecorder, handle: RunHandle) -> None:
    recorder.close_all_stopped()
    store.add_event(run_id, EventType.RUN_STOPPED, recorder.step, None, "stopped", {"error": STOP_MESSAGE})
    store.finish_run(run_id, RunStatus.STOPPED, STOP_MESSAGE, handle.result)

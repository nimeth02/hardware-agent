# agents/test_vector_agent/test_vector_generator_agent/matlab_session.py — one MATLAB MCP session for a
# whole agent run.
#
# MATLAB takes 1–2 minutes to boot and lives inside the MATLAB MCP server
# process, so the server (and its MATLAB) should be opened once per agent
# run, not once per attempt. The agent wraps its run in
# `with shared_matlab_session():`; every attempt inside the block reuses
# that one session (see `current_session()`).
#
# An MCP session is bound to the event loop it was opened on, and attempts
# are started from sync graph nodes (each run_sync call is a fresh loop). So
# a session that must outlive one attempt gets its own loop on its own
# thread; each attempt's tool loop is submitted to that loop. The session is
# entered and exited by the same task (`_serve`), as the MCP stdio transport
# requires.

from __future__ import annotations

import asyncio
import contextvars
import threading
from contextlib import contextmanager
from typing import Iterator

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.tools import load_mcp_tools

from services.mcp_client import get_mcp_client


class MatlabSession:
    """One MATLAB MCP session kept open on a private event-loop thread.
    Opened lazily (first `tools`/`run`, or start()); close() exits the
    session and stops the thread."""

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tools: list[BaseTool] | None = None
        self._error: BaseException | None = None
        self._ready = threading.Event()
        self._closing: asyncio.Event | None = None

    def start(self) -> None:
        """Spawn the MATLAB MCP server (which starts booting MATLAB) and wait
        for the session handshake. Raises if the server could not be opened."""
        if self._thread is not None:
            if self._error is not None:
                raise RuntimeError(f"MATLAB MCP session could not be opened: {self._error}") from self._error
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._thread_main, name="matlab-session", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            raise RuntimeError(f"MATLAB MCP session could not be opened: {self._error}") from self._error

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as e:  # surfaced to start()/run() callers
            self._error = e
        finally:
            self._ready.set()       # also wakes start() when opening failed
            self._loop.close()

    async def _serve(self) -> None:
        self._closing = asyncio.Event()
        async with get_mcp_client().session("matlab") as session:
            self._tools = await load_mcp_tools(session)
            self._ready.set()
            await self._closing.wait()

    @property
    def tools(self) -> list[BaseTool]:
        self.start()
        return list(self._tools or [])

    def run(self, coro):
        """Run a coroutine on the session's loop and return its result. The
        caller's context variables (observability recorder, step tracer) are
        carried over so tool calls are logged under the right run."""
        try:
            self.start()
        except BaseException:
            coro.close()
            raise
        ctx = contextvars.copy_context()

        async def in_context():
            for var, value in ctx.items():
                var.set(value)
            return await coro

        return asyncio.run_coroutine_threadsafe(in_context(), self._loop).result()

    def close(self) -> None:
        """Quit the MCP server (and so MATLAB) and stop the thread. Safe to
        call when never started or after a failed start."""
        if self._thread is None:
            return
        if self._closing is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._closing.set)
        self._thread.join()


_run_session: contextvars.ContextVar[MatlabSession | None] = contextvars.ContextVar("matlab_run_session", default=None)


def current_session() -> MatlabSession | None:
    """The run's shared session, or None outside `shared_matlab_session()`."""
    return _run_session.get()


@contextmanager
def shared_matlab_session() -> Iterator[MatlabSession]:
    """Every attempt inside the block shares one MATLAB session; it is
    opened on first use (or `.start()`) and closed on exit."""
    session = MatlabSession()
    token = _run_session.set(session)
    try:
        yield session
    finally:
        _run_session.reset(token)
        session.close()

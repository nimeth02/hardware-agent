# agents/test_vector_agent/test_vector_generator.py — LLM tool: spec
# (+ loaded skills, + validator feedback when fixing) -> test-vector CSV in
# run_dir, via a tool-calling loop over the MATLAB MCP server.
#
# Unlike the other generators this one is itself a small loop: the LLM
# calls generate_matlab_script -> run_matlab_script -> (fix) ... until the
# script runs. The loop is bounded here (step cap, failed-run cap, no
# re-running an unchanged script); the surrounding agent bounds the outer
# generate -> validate cycle.
#
# MATLAB takes 1–2 minutes to boot and lives inside the MATLAB MCP server
# process, so the server (and its MATLAB) should be opened once per agent
# run, not once per attempt. The agent wraps its run in
# `with shared_matlab_session():`; every generate() inside the block reuses
# that one session. A generate() outside the block opens its own.

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.errors import GraphRecursionError

from agents.json_utils import extract_json
from agents.mcp_utils import run_sync
from agents.test_vector_agent.orchestrator_feedback import feedback_inputs, feedback_rules, has_feedback
from agents.test_vector_agent.state import AGENT_NAME
from observability.callback import ObservabilityCallbackHandler
from prompts.test_vector_agent_prompt import (
    TEST_VECTOR_AGENT_SYSTEM_PROMPT,
    TEST_VECTOR_FIX_ADDENDUM,
    TEST_VECTOR_SKILLS_ADDENDUM,
)
from services.llm_client import get_llm_for_loop
from services.mcp_client import get_mcp_client

MAX_AGENT_STEPS = 30
MAX_RUN_FAILURES = 3


class RunAttemptsExceeded(Exception):
    pass


def parse_test_vector_response(raw: str) -> dict:
    """Summary JSON from the LLM's text; a failure stub if unparsable."""
    try:
        result = extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        result = None
    if not isinstance(result, dict):
        return failure_result("Test vector agent did not return valid JSON; raw reply follows.", str(raw)[:500])
    return result


def failure_result(reason: str, detail: str = "") -> dict:
    return {
        "script": "gen_test_vectors.m",
        "input_csv": "test_vectors_in.csv",
        "output_csv": "test_vectors_out.csv",
        "input_columns": [],
        "output_columns": [],
        "num_vectors": 0,
        "scenarios": [],
        "notes": [reason] + ([detail] if detail else []),
    }


FORBIDDEN_MATLAB = ("int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64",
                    "typecast", "bitshift", "bitsll", "bitsra", "bitsrl", "idivide", "intmax", "intmin")
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_MATLAB) + r")\s*\(")


def _forbidden_matlab(code: str) -> list[str]:
    """Integer-class MATLAB calls used in the script (comments ignored)."""
    text = "\n".join(line.split("%", 1)[0] for line in code.splitlines())
    return sorted({m.group(1) for m in _FORBIDDEN_RE.finditer(text)})


def _clear_old_csvs(project_dir: str) -> None:
    """Delete every CSV in the run folder before a MATLAB run. A run that
    errors out part-way (observed: run 161047, header changed, script
    failed at line 163) would otherwise leave the previous run's CSVs on
    disk, and the validator cannot tell them from fresh ones."""
    folder = Path(str(project_dir or ""))
    if not folder.is_dir():
        return
    for f in folder.glob("*.csv"):
        f.unlink(missing_ok=True)


def new_run_state() -> dict:
    """Bookkeeping shared between the guarded tools and the attempt:
    `last_run` is None until run_matlab_script is called, then
    {"status": "ok" | "error", "error": str}."""
    return {"failures": 0, "dirty": True, "last_error": "", "scripts": set(), "last_run": None}


def _guard_run_tool(tools: list[BaseTool], max_failures: int = MAX_RUN_FAILURES,
                    state: dict | None = None) -> list[BaseTool]:
    """Wrap generate/run so the loop can't burn steps re-running an unchanged
    script (observed with local models), and so failed runs are hard-capped."""
    by_name = {t.name: t for t in tools}
    gen_tool = by_name["generate_matlab_script"]
    run_tool = by_name["run_matlab_script"]
    if state is None:
        state = new_run_state()

    def _stem(name) -> str:
        return str(name or "").strip().removesuffix(".m")

    async def generate(**kwargs):
        # MATLAB refuses to mix integer classes; every observed failed run
        # came from these. Reject the script before it is written (not a
        # counted failure) so the LLM rewrites it in double arithmetic.
        bad = _forbidden_matlab(str(kwargs.get("code") or ""))
        if bad:
            return ("REJECTED: the script uses " + ", ".join(bad) + " — integer-class functions are not "
                    "allowed (they cause 'Integers can only be combined with integers of the same class'). "
                    "Do all arithmetic in double: mod(x, 2^N) to wrap, floor(x / 2^k) to shift right, "
                    "x * 2^k to shift left, hi * 2^lo_bits + lo to pack (NUMBER TYPES rule). Rewrite and call "
                    "generate_matlab_script again.")
        state["dirty"] = True
        state["scripts"].add(_stem(kwargs.get("filename")))
        return await gen_tool.ainvoke(kwargs)

    async def run(**kwargs):
        # Running anything but a script the LLM wrote (observed: `dir` to look
        # for a reference file) is a misuse, not a failed golden model: reject
        # it with a hint and do not count it against the failure cap.
        if _stem(kwargs.get("script_name")) not in state["scripts"]:
            return ("REJECTED: run_matlab_script only runs a script you wrote with "
                    "generate_matlab_script (e.g. gen_test_vectors). It cannot list folders "
                    "or run built-ins; the spec is your only input, there is no reference file.")
        if state["failures"] and not state["dirty"]:
            return ("REJECTED: the script has not changed since its last failed run. "
                    "Fix the error and call generate_matlab_script first.")
        _clear_old_csvs(kwargs.get("project_dir"))
        out = await run_tool.ainvoke(kwargs)
        state["dirty"] = False
        if '"status": "error"' in str(out):
            state["failures"] += 1
            state["last_error"] = str(out)[:1500]
            state["last_run"] = {"status": "error", "error": state["last_error"]}
            if state["failures"] >= max_failures:
                raise RunAttemptsExceeded(state["last_error"])
        else:
            state["last_run"] = {"status": "ok", "error": ""}
        return out

    wrapped = {
        gen_tool.name: StructuredTool.from_function(
            coroutine=generate, name=gen_tool.name, description=gen_tool.description, args_schema=gen_tool.args_schema),
        run_tool.name: StructuredTool.from_function(
            coroutine=run, name=run_tool.name, description=run_tool.description, args_schema=run_tool.args_schema),
    }
    return [wrapped.get(t.name, t) for t in tools]


# ── one MATLAB session for a whole agent run ───────────────────────────────
#
# An MCP session is bound to the event loop it was opened on, and generate()
# is called from sync graph nodes (each run_sync call is a fresh loop). So a
# session that must outlive one attempt gets its own loop on its own thread;
# each attempt's tool loop is submitted to that loop. The session is entered
# and exited by the same task (`_serve`), as the MCP stdio transport requires.

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

    @property
    def started(self) -> bool:
        return self._thread is not None

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
            self._ready.set()
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


@contextmanager
def shared_matlab_session() -> Iterator[MatlabSession]:
    """Every TestVectorGenerator.generate() inside the block shares one MATLAB
    session; it is opened on first use (or `.start()`) and closed on exit."""
    session = MatlabSession()
    token = _run_session.set(session)
    try:
        yield session
    finally:
        _run_session.reset(token)
        session.close()


class TestVectorGenerator:
    """Runs the MATLAB tool loop once: writes/runs the golden model in
    run_dir and returns the LLM's JSON summary. Never raises for a
    non-converging loop — returns a failure summary instead."""

    def __init__(self, llm=None):
        self._llm = llm

    @property
    def llm(self):
        """The injected client, or a fresh one per attempt: each attempt runs
        on its own event loop (run_sync or the MatlabSession thread) and the
        shared pool of the cached client raises "Event loop is closed"."""
        if self._llm is not None:
            return self._llm
        return get_llm_for_loop()

    def generate(self, spec: dict, run_dir: str, skills: dict[str, str] | None = None,
                 previous_result: dict | None = None, validation_issues: list[dict] | None = None,
                 attempt: int = 1, attempt_history: list[dict] | None = None) -> dict:
        """Inside `shared_matlab_session()` the attempt runs on the run's
        shared MATLAB; otherwise it opens a session of its own."""
        system, payload = self._prompt(spec, run_dir, skills, previous_result, validation_issues,
                                       attempt, attempt_history)
        shared = _run_session.get()
        if shared is None:
            return run_sync(self._generate_in_own_session(system, payload))
        return shared.run(self._tool_loop(shared.tools, system, payload))

    async def agenerate(self, spec: dict, run_dir: str, skills: dict[str, str] | None = None,
                        previous_result: dict | None = None, validation_issues: list[dict] | None = None,
                        attempt: int = 1, attempt_history: list[dict] | None = None) -> dict:
        system, payload = self._prompt(spec, run_dir, skills, previous_result, validation_issues,
                                       attempt, attempt_history)
        return await self._generate_in_own_session(system, payload)

    @staticmethod
    def _prompt(spec: dict, run_dir: str, skills: dict[str, str] | None,
                previous_result: dict | None, validation_issues: list[dict] | None,
                attempt: int = 1, attempt_history: list[dict] | None = None) -> tuple[str, dict]:
        system = TEST_VECTOR_AGENT_SYSTEM_PROMPT
        if skills:
            system += TEST_VECTOR_SKILLS_ADDENDUM + "\n\n".join(f"### {n}\n{c}" for n, c in skills.items())
        payload: dict = {"spec": spec, "project_dir": run_dir}
        if has_feedback(attempt, attempt_history):             # the orchestrator called us again and says how it went
            system += feedback_rules(attempt)
            payload.update(feedback_inputs(attempt_history))
        if previous_result is not None and validation_issues:  # the validator rejected our last CSVs in this run
            system += TEST_VECTOR_FIX_ADDENDUM
            payload["previous_result"] = previous_result
            payload["validation_issues"] = validation_issues
        return system, payload

    async def _generate_in_own_session(self, system: str, payload: dict) -> dict:
        async with get_mcp_client().session("matlab") as session:
            return await self._tool_loop(await load_mcp_tools(session), system, payload)

    async def _tool_loop(self, mcp_tools: list[BaseTool], system: str, payload: dict) -> dict:
        """One attempt: the LLM writes/runs the script against the given MATLAB
        tools until it succeeds or a cap trips. Caps are per attempt."""
        runs = new_run_state()
        tools = _guard_run_tool(mcp_tools, state=runs)
        agent = create_agent(self.llm, tools, system_prompt=system)
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        try:
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=json.dumps(payload, indent=2))]},
                config={"callbacks": [handler], "recursion_limit": MAX_AGENT_STEPS},
            )
        except RunAttemptsExceeded as e:
            summary = failure_result(f"Gave up after {MAX_RUN_FAILURES} failed MATLAB runs.", f"Last MATLAB error: {e}")
        except GraphRecursionError:
            summary = failure_result(f"Agent did not finish within {MAX_AGENT_STEPS} steps.")
        else:
            summary = parse_test_vector_response(result["messages"][-1].content)
        # What the validator needs and the LLM's summary cannot be trusted for:
        # did the LAST MATLAB run succeed (so the CSVs on disk are its output)?
        summary["last_matlab_run"] = runs["last_run"] or {"status": "never_ran", "error": ""}
        return summary

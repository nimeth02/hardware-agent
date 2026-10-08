# agents/test_vector_agent/test_vector_generator.py — LLM tool: spec
# (+ loaded skills, + validator feedback when fixing) -> test-vector CSV in
# run_dir, via a tool-calling loop over the MATLAB MCP server.
#
# Unlike the other generators this one is itself a small loop: the LLM
# calls generate_matlab_script -> run_matlab_script -> (fix) ... until the
# script runs. The loop is bounded here (step cap, failed-run cap, no
# re-running an unchanged script); the surrounding agent bounds the outer
# generate -> validate cycle.

from __future__ import annotations

import json

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.errors import GraphRecursionError

from agents.json_utils import extract_json
from agents.mcp_utils import run_sync
from agents.test_vector_agent.state import AGENT_NAME
from observability.callback import ObservabilityCallbackHandler
from prompts.test_vector_agent_prompt import (
    TEST_VECTOR_AGENT_SYSTEM_PROMPT,
    TEST_VECTOR_FIX_ADDENDUM,
    TEST_VECTOR_SKILLS_ADDENDUM,
)
from services.llm_client import get_llm
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
        "csv_file": "test_vectors.csv",
        "columns": [],
        "num_vectors": 0,
        "scenarios": [],
        "notes": [reason] + ([detail] if detail else []),
    }


def _guard_run_tool(tools: list[BaseTool], max_failures: int = MAX_RUN_FAILURES) -> list[BaseTool]:
    """Wrap generate/run so the loop can't burn steps re-running an unchanged
    script (observed with local models), and so failed runs are hard-capped."""
    by_name = {t.name: t for t in tools}
    gen_tool = by_name["generate_matlab_script"]
    run_tool = by_name["run_matlab_script"]
    state = {"failures": 0, "dirty": True, "last_error": ""}

    async def generate(**kwargs):
        state["dirty"] = True
        return await gen_tool.ainvoke(kwargs)

    async def run(**kwargs):
        if state["failures"] and not state["dirty"]:
            return ("REJECTED: the script has not changed since its last failed run. "
                    "Fix the error and call generate_matlab_script first.")
        out = await run_tool.ainvoke(kwargs)
        state["dirty"] = False
        if '"status": "error"' in str(out):
            state["failures"] += 1
            state["last_error"] = str(out)[:1500]
            if state["failures"] >= max_failures:
                raise RunAttemptsExceeded(state["last_error"])
        return out

    wrapped = {
        gen_tool.name: StructuredTool.from_function(
            coroutine=generate, name=gen_tool.name, description=gen_tool.description, args_schema=gen_tool.args_schema),
        run_tool.name: StructuredTool.from_function(
            coroutine=run, name=run_tool.name, description=run_tool.description, args_schema=run_tool.args_schema),
    }
    return [wrapped.get(t.name, t) for t in tools]


class TestVectorGenerator:
    """Runs the MATLAB tool loop once: writes/runs the golden model in
    run_dir and returns the LLM's JSON summary. Never raises for a
    non-converging loop — returns a failure summary instead."""

    def __init__(self, llm=None):
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def generate(self, spec: dict, run_dir: str, skills: dict[str, str] | None = None,
                 previous_result: dict | None = None, validation_issues: list[dict] | None = None) -> dict:
        return run_sync(self.agenerate(spec, run_dir, skills, previous_result, validation_issues))

    async def agenerate(self, spec: dict, run_dir: str, skills: dict[str, str] | None = None,
                        previous_result: dict | None = None, validation_issues: list[dict] | None = None) -> dict:
        system = TEST_VECTOR_AGENT_SYSTEM_PROMPT
        if skills:
            system += TEST_VECTOR_SKILLS_ADDENDUM + "\n\n".join(f"### {n}\n{c}" for n, c in skills.items())
        payload: dict = {"spec": spec, "project_dir": run_dir}
        if previous_result is not None and validation_issues:
            system += TEST_VECTOR_FIX_ADDENDUM
            payload["previous_result"] = previous_result
            payload["validation_issues"] = validation_issues

        client = get_mcp_client()
        async with client.session("matlab") as session:
            tools = _guard_run_tool(await load_mcp_tools(session))
            agent = create_agent(self.llm, tools, system_prompt=system)
            handler = ObservabilityCallbackHandler(source=AGENT_NAME)
            try:
                result = await agent.ainvoke(
                    {"messages": [HumanMessage(content=json.dumps(payload, indent=2))]},
                    config={"callbacks": [handler], "recursion_limit": MAX_AGENT_STEPS},
                )
            except RunAttemptsExceeded as e:
                return failure_result(f"Gave up after {MAX_RUN_FAILURES} failed MATLAB runs.", f"Last MATLAB error: {e}")
            except GraphRecursionError:
                return failure_result(f"Agent did not finish within {MAX_AGENT_STEPS} steps.")
        return parse_test_vector_response(result["messages"][-1].content)

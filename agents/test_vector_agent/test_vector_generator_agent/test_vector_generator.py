# agents/test_vector_agent/test_vector_generator_agent/test_vector_generator.py — LLM tool: spec
# (+ loaded skills, + validator feedback when fixing) -> test-vector CSV in
# run_dir, via a tool-calling loop over the MATLAB MCP server.
#
# Unlike the other generators this one is itself a small loop: the LLM
# calls generate_matlab_script -> run_matlab_script -> (fix) ... until the
# script runs. The loop is bounded (step cap here; failed-run cap and
# no re-running an unchanged script in matlab_guard); the surrounding agent
# bounds the outer generate -> validate cycle.
#
# Inside `shared_matlab_session()` (matlab_session) every attempt reuses the
# run's one MATLAB; outside it each generate() opens its own.

from __future__ import annotations

import json

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.errors import GraphRecursionError

from agents.json_utils import extract_json
from agents.mcp_utils import run_sync
from agents.test_vector_agent.orchestrator_feedback import feedback_inputs, feedback_rules, has_feedback
from agents.test_vector_agent.state import AGENT_NAME
from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import guard_matlab_tools
from agents.test_vector_agent.test_vector_generator_agent.matlab_session import current_session
from agents.test_vector_agent.test_vector_generator_agent.state import (
    MAX_AGENT_STEPS,
    MAX_RUN_FAILURES,
    RunAttemptsExceeded,
    failure_result,
    new_run_state,
)
from observability.callback import ObservabilityCallbackHandler
from prompts.test_vector_agent_prompt import (
    TEST_VECTOR_AGENT_SYSTEM_PROMPT,
    TEST_VECTOR_FIX_ADDENDUM,
    TEST_VECTOR_SKILLS_ADDENDUM,
)
from services.llm_client import get_llm_for_loop
from services.mcp_client import get_mcp_client


def parse_test_vector_response(raw: str) -> dict:
    """Summary JSON from the LLM's text; a failure stub if unparsable."""
    try:
        result = extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        result = None
    if not isinstance(result, dict):
        return failure_result("Test vector agent did not return valid JSON; raw reply follows.", str(raw)[:500])
    return result


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
        shared = current_session()
        if shared is None:
            return run_sync(self._generate_in_own_session(system, payload))
        return shared.run(self._tool_loop(shared.tools, system, payload))

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
        tools = guard_matlab_tools(mcp_tools, state=runs, spec=payload.get("spec"))
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

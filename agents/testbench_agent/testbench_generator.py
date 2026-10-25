# agents/testbench_agent/testbench_generator.py — LLM tool: spec + RTL
# (+ loaded skills, + lint feedback when fixing) -> testbench dict.

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.testbench_agent.orchestrator_feedback import feedback_inputs, feedback_rules, has_feedback
from agents.testbench_agent.state import AGENT_NAME
from observability.callback import ObservabilityCallbackHandler
from prompts.testbench_agent_prompt import (
    TESTBENCH_AGENT_SYSTEM_PROMPT,
    TESTBENCH_FIX_ADDENDUM,
    TESTBENCH_SKILLS_ADDENDUM,
)
from services.llm_client import get_llm


def parse_testbench_response(spec: dict, rtl: dict, raw: str) -> dict:
    """Testbench JSON from the LLM's text; the raw text as code if unparsable."""
    try:
        testbench = extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        testbench = None
    if not isinstance(testbench, dict):
        return {
            "entity_name": f"{spec.get('entity_name')}_tb",
            "code": str(raw),
            "notes": ["Testbench agent did not return valid JSON; showing raw output as code."],
        }
    return testbench


class TestbenchGenerator:
    """Writes the testbench on the first pass; rewrites it against the
    linter's findings on later passes. The LLM is created lazily so tests
    can inject a fake."""

    def __init__(self, llm=None):
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def generate_raw(self, spec: dict, rtl: dict, skills: dict[str, str] | None = None,
                     previous_testbench: dict | None = None, lint_issues: list[dict] | None = None,
                     attempt: int = 1, attempt_history: list[dict] | None = None) -> str:
        system = TESTBENCH_AGENT_SYSTEM_PROMPT
        if skills:
            system += TESTBENCH_SKILLS_ADDENDUM + "\n\n".join(f"### {n}\n{c}" for n, c in skills.items())
        rtl_view = {"entity_name": rtl.get("entity_name"), "code": rtl.get("code")}
        payload = {"spec": spec, "rtl": rtl_view}
        feedback = has_feedback(attempt, attempt_history)          # the orchestrator called us again and says how it went
        if feedback:
            system += feedback_rules(attempt)
            payload.update(feedback_inputs(attempt_history))
        fixing = previous_testbench is not None and bool(lint_issues)   # the linter rejected our last testbench in this run
        if fixing:
            system += TESTBENCH_FIX_ADDENDUM
            payload.update(previous_code=previous_testbench.get("code"), lint_issues=lint_issues)
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        response = self.llm.invoke(
            [SystemMessage(content=system), HumanMessage(content=json.dumps(payload, indent=2))],
            config={"callbacks": [handler]},
        )
        return response.content

    def generate(self, spec: dict, rtl: dict, skills: dict[str, str] | None = None,
                 previous_testbench: dict | None = None, lint_issues: list[dict] | None = None,
                 attempt: int = 1, attempt_history: list[dict] | None = None) -> dict:
        return parse_testbench_response(
            spec, rtl, self.generate_raw(spec, rtl, skills, previous_testbench, lint_issues, attempt, attempt_history))

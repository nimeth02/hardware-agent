# agents/rtl_agent/rtl_generator.py — LLM tool: approved spec (+ loaded
# skills, + lint feedback when fixing) -> design dict.

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.rtl_agent.orchestrator_feedback import feedback_inputs, feedback_rules, has_feedback, revise_rules
from agents.rtl_agent.state import AGENT_NAME
from agents.vhdl_version import prepare_spec_for_rtl, resolve_vhdl_version
from observability.callback import ObservabilityCallbackHandler
from prompts.rtl_agent_prompt import RTL_AGENT_SYSTEM_PROMPT, RTL_FIX_ADDENDUM, RTL_SKILLS_ADDENDUM
from services.llm_client import get_llm


def parse_rtl_response(spec: dict, raw: str) -> dict:
    """Design JSON from the LLM's text; the raw text as code if unparsable."""
    try:
        rtl = extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        rtl = None
    if not isinstance(rtl, dict):
        return {
            "entity_name": spec.get("entity_name"),
            "code": str(raw),
            "notes": ["RTL agent did not return valid JSON; showing raw output as code."],
        }
    return rtl


class RTLGenerator:
    """Writes the design on the first pass; rewrites it against the linter's
    findings on later passes. The LLM is created lazily so tests can inject
    a fake."""

    def __init__(self, llm=None):
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def generate_raw(self, spec: dict, skills: dict[str, str] | None = None,
                     previous_rtl: dict | None = None, lint_issues: list[dict] | None = None,
                     attempt: int = 1, attempt_history: list[dict] | None = None,
                     starting_rtl: dict | None = None) -> str:
        # previous_rtl / lint_issues: this run's own design that the linter
        # rejected (fix loop). starting_rtl: the design the orchestrator's
        # earlier call delivered, to be revised on a retry (repair loop).
        # The prompt names the edition (agents/vhdl_version.py decides it);
        # the spec itself is passed through unchanged.
        system = RTL_AGENT_SYSTEM_PROMPT.replace("{VHDL_VERSION}", resolve_vhdl_version(spec))
        if skills:
            system += RTL_SKILLS_ADDENDUM + "\n\n".join(f"### {name}\n{content}" for name, content in skills.items())
        feedback = has_feedback(attempt, attempt_history)        # the orchestrator called us again and says how it went
        if feedback:
            system += feedback_rules(attempt)
        fixing = previous_rtl is not None and bool(lint_issues)  # the linter rejected our last design in this run
        if fixing:
            system += RTL_FIX_ADDENDUM
        revising = feedback and not fixing and bool((starting_rtl or {}).get("code"))   # first write of a retry
        if revising:
            system += revise_rules()
        if not fixing and not feedback:
            user = json.dumps(spec, indent=2)
        else:
            payload = {"spec": spec}
            if feedback:
                payload.update(feedback_inputs(attempt_history, starting_rtl if revising else None))
            if fixing:
                payload.update(previous_code=previous_rtl.get("code"), lint_issues=lint_issues)
            user = json.dumps(payload, indent=2)
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        response = self.llm.invoke(
            [SystemMessage(content=system), HumanMessage(content=user)],
            config={"callbacks": [handler]},
        )
        return response.content

    def generate(self, spec: dict, skills: dict[str, str] | None = None,
                 previous_rtl: dict | None = None, lint_issues: list[dict] | None = None,
                 attempt: int = 1, attempt_history: list[dict] | None = None,
                 starting_rtl: dict | None = None) -> dict:
        return parse_rtl_response(spec, self.generate_raw(spec, skills, previous_rtl, lint_issues,
                                                          attempt, attempt_history, starting_rtl))

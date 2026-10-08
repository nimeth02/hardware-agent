# agents/spec_analyser_agent/spec_generator.py — LLM tool: requirement text
# (plus any clarification answers) -> structured spec dict.

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.spec_analyser_agent.state import AGENT_NAME
from observability.callback import ObservabilityCallbackHandler
from prompts.spec_analyzer_prompt import SPEC_ANALYZER_SYSTEM_PROMPT, SPEC_CLARIFICATION_ADDENDUM
from services.llm_client import get_llm


def parse_spec_response(raw: str) -> dict:
    """Spec JSON from the LLM's text; a clarification-needed stub if unparsable."""
    try:
        spec = extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        spec = None
    if not isinstance(spec, dict):
        return {
            "status": "needs_clarification",
            "ambiguities": ["Spec analyzer did not return valid JSON.", str(raw)[:500]],
        }
    return spec


class SpecGenerator:
    """Produces the spec on the first pass; refines it (previous spec +
    user answers) on later passes. The LLM is created lazily so tests can
    inject a fake."""

    def __init__(self, llm=None):
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def generate_raw(self, request: str, feedback: list[str] | None = None,
                     previous_spec: dict | None = None) -> str:
        refining = bool(feedback) or previous_spec is not None
        system = SPEC_ANALYZER_SYSTEM_PROMPT + (SPEC_CLARIFICATION_ADDENDUM if refining else "")
        if refining:
            user = json.dumps(
                {"original_request": request, "previous_spec": previous_spec, "user_answers": feedback or []},
                indent=2,
            )
        else:
            user = request
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        response = self.llm.invoke(
            [SystemMessage(content=system), HumanMessage(content=user)],
            config={"callbacks": [handler]},
        )
        return response.content

    def generate(self, request: str, feedback: list[str] | None = None,
                 previous_spec: dict | None = None) -> dict:
        return parse_spec_response(self.generate_raw(request, feedback, previous_spec))

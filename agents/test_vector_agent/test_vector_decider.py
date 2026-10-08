# agents/test_vector_agent/test_vector_decider.py — LLM planner: state
# summary -> next Action. It never judges the CSV (the validator does); the
# agent's guardrails validate whatever it returns.

from __future__ import annotations

import json
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.test_vector_agent.state import AGENT_NAME, Action
from observability.callback import ObservabilityCallbackHandler
from prompts.test_vector_agent_prompt import TEST_VECTOR_DECISION_PROMPT
from services.llm_client import get_llm


@dataclass
class Decision:
    action: Action
    reason: str = ""


class TestVectorDecider:
    def __init__(self, llm=None):
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def decide(self, summary: dict) -> Decision:
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        response = self.llm.invoke(
            [SystemMessage(content=TEST_VECTOR_DECISION_PROMPT), HumanMessage(content=json.dumps(summary, indent=2))],
            config={"callbacks": [handler]},
        )
        return self.parse(response.content, summary)

    @staticmethod
    def parse(raw: str, summary: dict) -> Decision:
        try:
            data = extract_json(raw)
            return Decision(Action(str(data.get("action", "")).strip().lower()), str(data.get("reason", "")))
        except (json.JSONDecodeError, ValueError, AttributeError):
            # Unusable planner output: fall back to the obvious safe choice.
            if not summary.get("skills_loaded"):
                return Decision(Action.LOAD_SKILLS, "planner output invalid; loading skills")
            if not summary.get("has_result"):
                return Decision(Action.GENERATE_VECTORS, "planner output invalid; generating")
            validation = summary.get("validation")
            if validation is None:
                return Decision(Action.VALIDATE_VECTORS, "planner output invalid; validating")
            if validation.get("issues"):
                return Decision(Action.GENERATE_VECTORS, "planner output invalid; fixing validation issues")
            return Decision(Action.FINALIZE_VECTORS, "planner output invalid; vectors are clean")

# agents/test_vector_agent/test_vector_decider.py — the decider: the LLM
# that picks the next Action from dto.decider_input_dto(state). It never
# judges the CSV (the validator does); the agent's guardrails validate
# whatever it returns.

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

    def decide(self, decider_input: dict) -> Decision:
        """decider_input: dto.decider_input_dto(state)."""
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        response = self.llm.invoke(
            [SystemMessage(content=TEST_VECTOR_DECISION_PROMPT), HumanMessage(content=json.dumps(decider_input, indent=2))],
            config={"callbacks": [handler]},
        )
        return self.parse(response.content, decider_input)

    @staticmethod
    def parse(raw: str, decider_input: dict) -> Decision:
        try:
            data = extract_json(raw)
            return Decision(Action(str(data.get("action", "")).strip().lower()), str(data.get("reason", "")))
        except (json.JSONDecodeError, ValueError, AttributeError):
            # Unusable decider output: fall back to the obvious safe choice.
            if not decider_input.get("skills_loaded"):
                return Decision(Action.LOAD_SKILLS, "decider output invalid; loading skills")
            if not decider_input.get("has_result"):
                return Decision(Action.GENERATE_VECTORS, "decider output invalid; generating")
            validation = decider_input.get("validation")
            if validation is None:
                return Decision(Action.VALIDATE_VECTORS, "decider output invalid; validating")
            if validation.get("issues"):
                return Decision(Action.GENERATE_VECTORS, "decider output invalid; fixing validation issues")
            return Decision(Action.FINALIZE_VECTORS, "decider output invalid; vectors are clean")

# agents/testbench_agent/testbench_decider.py — the decider: the LLM that
# picks the next Action from dto.decider_input_dto(state). It never judges
# the testbench (the linter does); the agent's guardrails validate whatever
# it returns.

from __future__ import annotations

import json
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.testbench_agent.state import AGENT_NAME, Action
from observability.callback import ObservabilityCallbackHandler
from prompts.testbench_agent_prompt import TESTBENCH_DECISION_PROMPT
from services.llm_client import get_llm


@dataclass
class Decision:
    action: Action
    reason: str = ""


class TestbenchDecider:
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
            [SystemMessage(content=TESTBENCH_DECISION_PROMPT), HumanMessage(content=json.dumps(decider_input, indent=2))],
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
            if not decider_input.get("has_testbench"):
                return Decision(Action.GENERATE_TESTBENCH, "decider output invalid; generating")
            lint = decider_input.get("lint")
            if lint is None:
                return Decision(Action.LINT_TESTBENCH, "decider output invalid; linting")
            if lint.get("issues"):
                return Decision(Action.GENERATE_TESTBENCH, "decider output invalid; fixing lint issues")
            return Decision(Action.FINALIZE_TESTBENCH, "decider output invalid; testbench is clean")

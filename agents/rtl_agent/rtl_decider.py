# agents/rtl_agent/rtl_decider.py — LLM planner: state summary -> next
# Action. It never judges the code (the linter does); the agent's guardrails
# validate whatever it returns.

from __future__ import annotations

import json
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.rtl_agent.state import AGENT_NAME, Action
from observability.callback import ObservabilityCallbackHandler
from prompts.rtl_agent_prompt import RTL_DECISION_PROMPT
from services.llm_client import get_llm


@dataclass
class Decision:
    action: Action
    reason: str = ""


class RTLDecider:
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
            [SystemMessage(content=RTL_DECISION_PROMPT), HumanMessage(content=json.dumps(summary, indent=2))],
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
            if not summary.get("has_rtl"):
                return Decision(Action.GENERATE_RTL, "planner output invalid; generating")
            lint = summary.get("lint")
            if lint is None:
                return Decision(Action.LINT_RTL, "planner output invalid; linting")
            if lint.get("issues"):
                return Decision(Action.GENERATE_RTL, "planner output invalid; fixing lint issues")
            return Decision(Action.FINALIZE_RTL, "planner output invalid; design is clean")

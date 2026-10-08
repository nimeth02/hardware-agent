# agents/implementation_agent/implementation_decider.py — LLM planner: state
# summary -> next Action. It never judges the implementation (the analyst
# does); the agent's guardrails validate whatever it returns.

from __future__ import annotations

import json
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from agents.implementation_agent.state import AGENT_NAME, Action
from agents.json_utils import extract_json
from observability.callback import ObservabilityCallbackHandler
from prompts.implementation_agent_prompt import IMPLEMENTATION_DECISION_PROMPT
from services.llm_client import get_llm


@dataclass
class Decision:
    action: Action
    reason: str = ""


class ImplementationDecider:
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
            [SystemMessage(content=IMPLEMENTATION_DECISION_PROMPT), HumanMessage(content=json.dumps(summary, indent=2))],
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
            if not summary.get("project_loaded"):
                return Decision(Action.LOAD_PROJECT, "planner output invalid; loading project")
            if not summary.get("scripts_written"):
                return Decision(Action.WRITE_SCRIPTS, "planner output invalid; writing scripts")
            if summary.get("batch") is None:
                return Decision(Action.RUN_IMPLEMENTATION, "planner output invalid; running implementation")
            if summary.get("analysis") is None:
                return Decision(Action.ANALYZE_RESULT, "planner output invalid; analyzing")
            return Decision(Action.FINALIZE_IMPLEMENTATION, "planner output invalid; verdict available")

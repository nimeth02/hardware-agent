# agents/spec_analyser_agent/spec_decider.py — LLM planner: state summary
# -> next Action. It never judges validity (the evaluator does); the agent's
# guardrails validate whatever it returns.

from __future__ import annotations

import json
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.spec_analyser_agent.state import AGENT_NAME, Action
from observability.callback import ObservabilityCallbackHandler
from prompts.spec_analyzer_prompt import SPEC_DECISION_PROMPT
from services.llm_client import get_llm


@dataclass
class Decision:
    action: Action
    question: str | None = None
    reason: str = ""


class SpecDecider:
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
            [SystemMessage(content=SPEC_DECISION_PROMPT), HumanMessage(content=json.dumps(summary, indent=2))],
            config={"callbacks": [handler]},
        )
        return self.parse(response.content, summary)

    @staticmethod
    def parse(raw: str, summary: dict) -> Decision:
        try:
            data = extract_json(raw)
            action = Action(str(data.get("action", "")).strip().lower())
            question = data.get("question")
            return Decision(action, question if isinstance(question, str) and question.strip() else None,
                            str(data.get("reason", "")))
        except (json.JSONDecodeError, ValueError, AttributeError):
            # Unusable planner output: fall back to the obvious safe choice.
            if summary.get("spec") is None or not summary.get("answers_applied", True):
                return Decision(Action.GENERATE_SPEC, reason="planner output invalid; generating")
            if summary.get("issues"):
                return Decision(Action.ASK_USER, reason="planner output invalid; issues remain")
            return Decision(Action.FINALIZE_SPEC, reason="planner output invalid; spec is clean")

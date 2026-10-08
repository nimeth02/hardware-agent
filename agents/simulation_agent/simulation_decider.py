# agents/simulation_agent/simulation_decider.py — LLM planner: state summary
# -> next Action. It never judges the simulation (the analyst does); the
# agent's guardrails validate whatever it returns.

from __future__ import annotations

import json
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.simulation_agent.state import MAX_SIM_RERUNS, AGENT_NAME, Action
from observability.callback import ObservabilityCallbackHandler
from prompts.simulation_agent_prompt import SIMULATION_DECISION_PROMPT
from services.llm_client import get_llm


@dataclass
class Decision:
    action: Action
    reason: str = ""


class SimulationDecider:
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
            [SystemMessage(content=SIMULATION_DECISION_PROMPT), HumanMessage(content=json.dumps(summary, indent=2))],
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
            compile_result = summary.get("compile")
            if compile_result is None:
                return Decision(Action.COMPILE_DESIGN, "planner output invalid; compiling")
            sim = summary.get("sim")
            if compile_result.get("status") == "ok" and sim is None:
                return Decision(Action.RUN_SIMULATION, "planner output invalid; simulating")
            if (sim or {}).get("outcome") == "no_marker" and summary.get("sim_runs", 0) <= MAX_SIM_RERUNS:
                return Decision(Action.RUN_SIMULATION, "planner output invalid; rerunning with a longer sim_time")
            if summary.get("analysis") is None:
                return Decision(Action.ANALYZE_RESULT, "planner output invalid; analyzing")
            return Decision(Action.FINALIZE_SIMULATION, "planner output invalid; verdict available")

# agents/simulation_agent/simulation_analyst.py — tool: turn compile +
# simulation results into a verdict. A clean pass and a run that never
# printed a marker are decided deterministically; everything else goes to
# the LLM analyst (prompts/simulation_agent_prompt.py).

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.simulation_agent.state import AGENT_NAME
from observability.callback import ObservabilityCallbackHandler
from prompts.simulation_agent_prompt import SIMULATION_AGENT_SYSTEM_PROMPT
from services.llm_client import get_llm


def deterministic_verdict(compile_result: dict | None, sim: dict | None) -> dict | None:
    """Verdict without an LLM when the evidence is unambiguous; None otherwise."""
    outcome = (sim or {}).get("outcome")
    if outcome == "passed":
        return {"verdict": "pass", "likely_cause": None, "failures": [],
                "summary": "Simulation passed: the testbench reported SIMULATION PASSED with no errors.",
                "suggested_fix": None}
    if outcome == "no_marker":
        return {"verdict": "error", "likely_cause": "testbench", "failures": [
                    f"No SIMULATION PASSED/FAILED marker within sim_time={sim.get('sim_time')}."],
                "summary": "The simulation ran to the end of sim_time without the testbench printing a result "
                           "marker: either the stimulus takes longer than sim_time or the testbench never "
                           "reports one.",
                "suggested_fix": "Shorten the stimulus or make the testbench print SIMULATION PASSED/FAILED "
                                 "and set done <= true at the end."}
    return None


def parse_analysis_response(raw: str, sim: dict | None) -> dict:
    try:
        data = extract_json(raw)
        if isinstance(data, dict) and data.get("verdict"):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    outcome = (sim or {}).get("outcome")
    return {"verdict": "fail" if outcome == "failed" else "error", "likely_cause": None,
            "failures": ["Simulation analyst did not return valid JSON; raw reply follows.", str(raw)[:500]],
            "summary": "", "suggested_fix": None}


class SimulationAnalyst:
    def __init__(self, llm=None):
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def analyze(self, project: dict, compile_result: dict | None, sim: dict | None) -> dict:
        verdict = deterministic_verdict(compile_result, sim)
        if verdict is not None:
            return verdict
        payload = {
            "spec": project.get("spec"),
            "testbench_code": project.get("testbench_code"),
            "stage": "simulate" if (compile_result or {}).get("status") == "ok" else "compile",
            "compile": compile_result,
            "sim": sim,
        }
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        response = self.llm.invoke(
            [SystemMessage(content=SIMULATION_AGENT_SYSTEM_PROMPT), HumanMessage(content=json.dumps(payload, indent=2))],
            config={"callbacks": [handler]},
        )
        return parse_analysis_response(response.content, sim)

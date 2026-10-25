# agents/simulation_agent/simulation_analyst.py — tool: turn compile +
# simulation results into a verdict. A clean pass and a run that never
# printed a marker are decided deterministically; everything else goes to
# the LLM analyst (prompts/simulation_agent_prompt.py).

from __future__ import annotations

import json
import re

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


# The Vivado MCP server now returns logs up to 200 KB so the UI can show the
# whole xsim/xvhdl output. The LLM needs the FIRST mismatches (the clearest
# evidence: the earliest cycle that went wrong) and the tail (the
# PASSED/FAILED marker), not the warnings in between, so keep both ends.
LLM_LOG_HEAD = 3000
LLM_LOG_TAIL = 6000
_FIRST_ERROR = re.compile(r"^.*\b(Error|ERROR|Failure|FAILURE|Fatal|FATAL)\b.*$", re.MULTILINE)


def _first_error_at(text: str) -> int:
    """Where the first error line starts (0 if there is none)."""
    match = _FIRST_ERROR.search(text)
    return match.start() if match else 0


def _tail(text, head: int = LLM_LOG_HEAD, tail: int = LLM_LOG_TAIL):
    """The log cut for the LLM: `head` chars from the first error line, then
    `tail` chars from the end. Short logs are returned as they are."""
    if not isinstance(text, str) or len(text) <= head + tail:
        return text
    start = _first_error_at(text)
    skipped = f"... [first {start} chars omitted for the LLM; full log in the run record]\n" if start else ""
    if len(text) - start <= head + tail:
        return skipped + text[start:]
    middle = len(text) - start - head - tail
    return (skipped + text[start:start + head]
            + f"\n... [{middle} chars omitted for the LLM; full log in the run record]\n" + text[-tail:])


def _trim_for_llm(compile_result: dict | None, sim: dict | None) -> tuple[dict | None, dict | None]:
    """Copies of the compile/sim dicts with long log fields cut to a tail."""
    compile_trimmed = None
    if compile_result:
        compile_trimmed = dict(compile_result)
        compile_trimmed["results"] = [
            {**r, "stdout": _tail(r.get("stdout")), "stderr": _tail(r.get("stderr"))}
            for r in (compile_result.get("results") or [])
        ]
    sim_trimmed = {**sim, "sim_output": _tail(sim.get("sim_output"))} if sim else None
    return compile_trimmed, sim_trimmed


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
        compile_for_llm, sim_for_llm = _trim_for_llm(compile_result, sim)
        payload = {
            "spec": project.get("spec"),
            "testbench_code": project.get("testbench_code"),
            "stage": "simulate" if (compile_result or {}).get("status") == "ok" else "compile",
            "compile": compile_for_llm,
            "sim": sim_for_llm,
        }
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        response = self.llm.invoke(
            [SystemMessage(content=SIMULATION_AGENT_SYSTEM_PROMPT), HumanMessage(content=json.dumps(payload, indent=2))],
            config={"callbacks": [handler]},
        )
        return parse_analysis_response(response.content, sim)

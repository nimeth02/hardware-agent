# agents/implementation_agent/implementation_analyst.py — tool: turn the
# batch result + parsed metrics into a verdict. A completed flow that meets
# timing (or has nothing to constrain) and fits the part is decided
# deterministically; everything else goes to the LLM analyst
# (prompts/implementation_agent_prompt.py).

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.implementation_agent.state import AGENT_NAME
from agents.json_utils import extract_json
from observability.callback import ObservabilityCallbackHandler
from prompts.implementation_agent_prompt import IMPLEMENTATION_AGENT_SYSTEM_PROMPT
from services.llm_client import get_llm

UTIL_KEYS = ("lut_pct", "ff_pct", "bram_pct", "dsp_pct")


def deterministic_verdict(batch: dict | None, metrics: dict | None) -> dict | None:
    """Verdict without an LLM when the evidence is unambiguous; None otherwise."""
    batch, m = batch or {}, metrics or {}
    if batch.get("status") != "ok":
        return None
    over = [k for k in UTIL_KEYS if (m.get(k) or 0) > 100]
    wns = m.get("wns_ns")
    timing_ok = (wns is not None and wns >= 0) or m.get("unconstrained")
    if not timing_ok or over:
        return None
    detail = f"WNS {wns:+.3f} ns" if wns is not None else "no clock constraint (timing not applicable)"
    return {
        "verdict": "pass",
        "likely_cause": None,
        "failures": [],
        "summary": f"Synthesis and implementation completed; {detail}. "
                   f"LUTs {m.get('lut_used')} ({m.get('lut_pct')}%), FFs {m.get('ff_used')} ({m.get('ff_pct')}%).",
        "suggested_fix": None,
    }


def parse_analysis_response(raw: str, batch: dict | None) -> dict:
    try:
        data = extract_json(raw)
        if isinstance(data, dict) and data.get("verdict"):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return {"verdict": "fail" if (batch or {}).get("status") == "ok" else "error", "likely_cause": None,
            "failures": ["Implementation analyst did not return valid JSON; raw reply follows.", str(raw)[:500]],
            "summary": "", "suggested_fix": None}


class ImplementationAnalyst:
    def __init__(self, llm=None):
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def analyze(self, project: dict, scripts: dict | None, batch: dict | None, metrics: dict | None) -> dict:
        verdict = deterministic_verdict(batch, metrics)
        if verdict is not None:
            return verdict
        payload = {
            "spec": project.get("spec"),
            "part": (scripts or {}).get("part"),
            "constraints": (scripts or {}).get("constraints") or f"project file {(scripts or {}).get('xdc')}",
            "batch": batch,
            "metrics": metrics,
        }
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        response = self.llm.invoke(
            [SystemMessage(content=IMPLEMENTATION_AGENT_SYSTEM_PROMPT), HumanMessage(content=json.dumps(payload, indent=2))],
            config={"callbacks": [handler]},
        )
        return parse_analysis_response(response.content, batch)

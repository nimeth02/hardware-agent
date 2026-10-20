# agents/registry.py — the agents that can be run (and tested) on their own,
# each with a name, version, entry point and an input template for the UI.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from agents.implementation_agent.implementation_agent import run_implementation
from agents.orchestrator_agent.orchestrator_agent import run_orchestration
from agents.rtl_agent.rtl_agent import run_rtl_generation
from agents.simulation_agent.simulation_agent import run_simulation
from agents.spec_analyser_agent.spec_analyzer_agent import resume_spec_analysis, run_spec_analysis
from agents.test_vector_agent.test_vector_agent import run_test_vector_generation
from agents.testbench_agent.testbench_agent import run_testbench_generation
from services.config import VIVADO_PART

_SPEC_EXAMPLE = {
    "status": "complete",
    "entity_name": "counter4",
    "description": "4-bit up counter with enable",
    "ports": [
        {"name": "clk", "direction": "input", "width": "1", "type": "clock"},
        {"name": "rst", "direction": "input", "width": "1", "type": "reset"},
        {"name": "en", "direction": "input", "width": "1", "type": "enable"},
        {"name": "count", "direction": "output", "width": "4", "type": "unsigned"},
    ],
    "clock": {"required": True, "name": "clk", "frequency_mhz": 100},
    "reset": {"required": True, "name": "rst", "type": "synchronous", "active": "high"},
    "behavior": [
        "On rst=1, count clears to 0 on the next rising clk edge.",
        "When en=1, count increments each rising clk edge.",
        "When en=0, count holds.",
        "Wraps from 15 to 0.",
    ],
    "constraints": [],
    "assumptions": [],
    "ambiguities": [],
}

_RTL_PLACEHOLDER = {"entity_name": "counter4", "code": "-- paste the RTL agent's code here", "notes": []}


@dataclass(frozen=True)
class AgentEntry:
    name: str
    version: str
    description: str
    run: Callable[..., Any]
    input_template: dict = field(default_factory=dict)
    # Agents that can pause for the user (status WAITING_FOR_USER) expose a
    # resume(session_id, answer) entry point; None for one-shot agents.
    resume: Callable[[str, str], Any] | None = None

    def invoke(self, payload: dict) -> Any:
        return self.run(**payload)

    @property
    def resumable(self) -> bool:
        return self.resume is not None


AGENTS: dict[str, AgentEntry] = {
    e.name: e
    for e in [
        AgentEntry("orchestrator", "1.0", "Requirement text -> the whole flow, autonomously: one LLM calls the other agents as tools and decides from their results what to run next",
                   run_orchestration, {"requirement_text": "4-bit up counter: clk 100 MHz, rst active-high synchronous, en input, count output, wraps from 15 to 0."}),
        AgentEntry("spec_analyzer", "2.0", "Requirement text -> structured spec; asks for clarification when the evaluator finds gaps",
                   run_spec_analysis, {"spec_text": "8-bit up counter, clk, rst active-high sync, en input, count output."},
                   resume=resume_spec_analysis),
        AgentEntry("rtl_agent", "2.0", "Approved spec -> VHDL RTL; loads skills/RTL rules, lint-fix loop until clean",
                   run_rtl_generation, {"spec": _SPEC_EXAMPLE}),
        AgentEntry("testbench_agent", "2.0", "Spec + RTL -> VHDL testbench that replays test_vectors_in/out.csv; loads skills/TESTBENCH rules, lint-fix loop until clean",
                   run_testbench_generation, {"spec": _SPEC_EXAMPLE, "rtl": _RTL_PLACEHOLDER}),
        AgentEntry("test_vector_agent", "2.0", "Spec -> golden test-vector CSV via MATLAB MCP tools; loads skills/TEST_VECTOR rules, validate-fix loop until clean",
                   run_test_vector_generation, {"spec": _SPEC_EXAMPLE}),
        AgentEntry("simulation_agent", "2.0", "Project folder (projects/<name>: src/, tb/, test_vectors_in/out.csv) -> xvhdl + xsim via the Vivado MCP; pass/fail/error verdict with analysis",
                   run_simulation, {"project": "counter4", "sim_time": "10us"}),
        AgentEntry("implementation_agent", "2.0", "Project folder (projects/<name>: src/, spec.json, optional constraints/) -> synth/place/route via the Vivado MCP; timing/utilization verdict with analysis",
                   run_implementation, {"project": "counter4", "part": VIVADO_PART}),
    ]
}


def describe_agents() -> list[dict]:
    return [
        {"name": e.name, "version": e.version, "description": e.description,
         "input_template": e.input_template, "resumable": e.resumable}
        for e in AGENTS.values()
    ]

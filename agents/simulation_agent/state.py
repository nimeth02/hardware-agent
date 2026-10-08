# agents/simulation_agent/state.py — state, statuses and actions of the
# Simulation agent.
#
# Data definitions only (no logic) so the agent, its tools, the API and the
# tests share one vocabulary.

from __future__ import annotations

from enum import Enum
from typing import TypedDict

MAX_ITERATIONS = 10              # planner decisions per run; checked in SimulationAgent.decide_node
MAX_SIM_RERUNS = 2               # extra simulation runs (with a longer sim_time) when no result marker appeared
AGENT_NAME = "simulation_agent"  # observability source tag for this agent's LLM calls
PROJECTS_DIR = "projects"        # where named projects live (repo-relative)
DEFAULT_SIM_TIME = "10us"


class SimulationStatus(str, Enum):
    """Where a run is. Set by the agent's nodes, read by the API/UI."""

    RUNNING = "RUNNING"             # graph is executing (never seen by the caller)
    COMPLETE = "COMPLETE"           # simulation ran and the testbench reported SIMULATION PASSED
    NEEDS_REVIEW = "NEEDS_REVIEW"   # a verdict exists but it is fail/error (RTL, testbench or environment problem)
    FAILED = "FAILED"               # tool/LLM exception, or limit hit before any verdict

    @property
    def terminal(self) -> bool:
        return self in (SimulationStatus.COMPLETE, SimulationStatus.NEEDS_REVIEW, SimulationStatus.FAILED)


class Action(str, Enum):
    """The only things the agent can do. Chosen by the planner LLM
    (SimulationDecider), validated by the guardrails; each value is also the
    name of the graph node that executes it."""

    LOAD_PROJECT = "load_project"               # read src/, tb/, spec.json, test vectors via the file-server MCP
    COMPILE_DESIGN = "compile_design"           # xvhdl every src/*.vhd into work_proj (Vivado MCP run_xvhdl)
    RUN_SIMULATION = "run_simulation"           # compile tb, elaborate, xsim (Vivado MCP run_simulation)
    ANALYZE_RESULT = "analyze_result"           # verdict: deterministic on pass, LLM analyst otherwise
    FINALIZE_SIMULATION = "finalize_simulation" # COMPLETE on pass, NEEDS_REVIEW on fail/error


class SimulationAgentState(TypedDict, total=False):
    """Everything LangGraph tracks for one run. Nodes return partial updates;
    SimulationAgent.summarize() turns it into the API response."""

    # --- inputs ---
    project_dir: str              # absolute path of the project folder
    sim_time: str                 # xsim run length; doubled on every rerun

    # --- working data ---
    project: dict | None          # loaded project: name, dir, src_files, tb_file, top_entity, spec, test_vectors, testbench_code
    compile: dict | None          # {status, results: [run_xvhdl result per src file], error_class}
    sim: dict | None              # last run_simulation result + sim_time + outcome (passed | failed | no_marker | error)
    sim_runs: int                 # simulations started so far; compared with 1 + MAX_SIM_RERUNS
    analysis: dict | None         # {verdict, likely_cause, failures, summary, suggested_fix}

    # --- control / reporting ---
    status: str                   # a SimulationStatus value
    iteration: int                # planner decisions taken so far; compared with MAX_ITERATIONS
    last_action: str | None       # the Action just chosen — routes decide -> that node
    decision_reason: str | None   # planner's (or guard's) one-line justification
    warnings: list[str]           # non-fatal problems (e.g. no spec.json in the project)
    error: str | None             # why the run ended in FAILED / NEEDS_REVIEW


def initial_state(project_dir: str, sim_time: str = DEFAULT_SIM_TIME) -> SimulationAgentState:
    return {
        "project_dir": project_dir,
        "sim_time": sim_time,
        "project": None,
        "compile": None,
        "sim": None,
        "sim_runs": 0,
        "analysis": None,
        "status": SimulationStatus.RUNNING.value,
        "iteration": 0,
        "last_action": None,
        "decision_reason": None,
        "warnings": [],
        "error": None,
    }

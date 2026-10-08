# agents/implementation_agent/state.py — state, statuses and actions of the
# Implementation agent.
#
# Data definitions only (no logic) so the agent, its tools, the API and the
# tests share one vocabulary.

from __future__ import annotations

from enum import Enum
from typing import TypedDict

MAX_ITERATIONS = 8                   # planner decisions per run; checked in ImplementationAgent.decide_node
AGENT_NAME = "implementation_agent"  # observability source tag for this agent's LLM calls
PROJECTS_DIR = "projects"            # where named projects live (repo-relative)
DEFAULT_CLOCK_MHZ = 100.0            # used when the spec names a clock without a frequency
BATCH_TIMEOUT_S = 1200               # run_vivado_batch limit (synth -> route of small designs takes minutes)


class ImplementationStatus(str, Enum):
    """Where a run is. Set by the agent's nodes, read by the API/UI."""

    RUNNING = "RUNNING"             # graph is executing (never seen by the caller)
    COMPLETE = "COMPLETE"           # flow completed, timing met (or unconstrained), part not over-used
    NEEDS_REVIEW = "NEEDS_REVIEW"   # a verdict exists but it is fail/error (timing, utilization, synthesis error)
    FAILED = "FAILED"               # tool/LLM exception, or limit hit before any verdict

    @property
    def terminal(self) -> bool:
        return self in (ImplementationStatus.COMPLETE, ImplementationStatus.NEEDS_REVIEW, ImplementationStatus.FAILED)


class Action(str, Enum):
    """The only things the agent can do. Chosen by the planner LLM
    (ImplementationDecider), validated by the guardrails; each value is also
    the name of the graph node that executes it."""

    LOAD_PROJECT = "load_project"                     # read src/, spec.json, constraints/ via the file-server MCP
    WRITE_SCRIPTS = "write_scripts"                   # clock XDC from the spec (unless the project has one) + synth->route TCL
    RUN_IMPLEMENTATION = "run_implementation"         # Vivado MCP run_vivado_batch, then parse impl_out/*.rpt
    ANALYZE_RESULT = "analyze_result"                 # verdict: deterministic on pass, LLM analyst otherwise
    FINALIZE_IMPLEMENTATION = "finalize_implementation"  # COMPLETE on pass, NEEDS_REVIEW on fail/error


class ImplementationAgentState(TypedDict, total=False):
    """Everything LangGraph tracks for one run. Nodes return partial updates;
    ImplementationAgent.summarize() turns it into the API response."""

    # --- inputs ---
    project_dir: str              # absolute path of the project folder
    part: str                     # target FPGA part

    # --- working data ---
    project: dict | None          # loaded project: name, dir, src_files, top_entity, spec, xdc_file
    scripts: dict | None          # {xdc, tcl, constraints, part, generated_xdc} once written
    batch: dict | None            # run_vivado_batch result: status, log_excerpt, reports_dir, error_class
    metrics: dict | None          # numbers parsed from impl_out/*.rpt (wns_ns, lut_used, ...)
    analysis: dict | None         # {verdict, likely_cause, failures, summary, suggested_fix}

    # --- control / reporting ---
    status: str                   # an ImplementationStatus value
    iteration: int                # planner decisions taken so far; compared with MAX_ITERATIONS
    last_action: str | None       # the Action just chosen — routes decide -> that node
    decision_reason: str | None   # planner's (or guard's) one-line justification
    warnings: list[str]           # non-fatal problems (e.g. no spec.json: timing left unconstrained)
    error: str | None             # why the run ended in FAILED / NEEDS_REVIEW


def initial_state(project_dir: str, part: str) -> ImplementationAgentState:
    return {
        "project_dir": project_dir,
        "part": part,
        "project": None,
        "scripts": None,
        "batch": None,
        "metrics": None,
        "analysis": None,
        "status": ImplementationStatus.RUNNING.value,
        "iteration": 0,
        "last_action": None,
        "decision_reason": None,
        "warnings": [],
        "error": None,
    }

# agents/test_vector_agent/state.py — state, statuses and actions of the
# Test Vector agent.
#
# Data definitions only (no logic) so the agent, its tools, the API and the
# tests share one vocabulary.

from __future__ import annotations

from enum import Enum
from typing import TypedDict

MAX_ITERATIONS = 6                 # planner decisions per run; checked in TestVectorAgent.decide_node
MAX_STALLED_FIXES = 2              # fix regenerations that leave the validation issues unchanged before giving up
AGENT_NAME = "test_vector_agent"   # observability source tag for this agent's LLM calls
SKILLS_DIR = "skills/TEST_VECTOR"  # golden-model/CSV rule files, loaded through the file-server MCP


class TestVectorStatus(str, Enum):
    """Where a run is. Set by the agent's nodes, read by the API/UI."""

    RUNNING = "RUNNING"             # graph is executing (never seen by the caller)
    COMPLETE = "COMPLETE"           # validator found no issues; CSV is final
    NEEDS_REVIEW = "NEEDS_REVIEW"   # stopped (iteration limit / fixes not converging) with a CSV that still has issues
    FAILED = "FAILED"               # LLM/tool error, or limit hit with no result at all

    @property
    def terminal(self) -> bool:
        return self in (TestVectorStatus.COMPLETE, TestVectorStatus.NEEDS_REVIEW, TestVectorStatus.FAILED)


class Action(str, Enum):
    """The only things the agent can do. Chosen by the planner LLM
    (TestVectorDecider), validated by the guardrails; each value is also the
    name of the graph node that executes it."""

    LOAD_SKILLS = "load_skills"             # read skills/TEST_VECTOR/* via the file-server MCP
    GENERATE_VECTORS = "generate_vectors"   # LLM writes/runs the MATLAB golden model via the MATLAB MCP (TestVectorGenerator)
    VALIDATE_VECTORS = "validate_vectors"   # deterministic CSV checks (test_vector_validator)
    FINALIZE_VECTORS = "finalize_vectors"   # mark COMPLETE — allowed only with zero validation issues


class TestVectorAgentState(TypedDict, total=False):
    """Everything LangGraph tracks for one run. Nodes return partial updates;
    TestVectorAgent.summarize() turns it into the API response."""

    # --- inputs ---
    spec: dict                    # the approved specification
    run_dir: str                  # folder the MATLAB script and CSV are written to (project_dir for the MCP tools)

    # --- working data ---
    skills: dict[str, str]        # skill file name -> content, injected into the generator prompt
    skills_loaded: bool           # True once load_skills ran (even if the library was empty)
    result: dict | None           # generator's JSON summary {script, input_csv, output_csv, input_columns,
                                  # output_columns, num_vectors, scenarios, notes}
    validation: dict | None       # validator result for the CSV: {csv, num_vectors, columns, issues}; None until validated

    # --- stall detection (guardrail) ---
    validation_signature: str | None   # fingerprint of the issues after the last validation
    stall_count: int                   # fix regenerations in a row that left the issues unchanged

    # --- control / reporting ---
    status: str                   # a TestVectorStatus value
    iteration: int                # planner decisions taken so far; compared with MAX_ITERATIONS
    last_action: str | None       # the Action just chosen — routes decide -> that node
    decision_reason: str | None   # planner's (or guard's) one-line justification
    warnings: list[str]           # non-fatal problems (e.g. skills could not be loaded)
    error: str | None             # why the run ended in FAILED / NEEDS_REVIEW


def initial_state(spec: dict, run_dir: str) -> TestVectorAgentState:
    return {
        "spec": spec,
        "run_dir": run_dir,
        "skills": {},
        "skills_loaded": False,
        "result": None,
        "validation": None,
        "validation_signature": None,
        "stall_count": 0,
        "status": TestVectorStatus.RUNNING.value,
        "iteration": 0,
        "last_action": None,
        "decision_reason": None,
        "warnings": [],
        "error": None,
    }

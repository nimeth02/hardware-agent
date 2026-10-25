# agents/orchestrator_agent/state.py — state, statuses and stages of the
# orchestrator (the manager AI that runs the other agents as tools).
#
# Data definitions only (no logic), in the same shape as the RTL agent's
# state.py: what the run started from, what the helpers produced, how each
# helper ended, and the manager's own control / error lines. Nothing reads
# or writes this yet; it is the vocabulary the LangChain agent will track.

from __future__ import annotations

from enum import Enum
from typing import TypedDict

from langchain.agents import AgentState

MAX_ITERATIONS = 25             # tool calls the manager may make in one run before it must stop
MAX_RETRIES_PER_STAGE = 2       # times one helper may be re-run after a non-COMPLETE result
AGENT_NAME = "orchestrator"     # observability source tag for the manager's LLM calls


class OrchestratorStatus(str, Enum):
    """Where the whole run is. Set by the manager, read by the API/UI."""

    RUNNING = "RUNNING"             # the manager is still deciding / calling helpers
    COMPLETE = "COMPLETE"           # simulation passed and implementation verdict is pass
    NEEDS_REVIEW = "NEEDS_REVIEW"   # stopped with something produced that a human must look at
    FAILED = "FAILED"               # a step could not be made to succeed, or the run crashed / hit a limit

    @property
    def terminal(self) -> bool:
        return self in (OrchestratorStatus.COMPLETE, OrchestratorStatus.NEEDS_REVIEW, OrchestratorStatus.FAILED)


class Stage(str, Enum):
    """The helpers the manager can run. Each value is also the name of the
    tool the LLM calls, and the key of its record in `stages`."""

    ANALYZE_SPEC = "analyze_spec"                   # requirement text -> spec
    GENERATE_RTL = "generate_rtl"                   # spec -> RTL
    GENERATE_TESTBENCH = "generate_testbench"       # spec + RTL -> testbench
    GENERATE_TEST_VECTORS = "generate_test_vectors" # spec -> golden CSVs via MATLAB
    WRITE_PROJECT = "write_project"                 # spec + RTL + testbench (+ CSVs) -> project folder
    SIMULATE = "simulate"                           # project folder -> simulation verdict
    IMPLEMENT = "implement"                         # project folder -> implementation verdict

    @property
    def produces(self) -> str:
        """The artifact field this stage fills in the state."""
        return PRODUCES[self]


# stage -> the artifact field it fills (None = the whole helper answer is kept there)
PRODUCES = {
    Stage.ANALYZE_SPEC: "spec",
    Stage.GENERATE_RTL: "rtl",
    Stage.GENERATE_TESTBENCH: "testbench",
    Stage.GENERATE_TEST_VECTORS: "test_vectors",
    Stage.WRITE_PROJECT: "project_dir",
    Stage.SIMULATE: "simulation",
    Stage.IMPLEMENT: "implementation",
}

# simulate / implement analysis "likely_cause" -> the stage whose work is blamed
# ("environment", "constraints" and None blame nobody)
BLAMED_STAGE = {"rtl": Stage.GENERATE_RTL, "testbench": Stage.GENERATE_TESTBENCH, "spec": Stage.ANALYZE_SPEC}


class StageRecord(TypedDict, total=False):
    """How one helper went, kept per stage so the manager (and the UI) can
    see every agent's own status and errors, not just the final artifact."""

    status: str | None            # the helper's own status: COMPLETE / NEEDS_REVIEW / FAILED / WAITING_FOR_USER
    attempts: int                 # how many times this helper has been run in this run
    error: str | None             # the helper's "error" text from its last run, if any
    attempt_history: list[dict]   # one entry per run of this helper, oldest first: {attempt, status, error, issues}
    issues: list | None           # the helper's remaining problems (lint issues, analysis), if any
    result: dict | None           # the helper's full last answer (the artifact is also copied to its field)


class OrchestratorState(AgentState, total=False):
    """Everything LangGraph tracks for one orchestration run. Inherits
    `messages` (the chat between the manager LLM and its tools) from
    LangChain's AgentState; the rest mirrors the RTL agent's state layout."""

    # --- inputs ---
    requirement_text: str         # what the user asked for
    requirement_id: str           # folder-safe id of this run: names projects/<requirement_id>/

    # --- artifacts (what the helpers produced; each overwritten by that helper's latest run) ---
    spec: dict | None             # analyze_spec -> the "spec" object
    rtl: dict | None              # generate_rtl -> {entity_name, code, notes}
    testbench: dict | None        # generate_testbench -> {entity_name, code, ...}
    test_vectors: dict | None     # generate_test_vectors -> its whole answer (run_dir + CSV names)
    project_dir: str | None       # write_project -> the project folder
    simulation: dict | None       # simulate -> its whole answer (verdict + analysis)
    implementation: dict | None   # implement -> its whole answer (verdict + metrics)

    # --- per-helper control (one record per Stage value that has run) ---
    stages: dict[str, StageRecord]

    # --- manager control / reporting ---
    status: str                   # an OrchestratorStatus value
    iteration: int                # tool calls made so far; compared with MAX_ITERATIONS
    current_stage: str | None     # the Stage just run (or being run)
    completed_stages: list[str]   # stages that ended COMPLETE, in order, never cleared
    decision_reason: str | None   # the manager's one-line reason for its last choice
    warnings: list[str]           # non-fatal problems (e.g. test vectors skipped)
    errors: list[str]             # every error seen during the run, in order, never cleared
    error: str | None             # why the run ended in FAILED / NEEDS_REVIEW
    summary: str | None           # the manager's final summary for the human
    next_steps: str | None        # what a human should do next, or None


def initial_state(requirement_text: str, requirement_id: str) -> OrchestratorState:
    return {
        "messages": [],
        "requirement_text": requirement_text,
        "requirement_id": requirement_id,
        "spec": None,
        "rtl": None,
        "testbench": None,
        "test_vectors": None,
        "project_dir": None,
        "simulation": None,
        "implementation": None,
        "stages": {},
        "status": OrchestratorStatus.RUNNING.value,
        "iteration": 0,
        "current_stage": None,
        "completed_stages": [],
        "decision_reason": None,
        "warnings": [],
        "errors": [],
        "error": None,
        "summary": None,
        "next_steps": None,
    }

# agents/test_vector_agent/test_vector_generator_agent/state.py — limits,
# bookkeeping and result shape of one generator attempt (the LLM + MATLAB
# tool loop that writes gen_test_vectors.m and the two CSVs).
#
# Data definitions only (no logic) so the generator, the MATLAB guard and
# the tests share one vocabulary. The agent-level state (statuses, actions,
# iteration counts across attempts) is in ../state.py.

from __future__ import annotations

from typing import TypedDict

from agents.test_vector_agent.test_vector_validator import INPUT_CSV, OUTPUT_CSV

MAX_AGENT_STEPS = 30       # LLM steps (tool calls + replies) per attempt; checked by the tool loop's recursion limit
MAX_RUN_FAILURES = 3       # failed MATLAB runs per attempt before the attempt gives up (RunAttemptsExceeded)
SCRIPT_NAME = "gen_test_vectors.m"   # the golden-model script the LLM writes in run_dir


class RunAttemptsExceeded(Exception):
    """Raised by the MATLAB guard when an attempt has used up MAX_RUN_FAILURES."""


class LastRun(TypedDict):
    """Outcome of the most recent run_matlab_script call."""

    status: str   # "ok" | "error" (the generator reports "never_ran" when no run happened)
    error: str    # MATLAB's error text (truncated), "" on success


class RunState(TypedDict):
    """Bookkeeping shared between the guarded MATLAB tools and the attempt."""

    failures: int             # failed MATLAB runs so far, compared with MAX_RUN_FAILURES
    dirty: bool               # True once the script changed since its last run; blocks re-running an unchanged script
    scripts: set[str]         # script stems the LLM wrote this attempt; run_matlab_script accepts only these
    last_run: LastRun | None  # None until run_matlab_script is called


def new_run_state() -> RunState:
    return {"failures": 0, "dirty": True, "scripts": set(), "last_run": None}


def failure_result(reason: str, detail: str = "") -> dict:
    """The generator's JSON summary shape, filled in for an attempt that
    produced nothing usable. `notes` carries the reason (and detail)."""
    return {
        "script": SCRIPT_NAME,
        "input_csv": INPUT_CSV,
        "output_csv": OUTPUT_CSV,
        "input_columns": [],
        "output_columns": [],
        "num_vectors": 0,
        "scenarios": [],
        "notes": [reason] + ([detail] if detail else []),
    }

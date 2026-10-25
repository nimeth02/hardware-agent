# agents/rtl_agent/state.py — state, statuses and actions of the RTL agent.
#
# Data definitions only (no logic) so the agent, its tools, the API and the
# tests share one vocabulary.

from __future__ import annotations

from enum import Enum
from typing import TypedDict

MAX_ITERATIONS = 10            # decider decisions per run; checked in RTLAgent.decide_node
MAX_STALLED_FIXES = 2           # fix regenerations that leave the lint issues unchanged before giving up
AGENT_NAME = "rtl_agent"        # observability source tag for this agent's LLM calls
SKILLS_DIR = "skills/RTL"       # design-rule files, loaded through the file-server MCP


class RTLStatus(str, Enum):
    """Where a run is. Set by the agent's nodes, read by the API/UI."""

    RUNNING = "RUNNING"             # graph is executing (never seen by the caller)
    COMPLETE = "COMPLETE"           # linter found no issues; design is final
    NEEDS_REVIEW = "NEEDS_REVIEW"   # stopped (iteration limit / fixes not converging) with a design that still has issues
    FAILED = "FAILED"               # LLM/tool error, or limit hit with no design at all

    @property
    def terminal(self) -> bool:
        return self in (RTLStatus.COMPLETE, RTLStatus.NEEDS_REVIEW, RTLStatus.FAILED)


class Action(str, Enum):
    """The only things the agent can do. Chosen by the decider LLM
    (RTLDecider), validated by the guardrails; each value is also the name
    of the graph node that executes it."""

    LOAD_SKILLS = "load_skills"       # read skills/RTL/* via the file-server MCP (RTLSkillLoader)
    GENERATE_RTL = "generate_rtl"     # LLM writes or fixes the design (RTLGenerator)
    LINT_RTL = "lint_rtl"             # deterministic checks + xvhdl compile (rtl_linter.lint_rtl)
    FINALIZE_RTL = "finalize_rtl"     # mark COMPLETE — allowed only with zero lint issues


class RTLAgentState(TypedDict, total=False):
    """Everything LangGraph tracks for one run. Nodes return partial updates;
    dto.agent_result_dto() turns it into the API response."""

    # --- inputs ---
    spec: dict                    # the approved specification
    requirement_id: str | None    # the orchestrator's run this call belongs to (None when called on its own)
    attempt: int                  # which call this is from the orchestrator for that requirement: 1 = first
    attempt_history: list[dict]   # how the earlier calls went, oldest first: {attempt, status, error, issues}
    previous_rtl: dict | None     # the design the previous call delivered (revised on a retry, not rewritten)

    # --- working data ---
    skills: dict[str, str]        # skill file name -> content, injected into the generator prompt
    skills_loaded: bool           # True once load_skills ran (even if the library was empty)
    rtl: dict | None              # latest design {entity_name, code, notes}
    lint: dict | None             # linter result for `rtl`: {compiler, issues}; None until linted

    # --- stall detection (guardrail) ---
    lint_signature: str | None    # fingerprint of the issues after the last lint
    stall_count: int              # fix regenerations in a row that left the issues unchanged

    # --- control / reporting ---
    status: str                   # an RTLStatus value
    iteration: int                # decider decisions taken so far; compared with MAX_ITERATIONS
    last_action: str | None       # the Action just chosen — routes decide -> that node
    decision_reason: str | None   # decider's (or guard's) one-line justification
    warnings: list[str]           # non-fatal problems (e.g. skills could not be loaded)
    error: str | None             # why the run ended in FAILED / NEEDS_REVIEW


def initial_state(spec: dict, requirement_id: str | None = None, attempt: int = 1,
                  attempt_history: list[dict] | None = None, previous_rtl: dict | None = None) -> RTLAgentState:
    return {
        "spec": spec,
        "requirement_id": requirement_id,
        "attempt": attempt,
        "attempt_history": attempt_history or [],
        "previous_rtl": previous_rtl,
        "skills": {},
        "skills_loaded": False,
        "rtl": None,
        "lint": None,
        "lint_signature": None,
        "stall_count": 0,
        "status": RTLStatus.RUNNING.value,
        "iteration": 0,
        "last_action": None,
        "decision_reason": None,
        "warnings": [],
        "error": None,
    }

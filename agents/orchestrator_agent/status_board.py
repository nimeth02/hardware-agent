# agents/orchestrator_agent/status_board.py — the short "where we are" board
# the manager AI reads at the end of its rules on every one of its LLM calls.
#
# It is rebuilt from the notebook (state.py) each call, so it is always
# current. Only small facts go in: each stage's status and attempt count,
# what is ready, how many tool calls are used. Never the RTL, testbench,
# spec or logs: they are already in the chat as tool answers and would cost
# thousands of tokens per call.

from __future__ import annotations

import json

from langchain.agents.middleware import ModelRequest, before_model, dynamic_prompt
from langchain_core.messages import AIMessage, ToolMessage

from agents.orchestrator_agent.state import MAX_ITERATIONS, PRODUCES, OrchestratorState, Stage


def stage_line(name: str, record: dict) -> str:
    """One board line for a stage that has run.

    `name` is the stage, e.g. "generate_rtl". `record` is its notebook entry:
      record["status"]           "COMPLETE" / "NEEDS_REVIEW" / "FAILED" / ...
      record["attempts"]         how many times it has run, e.g. 2
      record["error"]            its last error text, or None
      record["attempt_history"]  one dict per run; the last one may have "likely_cause"
    Wanted shape (one line, no newline), for example:
      "generate_rtl: NEEDS_REVIEW, 2 attempts, error: lint issues remain"
      "write_project: COMPLETE, 20 attempts"
    """
    attempts = int(record.get("attempts") or 0)
    line = f"{name}: {record.get('status')}, {attempts} attempt{'' if attempts == 1 else 's'}"
    error = record.get("error")
    if error:
        short = " ".join(str(error).split())
        line += ", error: " + (short if len(short) <= 120 else short[:119] + "…")
    history = record.get("attempt_history") or []
    last = history[-1] if history and isinstance(history[-1], dict) else {}
    if last.get("likely_cause"):
        line += f", blamed: {last['likely_cause']}"
    return line


def status_board(state: dict) -> str:
    """The whole board as text, from the notebook."""
    stages = state.get("stages") or {}
    ran = [stage_line(s.value, stages[s.value]) for s in Stage if s.value in stages]
    not_run = [s.value for s in Stage if s.value not in stages]
    ready = [field for field in PRODUCES.values() if state.get(field) is not None]
    used = sum(1 for m in state.get("messages") or [] if isinstance(m, ToolMessage))
    lines = [f"WHERE WE ARE (run {state.get('requirement_id')}, tool calls used: {used} of {MAX_ITERATIONS})"]
    lines += [f"  {line}" for line in ran] or ["  nothing has run yet"]
    if not_run:
        lines.append("  not run yet: " + ", ".join(not_run))
    lines.append("  ready for the next tools: " + (", ".join(ready) or "nothing yet"))
    return "\n".join(lines)


def is_done(state: dict) -> bool:
    """The finish rule: simulate and implement both reported verdict "pass"."""
    def verdict(answer: dict | None) -> str | None:
        answer = answer or {}
        return answer.get("verdict") or (answer.get("analysis") or {}).get("verdict")
    return verdict(state.get("simulation")) == "pass" and verdict(state.get("implementation")) == "pass"


@before_model(state_schema=OrchestratorState, can_jump_to=["end"])
def finish_when_done(state: OrchestratorState, runtime) -> dict | None:
    """Safety rule, checked before every manager LLM call: once the run is done
    the manager is not asked again; the code writes the final COMPLETE answer
    itself and ends the loop."""
    if not is_done(state):
        return None
    metrics = (state.get("implementation") or {}).get("metrics") or {}
    final = {
        "status": "COMPLETE",
        "stop_reason": "Simulation passed and the implementation verdict is pass; "
                       "the finish rule ended the run without asking the manager again.",
        "summary": f"simulation pass; implementation pass; WNS {metrics.get('wns_ns')} ns; "
                   f"LUT {metrics.get('lut_pct')}%; FF {metrics.get('ff_pct')}%",
        "next_steps": None,
    }
    return {"messages": [AIMessage(content=json.dumps(final))], "jump_to": "end"}


def with_status_board(system_prompt: str):
    """The manager's rules with the current board appended, rebuilt before
    every LLM call (a LangChain middleware for create_agent)."""
    @dynamic_prompt
    def rules_and_board(request: ModelRequest) -> str:
        return f"{system_prompt}\n\n{status_board(request.state)}"
    return rules_and_board

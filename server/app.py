# server/app.py — FastAPI backend: spec-analysis query endpoint,
# spec -> human review -> RTL -> test vectors -> testbench pipeline, and the observability API.
#
# Serves the static HTML/JS UI from server/static/ and exposes:
#   POST /api/analyze                  run the spec analyzer alone, return the result
#   POST /api/pipeline/start           start the spec->RTL->test vectors->testbench pipeline; pauses for human review
#   POST /api/pipeline/{id}/resume     submit the reviewed/edited spec, run RTL, test vectors and testbench generation
#   GET  /api/traces                   list recorded LLM call traces (newest first)
#   GET  /api/traces/{id}              full detail for one trace
#   GET  /api/agent-runs/{id}/errors   every error a run recorded, by requirement and step
#   GET  /api/steps/{run_id}           per-node step timings for a run (pipeline thread_id or analyze run)
#
# Run with: uvicorn server.app:app --reload

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Callable

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.pipeline_agent.pipeline import resume_pipeline, start_pipeline
from agents.registry import AGENTS, describe_agents
from agents.spec_analyser_agent.spec_analyzer_agent import resume_spec_analysis, run_spec_analysis
from observability.agent_run import STOP_MESSAGE, RunStatus, RunStopped, observe_run, request_stop
from observability.run_store import store as run_store
from observability.step_tracer import observe_steps
from observability.storage import get_trace, list_steps, list_traces
from services.config import REPO_ROOT, require_llm_model

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Hardware Agent")


class AnalyzeRequest(BaseModel):
    spec_text: str


class AnalyzeResponse(BaseModel):
    session_id: str
    status: str                      # RUNNING | WAITING_FOR_USER | COMPLETE | NEEDS_REVIEW | FAILED
    pending_question: str | None
    spec: dict | None
    issues: list[dict]
    suggestions: list[str]
    user_feedback: list[str]
    iteration: int
    decision_reason: str | None
    error: str | None
    run_id: str
    timings: list[dict]


class AnalyzeAnswerRequest(BaseModel):
    answer: str


@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
    if not req.spec_text.strip():
        raise HTTPException(status_code=400, detail="spec_text must not be empty")
    run_id = str(uuid.uuid4())
    with observe_steps(run_id) as steps:
        result = run_spec_analysis(req.spec_text)
    return AnalyzeResponse(**result, run_id=run_id, timings=steps.steps)


@app.post("/api/analyze/{session_id}/answer", response_model=AnalyzeResponse)
def analyze_answer(session_id: str, req: AnalyzeAnswerRequest) -> AnalyzeResponse:
    """Answer the spec analyzer's pending question and resume that session."""
    if not req.answer.strip():
        raise HTTPException(status_code=400, detail="answer must not be empty")
    run_id = str(uuid.uuid4())
    try:
        with observe_steps(run_id) as steps:
            result = resume_spec_analysis(session_id, req.answer)
    except ValueError as e:  # unknown session, or not waiting for an answer
        raise HTTPException(status_code=409, detail=str(e)) from e
    return AnalyzeResponse(**result, run_id=run_id, timings=steps.steps)


class PipelineStartRequest(BaseModel):
    requirement_text: str


class PipelineResumeRequest(BaseModel):
    spec: dict


@app.post("/api/pipeline/start")
def pipeline_start(req: PipelineStartRequest) -> dict:
    if not req.requirement_text.strip():
        raise HTTPException(status_code=400, detail="requirement_text must not be empty")
    thread_id = str(uuid.uuid4())
    with observe_steps(thread_id):
        result = start_pipeline(thread_id, req.requirement_text)
    return {**result, "timings": list_steps(thread_id)}


@app.post("/api/pipeline/{thread_id}/resume")
def pipeline_resume(thread_id: str, req: PipelineResumeRequest) -> dict:
    with observe_steps(thread_id):
        result = resume_pipeline(thread_id, req.spec)
    return {**result, "timings": list_steps(thread_id)}


@app.get("/api/steps/{run_id}")
def get_steps(run_id: str) -> list[dict]:
    return list_steps(run_id)


# ── Single-agent lab: run one agent on its own and inspect everything it did ──

class AgentRunRequest(BaseModel):
    input: dict


# xsim's `run <time>` needs a plain decimal plus unit ("10us", "2.5 ms"); other
# forms (scientific notation, bare numbers) hang the simulator. Same pattern as
# the UI (app.js) and the Vivado MCP server.
_SIM_TIME_RE = re.compile(r"^\s*\d+(\.\d+)?\s*(fs|ps|ns|us|ms|s)\s*$", re.IGNORECASE)


def _validate_sim_time(payload: dict) -> None:
    value = payload.get("sim_time")
    if value is None or value == "":
        return  # the agent applies its default (10us)
    if not isinstance(value, str) or not _SIM_TIME_RE.match(value):
        raise HTTPException(status_code=422, detail=f"Invalid sim_time {value!r}: expected a decimal number "
                            "followed by a unit (fs, ps, ns, us, ms or s), e.g. '10us' or '2.5ms'.")


@app.get("/api/agents")
def get_agents() -> list[dict]:
    return describe_agents()


def _execute_agent(entry, payload: dict, run_id: str, resume: bool, call: Callable[[], object]) -> None:
    """Background job: everything (steps, LLM/tool calls, result, errors) is
    recorded by observe_run; the client follows it over SSE."""
    try:
        with observe_run(entry.name, payload, agent_version=entry.version, model=require_llm_model(),
                         run_id=run_id, resume=resume) as run:
            run.set_result(call())
    except (Exception, RunStopped):
        pass  # already recorded as run_failed / run_stopped


@app.post("/api/agents/{agent_name}/run")
def run_single_agent(agent_name: str, req: AgentRunRequest, background: BackgroundTasks) -> dict:
    """Start the agent and return at once; follow /api/agent-runs/{run_id}/stream."""
    entry = AGENTS.get(agent_name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown agent '{agent_name}'")
    _validate_sim_time(req.input)
    run_id = str(uuid.uuid4())
    # Create the row now so a client can subscribe before the work starts.
    run_store.create_run(run_id, entry.name, entry.version, require_llm_model(), req.input)
    background.add_task(_execute_agent, entry, req.input, run_id, False, lambda: entry.invoke(req.input))
    return {"run_id": run_id, "status": "running"}


class AgentResumeRequest(BaseModel):
    session_id: str
    answer: str


@app.post("/api/agents/{agent_name}/resume")
def resume_single_agent(agent_name: str, req: AgentResumeRequest, background: BackgroundTasks) -> dict:
    """Answer a WAITING_FOR_USER agent and continue the same session — and the
    same run, so the timeline stays continuous across the pause."""
    entry = AGENTS.get(agent_name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown agent '{agent_name}'")
    if not entry.resumable:
        raise HTTPException(status_code=400, detail=f"agent '{agent_name}' does not support resume")
    if not req.answer.strip():
        raise HTTPException(status_code=400, detail="answer must not be empty")
    payload = {"session_id": req.session_id, "answer": req.answer}
    run_id = run_store.find_run_by_session(entry.name, req.session_id)
    resume = run_id is not None
    if resume:
        run_store.reopen_run(run_id)
    else:  # session not started through this API: record the continuation as a new run
        run_id = str(uuid.uuid4())
        run_store.create_run(run_id, entry.name, entry.version, require_llm_model(), payload)
    background.add_task(_execute_agent, entry, payload, run_id, resume,
                        lambda: entry.resume(req.session_id, req.answer))
    return {"run_id": run_id, "status": "running"}


@app.get("/api/agent-runs/{run_id}/stream")
async def stream_agent_run(run_id: str) -> StreamingResponse:
    """Server-Sent Events: one `step` event per recorded agent event as it
    happens, then a `done` event with the run's final status and result."""

    async def events():
        sent, waited = 0, 0.0
        while True:
            run = run_store.get_run(run_id)
            if run is None:
                if waited > 10:
                    yield 'event: error\ndata: {"detail": "run not found"}\n\n'
                    return
                await asyncio.sleep(0.3)
                waited += 0.3
                continue
            for e in run["events"][sent:]:
                yield f"event: step\ndata: {json.dumps(e, default=str)}\n\n"
            sent = len(run["events"])
            if run["status"] in ("completed", "failed", "stopped"):
                summary = {k: run[k] for k in ("id", "status", "error", "duration_ms", "final_result")}
                yield f"event: done\ndata: {json.dumps(summary, default=str)}\n\n"
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/agent-runs/{run_id}/stop")
def stop_agent_run(run_id: str) -> dict:
    """Ask a running agent to stop. Cooperative (threads cannot be killed):
    the run ends at its next step boundary — LLM call, tool call or graph
    node — so a tool already in flight (e.g. a simulator run inside an MCP
    server) finishes first. The run then ends with status "stopped"."""
    run = run_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run["status"] != RunStatus.RUNNING:
        raise HTTPException(status_code=409, detail=f"run is already {run['status']}")
    if request_stop(run_id):
        return {"run_id": run_id, "status": "stopping"}
    # Marked running in the store but not live in this process (the server was
    # restarted while it ran): nothing to interrupt, close the record now.
    run_store.finish_run(run_id, RunStatus.STOPPED, f"{STOP_MESSAGE} (run was no longer active on the server)", None)
    return {"run_id": run_id, "status": "stopped"}


class AnswerRequest(BaseModel):
    answer: str = ""


@app.get("/api/agent-runs/{run_id}/question")
def get_agent_run_question(run_id: str) -> dict:
    """The question a running orchestrator is waiting on, or {"pending": null}."""
    from agents.orchestrator_agent.human_input import get_pending
    pending = get_pending(run_id)
    return {"pending": pending}


@app.post("/api/agent-runs/{run_id}/answer")
def answer_agent_run(run_id: str, req: AnswerRequest) -> dict:
    """Hand the user's answer to a run that is waiting for one."""
    from agents.orchestrator_agent.human_input import submit_answer
    answer = req.answer.strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer must not be empty")
    if not submit_answer(run_id, answer):
        raise HTTPException(status_code=409, detail="this run is not waiting for an answer")
    return {"run_id": run_id, "status": "running"}


@app.get("/api/agent-runs")
def get_agent_runs(agent_name: str | None = None, limit: int = 50) -> list[dict]:
    runs = run_store.list_runs(agent_name=agent_name, limit=limit)
    if runs:
        # list_runs drops the saved report, so read stop_reason for these runs separately.
        from observability.run_store import _loads
        marks = ",".join("?" for _ in runs)
        with run_store._connect() as conn:
            rows = conn.execute(f"SELECT id, final_result FROM agent_runs WHERE id IN ({marks})",
                                [r["id"] for r in runs]).fetchall()
        reasons = {}
        for row in rows:
            fr = _loads(row["final_result"])
            reasons[row["id"]] = fr.get("stop_reason") if isinstance(fr, dict) else None
        for r in runs:
            r["stop_reason"] = reasons.get(r["id"])
    return runs


@app.get("/api/agent-runs/{run_id}")
def get_agent_run(run_id: str) -> dict:
    run = run_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


# ── Errors for the FPGA engineer: every problem a run recorded, by requirement and step ──

STAGE_LABELS = {
    "analyze_spec": "Spec analysis", "generate_test_vectors": "Test vectors", "generate_rtl": "RTL",
    "generate_testbench": "Testbench", "write_project": "Writing the project folder",
    "simulate": "Simulation", "implement": "Implementation (turning the RTL into a chip layout)",
}
STAGE_ORDER = list(STAGE_LABELS)


def _text(item) -> str:
    """One problem as a single readable line (lint issues are {code, detail} dicts)."""
    if isinstance(item, dict):
        code, detail = item.get("code"), item.get("detail") or item.get("message") or item.get("text")
        if code and detail:
            return f"{code}: {detail}"
        return str(detail or code or json.dumps(item, default=str, ensure_ascii=False))
    return str(item)


LINT_STAGES = {"generate_rtl": "RTL lint", "generate_testbench": "Testbench lint"}
_LOC_RE = re.compile(r"\[([^\[\]]+?\.[A-Za-z0-9]+):(\d+)\]")      # [llr_demap16.vhd:88]
_LINE_RE = re.compile(r"^(?:[\w ]+: )?line (\d+):")                    # "line 119: ..." (static checks)
_HIDDEN_RE = re.compile(r"\d+ follow-on error\(s\) hidden")


def _lint_item(item: dict) -> dict:
    """One lint result {code, detail} plus where it points (file:line) and whether
    it is only a note (the 'N follow-on error(s) hidden' line is not an error)."""
    code, detail = str(item.get("code") or "lint"), str(item.get("detail") or item.get("message") or "")
    m, file, line = _LOC_RE.search(detail), None, None
    if m:
        file, line = m.group(1), int(m.group(2))
    else:
        m = _LINE_RE.match(detail)
        if m:
            line = int(m.group(1))
    return {"code": code, "detail": detail, "file": file, "line": line,
            "location": (f"{file}:{line}" if file else f"line {line}") if line else None,
            "note": bool(_HIDDEN_RE.search(detail))}


def _is_lint(item) -> bool:
    return isinstance(item, dict) and bool(item.get("code")) and bool(item.get("detail"))


def _issue_item(item) -> dict:
    """One validator/analyst problem as {code, detail} (plain strings get the code 'problem')."""
    if isinstance(item, dict):
        return {"code": str(item.get("code") or "problem"),
                "detail": str(item.get("detail") or item.get("message") or item.get("text") or json.dumps(item, default=str, ensure_ascii=False))}
    return {"code": "problem", "detail": str(item)}


def _output_details(out: dict | None) -> dict:
    """What one helper run recorded about why it ended as it did: the lint list,
    the validator's/analyst's issue list, the AI's own explanation and fix."""
    out = out or {}
    analysis = out.get("analysis") if isinstance(out.get("analysis"), dict) else {}
    validation = out.get("validation") if isinstance(out.get("validation"), dict) else {}
    lint = out.get("lint") if isinstance(out.get("lint"), dict) else {}
    issues = [i for src in (validation.get("issues"), out.get("issues"), analysis.get("failures")) for i in (src or [])]
    return {"lint": list(lint.get("issues") or []), "issues": issues,
            "explanation": analysis.get("summary") or out.get("decision_reason") or None,
            "suggested_fix": analysis.get("suggested_fix") or None}


def _stage_outputs(events: list) -> dict:
    """Each helper's recorded answers (agent_output events), oldest first, by stage."""
    by = {}
    for e in events:
        d = e.get("data") or {}
        if e.get("event_type") == "agent_output" and isinstance(d.get("output"), dict):
            by.setdefault(d.get("stage"), []).append(d["output"])
    return by


def _output_for(outputs: list, attempts: list, idx: int, number) -> dict | None:
    """The recorded answer that belongs to attempt `idx`: by its attempt number when
    the answers carry one, else by position when both lists have the same length."""
    by_number = [o for o in outputs if o.get("attempt") is not None and o.get("attempt") == number]
    if by_number:
        return by_number[-1]
    return outputs[idx] if len(outputs) == len(attempts) else None


def _fix_round_records(run: dict) -> list[tuple[int, str, dict]]:
    """Every record the RTL/testbench planner was shown, with its place in the run.
    The planner's last message is JSON {spec, lint:{compiler, issues}, iteration,
    max_iterations, has_rtl | has_testbench}; the lint result of each inner fix
    round lives only there. Returns (step, stage, record), oldest first."""
    found = []
    for c in run.get("llm_calls") or []:
        msgs = c.get("input")
        if not isinstance(msgs, list) or not msgs or not isinstance(msgs[-1], dict):
            continue
        content = msgs[-1].get("content")
        if not isinstance(content, str) or "max_iterations" not in content:
            continue
        try:
            rec = json.loads(content)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("lint"), dict):
            continue
        stage = "generate_rtl" if "has_rtl" in rec else "generate_testbench" if "has_testbench" in rec else None
        if stage:
            found.append((c.get("step") or 0, stage, rec))
    return found


def _rounds_from(records: list[dict]) -> list[dict]:
    """One entry per inner iteration: {round, max, compiler, lint:[...]} with the
    same error never listed twice inside a round."""
    by_round: dict = {}
    for rec in records:
        lint = rec["lint"]
        it = rec.get("iteration")
        entry = by_round.setdefault(it, {"round": it, "max": rec.get("max_iterations"),
                                         "compiler": lint.get("compiler"), "lint": [], "_seen": set()})
        for i in lint.get("issues") or []:
            if _is_lint(i) and i["detail"] not in entry["_seen"]:
                entry["_seen"].add(i["detail"])
                entry["lint"].append(_lint_item(i))
    out = [e for e in by_round.values() if e["lint"]]
    for e in out:
        del e["_seen"]
    return sorted(out, key=lambda e: (e["round"] is None, e["round"] or 0))


def _rounds_by_attempt(run: dict, events: list, stages: dict) -> dict:
    """{stage: {attempt number: [rounds]}} - each planner record is placed in the
    orchestrator attempt whose start/finish events surround it."""
    records = _fix_round_records(run)
    if not records:
        return {}
    out: dict = {}
    for stage in ("generate_rtl", "generate_testbench"):
        marks = sorted(((e.get("step") or 0, e["event_type"], e.get("data") or {}) for e in events
                        if e.get("event_type") in ("agent_input", "agent_output") and (e.get("data") or {}).get("stage") == stage),
                       key=lambda m: m[0])
        windows, start, last = [], 0, 0
        for step, kind, d in marks:
            if kind == "agent_input":
                start = step
            else:
                att = (d.get("output") or {}).get("attempt")
                att = att if isinstance(att, int) else last + 1
                windows.append((start, step, att)); last = att; start = step
        if marks and marks[-1][1] == "agent_input":
            windows.append((marks[-1][0], float("inf"), last + 1))
        if not windows:
            windows = [(0, float("inf"), 1)]
        for att in {w[2] for w in windows}:
            recs = [r for st, sg, r in records if sg == stage and any(a < st <= b and n == att for a, b, n in windows)]
            rounds = _rounds_from(recs)
            if rounds:
                out.setdefault(stage, {})[att] = rounds
    return out


def _step_attempts(rec: dict, outputs: list | None = None, is_lint_stage: bool = False, rounds: dict | None = None) -> list[dict]:
    attempts = []
    rounds = rounds or {}
    history = rec.get("attempt_history") or []
    for idx, h in enumerate(history):
        failed = h.get("status") not in ("COMPLETE",)
        det = _output_details(_output_for(outputs or [], history, idx, h.get("attempt"))) if failed else _output_details(None)
        lint, issues, errs, seen = [], [], [], set()
        for is_lint_src, items in ((is_lint_stage, list(h.get("issues") or []) + det["lint"]), (False, det["issues"])):
            for i in items:
                if i in ("", None) or _text(i) in seen:
                    continue
                seen.add(_text(i))
                if is_lint_src and _is_lint(i):
                    lint.append(_lint_item(i))
                else:
                    issues.append(_issue_item(i))
        my_rounds = rounds.get(h.get("attempt")) or []
        in_rounds = {i["detail"] for r in my_rounds for i in r["lint"]}
        lint = [i for i in lint if i["detail"] not in in_rounds]   # already listed under its fix round
        if h.get("error") and failed and h["error"] not in errs:
            errs.insert(0, str(h["error"]))
        notes = []
        for d in h.get("downstream") or []:
            if d.get("second_opinion"):
                continue
            fails = "; ".join(_text(f) for f in (d.get("failures") or [])) or d.get("summary") or ""
            notes.append(f"Found later by {STAGE_LABELS.get(d.get('stage'), d.get('stage'))} "
                         f"({d.get('verdict')}): {fails}")
        if h.get("human_note"):
            notes.append(f"Your earlier instruction: {h['human_note']}")
        attempts.append({"attempt": h.get("attempt"), "status": h.get("status"), "errors": errs, "issues": issues,
                         "lint": lint, "rounds": my_rounds, "explanation": det["explanation"] if failed else None,
                         "suggested_fix": det["suggested_fix"] if failed else None, "notes": notes})
    known = {a["attempt"] for a in attempts}
    for n, rs in sorted(rounds.items()):       # fix rounds of an attempt the saved history does not list
        if n not in known:
            attempts.append({"attempt": n, "status": None, "errors": [], "issues": [], "lint": [], "rounds": rs,
                             "explanation": None, "suggested_fix": None, "notes": []})
    return attempts


_NUM_LOC_RE = re.compile(r"\[[^\[\]]+?:\d+\]|\bline \d+\b|\b[\w.]+:\d+(?::\d+)?\b", re.I)


def _err_key(code, text) -> str:
    """Same error in two rounds = same code + same message once line numbers are ignored."""
    return f"{code or ''}|{re.sub(r'\s+', ' ', _NUM_LOC_RE.sub('', str(text))).strip().lower()}"


def _mark_fixed(step: dict) -> None:
    """Mark every error of a step 'open' / 'fixed' / 'carried' (still there next time).
    Inside an attempt: an error missing from the next fix round is fixed in that
    round (round + 1); errors of the last round stay open (fixed if the attempt
    finished COMPLETE). Across attempts: an error of attempt N that attempt N+1
    no longer has is fixed in attempt N+1. Then count each distinct error once,
    by its latest appearance."""
    real = [a for a in step["attempts"] if a.get("attempt") is not None]
    keysets = []
    for a in real:
        ks = {_err_key("error", t) for t in a["errors"]}
        ks |= {_err_key(i["code"], i["detail"]) for i in a["issues"] + a["lint"] if not i.get("note")}
        ks |= {_err_key(i["code"], i["detail"]) for r in a["rounds"] for i in r["lint"] if not i.get("note")}
        keysets.append(ks)
    last_key_state: dict = {}

    def settle(item, key, fixed_label):
        item["state"] = "fixed" if fixed_label else "open"
        item["fixed_label"] = fixed_label
        last_key_state[key] = item["state"]

    position = {id(a): k for k, a in enumerate(real)}
    for a in step["attempts"]:
        idx = position.get(id(a))
        nxt = keysets[idx + 1] if idx is not None and idx + 1 < len(real) else None
        nxt_no = real[idx + 1]["attempt"] if nxt is not None else None

        def after_attempt(item, key):    # state of an error still present at the end of the attempt
            if nxt is None:
                return None
            return None if key in nxt else f"Fixed in attempt {nxt_no}"

        def final_state(item, key):
            label = after_attempt(item, key)
            if label:
                settle(item, key, label)
            else:
                item["state"] = "carried" if nxt is not None else "open"
                item["fixed_label"] = None
                last_key_state[key] = "carried" if nxt is not None else "open"

        a["error_states"] = []
        for t in a["errors"]:
            item = {}
            final_state(item, _err_key("error", t))
            a["error_states"].append({"state": item["state"], "fixed_label": item["fixed_label"]})
        for i in a["issues"] + a["lint"]:
            if not i.get("note"):
                final_state(i, _err_key(i["code"], i["detail"]))
        rounds = a["rounds"]
        for ri, r in enumerate(rounds):
            nxt_round = rounds[ri + 1] if ri + 1 < len(rounds) else None
            later = {_err_key(x["code"], x["detail"]) for x in nxt_round["lint"]} if nxt_round else set()
            fixed_no = (r["round"] + 1) if isinstance(r["round"], int) else None
            for i in r["lint"]:
                if i.get("note"):
                    continue
                key = _err_key(i["code"], i["detail"])
                if nxt_round is not None:
                    if key in later and nxt_round["round"] == fixed_no:
                        i["state"], i["fixed_label"] = "carried", None
                        last_key_state[key] = "carried"
                    else:
                        settle(i, key, f"Fixed in fix round {fixed_no}" if fixed_no else "Fixed in a later fix round")
                elif a.get("status") == "COMPLETE":
                    settle(i, key, f"Fixed in fix round {fixed_no}" if fixed_no else "Fixed (attempt completed)")
                else:
                    final_state(i, key)
    counted = list(last_key_state.values())
    step["open_count"] = counted.count("open")
    step["fixed_count"] = counted.count("fixed")
    step["found_count"] = step["open_count"] + step["fixed_count"]


def _requirement_view(inp: dict, spec) -> dict:
    """A short title plus the readable requirement text (the saved text is often a
    JSON wrapper around the real text) and the spec the analyser extracted."""
    text = inp.get("requirement_text") or inp.get("spec_text")
    if not text:
        text = json.dumps(inp, default=str, ensure_ascii=False)
    text = str(text)
    for _ in range(2):                       # unwrap {"requirement_text": "..."}
        try:
            obj = json.loads(text, strict=False)
        except ValueError:     # wrapper with unescaped quotes inside: cut the wrapper off by hand
            m = re.match(r'\s*\{\s*"requirement_text"\s*:\s*"(.*)"\s*\}\s*$', text, re.S)
            if not m:
                break
            obj = {"requirement_text": m.group(1)}
        if isinstance(obj, dict) and isinstance(obj.get("requirement_text"), str):
            text = obj["requirement_text"]
        else:
            break
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    clean_spec = spec if isinstance(spec, dict) and spec else None
    return {"title": first[:200] or "(no text)", "text": text, "spec": clean_spec}


def collect_run_errors(run: dict) -> dict:
    """Build the engineer's error list from what the run already recorded:
    the orchestrator's last saved state (per-step attempt history), its
    error events, and the run's own status/error. Nothing is stored."""
    events = run.get("events") or []
    final = run.get("final_result") or {}
    states = [e["data"].get("after") for e in events if e.get("event_type") == "state" and e.get("data")]
    state = next((s for s in reversed(states) if s), None) or {}
    stages = final.get("stages") if isinstance(final.get("stages"), dict) and final.get("stages") else state.get("stages") or {}
    is_orch = run.get("agent_name") == "orchestrator"

    steps, seen = [], set()
    if is_orch:
        outputs = _stage_outputs(events)
        all_rounds = _rounds_by_attempt(run, events, stages)
        for key in STAGE_ORDER + [k for k in stages if k not in STAGE_ORDER]:
            rec = stages.get(key)
            if rec:
                steps.append({"stage": key, "label": STAGE_LABELS.get(key, key), "status": rec.get("status"),
                              "attempts": _step_attempts(rec, outputs.get(key), key in LINT_STAGES, all_rounds.get(key))})
    else:
        name = run.get("agent_name")
        det = _output_details(final)
        lint, issues = [], []
        for i in det["lint"]:                       # the same issue can be in both lists
            li = _lint_item(i)
            if all(li["detail"] != x["detail"] for x in lint):
                lint.append(li)
        for i in det["issues"]:
            if _is_lint(i) and name in ("rtl_agent", "testbench_agent"):
                li = _lint_item(i)
                if all(li["detail"] != x["detail"] for x in lint):
                    lint.append(li)
            else:
                issues.append(_issue_item(i))
        stage_key = {"rtl_agent": "generate_rtl", "testbench_agent": "generate_testbench"}.get(name)
        my_rounds = _rounds_from([r for _, sg, r in _fix_round_records(run) if sg == stage_key])
        in_rounds = {i["detail"] for r in my_rounds for i in r["lint"]}
        lint = [i for i in lint if i["detail"] not in in_rounds]
        errs = [str(final["error"])] if final.get("error") else []
        failed = final.get("status") not in ("COMPLETE", None)
        attempt = {"attempt": final.get("attempt") or final.get("iteration") or 1, "status": final.get("status")}
        steps.append({"stage": name, "label": name, "status": final.get("status"),
                      "attempts": [{**attempt, "errors": errs, "issues": issues, "lint": lint, "rounds": my_rounds, "notes": [],
                                    "explanation": det["explanation"] if failed else None,
                                    "suggested_fix": det["suggested_fix"] if failed else None}]})
    by_stage = {s["stage"]: s for s in steps}
    for s in steps:
        for a in s["attempts"]:
            seen.update(a["errors"])
            seen.update(_text(i) for i in a.get("lint", []) + a.get("issues", []))

    run_level, notes = [], []

    def add_run_level(text):
        if text and text not in seen:
            seen.add(text)
            run_level.append(text)

    for e in events:  # errors raised while a step was running
        d = e.get("data") or {}
        if e.get("event_type") != "error":
            continue
        text = d.get("error") or d.get("llm_error")
        if not text or text in seen:
            continue
        step = by_stage.get(d.get("tool"))
        if d.get("tool") in STAGE_LABELS:
            if step is None:
                step = {"stage": d["tool"], "label": STAGE_LABELS[d["tool"]], "status": None, "attempts": []}
                steps.append(step); by_stage[d["tool"]] = step
            seen.add(text)
            step["attempts"].append({"attempt": None, "status": "error", "errors": [str(text)], "issues": [], "lint": [], "notes": []})
        else:
            add_run_level(f"{d.get('node') or d.get('tool') or 'LLM'}: {text}" if (d.get("node") or d.get("tool")) else str(text))
    for t in state.get("errors") or []:
        add_run_level(_text(t))
    for t in (state.get("error"), final.get("error"), run.get("error") if run.get("status") == "failed" else None):
        add_run_level(_text(t) if t else None)
    for t in state.get("warnings") or []:
        notes.append(f"Warning: {_text(t)}")
    if run.get("status") == "stopped":
        notes.append("The run was stopped by the user before it finished; the list below is what was found up to then.")
    if final.get("next_steps"):
        notes.append(f"The AI suggests next: {final['next_steps']}")

    steps.sort(key=lambda s: STAGE_ORDER.index(s["stage"]) if s["stage"] in STAGE_ORDER else 99)
    for s in steps:
        _mark_fixed(s)
        s["count"] = s["open_count"] + sum(1 for a in s["attempts"] if a.get("attempt") is None for _ in a["errors"])
        s["found_count"] += s["count"] - s["open_count"]
    inp = run.get("input") or {}
    view = _requirement_view(inp, state.get("spec"))
    open_total = sum(s["count"] for s in steps) + len(run_level)
    fixed_total = sum(s["fixed_count"] for s in steps)
    req = {"id": state.get("requirement_id") or final.get("requirement_id") or run["id"],
           "title": view["title"], "text": view["text"], "spec": view["spec"],
           "status": final.get("status") or run.get("status"),
           "steps": steps, "run_level": run_level, "notes": notes,
           "count": open_total, "fixed_count": fixed_total, "found_count": open_total + fixed_total}
    stop_reason = final.get("stop_reason") if isinstance(final.get("stop_reason"), str) and final.get("stop_reason") else None
    if not stop_reason and run.get("status") == "stopped" and not final:
        stop_reason = "you pressed Stop"
    return {"run_id": run["id"], "agent_name": run.get("agent_name"), "run_status": run.get("status"),
            "stop_reason": stop_reason,
            "finished": run.get("status") != "running", "total": open_total, "fixed_total": fixed_total,
            "requirements": [req], "can_rerun": is_orch, "requirement_text": inp.get("requirement_text")}


@app.get("/api/agent-runs/{run_id}/errors")
def get_agent_run_errors(run_id: str) -> dict:
    """Every error found in the run so far, grouped by requirement then step."""
    run = run_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return collect_run_errors(run)


RUNS_DIR = (REPO_ROOT / "runs").resolve()
RUN_FILE_MAX_BYTES = 2 * 1024 * 1024


@app.get("/api/run-files")
def get_run_file(path: str) -> dict:
    """Serve a text file produced by an agent run. Only files under
    `<repo>/runs` are allowed; anything else resolves to 403."""
    raw = Path(path)
    target = (raw if raw.is_absolute() else REPO_ROOT / raw).resolve()
    try:
        inside = os.path.commonpath([str(RUNS_DIR), str(target)]) == str(RUNS_DIR)
    except ValueError:  # different drives on Windows
        inside = False
    if not inside:
        raise HTTPException(status_code=403, detail="path is outside the runs directory")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    size = target.stat().st_size
    with target.open("rb") as fh:
        content = fh.read(RUN_FILE_MAX_BYTES).decode("utf-8", errors="replace")
    return {"path": str(target), "content": content, "size": size,
            "truncated": size > RUN_FILE_MAX_BYTES}


@app.get("/api/traces")
def get_traces(limit: int = 100) -> list[dict]:
    return list_traces(limit=limit)


@app.get("/api/traces/{trace_id}")
def get_trace_detail(trace_id: str) -> dict:
    trace = get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return trace


class NoCacheStaticFiles(StaticFiles):
    """Static UI files are revalidated on every request so updates show up
    without a hard refresh. ETag/Last-Modified are kept, so conditional
    requests still return 304. API routes are unaffected (not served here)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")

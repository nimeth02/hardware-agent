# server/app.py — FastAPI backend: spec-analysis query endpoint,
# spec -> human review -> RTL -> test vectors -> testbench pipeline, and the observability API.
#
# Serves the static HTML/JS UI from server/static/ and exposes:
#   POST /api/analyze                  run the spec analyzer alone, return the result
#   POST /api/pipeline/start           start the spec->RTL->test vectors->testbench pipeline; pauses for human review
#   POST /api/pipeline/{id}/resume     submit the reviewed/edited spec, run RTL, test vectors and testbench generation
#   GET  /api/traces                   list recorded LLM call traces (newest first)
#   GET  /api/traces/{id}              full detail for one trace
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


@app.get("/api/agent-runs")
def get_agent_runs(agent_name: str | None = None, limit: int = 50) -> list[dict]:
    return run_store.list_runs(agent_name=agent_name, limit=limit)


@app.get("/api/agent-runs/{run_id}")
def get_agent_run(run_id: str) -> dict:
    run = run_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


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

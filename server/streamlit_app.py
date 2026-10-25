# server/streamlit_app.py — Streamlit version of the Hardware Agent web UI.
#
# HOW TO RUN
#   1. FastAPI backend on port 8000 (already running):
#        python -m uvicorn server.app:app --port 8000
#   2. This Streamlit UI on port 8501:
#        python -m streamlit run server/streamlit_app.py --server.port 8501
#   Backend address: env var HARDWARE_AGENT_API (default http://127.0.0.1:8000).
#   This file talks to the backend over HTTP only (including the live
#   server-sent-event streams). It imports no agent code. The old UI in
#   server/static keeps working at http://127.0.0.1:8000/ .
#
# FEATURE CHECKLIST (everything the old HTML/JS UI does)
#   Tabs: Agents (default), Pipeline, Observability
#   [x] Agents: agent picker ("name vX") + description
#   [x] Agents: Run / Previous runs sub-views
#   [x] Agents: input box prefilled from the agent's template (sim_time default 10us)
#   [x] Agents: plain text accepted for agents with one text field (else JSON)
#   [x] Agents: sim_time check (number + fs/ps/ns/us/ms/s) with hint
#   [x] Agents: example buttons (counter4 projects, 4-bit counter, FIFO/UART/Counter/SPI specs)
#   [x] Agents: "Fill from latest runs" (spec / rtl / testbench from earlier runs)
#   [x] Agents: Run agent -> background run -> live progress over SSE
#   [x] Agents: live status line (event, node, context size) and elapsed time
#   [x] Agents: Stop button (cooperative stop, "stopping..." state)
#   [x] Agents: done/failed/stopped status line, coloured
#   [x] Agents: run summary line (id, time, agent, status, duration, model, calls, context size)
#   [x] Agents: "Use as input for <agent>" buttons on producer runs
#   [x] Agents: timeline table (#, event, detail, status, duration, context badge)
#   [x] Agents: click a row (or "show all") for full details: tool args/result, LLM prompt/response,
#               errors, raw record
#   [x] Agents: orchestrator timeline grouped by sub-agent call, with nested events
#   [x] Agents: orchestrator hand-off / notebook events (input, answer, before/after)
#   [x] Agents: LLM calls list and Tool calls list (full input/output)
#   [x] Agents: waiting-for-user question + answer box -> resume the same session
#   [x] Agents: orchestrator panel (status, summary, next steps, what was produced, per-agent sections)
#   [x] Agents: simulation panel (verdict, analysis, project, output, compile errors, warnings)
#   [x] Agents: implementation panel (verdict, metrics table, analysis, constraints, batch log)
#   [x] Agents: suggestions list, generated code blocks (RTL / testbench), copy buttons
#   [x] Agents: test-vector CSV table (first 50 rows), copy CSV, download, validation issues
#   [x] Agents: final result JSON
#   [x] Agents: Previous runs table (for the picked agent), click to open in Run view, Refresh
#   [x] Pipeline: requirement text + 4 example buttons
#   [x] Pipeline: Start Pipeline -> spec analysis -> steps/timings table
#   [x] Pipeline: suggestions list + editable spec JSON + "Approve & Generate"
#   [x] Pipeline: generated RTL (entity, code, lint, notes)
#   [x] Pipeline: test vectors status line (done / not generated + reasons)
#   [x] Pipeline: testbench (status, entity, code, lint, notes + warnings)
#   [x] Pipeline: timings with measured human-review wait
#   [x] Observability: LLM call traces table (time, source, model, duration, status, preview)
#   [x] Observability: Refresh, click a trace for detail (messages, output/error, usage), Close
#
# ENHANCEMENTS (new, not in the old UI)
#   + run-history picker (dropdown) in addition to the table
#   + auto-refresh toggle (sidebar) for history and traces
#   + download buttons: RTL, testbench, CSV, constraints, spec JSON
#   + timings bar chart; "Follow live" button for runs still in progress
#   + connection check with a clear message when the backend is down

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from datetime import datetime

import httpx
import pandas as pd
import streamlit as st

API = os.environ.get("HARDWARE_AGENT_API", "http://127.0.0.1:8000").rstrip("/")

st.set_page_config(page_title="Hardware Agent", layout="wide")


# ───────────────────────── backend access ─────────────────────────

class ApiError(Exception):
    pass


def api(method: str, path: str, *, json_body=None, params=None, timeout=60.0):
    """Call the FastAPI backend; returns parsed JSON or raises ApiError."""
    try:
        r = httpx.request(method, API + path, json=json_body, params=params, timeout=timeout)
    except httpx.HTTPError as e:
        raise ApiError(f"cannot reach the backend at {API} ({e.__class__.__name__}: {e})") from e
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise ApiError(str(detail) if detail else f"HTTP {r.status_code}")
    return data


@st.cache_data(ttl=30, show_spinner=False)
def load_agents() -> list[dict]:
    return api("GET", "/api/agents")


@st.cache_data(ttl=120, show_spinner=False)
def load_run_file(path: str) -> dict | None:
    try:
        return api("GET", "/api/run-files", params={"path": path})
    except ApiError as e:
        return {"error": str(e)}


def sse_events(path: str):
    """Yield (event_name, data_dict) from a server-sent-event stream."""
    with httpx.stream("GET", API + path, timeout=httpx.Timeout(10.0, read=None)) as r:
        r.raise_for_status()
        name, buf = "message", []
        for line in r.iter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                buf.append(line[5:].strip())
            elif line == "":
                if buf:
                    try:
                        yield name, json.loads("\n".join(buf))
                    except ValueError:
                        pass
                name, buf = "message", []


# ───────────────────────── small helpers ─────────────────────────

def fmt_ms(ms) -> str:
    ms = ms or 0
    return f"{ms / 1000:.2f} s" if ms >= 1000 else f"{round(ms)} ms"


def fmt_time(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def fmt_tok(n) -> str:
    return "?" if n is None else f"{int(n):,}"


def nk(prefix: str) -> str:
    """Deterministic unique widget key within one script run."""
    st.session_state["_nk"] = st.session_state.get("_nk", 0) + 1
    return f"{prefix}_{st.session_state['_nk']}"


def pretty(v) -> str:
    return v if isinstance(v, str) else json.dumps(v, indent=2, default=str)


# Plain-language names for the status words the agents use.
STATUS_LABEL = {
    "COMPLETE": "done", "NEEDS_REVIEW": "done but needs a human look", "FAILED": "gave up",
    "WAITING_FOR_USER": "waiting for you", "CANCELLED": "stopped", "RUNNING": "running",
    "completed": "finished", "failed": "failed", "stopped": "stopped", "running": "running",
    "ok": "ok", "error": "error",
}
GOOD = {"COMPLETE", "completed", "ok", "pass"}
WARN = {"NEEDS_REVIEW", "WAITING_FOR_USER"}
BAD = {"FAILED", "failed", "error", "fail"}
STOP = {"CANCELLED", "stopped", "requested"}


def status_color(status) -> str:
    if status in GOOD:
        return "green"
    if status in WARN:
        return "orange"
    if status in BAD:
        return "red"
    if status in STOP:
        return "gray"
    return "blue" if status in ("running", "RUNNING") else "gray"


def badge(status, text: str | None = None) -> str:
    label = text or STATUS_LABEL.get(status, str(status))
    label = re.sub(r"[\[\]]", "", str(label))
    return f":{status_color(status)}-background[{label}]"


def run_badge_status(r: dict) -> str:
    """One status for a history row: the agent's own verdict wins when it is bad/odd."""
    a = r.get("agent_status")
    if r.get("status") == "stopped" or a == "CANCELLED":
        return "stopped"
    if r.get("status") == "failed" or a == "FAILED":
        return "FAILED"
    if a in WARN:
        return a
    return r.get("status") or ""


def style_status_col(df: pd.DataFrame, col: str, raw_col: str):
    colors = {"green": "#d4edda", "orange": "#ffe8c2", "red": "#f8d7da", "gray": "#e2e3e5", "blue": "#d6e4ff"}

    def f(series):
        return [f"background-color: {colors[status_color(raw)]}" for raw in df[raw_col]]

    return df.style.apply(f, subset=[col])


def code_block(title: str, text: str, language: str | None = None, *, filename: str | None = None,
               live: bool = False, mime: str = "text/plain"):
    """Titled code block (st.code has a built-in copy button) + optional download."""
    st.caption(title)
    st.code(text if text else "(empty)", language=language, wrap_lines=True)
    if filename and text and not live:
        st.download_button(f"Download {filename}", text, file_name=filename, mime=mime, key=nk("dl"))


def bullet_list(items, empty_text: str = ""):
    items = list(items or [])
    if not items:
        if empty_text:
            st.caption(empty_text)
        return
    st.markdown("\n".join("- " + (i if isinstance(i, str) else json.dumps(i, default=str)).replace("\n", " ")
                          for i in items))


def line(text: str, kind: str = "status"):
    if kind == "error":
        st.error(text)
    elif kind == "warn":
        st.warning(text)
    else:
        st.write(text)


# ───────────────────────── timings / lint / suggestions ─────────────────────────

def render_suggestions(suggestions):
    bullet_list(suggestions, "No issues found — spec looks structurally complete.")


def render_lint(lint):
    issues = (lint or {}).get("issues") or []
    via = f"static checks + {lint['compiler']}" if lint and lint.get("compiler") else "static checks only"
    if not issues:
        st.caption(f"No lint issues ({via}).")
        return
    bullet_list([f"[{i.get('code')}] {i.get('detail')}" for i in issues])


def render_timings(timings):
    items = timings or []
    if not items:
        st.caption("No step timings recorded.")
        return
    rows, total = [], 0
    for t in items:
        total += t.get("duration_ms") or 0
        s = t.get("status") or ""
        rows.append({"step": t.get("step"), "status": "paused" if s == "interrupted" else s,
                     "duration": fmt_ms(t.get("duration_ms")), "ms": round(t.get("duration_ms") or 0)})
    rows.append({"step": "total", "status": "", "duration": fmt_ms(total), "ms": None})
    df = pd.DataFrame(rows)
    st.dataframe(df[["step", "status", "duration"]], hide_index=True, width="stretch")
    st.bar_chart(df.iloc[:-1].set_index("step")["ms"], height=200)


# ───────────────────────── example texts ─────────────────────────

EXAMPLE_SPECS = [
    ("Synchronous FIFO", """Design a synchronous FIFO in VHDL.
- DATA_WIDTH parameter, default 8
- DEPTH parameter, default 16
- Single clock domain, clk, active-high synchronous reset rst
- Inputs: wr_en, rd_en, din[DATA_WIDTH-1:0]
- Outputs: dout[DATA_WIDTH-1:0], full, empty
- Must not allow writes when full or reads when empty"""),
    ("UART Transmitter", """Design a UART transmitter entity.
- Inputs: clk (50 MHz), rst_n (active-low async reset), tx_start, tx_data[7:0]
- Output: tx_serial, tx_busy
- Configurable baud rate via BAUD_RATE parameter, default 115200
- Frame format: 1 start bit, 8 data bits, no parity, 1 stop bit
- tx_busy must be asserted for the full duration of a transmission
- Must ignore tx_start while a transmission is in progress"""),
    ("8-bit Up/Down Counter", """Design an 8-bit up/down counter.
- Inputs: clk, rst (active-high sync reset), en, up_down (1 = count up, 0 = count down)
- Output: count[7:0]
- Counter should saturate at 0xFF and 0x00 rather than wrapping around
- Only increments/decrements when en is asserted"""),
    ("SPI Master", """Design an SPI master controller, Mode 0 (CPOL=0, CPHA=0).
- Inputs: clk (100 MHz system clock), rst_n, start, data_in[7:0], miso
- Outputs: sclk, mosi, cs_n, data_out[7:0], done
- SCLK_DIV parameter to derive sclk frequency from system clk, default divide-by-4
- Single chip-select cs_n, active-low, asserted for the whole 8-bit transfer
- done pulses for one clk cycle when the 8-bit transfer completes"""),
]

AGENT_EXAMPLES = {
    "simulation_agent": [("counter4 project (10us)", {"project": "counter4", "sim_time": "10us"})],
    "implementation_agent": [("counter4 project (xc7a35tcpg236-1)",
                              {"project": "counter4", "part": "xc7a35tcpg236-1"})],
    "orchestrator": [("4-bit up counter (full flow)", {"requirement_text":
        "4-bit up counter: clk 100 MHz, rst active-high synchronous, en input, count output, wraps from 15 to 0."})],
}

PRODUCERS = {
    "spec": {"agent": "spec_analyzer", "statuses": ["COMPLETE"]},
    "rtl": {"agent": "rtl_agent", "statuses": ["COMPLETE", "NEEDS_REVIEW"]},
    "testbench": {"agent": "testbench_agent", "statuses": ["COMPLETE", "NEEDS_REVIEW"]},
}

SIM_TIME_RE = re.compile(r"^\s*\d+(\.\d+)?\s*(fs|ps|ns|us|ms|s)\s*$", re.IGNORECASE)
SIM_TIME_HINT = "sim_time must be a decimal number followed by fs/ps/ns/us/ms/s, e.g. 10us or 2.5ms"


# ───────────────────────── agent input handling ─────────────────────────

def agent_by_name(name):
    return next((a for a in load_agents() if a["name"] == name), None)


def template_text(agent) -> str:
    t = dict(agent.get("input_template") or {})
    if "sim_time" in t and not t["sim_time"]:
        t["sim_time"] = "10us"
    return json.dumps(t, indent=2)


def plain_text_field(agent):
    t = (agent or {}).get("input_template") or {}
    keys = list(t)
    return keys[0] if len(keys) == 1 and isinstance(t[keys[0]], str) else None


def parse_agent_input(agent, raw: str):
    """Returns (input_dict, error). Valid JSON as-is; else wrapped into the agent's single text field."""
    try:
        return json.loads(raw), None
    except ValueError as err:
        field = plain_text_field(agent)
        text = raw.strip()
        if field and text:
            return {**agent["input_template"], field: text}, None
        if field:
            return None, f"Input is empty: type the requirement text for {agent['name']}."
        fields = ", ".join((agent.get("input_template") or {}).keys()) or "(none)"
        text_agents = ", ".join(a["name"] for a in load_agents() if plain_text_field(a))
        return None, (f"Input must be JSON with fields: {fields} — or pick an agent that takes plain text "
                      f"({text_agents}). JSON error: {err}")


def sim_time_error(inp):
    if not isinstance(inp, dict) or "sim_time" not in inp:
        return None
    v = inp["sim_time"]
    if v is None or v == "":
        return None
    return None if isinstance(v, str) and SIM_TIME_RE.match(v) else f"Invalid sim_time {json.dumps(v)}: {SIM_TIME_HINT}."


def set_msg(kind: str, text: str):
    st.session_state["agent_msg"] = (kind, text)


def current_agent_input() -> dict:
    a = agent_by_name(st.session_state.get("agent_name"))
    v, _ = parse_agent_input(a, st.session_state.get("agent_input", "")) if a else (None, None)
    if isinstance(v, dict):
        return v
    return dict(a["input_template"]) if a else {}


def on_agent_change():
    a = agent_by_name(st.session_state["agent_name"])
    if a:
        st.session_state["agent_input"] = template_text(a)
    st.session_state.pop("agent_msg", None)


def cb_set_input(obj: dict):
    st.session_state["agent_input"] = json.dumps(obj, indent=2)


def produced_value(run, key):
    fr = run.get("final_result")
    p = PRODUCERS.get(key)
    if not p or not isinstance(fr, dict) or run.get("agent_name") != p["agent"]:
        return None
    if fr.get("status") not in p["statuses"] or fr.get(key) is None:
        return None
    return fr[key]


def run_label(run) -> str:
    return f"run {run['id'][:8]} ({fmt_time(run.get('started_at'))})"


def cb_fill_latest():
    inp = current_agent_input()
    keys = [k for k in PRODUCERS if k in inp]
    if not keys:
        set_msg("warn", "This input has no spec / rtl / testbench field to fill.")
        return
    filled, missing = [], []
    try:
        for key in keys:
            hit = None
            runs = api("GET", "/api/agent-runs", params={"agent_name": PRODUCERS[key]["agent"], "limit": 20})
            for r in runs:
                try:
                    run = api("GET", f"/api/agent-runs/{r['id']}")
                except ApiError:
                    continue
                v = produced_value(run, key)
                if v is not None:
                    hit = (run, v)
                    break
            if hit:
                inp[key] = hit[1]
                filled.append(f"{key} from {run_label(hit[0])}")
            else:
                missing.append(f"{key} (no {PRODUCERS[key]['agent']} run with a usable result)")
    except ApiError as e:
        set_msg("error", f"Error: {e}")
        return
    st.session_state["agent_input"] = json.dumps(inp, indent=2)
    text = (f"Filled {'; '.join(filled)}." if filled else "Nothing filled.") + \
        (f" Not found: {'; '.join(missing)}." if missing else "")
    set_msg("ok" if filled else "error", text)


def cb_use_as_input(agent_name: str, key: str, value, run_id: str):
    if st.session_state.get("agent_name") != agent_name:
        st.session_state["agent_name"] = agent_name
        on_agent_change()
    inp = current_agent_input()
    inp[key] = value
    st.session_state["agent_input"] = json.dumps(inp, indent=2)
    set_msg("ok", f"{key} filled from run {run_id[:8]}")
    st.session_state["agents_view"] = "Run"


def cb_stop(run_id: str):
    try:
        api("POST", f"/api/agent-runs/{run_id}/stop")
        st.session_state["stopping"] = True
        set_msg("warn", "stopping… the current step finishes first")
    except ApiError as e:
        set_msg("error", f"Could not stop: {e}")


def cb_follow(run_id: str):
    st.session_state["active_run"] = {"id": run_id, "started": time.time()}
    st.session_state["viewed_run"] = run_id
    st.session_state["stopping"] = False


# ───────────────────────── event timeline ─────────────────────────

CTX_WARN, CTX_HIGH = 16000, 32000
LOG_FIELDS = ["sim_output", "stdout", "stderr", "log_excerpt", "log", "elaboration_output", "compile_output"]
ORCH_META = ("agent_input", "agent_output", "state")
ORCH_TOOL_AGENTS = {
    "analyze_spec": "spec_analyzer", "answer_spec_question": "spec_analyzer", "generate_rtl": "rtl_agent",
    "generate_testbench": "testbench_agent", "generate_test_vectors": "test_vector_agent",
    "write_project": None, "simulate": "simulation_agent", "implement": "implementation_agent",
}
TRUNC_NOTE = ("This text was cut when it was captured (marker in the text): the recorder stores at most 4000 chars "
              "of a tool output and the Vivado MCP server caps console output at 4000 chars. "
              "Raise those caps for new runs; old runs cannot be recovered.")


def strip_trunc(s) -> str:
    return re.sub(r"\.{3} \[truncated \d+ chars\]\s*$", "…", str(s if s is not None else ""))


def is_code_string(v) -> bool:
    return isinstance(v, str) and (len(v.split("\n")) >= 4 or len(v) > 400)


def code_label(s: str) -> str:
    n = len(s.split("\n"))
    kind = "VHDL code" if re.search(r"\b(entity|architecture|library\s+ieee|std_logic)\b", s, re.I) else "text"
    return f"{kind}, {n} line{'' if n == 1 else 's'}"


def fold_code(v, path: str, out: list):
    """Copy of v with long strings replaced by a placeholder; strings pushed to out."""
    if is_code_string(v):
        out.append((path, v))
        return f"<< {code_label(v)} — shown below >>"
    if isinstance(v, list):
        return [fold_code(x, f"{path}[{i}]", out) for i, x in enumerate(v)]
    if isinstance(v, dict):
        return {k: fold_code(x, f"{path}.{k}" if path else k, out) for k, x in v.items()}
    return v


def describe_value(v) -> str:
    if v is None:
        return "nothing"
    if is_code_string(v):
        return code_label(v)
    if isinstance(v, str):
        return f'"{v[:40]}…"' if len(v) > 40 else f'"{v}"'
    if isinstance(v, list):
        return f"list of {len(v)}"
    if isinstance(v, dict):
        k = list(v)
        return "{" + ", ".join(k[:5]) + (", …" if len(k) > 5 else "") + "}"
    return str(v)


def state_changed_keys(d: dict) -> list:
    before, after = d.get("before"), d.get("after") or {}

    def empty(v):
        return v is None or (isinstance(v, (list, dict)) and not v)

    if not isinstance(before, dict):
        return [k for k, v in after.items() if not empty(v)]
    keys = list(dict.fromkeys([*before, *after]))
    return [k for k in keys if json.dumps(before.get(k), default=str, sort_keys=True)
            != json.dumps(after.get(k), default=str, sort_keys=True)]


def orch_meta_summary(e: dict) -> str:
    d = e.get("data") or {}
    stage = d.get("stage") or "?"
    t = e["event_type"]
    if t == "agent_input":
        inp = d.get("input")
        items = inp if isinstance(inp, list) else ([] if inp is None else [inp])
        return f"Handed to {stage}: " + (", ".join(describe_value(i) for i in items) or "nothing (reads the notebook)")
    if t == "agent_output":
        out = d.get("output") if isinstance(d.get("output"), dict) else {}
        status = e.get("status") or out.get("status") or "done"
        err = f" — error: {str(out['error'])[:120]}" if out.get("error") else ""
        keys = [k for k in out if k not in ("status", "error") and out[k] is not None]
        return f"{stage} answered: {status}{err}" + (f" ({', '.join(keys)})" if keys else "")
    changed = state_changed_keys(d)
    what = "changed" if d.get("before") else "set"
    return f"Notebook after {stage} — " + (f"{what}: {', '.join(changed)}" if changed else "nothing changed")


def event_summary(e: dict) -> str:
    d = e.get("data") or {}
    t = e["event_type"]
    if t == "agent_step":
        return f"node: {d.get('node')}"
    if t == "llm_call":
        tc = d.get("tool_calls")
        return (f"-> tools: {', '.join(tc)}" if tc else "") + (f" {strip_trunc(d['output_preview'])}" if d.get("output_preview") else "")
    if t == "tool_call":
        return f"{d.get('tool')}({json.dumps(d.get('input'), default=str)[:160]})"
    if t == "tool_result":
        return f"{d.get('tool')}: {strip_trunc(d.get('output_preview'))}"
    if t == "error":
        return d.get("error") or d.get("llm_error") or ""
    if t == "run_resumed":
        return f"answer: {(d.get('input') or {}).get('answer', '')}"
    if t in ORCH_META:
        return orch_meta_summary(e)
    return d.get("agent") or d.get("error") or ""


def make_ctx(run: dict) -> dict:
    tools, llms = {}, {}
    for t in run.get("tool_calls") or []:
        tools.setdefault(t["step"], []).append(t)
    for c in run.get("llm_calls") or []:
        llms[c["step"]] = c
    return {"run": run, "tools": tools, "llms": llms}


def llm_context(e: dict, ctx: dict) -> dict:
    d = e.get("data") or {}
    rec = ctx["llms"].get(e.get("step")) or {}

    def pick(a, b):
        return a if a is not None else b

    return {"input": pick(d.get("input_tokens"), rec.get("input_tokens")),
            "output": pick(d.get("output_tokens"), rec.get("output_tokens")),
            "messages": pick(d.get("messages"), len(rec["input"]) if isinstance(rec.get("input"), list) else None)}


def ctx_badge(e: dict, ctx: dict) -> str:
    c = llm_context(e, ctx)
    parts = [f"ctx {fmt_tok(c['input'])}{' tok' if c['input'] is not None else ''}"]
    if c["output"] is not None:
        parts.append(f"out {fmt_tok(c['output'])}")
    if c["messages"] is not None:
        parts.append(f"{fmt_tok(c['messages'])} msgs")
    flag = ""
    if c["input"] is not None:
        flag = " (HIGH)" if c["input"] > CTX_HIGH else " (large)" if c["input"] > CTX_WARN else ""
    return "[" + " | ".join(parts) + flag + "] "


def event_row(e: dict, ctx: dict, label: str | None = None, detail: str | None = None, prefix: str = "") -> dict:
    ctxb = ctx_badge(e, ctx) if e["event_type"] == "llm_call" else ""
    return {"#": e.get("step"), "Event": prefix + (label or e["event_type"]),
            "Detail": ctxb + (detail if detail is not None else event_summary(e)),
            "Status": e.get("status") or "",
            "Duration": fmt_ms(e["duration_ms"]) if e.get("duration_ms") is not None else "",
            "_e": e}


def orch_tool_outcome(e: dict, rec: dict | None) -> tuple[str, str]:
    d = e.get("data") or {}
    out = rec.get("output") if rec else None
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            out = None
    if not isinstance(out, dict):
        out = {}
        prev = d.get("output_preview") or ""
        for k in ("status", "verdict", "error"):
            m = re.search(rf'"{k}"\s*:\s*"([^"]*)"', prev)
            if m:
                out[k] = m.group(1)
    error = (rec or {}).get("error") or d.get("error") or (out["error"] if isinstance(out.get("error"), str) else "")
    parts = [str(out[k]) for k in ("status", "verdict") if out.get(k)]
    if error:
        parts.append(f"error: {str(error)[:80]}")
    if not parts:
        parts.append(e.get("status") or "done")
    bad = e.get("status") == "error" or bool(error) or out.get("status") == "FAILED" or out.get("verdict") == "fail"
    warn = not bad and (out.get("status") in WARN or out.get("verdict") == "error")
    return " · ".join(parts), ("error" if bad else "NEEDS_REVIEW" if warn else "ok")


def is_orch_event(e: dict, parent: dict | None) -> bool:
    d = e.get("data") or {}
    t = e["event_type"]
    if t == "agent_step":
        return d.get("node") in ("model", "tools")
    if t in ("tool_call", "tool_result"):
        return d.get("tool") in ORCH_TOOL_AGENTS
    if t == "llm_call":
        tc = d.get("tool_calls")
        if tc:
            return any(x in ORCH_TOOL_AGENTS for x in tc)
        return parent is None or (parent["end_ts"] is not None and e["timestamp"] > parent["end_ts"])
    return True


def orchestrator_rows(run: dict, ctx: dict) -> list[dict]:
    """Top-level rows per orchestrator decision; one group row per sub-agent call
    with its nested events indented below."""
    records = {f"{t['step']}:{t['tool_name']}": t for t in run.get("tool_calls") or []
               if t.get("tool_name") in ORCH_TOOL_AGENTS}
    rows, parent = [], None
    for e in run.get("events") or []:
        d = e.get("data") or {}
        t = e["event_type"]
        orch = is_orch_event(e, parent)
        if (parent and orch and t != "tool_result" and not (t == "agent_step" and d.get("node") == "tools")
                and t not in ORCH_META and (parent["end_ts"] is None or e["timestamp"] > parent["end_ts"])):
            parent = None
        if orch and t == "tool_call":
            agent = ORCH_TOOL_AGENTS.get(d.get("tool"))
            row = event_row(e, ctx, "tool_call",
                            f"{d.get('tool')} → {agent or 'write_project (file writes, no agent)'}")
            row["Status"], row["Duration"] = "running...", ""
            rows.append(row)
            parent = {"tool": d.get("tool"), "step": e.get("step"), "end_ts": None, "row": row, "n": 0}
            continue
        if orch and t == "tool_result" and parent and parent["tool"] == d.get("tool") and parent["step"] == e.get("step"):
            parent["end_ts"] = e["timestamp"]
            label, kind = orch_tool_outcome(e, records.get(f"{e.get('step')}:{parent['tool']}"))
            parent["row"]["Status"] = label
            parent["row"]["_kind"] = kind
            parent["row"]["Duration"] = fmt_ms(e["duration_ms"]) if e.get("duration_ms") is not None else ""
            continue
        if orch:
            if t == "agent_step":
                continue
            if t == "llm_call":
                tc = d.get("tool_calls")
                detail = f"call {', '.join(tc)}" if tc else f"final answer: {strip_trunc(d.get('output_preview')) or '(empty)'}"
                rows.append(event_row(e, ctx, "decision", detail))
            else:
                rows.append(event_row(e, ctx))
            continue
        row = event_row(e, ctx, "node" if t == "agent_step" else None,
                        (d.get("node") or "") if t == "agent_step" else None,
                        prefix="      ↳ " if parent else "")
        rows.append(row)
        if parent:
            parent["n"] += 1
            parent["row"]["Event"] = f"tool_call ({parent['n']} nested)"
    return rows


def render_tool_output(output):
    if isinstance(output, list):
        text = "\n".join(p["text"] if isinstance(p, dict) and isinstance(p.get("text"), str)
                         else p if isinstance(p, str) else json.dumps(p, indent=2) for p in output)
    elif isinstance(output, dict):
        text = json.dumps(output, indent=2)
    else:
        text = "" if output is None else str(output)
    parsed = None
    try:
        parsed = json.loads(text)
    except ValueError:
        pass
    if isinstance(parsed, (dict, list)):
        logs: list = []
        _collect_logs(parsed, "", logs)
        for path, txt in logs:
            code_block(f"{path} ({len(txt):,} chars)", txt)
        code_block("result (JSON)", json.dumps(parsed, indent=2), "json")
    else:
        code_block(f"output ({len(text):,} chars)", text or "(empty)")
    if re.search(r"\[truncated[, ]", text):
        st.warning(TRUNC_NOTE)


def _collect_logs(obj, path, out):
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            _collect_logs(v, f"{path}[{i}]", out)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if k in LOG_FIELDS and isinstance(v, str) and v.strip():
                out.append((p, v))
            elif isinstance(v, (dict, list)):
                _collect_logs(v, p, out)


def json_with_folded_code(title: str, value, root: str = ""):
    folded: list = []
    shown = fold_code(value, root, folded)
    code_block(title, json.dumps(shown, indent=2, default=str), "json")
    seen = set()
    for path, text in folded:
        if path not in seen:
            seen.add(path)
            code_block(f"{path or title} — {code_label(text)}", text)


def build_event_detail(e: dict, ctx: dict):
    """Full record of one timeline event (no expanders inside: Streamlit forbids nesting)."""
    d = e.get("data") or {}
    t = e["event_type"]
    tool_rec = next((x for x in ctx["tools"].get(e.get("step"), []) if not d.get("tool") or x.get("tool_name") == d.get("tool")), None)
    try:
        if t == "tool_call":
            inp = tool_rec["input"] if tool_rec else d.get("input")
            st.markdown(f"**{d.get('tool') or 'tool'} — arguments**")
            code_block("arguments (JSON)", pretty(inp), "json")
            if isinstance(inp, str) and re.search(r"\[truncated[, ]", inp):
                st.warning(TRUNC_NOTE)
        elif t == "tool_result":
            extra = f" | {fmt_ms(tool_rec.get('latency_ms'))} | {tool_rec.get('status')}" if tool_rec else ""
            st.markdown(f"**{d.get('tool') or 'tool'} — result{extra}**")
            if not tool_rec:
                st.caption("Full result not recorded yet (tool still running or record missing) — showing the event preview.")
                code_block("output preview", str(d.get("output_preview") or ""))
            else:
                if tool_rec.get("error"):
                    code_block("error", str(tool_rec["error"]))
                render_tool_output(tool_rec.get("output"))
        elif t == "llm_call":
            rec = ctx["llms"].get(e.get("step"))
            if not rec:
                st.caption("Full LLM record not stored yet — showing the event preview.")
                code_block("output preview", str(d.get("output_preview") or ""))
            else:
                st.caption(f"{rec.get('model') or ''} | {fmt_ms(rec.get('latency_ms'))} | tokens in "
                           f"{fmt_tok(rec.get('input_tokens'))} / out {fmt_tok(rec.get('output_tokens'))} | "
                           f"prompt {rec.get('prompt_version') or '-'} | {rec.get('status')}")
                msgs = rec.get("input") or []
                st.markdown(f"**Prompt — {len(msgs)} message(s)**")
                for i, m in enumerate(msgs):
                    tcs = m.get("tool_calls") or []
                    calls = f" — calls {', '.join(x.get('name', '?') for x in tcs)}" if tcs else ""
                    c = m.get("content")
                    body = (c if isinstance(c, str) else "" if c is None else json.dumps(c, indent=2))
                    if tcs:
                        body += "\n\n[tool_calls]\n" + json.dumps(tcs, indent=2)
                    code_block(f"[{i + 1}] {m.get('role')}{calls}", body or "(empty)")
                st.markdown("**Response**")
                out = rec.get("output") or {}
                if rec.get("error"):
                    code_block("error", str(rec["error"]))
                c = out.get("content")
                code_block("content", (c if isinstance(c, str) else "" if c is None else json.dumps(c, indent=2)) or "(empty)")
                if out.get("tool_calls"):
                    code_block("tool calls (JSON)", json.dumps(out["tool_calls"], indent=2), "json")
        elif t == "error":
            code_block("error", str(d.get("error") or d.get("llm_error") or ""))
            if tool_rec and tool_rec.get("output") is not None:
                render_tool_output(tool_rec["output"])
        elif t in ORCH_META:
            build_orch_meta_detail(e)
    except Exception as err:  # keep the page alive on odd records
        st.error(f"Could not render: {err}")
    st.caption("event record (raw JSON)")
    st.json(e, expanded=False)


def build_orch_meta_detail(e: dict):
    d = e.get("data") or {}
    stage = d.get("stage") or "?"
    t = e["event_type"]
    if t == "agent_input":
        inp = d.get("input")
        items = inp if isinstance(inp, list) else ([] if inp is None else [inp])
        st.markdown(f"**Handed to {stage} — {len(items)} item(s)**")
        if not items:
            st.caption("No arguments: the helper read what it needed from the notebook.")
        for i, v in enumerate(items):
            json_with_folded_code(f"input {i + 1} of {len(items)}: {describe_value(v)}", v, f"input[{i}]")
    elif t == "agent_output":
        out = d.get("output")
        st.markdown(f"**{stage} answered — {e.get('status') or (out or {}).get('status') or 'done'}**")
        if isinstance(out, dict) and out.get("error"):
            code_block("error", str(out["error"]))
        json_with_folded_code("answer (JSON)", out, "output")
    else:
        after = d.get("after") if isinstance(d.get("after"), dict) else {}
        changed = state_changed_keys(d)
        folded: list = []
        st.markdown(f"**Notebook after {stage}**")
        st.caption((f"{'Changed' if d.get('before') else 'Set'}: {', '.join(changed)}") if changed else "Nothing changed.")
        before = d.get("before")
        if isinstance(before, dict) and changed:
            rows = [{"field": k,
                     "before": "(not set)" if k not in before else json.dumps(fold_code(before[k], f"before.{k}", folded), default=str),
                     "after": "(not set)" if k not in after else json.dumps(fold_code(after[k], f"after.{k}", folded), default=str)}
                    for k in changed]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        code_block("notebook after this step (JSON)", json.dumps(fold_code(after, "after", []), indent=2, default=str), "json")
        seen = set()
        for path, text in folded:
            if path not in seen:
                seen.add(path)
                code_block(f"{path} — {code_label(text)}", text)


def render_timeline(run: dict, ctx: dict, live: bool):
    is_orch = run.get("agent_name") == "orchestrator"
    rows = orchestrator_rows(run, ctx) if is_orch else [event_row(e, ctx) for e in run.get("events") or []]
    st.markdown("**Timeline**")
    if is_orch:
        st.caption("Sub-agent calls are grouped; the indented rows below a call are the steps that sub-agent took.")
    if not rows:
        st.caption("No events yet.")
        return
    df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    raw = [r.get("_kind") or (r["_e"].get("status") or "") for r in rows]
    colors = {"green": "#d4edda", "orange": "#ffe8c2", "red": "#f8d7da", "gray": "#e2e3e5", "blue": "#d6e4ff"}
    styler = df.style.apply(
        lambda s: [f"background-color: {colors[status_color(x)]}" if x else "" for x in raw], subset=["Status"])
    if live:
        st.dataframe(styler, hide_index=True, width="stretch", height=min(420, 40 + 35 * len(df)))
        return
    expand_all = st.checkbox("Show full details of every event", key=nk("expand_all"),
                             help="Otherwise click a row to see what the tool or AI did, in full.")
    ev = st.dataframe(styler, hide_index=True, width="stretch", height=min(420, 40 + 35 * len(df)),
                      on_select="rerun", selection_mode="single-row", key=f"evt_{run['id']}")
    if expand_all:
        for r in rows:
            e = r["_e"]
            with st.expander(f"#{e.get('step')}  {r['Event'].strip()}  —  {r['Detail'][:90]}"):
                build_event_detail(e, ctx)
    else:
        sel = ev.selection.rows
        if sel and sel[0] < len(rows):
            e = rows[sel[0]]["_e"]
            st.markdown(f"**Event #{e.get('step')} — {e['event_type']}**")
            build_event_detail(e, ctx)
        else:
            st.caption("Click a row to see what the tool or AI did, in full.")


# ───────────────────────── result panels ─────────────────────────

def status_line(fr: dict):
    s = fr.get("status")
    text = f"{badge(s) if s else '?'}"
    if fr.get("iteration") is not None:
        text += f" attempt {fr['iteration']}"
    if fr.get("decision_reason"):
        text += f" · {fr['decision_reason']}"
    st.markdown(text)


def verdict_head(fr: dict):
    verdict = fr.get("verdict") or ("error" if fr.get("status") == "FAILED" else None)
    kind = {"pass": "green", "fail": "red", "error": "orange"}.get(verdict, "gray")
    head = f":{kind}-background[{verdict.upper() if verdict else 'NO VERDICT'}]"
    extra = (fr.get("status") or "")
    if fr.get("iteration") is not None:
        extra += f" · attempt {fr['iteration']}"
    if fr.get("decision_reason"):
        extra += f" · {fr['decision_reason']}"
    st.markdown(f"{head}  {extra}")


def append_analysis(an):
    if not an:
        return
    st.markdown("**Analysis**")
    if an.get("summary"):
        st.write(an["summary"])
    if an.get("likely_cause"):
        st.write(f"Likely cause: {an['likely_cause']}")
    failures = an.get("failures") if isinstance(an.get("failures"), list) else []
    if failures or an.get("verdict") != "pass":
        st.markdown("**Failures**")
        bullet_list(failures, "No failures reported")
    if an.get("suggested_fix"):
        st.markdown("**Suggested fix**")
        st.write(an["suggested_fix"])


def append_warnings(fr):
    w = fr.get("warnings") if isinstance(fr.get("warnings"), list) else []
    if w:
        st.markdown("**Warnings**")
        bullet_list(w)


def fill_simulation(fr: dict, live=False):
    verdict_head(fr)
    if fr.get("error"):
        st.error(f"Error: {fr['error']}")
    p = fr.get("project")
    if p:
        st.write(f"project {p.get('name') or '?'} · top {p.get('top_entity') or '?'} · tb {p.get('tb_file') or '?'}"
                 f" · src {', '.join(p.get('src_files') or []) or '-'} · vectors {p.get('test_vectors') or '-'}")
    elif fr.get("project_dir"):
        st.write(f"project dir {fr['project_dir']}")
    append_analysis(fr.get("analysis"))
    sim = fr.get("sim")
    if sim:
        st.markdown("**Simulation**")
        st.write(f"status {sim.get('status') or '?'} · outcome {sim.get('outcome') or '?'} · sim_time "
                 f"{sim.get('sim_time') or '?'}" + (f" · vcd {sim['vcd_path']}" if sim.get("vcd_path") else ""))
        if sim.get("sim_output"):
            code_block("sim_output", sim["sim_output"], live=live)
    c = fr.get("compile")
    if c:
        st.markdown("**Compile**")
        st.write(f"status {c.get('status') or '?'}" + (f" · {c['error_class']}" if c.get("error_class") else ""))
        if c.get("status") == "error":
            srcs = (p or {}).get("src_files") or []
            for i, r in enumerate(c.get("results") or []):
                if r.get("status") != "error" and not r.get("errors_found"):
                    continue
                name = srcs[i] if i < len(srcs) else f"file #{i + 1}"
                text = "\n--- stderr ---\n".join(s for s in (r.get("stdout"), r.get("stderr")) if s and s.strip())
                code_block(f"{name} ({r.get('error_class') or 'error'})", text or "(no compiler output)", live=live)
    if isinstance(fr.get("warnings"), list) and fr["warnings"]:
        st.markdown("**Warnings**")
        bullet_list(fr["warnings"])


def fill_implementation(fr: dict, live=False):
    verdict_head(fr)
    if fr.get("error"):
        st.error(f"Error: {fr['error']}")
    p = fr.get("project")
    part = fr.get("part") or (fr.get("scripts") or {}).get("part") or "?"
    if p:
        st.write(f"project {p.get('name') or '?'} · top {p.get('top_entity') or '?'} · src "
                 f"{', '.join(p.get('src_files') or []) or '-'} · xdc {p.get('xdc_file') or '-'} · part {part}")
    elif fr.get("project_dir"):
        st.write(f"project dir {fr['project_dir']} · part {part}")
    m = fr.get("metrics")
    if isinstance(m, dict) and m:
        st.markdown("**Metrics**")
        if m.get("unconstrained"):
            st.warning("unconstrained: no timing constraints applied, timing figures are not meaningful")

        def num(v, unit=""):
            return "-" if v is None or v == "" else f"{v}{unit}"

        def util(used, pct):
            return "-" if used is None and pct is None else f"{num(used)}{f' ({pct}%)' if pct is not None else ''}"

        rows = [("WNS", num(m.get("wns_ns"), " ns")), ("TNS", num(m.get("tns_ns"), " ns")),
                ("WHS", num(m.get("whs_ns"), " ns")), ("Failing endpoints", num(m.get("failing_endpoints"))),
                ("LUT", util(m.get("lut_used"), m.get("lut_pct"))), ("FF", util(m.get("ff_used"), m.get("ff_pct"))),
                ("BRAM", util(m.get("bram_used"), m.get("bram_pct"))), ("DSP", util(m.get("dsp_used"), m.get("dsp_pct")))]
        st.dataframe(pd.DataFrame(rows, columns=["metric", "value"]), hide_index=True)
        if isinstance(m.get("reports"), list) and m["reports"]:
            st.markdown("**Reports**")
            bullet_list(m["reports"])
    elif isinstance(m, dict):
        st.write("Metrics: none collected")
    append_analysis(fr.get("analysis"))
    s = fr.get("scripts")
    if s:
        st.markdown("**Constraints**")
        if s.get("generated_xdc") is False:
            st.write(f"project XDC: {s.get('xdc') or (p or {}).get('xdc_file') or '?'}")
        elif s.get("constraints"):
            code_block(s.get("xdc") or "generated.xdc", s["constraints"], filename=s.get("xdc") and os.path.basename(s["xdc"]) or "generated.xdc", live=live)
        else:
            st.write(f"xdc {s.get('xdc') or '-'} · no constraints text")
        if s.get("tcl"):
            st.write(f"tcl {s['tcl']}")
    b = fr.get("batch")
    if b:
        st.markdown("**Batch**")
        txt = f"status {b.get('status') or '?'}" + (f" · {b['error_class']}" if b.get("error_class") else "") + \
            (f" · journal {b['journal']}" if b.get("journal") else "") + \
            (f" · reports {b['reports_dir']}" if b.get("reports_dir") else "")
        (st.error if b.get("status") == "error" else st.write)(txt)
        if b.get("log_excerpt"):
            code_block("log_excerpt", b["log_excerpt"], live=live)
    if isinstance(fr.get("warnings"), list) and fr["warnings"]:
        st.markdown("**Warnings**")
        bullet_list(fr["warnings"])


def append_code_blocks(result, live=False) -> int:
    entries = [(k, v) for k, v in (result or {}).items()
               if isinstance(v, dict) and isinstance(v.get("code"), str) and v["code"]] if isinstance(result, dict) else []
    if not entries:
        return 0
    st.markdown("**Generated code**")
    for key, v in entries:
        ent = v.get("entity_name") or "?"
        code_block(f"{key} — entity {ent}", v["code"], "vhdl",
                   filename=f"{ent if ent != '?' else key}.vhd", live=live)
    return len(entries)


def fill_spec(fr: dict, live=False):
    status_line(fr)
    if fr.get("error"):
        st.error(f"Error: {fr['error']}")
    if fr.get("pending_question"):
        st.warning(f"Pending question: {fr['pending_question']}")
    if isinstance(fr.get("suggestions"), list):
        st.markdown("**Suggestions**")
        render_suggestions(fr["suggestions"])
    if fr.get("spec"):
        code_block(f"spec.json — entity {fr['spec'].get('entity_name') or '?'}", json.dumps(fr["spec"], indent=2),
                   "json", filename="spec.json", live=live, mime="application/json")


def fill_code_agent(fr: dict, key: str, live=False):
    status_line(fr)
    if fr.get("error"):
        st.error(f"Error: {fr['error']}")
    append_code_blocks(fr, live)
    notes = (fr.get(key) or {}).get("notes") if isinstance(fr.get(key), dict) else None
    if notes:
        st.markdown("**Notes**")
        bullet_list(notes)
    if fr.get("lint"):
        st.markdown("**Lint**")
        render_lint(fr["lint"])
    append_warnings(fr)


def render_vectors_table(validation: dict, live=False):
    """CSV preview (first 50 rows), copy-friendly text, download, validation issues."""
    issues = validation.get("issues") if isinstance(validation.get("issues"), list) else []
    if validation.get("csv"):
        f = load_run_file(validation["csv"])
        if not f or f.get("error"):
            st.warning(f"could not load CSV ({(f or {}).get('error', 'unknown')}): {validation['csv']}")
        else:
            text = f.get("content") or ""
            rows = list(csv.reader(io.StringIO(text)))
            header, body = (rows[0], rows[1:]) if rows else ([], [])
            cols = ", ".join(validation.get("columns") or header)
            st.caption(f"{len(body)} rows · columns: {cols}" + (" (showing first 50)" if len(body) > 50 else "")
                       + (" · file truncated at 2 MB" if f.get("truncated") else ""))
            if header:
                width = len(header)
                st.dataframe(pd.DataFrame([r + [""] * (width - len(r)) for r in body[:50]], columns=header),
                             hide_index=True, width="stretch")
            if not live:
                st.download_button("Download CSV", text, file_name="test_vectors.csv", mime="text/csv", key=nk("csv"))
                if st.checkbox("Show CSV as text (copy button inside)", key=nk("csvtxt")):
                    st.code(text, language="csv")
    st.markdown("**Validation**")
    bullet_list([f"[{i.get('code')}] {i.get('detail')}" for i in issues], "No validation issues")


def fill_test_vectors(fr: dict, live=False):
    status_line(fr)
    if fr.get("error"):
        st.error(f"Error: {fr['error']}")
    if fr.get("run_dir"):
        st.write(f"run dir {fr['run_dir']}")
    v = fr.get("validation")
    if v:
        render_vectors_table(v, live)
    elif fr.get("result"):
        code_block("result", json.dumps(fr["result"], indent=2), "json")
    append_warnings(fr)


def fill_project(fr: dict, live=False):
    status_line(fr)
    if fr.get("error"):
        st.error(f"Error: {fr['error']}")
    if fr.get("project_dir"):
        st.write(f"project dir {fr['project_dir']}")
    st.markdown("**Files**")
    bullet_list(fr.get("files") if isinstance(fr.get("files"), list) else [], "No files listed")


ORCH_SECTIONS = [
    ("spec", "Spec", fill_spec),
    ("rtl", "RTL", lambda fr, live: fill_code_agent(fr, "rtl", live)),
    ("testbench", "Testbench", lambda fr, live: fill_code_agent(fr, "testbench", live)),
    ("test_vectors", "Test vectors", fill_test_vectors),
    ("project", "Project", fill_project),
    ("simulation", "Simulation", fill_simulation),
    ("implementation", "Implementation", fill_implementation),
]


def produced_items(results: dict) -> list[str]:
    r = results or {}
    o = lambda v: isinstance(v, dict)  # noqa: E731
    items = []
    if o(r.get("spec")):
        items.append(f"Spec{' for ' + r['spec']['entity_name'] if r['spec'].get('entity_name') else ''}")
    if o(r.get("rtl")):
        items.append(f"RTL{' (' + r['rtl']['entity_name'] + ')' if r['rtl'].get('entity_name') else ''}")
    if o(r.get("testbench")):
        items.append(f"Testbench{' (' + r['testbench']['entity_name'] + ')' if r['testbench'].get('entity_name') else ''}")
    if o(r.get("test_vectors")):
        n = r["test_vectors"].get("num_vectors")
        items.append(f"Test vectors{f' ({n} rows)' if n is not None else ''}")
    if isinstance(r.get("project_dir"), str) and r["project_dir"]:
        items.append(f"Project folder: {r['project_dir']}")
    if o(r.get("simulation")):
        items.append(f"Simulation: {r['simulation'].get('verdict') or 'no verdict'}")
    if o(r.get("implementation")):
        items.append(f"Implementation: {r['implementation'].get('verdict') or 'no verdict'}")
    return items


def render_orchestrator_panel(fr: dict, live=False):
    st.subheader("Orchestrator")
    status = fr.get("status") or "UNKNOWN"
    st.markdown(badge(status, f"{STATUS_LABEL.get(status, status)} ({status})"))
    if fr.get("summary"):
        st.markdown("**Summary**")
        st.write(fr["summary"])
    if fr.get("next_steps"):
        st.markdown("**Next steps**")
        st.write(fr["next_steps"])
    if fr.get("error"):
        st.error(f"Error: {fr['error']}")
    results = fr.get("results") if isinstance(fr.get("results"), dict) else {}
    produced = produced_items(results)
    if produced:
        st.markdown("**What was produced**")
        bullet_list(produced)
    present = [s for s in ORCH_SECTIONS if isinstance(results.get(s[0]), dict)]
    if not present:
        st.write("No agent results recorded.")
    for key, title, fill in present:
        sub = results[key]
        with st.expander(f"{title} ({key})" + (f" — {sub['status']}" if sub.get("status") else ""), expanded=True):
            try:
                fill(sub, live)
            except Exception as err:
                st.error(f"Could not render: {err}")
                code_block(f"{key} (raw)", json.dumps(sub, indent=2, default=str), "json")


# ───────────────────────── one run, fully drawn ─────────────────────────

def context_summary(llm_calls) -> str:
    sizes = [c["input_tokens"] for c in llm_calls or [] if c.get("input_tokens") is not None]
    if not sizes:
        return ""
    return f" | largest context {fmt_tok(max(sizes))} tok, total input {fmt_tok(sum(sizes))} tok"


def render_run(run: dict, live: bool = False):
    ctx = make_ctx(run)
    fr = run.get("final_result") if isinstance(run.get("final_result"), dict) else {}
    err = run.get("error") or fr.get("error")
    cancelled = fr.get("status") == "CANCELLED" or run.get("status") == "stopped"
    meta = (f"run {run['id'][:8]} | {fmt_time(run.get('started_at'))} | {run['agent_name']} v{run.get('agent_version') or ''}"
            f" | {run['status']} | {fmt_ms(run['duration_ms']) if run.get('duration_ms') is not None else '-'}"
            f" | model {run.get('model') or ''} | {len(run.get('llm_calls') or [])} LLM call(s), "
            f"{len(run.get('tool_calls') or [])} tool call(s)" + context_summary(run.get("llm_calls")))
    if fr.get("status"):
        meta += f" | agent: {STATUS_LABEL.get(fr['status'], fr['status'])} ({fr['status']})"
    if err:
        meta += f" | error: {err}"
    kind = "stopped" if cancelled else "failed" if (err or run["status"] == "failed") else run["status"]
    st.markdown(f"{badge(kind)}  {meta}")

    # "Use as input for <agent>" buttons on producer runs
    if not live:
        key = next((k for k, p in PRODUCERS.items() if p["agent"] == run.get("agent_name")), None)
        value = produced_value(run, key) if key else None
        consumers = [a for a in load_agents() if key and key in (a.get("input_template") or {})] if value is not None else []
        if consumers:
            cols = st.columns(len(consumers))
            for col, a in zip(cols, consumers):
                col.button(f"Use as input for {a['name']}", key=nk("use"), on_click=cb_use_as_input,
                           args=(a["name"], key, value, run["id"]))
        if run["status"] == "running" and not st.session_state.get("active_run"):
            st.button("Follow live", key=nk("follow"), on_click=cb_follow, args=(run["id"],),
                      help="This run is still in progress: watch it update live.")

    render_timeline(run, ctx, live)

    st.markdown("**LLM calls (thinking)**")
    calls = run.get("llm_calls") or []
    if not calls:
        st.caption("none")
    for c in calls:
        out = c.get("output") or {}
        tcs = out.get("tool_calls") or []
        title = (f"#{c['step']} {c.get('model') or ''} | {fmt_ms(c.get('latency_ms'))} | tokens in {fmt_tok(c.get('input_tokens'))}"
                 f" / out {fmt_tok(c.get('output_tokens'))} | prompt {c.get('prompt_version') or '-'} | {c.get('status')}"
                 + (f" | calls {', '.join(t.get('name', '?') for t in tcs)}" if tcs else ""))
        with st.expander(title):
            if live:
                st.caption("(full text available when the run is finished)")
                continue
            body = "── INPUT ──\n" + "\n\n".join(
                f"[{m.get('role')}] {m['content'] if isinstance(m.get('content'), str) else json.dumps(m.get('content'))}"
                for m in c.get("input") or [])
            oc = out.get("content")
            body += "\n\n── OUTPUT ──\n" + (f"ERROR: {c['error']}" if c.get("error") else oc if isinstance(oc, str) else json.dumps(oc))
            if tcs:
                body += "\n\n── TOOL CALLS ──\n" + json.dumps(tcs, indent=2)
            st.code(body, language=None, wrap_lines=True)

    st.markdown("**Tool calls**")
    tcalls = run.get("tool_calls") or []
    if not tcalls:
        st.caption("none")
    for t in tcalls:
        with st.expander(f"#{t['step']} {t['tool_name']} | {fmt_ms(t.get('latency_ms'))} | {t.get('status')}"):
            if live:
                st.caption("(full text available when the run is finished)")
                continue
            o = t.get("output")
            body = "── INPUT ──\n" + json.dumps(t.get("input"), indent=2, default=str) + "\n\n── OUTPUT ──\n" + \
                (f"ERROR: {t['error']}" if t.get("error") else o if isinstance(o, str) else json.dumps(o, indent=2, default=str))
            st.code(body, language=None, wrap_lines=True)

    # Waiting for the user: question + answer box -> resume the same session
    if not live and fr.get("status") == "WAITING_FOR_USER" and fr.get("session_id"):
        st.subheader("The agent is waiting for you")
        st.info(fr.get("pending_question") or "")
        ans = st.text_area("Your answer", key=f"answer_{run['id']}", height=90, placeholder="Your answer...")
        if st.button("Answer & resume session", key=f"answer_btn_{run['id']}", disabled=bool(st.session_state.get("active_run"))):
            if not ans.strip():
                st.error("Type an answer first.")
            else:
                try:
                    data = api("POST", f"/api/agents/{run['agent_name']}/resume",
                               json_body={"session_id": fr["session_id"], "answer": ans})
                    cb_follow(data["run_id"])
                    set_msg("info", "Resuming...")
                    st.rerun()
                except ApiError as e:
                    st.error(f"Error: {e}")

    if run.get("agent_name") == "orchestrator" and fr:
        render_orchestrator_panel(fr, live)
    if run.get("agent_name") == "simulation_agent" and fr:
        st.subheader("Simulation")
        fill_simulation(fr, live)
    if run.get("agent_name") == "implementation_agent" and fr:
        st.subheader("Implementation")
        fill_implementation(fr, live)
    if isinstance(fr.get("suggestions"), list):
        st.subheader("Suggestions")
        render_suggestions(fr["suggestions"])
    if run.get("agent_name") != "orchestrator" and append_code_blocks_header(fr, live):
        pass
    v = fr.get("validation") if isinstance(fr.get("validation"), dict) else None
    if v and v.get("csv") and run.get("agent_name") != "orchestrator":
        st.subheader("Test vectors")
        render_vectors_table(v, live)

    st.markdown("**Final result**")
    st.code(json.dumps(run["final_result"], indent=2, default=str) if run.get("final_result") is not None
            else (run.get("error") or "(no result)"), language="json", wrap_lines=True)


def append_code_blocks_header(fr: dict, live: bool) -> int:
    return append_code_blocks(fr, live)


# ───────────────────────── Agents tab ─────────────────────────

def format_run_status_text(run: dict, started: float) -> tuple[str, str]:
    agent = run.get("final_result") if isinstance(run.get("final_result"), dict) else {}
    stopped = run.get("status") == "stopped"
    text = f"{run['status']}{' · ' + agent['status'] if agent.get('status') else ''} in {time.time() - started:.1f}s"
    if stopped or agent.get("status") == "CANCELLED":
        return "warn", text
    if run.get("status") == "failed" or agent.get("status") in ("FAILED", "NEEDS_REVIEW"):
        return "error", text
    return "ok", text


def show_msg():
    m = st.session_state.get("agent_msg")
    if not m:
        return
    kind, text = m
    {"error": st.error, "warn": st.warning, "ok": st.success}.get(kind, st.info)(text)


def follow_live(active: dict):
    """Follow a running agent over SSE: redraw the run on every recorded event."""
    rid = active["id"]
    status_ph = st.empty()
    panel_ph = st.empty()
    started = active.get("started") or time.time()
    last_draw = 0.0

    def draw(force=False):
        nonlocal last_draw
        if not force and time.time() - last_draw < 0.7:
            return
        last_draw = time.time()
        try:
            run = api("GET", f"/api/agent-runs/{rid}")
        except ApiError:
            return
        with panel_ph.container():
            render_run(run, live=True)

    draw(True)
    status_ph.info("running... (live)")
    try:
        for name, data in sse_events(f"/api/agent-runs/{rid}/stream"):
            if name == "step":
                dd = data.get("data") or {}
                verb = "stopping… finishing the current step:" if st.session_state.get("stopping") else "running..."
                msg = f"{verb} {data.get('event_type')}" + (f" {dd.get('node') or dd.get('stage')}" if dd.get("node") or dd.get("stage") else "")
                status_ph.info(msg)
                draw()
            elif name == "done":
                try:
                    run = api("GET", f"/api/agent-runs/{rid}")
                except ApiError:
                    run = data
                kind, text = format_run_status_text({**run, "status": data.get("status", run.get("status"))}, started)
                set_msg(kind, text)
                st.session_state["active_run"] = None
                st.session_state["stopping"] = False
                st.rerun()
            elif name == "error":
                raise ApiError(data.get("detail", "stream error"))
    except (httpx.HTTPError, ApiError) as e:
        set_msg("error", f"stream lost — {e}. Pick the run again from Previous runs.")
        st.session_state["active_run"] = None
        st.session_state["stopping"] = False
        st.rerun()


def render_agents_run_view(agent: dict):
    active = st.session_state.get("active_run")
    with st.container(border=True):
        l, r = st.columns([1, 1])
        l.markdown("**Input**")
        if any(k in (agent.get("input_template") or {}) for k in PRODUCERS):
            r.button("Fill from latest runs", key="fill_btn", on_click=cb_fill_latest, disabled=bool(active),
                     help="Replace spec / rtl / testbench fields with the latest results of the producing agents")
        examples = [(lbl, obj) for lbl, obj in AGENT_EXAMPLES.get(agent["name"], [])]
        if "spec_text" in (agent.get("input_template") or {}):
            examples += [(lbl, {**agent["input_template"], "spec_text": txt}) for lbl, txt in EXAMPLE_SPECS]
        if examples:
            cols = st.columns(len(examples))
            for i, (col, (lbl, obj)) in enumerate(zip(cols, examples)):
                col.button(lbl, key=f"ex_{agent['name']}_{i}", on_click=cb_set_input, args=(obj,))
        st.text_area("Input", key="agent_input", height=260, label_visibility="collapsed",
                     placeholder="Plain text for orchestrator / spec analyzer, or JSON for the other agents")
        if "sim_time" in (agent.get("input_template") or {}):
            st.caption(f"{SIM_TIME_HINT} (default 10us).")
        b1, b2, b3 = st.columns([1, 1, 4])
        run_clicked = b1.button("Run agent", key="run_btn", type="primary", disabled=bool(active))
        if active:
            b2.button("Stop" if not st.session_state.get("stopping") else "stopping…", key="stop_btn",
                      on_click=cb_stop, args=(active["id"],), disabled=bool(st.session_state.get("stopping")),
                      help="Ask the running agent to stop at its next step")

    if run_clicked:
        inp, perr = parse_agent_input(agent, st.session_state.get("agent_input", ""))
        serr = None if perr else sim_time_error(inp)
        if perr or serr:
            set_msg("error", perr or serr)
        else:
            try:
                data = api("POST", f"/api/agents/{agent['name']}/run", json_body={"input": inp})
                cb_follow(data["run_id"])
                set_msg("info", f"Starting {agent['name']}...")
            except ApiError as e:
                set_msg("error", f"Error: {e}")
        st.rerun()

    active = st.session_state.get("active_run")
    if active:
        follow_live(active)  # ends with st.rerun()
        return
    show_msg()
    rid = st.session_state.get("viewed_run")
    if rid:
        try:
            run = api("GET", f"/api/agent-runs/{rid}")
        except ApiError as e:
            st.error(f"Could not load run: {e}")
            return
        with st.container(border=True):
            render_run(run)


def history_df(runs: list[dict]) -> tuple[pd.DataFrame, list[str]]:
    rows = []
    for r in runs:
        raw = run_badge_status(r)
        text = r.get("status") or ""
        if r.get("agent_status"):
            text = f"{text} · {r['agent_status']}"
        err = (r.get("error") or r.get("agent_error") or "")[:80]
        rows.append({"Started": fmt_time(r.get("started_at")), "Agent": f"{r['agent_name']} v{r.get('agent_version') or ''}",
                     "Status": f"{STATUS_LABEL.get(raw, raw)} ({text})" if text else "",
                     "Duration": fmt_ms(r["duration_ms"]) if r.get("duration_ms") is not None else "-",
                     "Error": err, "_raw": raw})
    return pd.DataFrame(rows, columns=["Started", "Agent", "Status", "Duration", "Error", "_raw"]), [r["id"] for r in runs]


def render_history(agent_name: str):
    runs = api("GET", "/api/agent-runs", params={"agent_name": agent_name, "limit": 30})
    st.caption("Previous runs — click one to open it in the Run view")
    if not runs:
        st.info("No runs yet for this agent.")
        return
    df, ids = history_df(runs)
    shown = df.drop(columns=["_raw"])
    styler = shown.style.apply(lambda s: [f"background-color: {dict(green='#d4edda', orange='#ffe8c2', red='#f8d7da', gray='#e2e3e5', blue='#d6e4ff')[status_color(x)]}" for x in df["_raw"]], subset=["Status"])
    ev = st.dataframe(styler, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                      key=f"hist_{agent_name}_{st.session_state.get('hist_n', 0)}")
    picked = None
    if ev.selection.rows:
        picked = ids[ev.selection.rows[0]]
    # Enhancement: dropdown picker as an alternative to clicking
    choice = st.selectbox("Or pick a run", ["—"] + ids, key=f"hist_pick_{agent_name}",
                          format_func=lambda i: i if i == "—" else
                          f"{fmt_time(next(r['started_at'] for r in runs if r['id'] == i))}  ·  {i[:8]}  ·  "
                          f"{STATUS_LABEL.get(run_badge_status(next(r for r in runs if r['id'] == i)), '')}")
    if choice != "—":
        picked = choice
    if picked:
        st.session_state["_goto_run"] = picked
        st.session_state["hist_n"] = st.session_state.get("hist_n", 0) + 1
        st.session_state.pop(f"hist_pick_{agent_name}", None)
        st.rerun()


def render_agents_tab():
    agents = load_agents()
    names = [a["name"] for a in agents]
    ver = {a["name"]: a["version"] for a in agents}
    if "agent_name" not in st.session_state:
        st.session_state["agent_name"] = names[0]
        st.session_state["agent_input"] = template_text(agents[0])
    goto = st.session_state.pop("_goto_run", None)
    if goto:
        st.session_state["viewed_run"] = goto
        st.session_state["agents_view"] = "Run"
    st.session_state.setdefault("agents_view", "Run")

    c1, c2, c3 = st.columns([2, 4, 3])
    c1.selectbox("Agent", names, key="agent_name", format_func=lambda n: f"{n} v{ver[n]}", on_change=on_agent_change)
    agent = agent_by_name(st.session_state["agent_name"])
    c2.caption(agent["description"])
    c3.radio("View", ["Run", "Previous runs"], key="agents_view", horizontal=True, label_visibility="collapsed")

    if st.session_state["agents_view"] == "Run":
        render_agents_run_view(agent)
    else:
        if st.button("Refresh", key="hist_refresh"):
            st.rerun()
        auto = st.session_state.get("auto_refresh", False)
        st.fragment(run_every=5 if auto else None)(lambda: render_history(agent["name"]))()


# ───────────────────────── Pipeline tab ─────────────────────────

def pipe_status_badge(status) -> str:
    return badge(status)


def render_pipeline_tab():
    P = st.session_state.setdefault("pipe", {})
    with st.container(border=True):
        st.markdown("**Requirement text**")
        cols = st.columns(len(EXAMPLE_SPECS))
        for i, (col, (lbl, txt)) in enumerate(zip(cols, EXAMPLE_SPECS)):
            col.button(lbl, key=f"pex_{i}", on_click=lambda t=txt: st.session_state.__setitem__("pipe_input", t))
        st.text_area("Requirement text", key="pipe_input", height=220, label_visibility="collapsed",
                     placeholder="Describe the hardware you need...")
        start = st.button("Start Pipeline", key="pipe_start", type="primary")
        if P.get("start_msg"):
            (st.error if P.get("start_err") else st.success)(P["start_msg"])

    if start:
        text = st.session_state.get("pipe_input", "")
        if not text.strip():
            P.clear()
            P.update(start_msg="Enter a requirement first.", start_err=True)
            st.rerun()
        P.clear()
        try:
            with st.spinner("Analyzing spec..."):
                data = api("POST", "/api/pipeline/start", json_body={"requirement_text": text}, timeout=None)
            P["thread_id"] = data.get("thread_id")
            if data.get("status") == "awaiting_review":
                st.session_state["pipe_spec"] = json.dumps(data["review"]["spec"], indent=2)
                P.update(suggestions=data["review"].get("suggestions"), timings=data.get("timings"),
                         review=True, review_shown_at=time.time(),
                         start_msg="Spec ready — review below, then approve.", start_err=False)
            else:
                P.update(start_msg="Unexpected pipeline status: " + str(data.get("status")), start_err=True)
        except ApiError as e:
            P.update(start_msg=f"Error: {e}", start_err=True)
        st.rerun()

    if P.get("timings"):
        with st.container(border=True):
            st.markdown("**Steps**")
            render_timings(P["timings"])

    if P.get("review"):
        with st.container(border=True):
            st.markdown("**Suggestions**")
            render_suggestions(P.get("suggestions"))
            st.markdown("**Spec review (edit the JSON if needed, then approve)**")
            st.text_area("Spec JSON", key="pipe_spec", height=380, label_visibility="collapsed")
            approve = st.button("Approve & Generate", key="pipe_approve", type="primary")
            if P.get("approve_msg"):
                (st.error if P.get("approve_err") else st.success)(P["approve_msg"])
        if approve:
            P.pop("approve_msg", None)
            try:
                spec = json.loads(st.session_state.get("pipe_spec", ""))
            except ValueError as err:
                P.update(approve_msg=f"Spec JSON is invalid: {err}", approve_err=True)
                st.rerun()
            if not P.get("thread_id"):
                P.update(approve_msg="No pipeline in progress — start one first.", approve_err=True)
                st.rerun()
            try:
                with st.spinner("Generating RTL, test vectors and testbench..."):
                    data = api("POST", f"/api/pipeline/{P['thread_id']}/resume", json_body={"spec": spec}, timeout=None)
                if data.get("status") == "done":
                    timings = list(data.get("timings") or [])
                    if P.get("review_shown_at"):  # measured human wait (time between review shown and Approve)
                        at = len(timings)
                        for i in range(len(timings) - 1, -1, -1):
                            if timings[i].get("status") == "interrupted":
                                at = i + 1
                                break
                        timings.insert(at, {"step": "human_review (wait)", "status": "human",
                                            "duration_ms": (time.time() - P["review_shown_at"]) * 1000})
                    P.update(timings=timings, rtl=data.get("rtl") or {}, lint=data.get("lint"),
                             tv=data.get("test_vectors") or {}, tb=data.get("testbench") or {}, done=True,
                             approve_msg="RTL, test vectors and testbench generated.", approve_err=False)
                else:
                    P.update(approve_msg="Unexpected pipeline status: " + str(data.get("status")), approve_err=True)
            except ApiError as e:
                P.update(approve_msg=f"Error: {e}", approve_err=True)
            st.rerun()

    if P.get("done"):
        rtl = P.get("rtl") or {}
        with st.container(border=True):
            st.markdown("**Generated RTL**")
            ent = rtl.get("entity_name") or "?"
            st.caption(f"Entity: {ent}")
            code_block("RTL code", rtl.get("code") or "(no code returned)", "vhdl",
                       filename=f"{ent if ent != '?' else 'rtl'}.vhd")
            st.markdown("**Lint** (a spell-checker for code)")
            render_lint(P.get("lint"))
            st.markdown("**Notes**")
            bullet_list(rtl.get("notes"))

        tv = P.get("tv") or {}
        st_ = tv.get("status") or "FAILED"
        v = tv.get("validation") or {}
        with st.container(border=True):
            st.markdown("**Test vectors**")
            st.markdown(f"Status: {pipe_status_badge(st_)}")
            csv_path = (tv.get("result") or {}).get("csv_file") or ""
            if st_ == "COMPLETE":
                st.caption(f"{v.get('num_vectors', '?')} rows, columns: {', '.join(v.get('columns') or []) or '?'}"
                           + (f" ({re.split(r'[\\\\/]', csv_path)[-1]})" if csv_path else ""))
                if csv_path:  # enhancement: preview + download of the CSV
                    f = load_run_file(csv_path)
                    if f and not f.get("error"):
                        rows = list(csv.reader(io.StringIO(f.get("content") or "")))
                        if rows:
                            st.dataframe(pd.DataFrame([r + [""] * (len(rows[0]) - len(r)) for r in rows[1:51]],
                                                      columns=rows[0]), hide_index=True, width="stretch")
                            st.download_button("Download CSV", f.get("content") or "", file_name="test_vectors.csv",
                                               mime="text/csv", key="pipe_csv")
            else:
                reasons = [x for x in [tv.get("error"), *(tv.get("warnings") or []), *(v.get("issues") or [])] if x]
                st.caption("Not generated — the testbench will use hand-written checks instead."
                           + (" " + " | ".join(json.dumps(x) if not isinstance(x, str) else x for x in reasons) if reasons else ""))

        tbr = P.get("tb") or {}
        tb = tbr.get("testbench") or {}
        tb_st = tbr.get("status") or "FAILED"
        with st.container(border=True):
            st.markdown("**Testbench** (the test program that checks the RTL)")
            st.markdown(f"Status: {pipe_status_badge(tb_st)} — Entity: {tb.get('entity_name') or '?'}"
                        + (f" — Error: {tbr['error']}" if tbr.get("error") else ""))
            ent = tb.get("entity_name") or "testbench"
            code_block("Testbench code", tb.get("code") or "(no code returned)", "vhdl", filename=f"{ent}.vhd")
            st.markdown("**Lint**")
            render_lint(tbr.get("lint"))
            st.markdown("**Notes**")
            bullet_list([*(tb.get("notes") or []), *(tbr.get("warnings") or [])])


# ───────────────────────── Observability tab ─────────────────────────

def render_traces():
    traces = api("GET", "/api/traces", params={"limit": 100})
    if not traces:
        st.info("No traces recorded yet.")
        return
    rows = [{"Time": datetime.fromtimestamp(t["created_at"]).strftime("%H:%M:%S"), "Source": t.get("source") or "",
             "Model": t.get("model") or "", "Duration (ms)": f"{t['duration_ms']:.0f}" if t.get("duration_ms") is not None else "-",
             "Status": "error" if t.get("error") else "ok",
             "Output preview": (t.get("output_text") or t.get("error") or "")[:80]} for t in traces]
    df = pd.DataFrame(rows)
    styler = df.style.apply(lambda s: ["background-color: #f8d7da" if x == "error" else "background-color: #d4edda" for x in df["Status"]], subset=["Status"])
    n = st.session_state.get("trace_n", 0)
    ev = st.dataframe(styler, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key=f"traces_{n}")
    if ev.selection.rows:
        t = api("GET", f"/api/traces/{traces[ev.selection.rows[0]]['id']}")
        with st.container(border=True):
            h1, h2 = st.columns([6, 1])
            h1.markdown("**Trace detail**")
            if h2.button("Close", key=f"trace_close_{n}"):
                st.session_state["trace_n"] = n + 1
                st.rerun()
            st.write(f"Model: {t.get('model') or ''}   ·   Source: {t.get('source') or ''}   ·   "
                     f"Duration: {t['duration_ms']:.0f} ms" if t.get("duration_ms") is not None else
                     f"Model: {t.get('model') or ''}   ·   Source: {t.get('source') or ''}")
            st.markdown("**Input messages**")
            for m in t.get("input_messages") or []:
                code_block(str(m.get("role")), str(m.get("content") if m.get("content") is not None else ""))
            st.markdown("**Output**")
            if t.get("error"):
                code_block("error", str(t["error"]))
            else:
                code_block("output", t.get("output_text") or "")
            if t.get("usage"):
                code_block("usage", json.dumps(t["usage"], indent=2), "json")


def render_observability_tab():
    if st.button("Refresh", key="traces_refresh"):
        st.rerun()
    auto = st.session_state.get("auto_refresh", False)
    st.fragment(run_every=5 if auto else None)(render_traces)()


# ───────────────────────── page ─────────────────────────

def main():
    st.session_state["_nk"] = 0
    st.title("Hardware Agent")
    with st.sidebar:
        st.markdown("**Settings**")
        st.toggle("Auto-refresh history and traces (every 5 s)", key="auto_refresh")
        st.caption(f"Backend: {API}")
        st.caption("The previous web UI is still at the backend address.")
    try:
        load_agents()
    except ApiError as e:
        st.error(f"The backend is not reachable: {e}")
        st.info("Start it with: python -m uvicorn server.app:app --port 8000   "
                f"(address can be changed with the HARDWARE_AGENT_API environment variable; now {API})")
        if st.button("Try again"):
            load_agents.clear()
            st.rerun()
        st.stop()

    tab_agents, tab_pipeline, tab_obs = st.tabs(["Agents", "Pipeline", "Observability"])
    # The Agents tab can block while it follows a live run, so fill the others first.
    with tab_pipeline:
        render_pipeline_tab()
    with tab_obs:
        render_observability_tab()
    with tab_agents:
        render_agents_tab()


main()

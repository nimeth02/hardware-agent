# observability/run_store.py — SQLite store for single-agent runs:
# agent_runs / agent_events / llm_calls / tool_calls (same file as traces.db).

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "traces.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    agent_version TEXT,
    model TEXT,
    input TEXT,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL,
    duration_ms REAL,
    error TEXT,
    final_result TEXT
);
CREATE TABLE IF NOT EXISTS agent_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    step INTEGER,
    duration_ms REAL,
    status TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step INTEGER,
    model TEXT,
    prompt_version TEXT,
    input TEXT,
    output TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms REAL,
    status TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step INTEGER,
    tool_name TEXT,
    input TEXT,
    output TEXT,
    latency_ms REAL,
    status TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id);
"""


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


class AgentRunStore:
    """Persistence for one agent run and everything observed inside it."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── writes ────────────────────────────────────────────────────────────
    def create_run(self, run_id: str, agent_name: str, agent_version: str | None,
                   model: str | None, input: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_runs (id, agent_name, agent_version, model, input, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?)",
                (run_id, agent_name, agent_version, model, _dumps(input), time.time()),
            )

    def reopen_run(self, run_id: str) -> None:
        """A paused session is being resumed: the same run continues."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_runs SET status = 'running', completed_at = NULL, duration_ms = NULL, error = NULL "
                "WHERE id = ?",
                (run_id,),
            )

    def last_step(self, run_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(step) AS s FROM agent_events WHERE run_id = ?", (run_id,)).fetchone()
        return int(row["s"] or 0)

    def find_run_by_session(self, agent_name: str, session_id: str) -> str | None:
        """Latest run of `agent_name` whose final_result carries this session_id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM agent_runs WHERE agent_name = ? "
                "AND json_extract(final_result, '$.session_id') = ? ORDER BY started_at DESC LIMIT 1",
                (agent_name, session_id),
            ).fetchone()
        return row["id"] if row else None

    def finish_run(self, run_id: str, status: str, error: str | None, final_result: Any) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_runs SET status = ?, completed_at = ?, "
                "duration_ms = (? - started_at) * 1000, error = ?, final_result = ? WHERE id = ?",
                (status, now, now, error, _dumps(final_result), run_id),
            )

    def add_event(self, run_id: str, event_type: str, step: int | None, duration_ms: float | None,
                  status: str | None, data: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_events (id, run_id, timestamp, event_type, step, duration_ms, status, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), run_id, time.time(), event_type, step, duration_ms, status, _dumps(data)),
            )

    def add_llm_call(self, run_id: str, step: int, model: str | None, prompt_version: str | None,
                     input: Any, output: Any, input_tokens: int | None, output_tokens: int | None,
                     latency_ms: float, status: str, error: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO llm_calls (id, run_id, step, model, prompt_version, input, output, "
                "input_tokens, output_tokens, latency_ms, status, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), run_id, step, model, prompt_version, _dumps(input), _dumps(output),
                 input_tokens, output_tokens, latency_ms, status, error),
            )

    def add_tool_call(self, run_id: str, step: int, tool_name: str, input: Any, output: Any,
                      latency_ms: float, status: str, error: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tool_calls (id, run_id, step, tool_name, input, output, latency_ms, status, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), run_id, step, tool_name, _dumps(input), _dumps(output), latency_ms, status, error),
            )

    # ── reads ─────────────────────────────────────────────────────────────
    def list_runs(self, agent_name: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT id, agent_name, agent_version, model, status, started_at, completed_at, duration_ms, error, " \
              "final_result FROM agent_runs"
        params: tuple = ()
        if agent_name:
            sql += " WHERE agent_name = ?"
            params = (agent_name,)
        sql += " ORDER BY started_at DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, (*params, limit)).fetchall()
        runs = []
        for r in rows:
            run = dict(r)
            # A run can complete while the agent itself ended badly (e.g. the
            # iteration limit): surface the agent's own status/error alongside.
            result = _loads(run.pop("final_result"))
            run["agent_status"] = result.get("status") if isinstance(result, dict) else None
            run["agent_error"] = result.get("error") if isinstance(result, dict) else None
            runs.append(run)
        return runs

    def get_run(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                return None
            events = conn.execute(
                "SELECT * FROM agent_events WHERE run_id = ? ORDER BY step, timestamp", (run_id,)).fetchall()
            llm_calls = conn.execute(
                "SELECT * FROM llm_calls WHERE run_id = ? ORDER BY step", (run_id,)).fetchall()
            tool_calls = conn.execute(
                "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY step", (run_id,)).fetchall()
        out = dict(run)
        out["input"] = _loads(out["input"])
        out["final_result"] = _loads(out["final_result"])
        out["events"] = [{**dict(e), "data": _loads(e["data"])} for e in events]
        out["llm_calls"] = [{**dict(c), "input": _loads(c["input"]), "output": _loads(c["output"])} for c in llm_calls]
        out["tool_calls"] = [{**dict(c), "input": _loads(c["input"]), "output": _loads(c["output"])} for c in tool_calls]
        return out


store = AgentRunStore()

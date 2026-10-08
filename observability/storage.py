# observability/storage.py — SQLite persistence for LLM call traces
#
# One flat table, newest-first reads. SQLite (stdlib) keeps this dependency-free
# and durable across restarts without needing a real database server.

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "traces.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS traces (
            id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            source TEXT,
            model TEXT,
            input_messages TEXT,
            output_text TEXT,
            duration_ms REAL,
            usage TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS steps (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            step TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            duration_ms REAL,
            error TEXT
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def save_trace(trace: dict) -> str:
    trace_id = str(uuid.uuid4())
    conn = _connect()
    conn.execute(
        """
        INSERT INTO traces
            (id, created_at, source, model, input_messages, output_text, duration_ms, usage, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace_id,
            time.time(),
            trace.get("source"),
            trace.get("model"),
            json.dumps(trace.get("input_messages") or []),
            trace.get("output_text"),
            trace.get("duration_ms"),
            json.dumps(trace.get("usage")) if trace.get("usage") else None,
            trace.get("error"),
        ),
    )
    conn.commit()
    conn.close()
    return trace_id


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["input_messages"] = json.loads(d["input_messages"]) if d["input_messages"] else []
    d["usage"] = json.loads(d["usage"]) if d["usage"] else None
    return d


def list_traces(limit: int = 100) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM traces ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_trace(trace_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM traces WHERE id = ?", (trace_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def save_steps(steps: list[dict]) -> None:
    conn = _connect()
    conn.executemany(
        """
        INSERT INTO steps (id, run_id, step, status, started_at, duration_ms, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                str(uuid.uuid4()),
                s["run_id"],
                s["step"],
                s["status"],
                s["started_at"],
                s.get("duration_ms"),
                s.get("error"),
            )
            for s in steps
        ],
    )
    conn.commit()
    conn.close()


def list_steps(run_id: str) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM steps WHERE run_id = ? ORDER BY started_at", (run_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

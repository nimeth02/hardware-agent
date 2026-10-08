# services/checkpointer.py — SQLite checkpointers for pausable LangGraph agents

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from services.config import REPO_ROOT


class CheckpointStore:
    """One SQLite-backed checkpointer per agent name: <base_dir>/<name>.db"""

    def __init__(self, base_dir: Path = REPO_ROOT / "checkpoints"):
        self.base_dir = base_dir

    def sqlite(self, name: str) -> SqliteSaver:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI serves sync endpoints from a threadpool
        saver = SqliteSaver(sqlite3.connect(self.base_dir / f"{name}.db", check_same_thread=False))
        saver.setup()
        return saver


checkpoints = CheckpointStore()

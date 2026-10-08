# observability/step_tracer.py — per-node step timing for LangGraph runs,
# captured from outside the agents.
#
# The handler is injected through LangChain's configure-hook context var, so
# callers wrap a graph invocation in `observe_steps(run_id)` and every node
# (spec generation, lint, human review, ...) is timed without the agents
# passing callbacks or knowing observability exists.

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook
from langgraph.errors import GraphInterrupt

from observability.storage import save_steps


class StepTimingHandler(BaseCallbackHandler):
    """Records start/end/status of every LangGraph node in a run."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.steps: list[dict] = []
        self._open: dict[str, dict] = {}

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if not metadata or "langgraph_node" not in metadata:
            return  # the graph's own root run, not a node
        parent = self._open.get(str(parent_run_id)) if parent_run_id else None
        if parent and parent["step"] == metadata["langgraph_node"]:
            return  # LangGraph wraps a node's callable in a second run with the same metadata
        self._open[str(run_id)] = {
            "step": metadata["langgraph_node"],
            "started_at": time.time(),
            "_perf": time.perf_counter(),
        }

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._close(run_id, "ok", None)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        if isinstance(error, GraphInterrupt):
            self._close(run_id, "interrupted", None)
        else:
            self._close(run_id, "error", str(error))

    def _close(self, run_id: UUID, status: str, error: str | None) -> None:
        start = self._open.pop(str(run_id), None)
        if start is None:
            return
        self.steps.append(
            {
                "run_id": self.run_id,
                "step": start["step"],
                "status": status,
                "started_at": start["started_at"],
                "duration_ms": round((time.perf_counter() - start["_perf"]) * 1000, 1),
                "error": error,
            }
        )


_current_handler: ContextVar[StepTimingHandler | None] = ContextVar("step_timing_handler", default=None)
register_configure_hook(_current_handler, inheritable=True)


@contextmanager
def observe_steps(run_id: str) -> Iterator[StepTimingHandler]:
    """Time every graph node executed inside the block and persist the
    results under `run_id`. Yields the handler; read `.steps` after the block."""
    handler = StepTimingHandler(run_id)
    token = _current_handler.set(handler)
    try:
        yield handler
    finally:
        _current_handler.reset(token)
        if handler.steps:
            save_steps(handler.steps)

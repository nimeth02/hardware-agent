"""Stop button behaviour — no LLM, no MCP: a fake agent calls a plain
LangChain tool in a loop (each call is a recorded step), the stop flag is
flipped from another thread like the web request does, and the run must end
with status "stopped" at the next step."""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import tool

import server.app as server_app
from observability.agent_run import (RunStatus, RunStopped, STOP_MESSAGE, is_live, observe_run,
                                     request_stop)
from observability.run_store import AgentRunStore


@tool
def ping(n: int) -> str:
    """Fake tool standing in for an LLM or MCP call."""
    return f"pong {n}"


@pytest.fixture
def store(tmp_path):
    return AgentRunStore(tmp_path / "runs.db")


def _run_in_thread(store, run_id, agent):
    """Run `agent` inside observe_run on a worker thread (as the server's
    background task does); returns (thread, outcome dict)."""
    outcome: dict = {}

    def work():
        try:
            with observe_run("fake_agent", {}, store=store, run_id=run_id) as run:
                run.set_result(agent())
            outcome["ended"] = "completed"
        except RunStopped:
            outcome["ended"] = "stopped"
        except Exception as e:  # pragma: no cover - makes failures readable
            outcome["ended"] = f"error: {e}"

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t, outcome


def test_stop_flag_ends_run_at_next_step(store):
    run_id = str(uuid.uuid4())
    store.create_run(run_id, "fake_agent", "1", None, {})
    first_step_done = threading.Event()

    def agent():
        for i in range(200):
            ping.invoke({"n": i})         # the recorder checks the stop flag here
            first_step_done.set()
            time.sleep(0.01)
        return {"status": "COMPLETE"}

    t, outcome = _run_in_thread(store, run_id, agent)
    assert first_step_done.wait(5)
    assert is_live(run_id)
    assert request_stop(run_id) is True
    t.join(5)
    assert not t.is_alive()

    assert outcome["ended"] == "stopped"
    run = store.get_run(run_id)
    assert run["status"] == RunStatus.STOPPED == "stopped"
    assert run["error"] == STOP_MESSAGE
    assert len(run["tool_calls"]) < 200, "the loop must not run to the end"
    types = [e["event_type"] for e in run["events"]]
    assert "stop_requested" in types and types[-1] == "run_stopped"
    assert not is_live(run_id)


def test_stop_is_not_swallowed_by_agent_error_handling(store):
    """Agents turn any Exception into a FAILED result and carry on; the stop
    signal must get past that so the whole run unwinds."""
    run_id = str(uuid.uuid4())
    store.create_run(run_id, "fake_agent", "1", None, {})

    def agent():
        request_stop(run_id)                # user clicks Stop mid-run
        try:
            ping.invoke({"n": 1})
        except Exception:                   # the agents' usual fall-back
            return {"status": "FAILED", "error": "swallowed"}
        return {"status": "COMPLETE"}

    t, outcome = _run_in_thread(store, run_id, agent)
    t.join(5)
    assert outcome["ended"] == "stopped"
    assert store.get_run(run_id)["status"] == "stopped"


def test_run_without_stop_completes_normally(store):
    run_id = str(uuid.uuid4())
    store.create_run(run_id, "fake_agent", "1", None, {})
    t, outcome = _run_in_thread(store, run_id, lambda: ping.invoke({"n": 7}))
    t.join(5)
    assert outcome["ended"] == "completed"
    run = store.get_run(run_id)
    assert run["status"] == "completed" and run["error"] is None
    assert [c["status"] for c in run["tool_calls"]] == ["ok"]


def test_request_stop_unknown_run_returns_false():
    assert request_stop(str(uuid.uuid4())) is False


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(server_app, "run_store", store)
    return TestClient(server_app.app)


def test_stop_endpoint_unknown_run_is_404(client):
    res = client.post(f"/api/agent-runs/{uuid.uuid4()}/stop")
    assert res.status_code == 404


def test_stop_endpoint_finished_run_is_409(client, store):
    run_id = str(uuid.uuid4())
    store.create_run(run_id, "fake_agent", "1", None, {})
    store.finish_run(run_id, "completed", None, {"status": "COMPLETE"})
    res = client.post(f"/api/agent-runs/{run_id}/stop")
    assert res.status_code == 409
    assert "completed" in res.json()["detail"]


def test_stop_endpoint_stale_running_row_is_closed(client, store):
    """A row left 'running' by a server restart has nothing to interrupt: the
    endpoint closes it as stopped so the UI stops showing it as live."""
    run_id = str(uuid.uuid4())
    store.create_run(run_id, "fake_agent", "1", None, {})
    res = client.post(f"/api/agent-runs/{run_id}/stop")
    assert res.status_code == 200 and res.json()["status"] == "stopped"
    assert store.get_run(run_id)["status"] == "stopped"


def test_stop_endpoint_live_run(client, store):
    run_id = str(uuid.uuid4())
    store.create_run(run_id, "fake_agent", "1", None, {})
    started = threading.Event()

    def agent():
        for i in range(200):
            ping.invoke({"n": i})
            started.set()
            time.sleep(0.01)

    t, outcome = _run_in_thread(store, run_id, agent)
    assert started.wait(5)
    res = client.post(f"/api/agent-runs/{run_id}/stop")
    assert res.status_code == 200 and res.json()["status"] == "stopping"
    t.join(5)
    assert outcome["ended"] == "stopped"
    assert client.get(f"/api/agent-runs/{run_id}").json()["status"] == "stopped"

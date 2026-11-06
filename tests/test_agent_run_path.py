"""Every recorded event carries `path`: the graph nodes running around it,
outermost first. The UI indents rows of an inner agent (the test-vector
generator inside `generate_vectors`, say) under the outer step with it.
No LLM, no MCP: two small LangGraph graphs, one nested in the other."""

from __future__ import annotations

import uuid
from typing import TypedDict

import pytest
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from observability.agent_run import observe_run
from observability.run_store import AgentRunStore


class S(TypedDict, total=False):
    n: int


@tool
def ping(n: int) -> str:
    """Fake tool standing in for an LLM or MCP call."""
    return f"pong {n}"


@pytest.fixture
def store(tmp_path):
    return AgentRunStore(tmp_path / "runs.db")


def _inner_graph():
    def model(state: S) -> S:
        ping.invoke({"n": 2})                      # the inner agent's tool call
        return {"n": state.get("n", 0) + 1}
    g = StateGraph(S)
    g.add_node("model", model)
    g.add_edge(START, "model")
    g.add_edge("model", END)
    return g.compile()


def _outer_graph():
    inner = _inner_graph()

    def decide(state: S) -> S:
        ping.invoke({"n": 1})                      # the outer step's own tool call
        return state

    def generate_vectors(state: S) -> S:
        return inner.invoke(state)                 # a whole agent inside one step

    g = StateGraph(S)
    g.add_node("decide", decide)
    g.add_node("generate_vectors", generate_vectors)
    g.add_edge(START, "decide")
    g.add_edge("decide", "generate_vectors")
    g.add_edge("generate_vectors", END)
    return g.compile()


def test_events_carry_the_enclosing_node_path(store):
    run_id = str(uuid.uuid4())
    store.create_run(run_id, "fake_agent", "1", None, {})
    with observe_run("fake_agent", {}, store=store, run_id=run_id) as run:
        run.set_result(_outer_graph().invoke({"n": 0}))

    events = store.get_run(run_id)["events"]
    steps = {e["data"]["node"]: e["data"]["path"] for e in events if e["event_type"] == "agent_step"}
    assert steps["decide"] == []
    assert steps["generate_vectors"] == []
    assert steps["model"] == ["generate_vectors"], "the inner agent's node sits inside the outer step"

    tools = {e["data"]["input"]["n"]: e["data"]["path"] for e in events if e["event_type"] == "tool_call"}
    assert tools[1] == ["decide"]                             # outer step's own call: 1 deep
    assert tools[2] == ["generate_vectors", "model"]          # inner agent's call: 2 deep
    results = [e for e in events if e["event_type"] == "tool_result"]
    assert {tuple(e["data"]["path"]) for e in results} == {("decide",), ("generate_vectors", "model")}

    # Rows outside any node (run_started, run_completed) are untouched.
    assert "path" not in next(e for e in events if e["event_type"] == "run_started")["data"]

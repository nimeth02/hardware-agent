# agents/pipeline.py — spec_analyzer -> human review -> rtl_agent, with a
# human-in-the-loop pause between the two agents.
#
# Follows LangGraph's documented human-in-the-loop pattern: interrupt()
# pauses the "human_review" node and surfaces a payload to the caller.
# Resuming requires a checkpointer (here, SQLite, so a pause survives a
# server restart) and happens via graph.invoke(Command(resume=...), config)
# against the same thread_id that was used to start the run.

from __future__ import annotations

import json
import threading
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agents.json_utils import extract_json
from agents.rtl_agent.rtl_agent import call_rtl_agent, parse_rtl_response
from agents.rtl_agent.rtl_linter import lint_rtl
from agents.spec_analyser_agent.spec_analyzer_agent import call_spec_analyzer
from agents.spec_analyser_agent.spec_evaluator import evaluate_spec
from agents.spec_analyser_agent.spec_suggester import generate_suggestions
from services.checkpointer import checkpoints


class PipelineState(TypedDict):
    requirement_text: str
    spec: dict
    issues: list[dict]
    suggestions: list[str]
    human_decision: str
    rtl: dict
    lint: dict


def analyze_spec_node(state: PipelineState) -> dict:
    """Spec generation (LLM) followed by spec evaluation (deterministic
    structural checks) and suggestion generation — all before the spec
    ever reaches a human."""
    raw = call_spec_analyzer(state["requirement_text"])
    try:
        spec = extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        spec = {
            "status": "needs_clarification",
            "ambiguities": ["Spec analyzer did not return valid JSON.", raw],
        }
    issues = evaluate_spec(spec)
    suggestions = generate_suggestions(issues)
    return {"spec": spec, "issues": issues, "suggestions": suggestions}


def human_review_node(state: PipelineState) -> dict:
    """Pauses the graph and hands the extracted spec (plus evaluator
    suggestions) to a human. Resuming with Command(resume={"spec": {...}})
    supplies the spec the human approved (edited or not), which is what
    generate_rtl_node acts on."""
    decision = interrupt(
        {
            "type": "spec_review",
            "spec": state["spec"],
            "suggestions": state.get("suggestions", []),
        }
    )
    return {"spec": decision["spec"], "human_decision": "approved"}


def generate_rtl_node(state: PipelineState) -> dict:
    """RTL generation (LLM) followed by a lint check (static checks plus an
    xvhdl compile when Vivado is reachable)."""
    rtl = parse_rtl_response(state["spec"], call_rtl_agent(state["spec"]))
    return {"rtl": rtl, "lint": lint_rtl(state["spec"], rtl)}


def _build_graph() -> StateGraph:
    graph = StateGraph(PipelineState)
    graph.add_node("analyze_spec", analyze_spec_node)
    graph.add_node("human_review", human_review_node)
    graph.add_node("generate_rtl", generate_rtl_node)
    graph.add_edge(START, "analyze_spec")
    graph.add_edge("analyze_spec", "human_review")
    graph.add_edge("human_review", "generate_rtl")
    graph.add_edge("generate_rtl", END)
    return graph


# The checkpointer's sqlite connection is shared across requests, so
# serialize graph invocations with a lock.
_lock = threading.Lock()
_graph = _build_graph().compile(checkpointer=checkpoints.sqlite("pipeline"))


def _summarize(thread_id: str, result: dict) -> dict:
    interrupts = result.get("__interrupt__")
    if interrupts:
        return {"thread_id": thread_id, "status": "awaiting_review", "review": interrupts[0].value}
    return {
        "thread_id": thread_id,
        "status": "done",
        "spec": result.get("spec"),
        "rtl": result.get("rtl"),
        "lint": result.get("lint"),
    }


def start_pipeline(thread_id: str, requirement_text: str) -> dict:
    config = {"configurable": {"thread_id": thread_id}}
    with _lock:
        result = _graph.invoke({"requirement_text": requirement_text}, config=config)
    return _summarize(thread_id, result)


def resume_pipeline(thread_id: str, spec: dict) -> dict:
    config = {"configurable": {"thread_id": thread_id}}
    with _lock:
        result = _graph.invoke(Command(resume={"spec": spec}), config=config)
    return _summarize(thread_id, result)

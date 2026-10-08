"""Orchestrator tests — no LLM, no MCP, no Vivado: a scripted tool-calling
chat model plays the LLM and the agents (imported into tools.py) are
monkeypatched fakes; the tool loop, result storage and final-answer
handling are real."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import agents.orchestrator_agent.orchestrator_agent as orch_agent
import agents.orchestrator_agent.tools as orch

SPEC = {"entity_name": "counter4", "clock": {"required": True, "name": "clk", "frequency_mhz": 100}}
RTL = {"entity_name": "counter4", "code": "entity counter4 is end;"}
TB = {"entity_name": "counter4_tb", "code": "entity counter4_tb is end;"}


class ScriptedToolModel(FakeMessagesListChatModel):
    """Returns the scripted AI messages in order; accepts bind_tools."""

    def bind_tools(self, tools, **kwargs):
        return self


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


def final(status, summary="done"):
    return AIMessage(content=json.dumps({"status": status, "summary": summary, "next_steps": None}))


@pytest.fixture
def fakes(monkeypatch):
    calls = []
    written = {}
    monkeypatch.setattr(orch, "run_spec_analysis", lambda text: calls.append("spec") or
                        {"session_id": "s1", "status": "WAITING_FOR_USER", "pending_question": "width?", "spec": None})
    monkeypatch.setattr(orch, "resume_spec_analysis", lambda sid, ans: calls.append(f"answer:{sid}:{ans}") or
                        {"session_id": sid, "status": "COMPLETE", "pending_question": None, "spec": SPEC})
    monkeypatch.setattr(orch, "run_rtl_generation", lambda spec: calls.append("rtl") or {"status": "COMPLETE", "rtl": RTL})
    monkeypatch.setattr(orch, "run_testbench_generation", lambda spec, rtl: calls.append("tb") or {"status": "COMPLETE", "testbench": TB})
    monkeypatch.setattr(orch, "run_test_vector_generation", lambda spec: calls.append("vectors") or
                        {"status": "COMPLETE", "run_dir": "C:/runs/tv", "result": {"csv_file": "test_vectors.csv"}})
    monkeypatch.setattr(orch, "mcp_read_file", lambda d, f: "cycle,rst,en,count\n0,1,0,0\n")
    monkeypatch.setattr(orch, "mcp_write_files", lambda root, files: written.update(files))
    monkeypatch.setattr(orch, "run_simulation", lambda project, sim_time: calls.append(f"sim:{sim_time}") or
                        {"status": "COMPLETE", "verdict": "pass", "analysis": {"verdict": "pass"}})
    monkeypatch.setattr(orch, "run_implementation", lambda project, part: calls.append(f"impl:{part}") or
                        {"status": "COMPLETE", "verdict": "pass", "metrics": {"wns_ns": 8.6}})
    return calls, written


def run_with(script, max_steps=60):
    model = ScriptedToolModel(responses=script)
    original = orch_agent.get_llm
    orch_agent.get_llm = lambda: model
    try:
        return orch_agent.run_orchestration("4-bit counter", max_steps=max_steps)
    finally:
        orch_agent.get_llm = original


def test_llm_drives_the_whole_flow_through_the_tools(fakes):
    calls, written = fakes
    out = run_with([
        call("analyze_spec", requirement_text="4-bit counter"),
        call("answer_spec_question", answer="4 bits"),
        call("generate_rtl"), call("generate_testbench"), call("generate_test_vectors"),
        call("write_project"), call("simulate", sim_time="20us"), call("implement", part="xc7a35tcpg236-1"),
        final("COMPLETE", "counter implemented"),
    ])
    assert out["status"] == "COMPLETE" and out["summary"] == "counter implemented" and out["error"] is None
    assert calls == ["spec", "answer:s1:4 bits", "rtl", "tb", "vectors", "sim:20us", "impl:xc7a35tcpg236-1"]
    assert out["results"]["spec"]["spec"] == SPEC and out["results"]["simulation"]["verdict"] == "pass"
    name = Path(out["results"]["project"]["project_dir"]).name
    assert name.startswith("counter4_")
    assert sorted(written) == [f"projects/{name}/spec.json", f"projects/{name}/src/counter4.vhd",
                               f"projects/{name}/tb/counter4_tb.vhd", f"projects/{name}/test_vectors.csv"]
    assert json.loads(written[f"projects/{name}/spec.json"]) == SPEC


def test_tool_errors_go_back_to_the_llm_instead_of_crashing(fakes):
    # generate_rtl before any spec: the tool reports the missing prerequisite, the loop continues
    out = run_with([call("generate_rtl"), final("FAILED", "no spec")])
    assert out["status"] == "FAILED" and out["summary"] == "no spec" and "rtl" not in out["results"]
    assert fakes[0] == []


def test_step_limit_ends_the_run(fakes):
    out = run_with([call("analyze_spec", requirement_text="x") for _ in range(20)], max_steps=6)
    assert out["status"] == "FAILED" and "step limit" in out["error"]
    assert "spec" in out["results"]          # what was produced before the limit is still returned


def test_invalid_final_answer_is_reported(fakes):
    out = run_with([AIMessage(content="all good!")])
    assert out["status"] == "FAILED" and "orchestration failed" in out["error"]


def test_repeated_identical_results_are_flagged_to_the_llm(fakes, monkeypatch):
    seen = []
    monkeypatch.setattr(orch, "run_testbench_generation", lambda spec, rtl: seen.append(spec) or {"status": "COMPLETE", "testbench": TB})
    out = run_with([
        call("analyze_spec", requirement_text="x"), call("answer_spec_question", answer="4 bits"), call("generate_rtl"),
        call("generate_testbench"), call("generate_testbench"),
        final("NEEDS_REVIEW"),
    ])
    assert out["status"] == "NEEDS_REVIEW"
    assert seen == [SPEC, SPEC]                                               # the agent always gets the plain spec
    assert "note" not in out["results"]["testbench"]                          # the stored result never carries the note
    assert "note" in json.loads(orch.make_tools({"testbench": {"status": "COMPLETE", "testbench": TB}, "spec": {"spec": SPEC}, "rtl": {"rtl": RTL}})[3].invoke({}))


def test_tools_are_exactly_the_existing_agents():
    names = {t.name for t in orch.make_tools({})}
    assert names == {"analyze_spec", "answer_spec_question", "generate_rtl", "generate_testbench",
                     "generate_test_vectors", "write_project", "simulate", "implement"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

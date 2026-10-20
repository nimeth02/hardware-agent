"""Orchestrator tests — no LLM, no MCP, no Vivado: a scripted tool-calling
chat model plays the LLM and the agents (imported into tools.py) are
monkeypatched fakes; the tool loop and final-answer handling are real."""

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
VECTORS = {"status": "COMPLETE", "run_dir": "C:/runs/tv",
           "result": {"input_csv": "test_vectors_in.csv", "output_csv": "test_vectors_out.csv"}}


class ScriptedToolModel(FakeMessagesListChatModel):
    """Returns the scripted AI messages in order; accepts bind_tools."""

    def bind_tools(self, tools, **kwargs):
        return self


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


def analyze():
    return call("analyze_spec")                      # no arguments: the requirement comes from the state


def final(status, summary="done", next_steps=None):
    return AIMessage(content=json.dumps({"status": status, "summary": summary, "next_steps": next_steps}))


@pytest.fixture
def fakes(monkeypatch):
    calls = []
    written = {}
    monkeypatch.setattr(orch, "run_spec_analysis", lambda text: calls.append(("spec", text)) or
                        {"session_id": "s1", "status": "COMPLETE", "pending_question": None, "spec": SPEC})
    monkeypatch.setattr(orch, "run_rtl_generation", lambda spec: calls.append(("rtl", spec)) or {"status": "COMPLETE", "rtl": RTL})
    monkeypatch.setattr(orch, "run_testbench_generation", lambda spec, rtl: calls.append(("tb", spec, rtl)) or
                        {"status": "COMPLETE", "testbench": TB})
    monkeypatch.setattr(orch, "run_test_vector_generation", lambda spec: calls.append("vectors") or VECTORS)
    monkeypatch.setattr(orch.project_writer, "mcp_read_file", lambda d, f: f"{f}:cycle,...\n0,...\n")
    monkeypatch.setattr(orch.project_writer, "mcp_write_files", lambda root, files: written.update(files))
    monkeypatch.setattr(orch, "run_simulation", lambda project, sim_time: calls.append(f"sim:{project}:{sim_time}") or
                        {"status": "COMPLETE", "verdict": "pass", "analysis": {"verdict": "pass"}})
    monkeypatch.setattr(orch, "run_implementation", lambda project, part: calls.append(f"impl:{part}") or
                        {"status": "COMPLETE", "verdict": "pass", "metrics": {"wns_ns": 8.6}})
    return calls, written


def run_with(script, requirement="4-bit counter", model_cls=ScriptedToolModel):
    model = model_cls(responses=script)
    original = orch_agent.get_llm
    orch_agent.get_llm = lambda: model
    try:
        return orch_agent.run_orchestration(requirement)
    finally:
        orch_agent.get_llm = original


def test_llm_drives_the_whole_flow_passing_results_between_the_tools(fakes):
    calls, written = fakes
    out = run_with([
        analyze(), call("generate_rtl"), call("generate_testbench"), call("generate_test_vectors"),
        call("write_project"), call("simulate", sim_time="20us"), call("implement", part="xc7a35tcpg236-1"),
        final("COMPLETE", "counter implemented"),
    ])
    assert out["status"] == "COMPLETE" and out["summary"] == "counter implemented" and out["error"] is None
    name = out["requirement_id"]
    project = str(orch.project_writer.REPO_ROOT / "projects" / name)        # the tools hand spec/rtl/... to each other
    assert calls == [("spec", "4-bit counter"), ("rtl", SPEC), ("tb", SPEC, RTL), "vectors",
                     f"sim:{project}:20us", "impl:xc7a35tcpg236-1"]
    assert sorted(written) == [f"projects/{name}/spec.json", f"projects/{name}/src/counter4.vhd",
                               f"projects/{name}/tb/counter4_tb.vhd", f"projects/{name}/test_vectors_in.csv",
                               f"projects/{name}/test_vectors_out.csv"]
    assert json.loads(written[f"projects/{name}/spec.json"]) == SPEC
    assert written[f"projects/{name}/test_vectors_out.csv"].startswith("test_vectors_out.csv:")
    assert out["results"]["spec"] == SPEC and out["results"]["rtl"] == RTL   # the report carries what was produced
    assert out["results"]["project_dir"] == project and out["results"]["implementation"]["verdict"] == "pass"


def state_with(**fields):
    """A tool's view of the run's state, for calling a tool on its own."""
    from langchain.tools import ToolRuntime
    return ToolRuntime(state={"stages": {}, "test_vectors": None, **fields}, tool_call_id="t1",
                       config={}, context=None, store=None, stream_writer=None, tools=[])


def test_write_project_returns_the_folder_named_after_the_requirement_id(fakes):
    tools = {t.name: t for t in orch.make_tools()}
    cmd = tools["write_project"].invoke({"runtime": state_with(requirement_id="20261016_101500_4_bit_counter",
                                                                spec=SPEC, rtl=RTL, testbench=TB)})
    answer = json.loads(cmd.update["messages"][0].content)                  # what the LLM reads
    assert answer["status"] == "COMPLETE" and Path(cmd.update["project_dir"]).name == "20261016_101500_4_bit_counter"
    assert not any(f.endswith(".csv") for f in answer["files"])             # no vectors given -> no CSVs
    assert cmd.update["stages"] == {"write_project": {"status": "COMPLETE", "attempts": 1, "error": None}}


def test_each_helper_marks_its_status_attempts_and_error_in_the_state(fakes, monkeypatch):
    monkeypatch.setattr(orch, "run_rtl_generation",
                        lambda spec: {"status": "NEEDS_REVIEW", "rtl": RTL, "error": "lint issues remain"})
    out = run_with([analyze(), call("generate_rtl"), call("generate_rtl"), final("NEEDS_REVIEW", "rtl has issues")])
    assert out["stages"] == {"analyze_spec": {"status": "COMPLETE", "attempts": 1, "error": None},
                             "generate_rtl": {"status": "NEEDS_REVIEW", "attempts": 2, "error": "lint issues remain"}}
    assert out["results"]["spec"] == SPEC and out["results"]["rtl"] == RTL
    tools = {t.name: t for t in orch.make_tools()}
    assert set(tools["simulate"].args) == {"sim_time"}                      # the AI passes settings only,
    assert tools["analyze_spec"].args == {} and tools["generate_rtl"].args == {}   # never the requirement, spec or code


def test_requirement_id_is_name_then_time_then_date():
    from datetime import datetime
    from agents.orchestrator_agent.requirement_id import new_requirement_id
    at = datetime(2026, 10, 16, 11, 35)
    assert new_requirement_id("Design a 4-bit UP counter: clk 100 MHz!", at) == "design_a_4_bit_up_counter_clk_T-11.35_D-16.10.2026"
    assert new_requirement_id("   ", at) == "requirement_T-11.35_D-16.10.2026"
    assert not set(':/\\') & set(new_requirement_id("a/b:c", at))        # always a valid folder name


def test_a_crashed_agent_fails_the_run(fakes, monkeypatch):
    def boom(spec):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(orch, "run_rtl_generation", boom)
    out = run_with([analyze(), call("generate_rtl"), final("FAILED", "rtl failed")])
    assert out["status"] == "FAILED"
    assert out["error"] is None or "ollama down" in out["error"]      # relayed to the LLM, or ended the run


def test_runaway_loop_hits_the_runtime_step_limit(fakes):
    out = run_with([analyze() for _ in range(40)])
    assert out["status"] == "FAILED" and "orchestration failed" in out["error"]


def test_invalid_final_answer_is_reported(fakes):
    out = run_with([AIMessage(content="all good!")])
    assert out["status"] == "FAILED" and "orchestration failed" in out["error"]


def test_a_spec_question_is_reported_to_the_human_not_answered(fakes, monkeypatch):
    monkeypatch.setattr(orch, "run_spec_analysis", lambda text:
                        {"session_id": "s1", "status": "WAITING_FOR_USER", "pending_question": "bus width?", "spec": None})
    out = run_with([analyze(), final("NEEDS_REVIEW", "spec unclear", "bus width?")])
    assert out["status"] == "NEEDS_REVIEW" and out["next_steps"] == "bus width?"
    assert out["results"]["spec"] is None                                   # nothing usable was produced


def test_manager_decides_from_the_chat_so_far(fakes):
    seen = []

    class Spy(ScriptedToolModel):
        def _generate(self, messages, *args, **kwargs):
            seen.append(messages)
            return super()._generate(messages, *args, **kwargs)

    run_with([analyze(), call("generate_rtl"), call("generate_testbench"), final("FAILED", "stop")], model_cls=Spy)
    assert [len(m) for m in seen] == [2, 4, 6, 8]                           # rules + requirement, then +1 AI call +1 answer per tool
    assert [m.type for m in seen[-1]] == ["system", "human", "ai", "tool", "ai", "tool", "ai", "tool"]
    assert [m.name for m in seen[-1] if m.type == "tool"] == ["analyze_spec", "generate_rtl", "generate_testbench"]
    assert "counter4" in seen[-1][3].content                                # a helper's answer reaches the AI


def test_requirement_is_recorded_in_the_state_and_shown_to_the_llm_from_there(fakes):
    calls, _ = fakes
    seen = []

    class Spy(ScriptedToolModel):
        def _generate(self, messages, *args, **kwargs):
            seen.append(messages)
            return super()._generate(messages, *args, **kwargs)

    out = run_with([analyze(), final("FAILED", "stop")], requirement="  Design a 4-bit UP counter  ", model_cls=Spy)
    first = seen[0][1]
    assert first.type == "human" and first.content != "  Design a 4-bit UP counter  "       # not the raw text
    assert first.content.startswith(f"REQUIREMENT (run {out['requirement_id']})")
    assert "Design a 4-bit UP counter" in first.content
    assert calls == [("spec", "  Design a 4-bit UP counter  ")]           # the spec tool read the state, not the LLM


def test_every_step_is_logged_with_inputs_outputs_and_state_before_and_after(fakes, monkeypatch):
    import agents.orchestrator_agent.tracing as tracing
    logged = []
    monkeypatch.setattr(tracing, "emit_event", lambda kind, status, data: logged.append((kind.value, data)))
    run_with([analyze(), call("generate_rtl"), final("FAILED", "stop")], requirement="4-bit counter")
    assert [(k, d["stage"]) for k, d in logged] == [
        ("state", "start"),
        ("agent_input", "analyze_spec"), ("agent_output", "analyze_spec"), ("state", "analyze_spec"),
        ("agent_input", "generate_rtl"), ("agent_output", "generate_rtl"), ("state", "generate_rtl"),
    ]
    start, _, rtl_out, rtl_state = logged[0][1], logged[4][1], logged[5][1], logged[6][1]
    assert start["after"]["requirement_text"] == "4-bit counter" and "messages" not in start["after"]
    assert logged[4][1]["input"] == [SPEC]                                  # exactly what the RTL helper was handed
    assert rtl_out["output"] == {"status": "COMPLETE", "rtl": RTL}         # exactly what came back
    assert rtl_state["before"]["rtl"] is None and rtl_state["after"]["rtl"] == RTL
    assert rtl_state["after"]["stages"]["generate_rtl"] == {"status": "COMPLETE", "attempts": 1, "error": None}


def test_tools_are_exactly_the_existing_agents():
    names = {t.name for t in orch.make_tools()}
    assert names == {"analyze_spec", "generate_rtl", "generate_testbench", "generate_test_vectors",
                     "write_project", "simulate", "implement"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

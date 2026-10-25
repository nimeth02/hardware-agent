"""Orchestrator tests — no LLM, no MCP, no Vivado: a scripted tool-calling
chat model plays the LLM and the agents (imported into tools.py) are
monkeypatched fakes; the tool loop and final-answer handling are real."""

from __future__ import annotations

import itertools
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


_ids = itertools.count(1)


def call(name, **args):
    """One scripted tool call; ids must be unique across the chat or the loop misroutes."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}_{next(_ids)}"}])


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
    monkeypatch.setattr(orch, "run_rtl_generation", lambda spec, requirement_id, attempt, previous, last_rtl:
                        calls.append(("rtl", spec, requirement_id, attempt, previous)) or {"status": "COMPLETE", "rtl": RTL})
    monkeypatch.setattr(orch, "run_testbench_generation", lambda spec, rtl, requirement_id, attempt, history:
                        calls.append(("tb", spec, rtl, requirement_id, attempt, history)) or {"status": "COMPLETE", "testbench": TB})
    monkeypatch.setattr(orch, "run_test_vector_generation", lambda spec, run_dir, requirement_id, attempt, history:
                        calls.append(("vectors", spec, run_dir, requirement_id, attempt, history)) or VECTORS)
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
    assert calls == [("spec", "4-bit counter"), ("rtl", SPEC, name, 1, []), ("tb", SPEC, RTL, name, 1, []),
                     ("vectors", SPEC, None, name, 1, []),                   # RTL, testbench and test vector agents are
                     f"sim:{project}:20us", "impl:xc7a35tcpg236-1"]           # told: run id, 1st call, no history
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
    assert cmd.update["stages"] == {"write_project": {"status": "COMPLETE", "attempts": 1, "error": None, "attempt_history": [
        {"attempt": 1, "status": "COMPLETE", "error": None, "issues": None, "likely_cause": None}]}}


def test_each_helper_marks_its_status_attempts_and_error_in_the_state(fakes, monkeypatch):
    told = []
    issues = [{"code": "E1", "detail": "missing reset"}]
    monkeypatch.setattr(orch, "run_rtl_generation", lambda spec, requirement_id, attempt, previous, last_rtl:
                        told.append((attempt, previous)) or
                        {"status": "NEEDS_REVIEW", "rtl": RTL, "lint": {"issues": issues}, "error": "lint issues remain"})
    out = run_with([analyze(), call("generate_rtl"), call("generate_rtl"), final("NEEDS_REVIEW", "rtl has issues")])
    first = {"attempt": 1, "status": "NEEDS_REVIEW", "error": "lint issues remain", "issues": issues, "likely_cause": None}
    second = {**first, "attempt": 2}
    assert out["stages"]["analyze_spec"] == {"status": "COMPLETE", "attempts": 1, "error": None, "attempt_history": [
        {"attempt": 1, "status": "COMPLETE", "error": None, "issues": None, "likely_cause": None}]}
    assert out["stages"]["generate_rtl"] == {"status": "NEEDS_REVIEW", "attempts": 2, "error": "lint issues remain",
                                             "attempt_history": [first, second]}
    assert told == [(1, []), (2, [first])]                                  # 2nd call: the RTL agent is handed the 1st attempt
    assert out["results"]["spec"] == SPEC and out["results"]["rtl"] == RTL
    tools = {t.name: t for t in orch.make_tools()}
    assert set(tools["simulate"].args) == {"sim_time"}                      # the AI passes settings only,
    assert tools["analyze_spec"].args == {} and tools["generate_rtl"].args == {}   # never the requirement, spec or code


def test_a_failed_simulation_blames_the_rtl_and_the_rtl_agent_is_told_on_its_next_call(fakes, monkeypatch):
    analysis = {"verdict": "fail", "likely_cause": "rtl", "failures": ["cycle 3: count expected 0011 got 0010"],
                "summary": "count skips a value after reset", "suggested_fix": "increment on every rising edge"}
    monkeypatch.setattr(orch, "run_simulation", lambda project, sim_time:
                        {"status": "NEEDS_REVIEW", "verdict": "fail", "analysis": analysis, "error": "simulation fail"})
    told = []
    monkeypatch.setattr(orch, "run_rtl_generation", lambda spec, requirement_id, attempt, history, last_rtl:
                        told.append((attempt, history, last_rtl)) or {"status": "COMPLETE", "rtl": RTL})
    out = run_with([analyze(), call("generate_rtl"), call("generate_testbench"), call("write_project"),
                    call("simulate"), call("generate_rtl"), final("NEEDS_REVIEW", "stop")])
    rtl_first = {"attempt": 1, "status": "COMPLETE", "error": None, "issues": None, "likely_cause": None,
                 "downstream": [{"stage": "simulate", "verdict": "fail", "failures": analysis["failures"],
                                 "summary": analysis["summary"], "suggested_fix": analysis["suggested_fix"]}]}
    assert out["stages"]["generate_rtl"]["attempt_history"][0] == rtl_first    # the blame lands on RTL attempt 1
    assert out["stages"]["generate_rtl"]["status"] == "COMPLETE"               # its own status is untouched
    assert out["stages"]["simulate"]["attempt_history"] == [{"attempt": 1, "status": "NEEDS_REVIEW", "error": "simulation fail",
                                                             "issues": analysis["failures"], "likely_cause": "rtl"}]
    assert "downstream" not in out["stages"]["generate_testbench"]["attempt_history"][0]   # not blamed
    assert told == [(1, [], None), (2, [rtl_first], RTL)]                      # 2nd RTL call is handed the blame + its old RTL


def test_a_failed_simulation_blames_the_testbench_and_the_testbench_agent_is_told_on_its_next_call(fakes, monkeypatch):
    analysis = {"verdict": "fail", "likely_cause": "testbench", "failures": ["cannot open test_vectors_out.csv"],
                "summary": "the testbench opens the wrong file name", "suggested_fix": "open test_vectors_out.csv"}
    monkeypatch.setattr(orch, "run_simulation", lambda project, sim_time:
                        {"status": "NEEDS_REVIEW", "verdict": "fail", "analysis": analysis, "error": "simulation fail"})
    told = []
    monkeypatch.setattr(orch, "run_testbench_generation", lambda spec, rtl, requirement_id, attempt, history:
                        told.append((attempt, history)) or {"status": "COMPLETE", "testbench": TB})
    out = run_with([analyze(), call("generate_rtl"), call("generate_testbench"), call("write_project"),
                    call("simulate"), call("generate_testbench"), final("NEEDS_REVIEW", "stop")])
    tb_first = out["stages"]["generate_testbench"]["attempt_history"][0]
    assert tb_first["downstream"] == [{"stage": "simulate", "verdict": "fail", "failures": analysis["failures"],
                                       "summary": analysis["summary"], "suggested_fix": analysis["suggested_fix"]}]
    assert "downstream" not in out["stages"]["generate_rtl"]["attempt_history"][0]        # the RTL is not blamed
    assert told == [(1, []), (2, [tb_first])]                                  # 2nd testbench call is handed the blame


def test_requirement_id_is_name_then_time_then_date():
    from datetime import datetime
    from agents.orchestrator_agent.requirement_id import new_requirement_id
    at = datetime(2026, 10, 16, 11, 35)
    assert new_requirement_id("Design a 4-bit UP counter: clk 100 MHz!", at) == "design_a_4_bit_up_counter_clk_T-11.35_D-16.10.2026"
    assert new_requirement_id("   ", at) == "requirement_T-11.35_D-16.10.2026"
    assert not set(':/\\') & set(new_requirement_id("a/b:c", at))        # always a valid folder name


def test_a_crashed_agent_fails_the_run(fakes, monkeypatch):
    def boom(spec, requirement_id, attempt, previous, last_rtl):
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
    out = run_with([analyze(), call("generate_rtl"), final("FAILED", "stop")], requirement="4-bit counter")
    assert [(k, d["stage"]) for k, d in logged] == [
        ("state", "start"),
        ("agent_input", "analyze_spec"), ("agent_output", "analyze_spec"), ("state", "analyze_spec"),
        ("agent_input", "generate_rtl"), ("agent_output", "generate_rtl"), ("state", "generate_rtl"),
    ]
    start, _, rtl_out, rtl_state = logged[0][1], logged[4][1], logged[5][1], logged[6][1]
    assert start["after"]["requirement_text"] == "4-bit counter" and "messages" not in start["after"]
    assert logged[4][1]["input"] == [SPEC, out["requirement_id"], 1, [], None]   # exactly what the RTL helper was handed (no RTL yet)
    assert rtl_out["output"] == {"status": "COMPLETE", "rtl": RTL}         # exactly what came back
    assert rtl_state["before"]["rtl"] is None and rtl_state["after"]["rtl"] == RTL
    assert rtl_state["after"]["stages"]["generate_rtl"]["attempts"] == 1


def test_tools_are_exactly_the_existing_agents():
    names = {t.name for t in orch.make_tools()}
    assert names == {"analyze_spec", "generate_rtl", "generate_testbench", "generate_test_vectors",
                     "write_project", "simulate", "implement"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def failing_sim(monkeypatch, likely_cause="rtl"):
    analysis = {"verdict": "fail", "likely_cause": likely_cause, "failures": ["cycle 3: tvalid expected 1 got 0"],
                "summary": "outputs come 4 cycles late", "suggested_fix": "fix the pipeline"}
    monkeypatch.setattr(orch, "run_simulation", lambda project, sim_time:
                        {"status": "NEEDS_REVIEW", "verdict": "fail", "analysis": analysis, "error": "simulation fail"})
    return analysis


def test_after_two_rtl_retries_with_the_same_failure_the_testbench_and_vectors_get_a_second_opinion(fakes, monkeypatch):
    failing_sim(monkeypatch)
    tb_told, tv_told, messages = [], [], []
    monkeypatch.setattr(orch, "run_testbench_generation", lambda spec, rtl, requirement_id, attempt, history:
                        tb_told.append((attempt, history)) or {"status": "COMPLETE", "testbench": TB})
    monkeypatch.setattr(orch, "run_test_vector_generation", lambda spec, run_dir, requirement_id, attempt, history:
                        tv_told.append((attempt, history)) or VECTORS)
    fix = lambda: [call("generate_rtl"), call("write_project"), call("simulate")]
    out = run_with([analyze(), call("generate_rtl"), call("generate_testbench"), call("generate_test_vectors"),
                    call("write_project"), call("simulate"), *fix(), *fix(),     # sim fails 3 times blaming the RTL
                    call("generate_testbench"), call("generate_test_vectors"), final("NEEDS_REVIEW", "stop")])
    tb_notes = out["stages"]["generate_testbench"]["attempt_history"][0].get("downstream", [])
    tv_notes = out["stages"]["generate_test_vectors"]["attempt_history"][0].get("downstream", [])
    assert len(tb_notes) == 1 and tb_notes[0]["second_opinion"] is True       # only the 3rd failure adds the note
    assert "generate_rtl has been blamed 3 times" in tb_notes[0]["summary"]
    assert "order the design presents them" in tb_notes[0]["summary"]         # told what to check
    assert len(tv_notes) == 1 and "latency/handshake" in tv_notes[0]["summary"]
    assert sum(len(a.get("downstream", [])) for a in out["stages"]["generate_rtl"]["attempt_history"]) == 3
    assert tb_told[1][1][0]["downstream"] == tb_notes                         # the testbench agent is handed it
    assert tv_told[1][1][0]["downstream"] == tv_notes


def test_the_third_failure_tells_the_manager_to_re_run_the_other_side(fakes, monkeypatch):
    failing_sim(monkeypatch)
    tools = {t.name: t for t in orch.make_tools()}
    blamed = {"attempt": 1, "status": "COMPLETE", "downstream": [{"stage": "simulate"}, {"stage": "simulate"}]}
    runtime = state_with(project_dir="p", stages={
        "generate_rtl": {"attempts": 3, "attempt_history": [blamed, {"attempt": 2}, {"attempt": 3}]},
        "generate_testbench": {"attempts": 1, "attempt_history": [{"attempt": 1}]}})
    answer = json.loads(tools["simulate"].invoke({"runtime": runtime}).update["messages"][0].content)
    assert "Re-run generate_testbench" in answer["second_opinion"]           # vectors never ran: not named
    assert "before calling generate_rtl again" in answer["second_opinion"]
    first_runtime = state_with(project_dir="p", stages={"generate_rtl": {"attempts": 1, "attempt_history": [{"attempt": 1}]}})
    first = json.loads(tools["simulate"].invoke({"runtime": first_runtime}).update["messages"][0].content)
    assert "second_opinion" not in first                                      # a first failure is just a failure


def test_a_fourth_call_of_a_helper_is_refused_and_ends_the_run_when_nobody_can_answer(fakes, monkeypatch):
    told = []
    monkeypatch.setattr(orch, "run_rtl_generation", lambda spec, requirement_id, attempt, previous, last_rtl:
                        told.append(attempt) or {"status": "NEEDS_REVIEW", "rtl": RTL, "error": "lint issues remain"})
    out = run_with([analyze(), *[call("generate_rtl") for _ in range(4)], final("NEEDS_REVIEW", "gave up")])
    assert told == [1, 2, 3]                                                  # the 4th call never ran the agent
    assert out["stages"]["generate_rtl"]["attempts"] == 3
    assert out["status"] == "NEEDS_REVIEW"


def test_at_the_retry_limit_the_person_is_asked_and_their_answer_buys_another_round(fakes, monkeypatch):
    asked = []
    monkeypatch.setattr(orch.human_input, "current_run_id", lambda: "run-1")
    monkeypatch.setattr(orch.human_input, "ask", lambda run_id, q, s: asked.append((run_id, q, s)) or
                        "Try the RTL once more: use a signed multiply")
    told = []
    monkeypatch.setattr(orch, "run_rtl_generation", lambda spec, requirement_id, attempt, previous, last_rtl:
                        told.append((attempt, previous)) or {"status": "NEEDS_REVIEW", "rtl": RTL, "error": "lint"})
    tools = {t.name: t for t in orch.make_tools()}
    history = [{"attempt": n, "status": "NEEDS_REVIEW"} for n in (1, 2, 3)]
    runtime = state_with(spec=SPEC, rtl=RTL, requirement_id="r", stages={
        "generate_rtl": {"attempts": 3, "status": "NEEDS_REVIEW", "error": "lint", "attempt_history": history}})
    cmd = tools["generate_rtl"].invoke({"runtime": runtime})
    answer = json.loads(cmd.update["messages"][0].content)
    assert told == []                                                         # not run: the limit was reached
    assert answer["status"] == "RETRY_LIMIT" and answer["attempts"] == 3
    assert answer["human_answer"] == "Try the RTL once more: use a signed multiply"
    assert asked[0][0] == "run-1" and "generate_rtl" in asked[0][1]           # the person saw which helper is stuck
    rtl_stage = cmd.update["stages"]["generate_rtl"]
    assert rtl_stage["retry_limit"] == 5                                      # 3 + 2 more
    assert rtl_stage["attempt_history"][-1]["human_note"] == "Try the RTL once more: use a signed multiply"
    again = state_with(spec=SPEC, rtl=RTL, requirement_id="r", stages=cmd.update["stages"])
    cmd2 = tools["generate_rtl"].invoke({"runtime": again})
    assert told == [(4, rtl_stage["attempt_history"])]                        # the 4th call runs, handed the note
    assert cmd2.update["stages"]["generate_rtl"]["retry_limit"] == 5           # the grant survives the record

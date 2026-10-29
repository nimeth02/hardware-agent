"""Simulation agent tests — no LLM, no MCP, no Vivado: project loader,
compiler, simulator, analyst and planner are fakes; the graph, guardrails
and rerun/limit logic are real."""

from __future__ import annotations

import pytest

from agents.simulation_agent.simulation_agent import SimulationAgent, resolve_project_dir
from agents.simulation_agent.simulation_analyst import (
    SimulationAnalyst,
    all_zero_output_hint,
    deterministic_verdict,
    fixed_rule_hint,
    parse_analysis_response,
    shifted_values_hint,
)
from agents.simulation_agent.simulation_decider import Decision, SimulationDecider
from agents.simulation_agent.simulation_runner import double_sim_time, outcome_of
from agents.simulation_agent.state import MAX_SIM_RERUNS, Action, SimulationStatus
from agents.simulation_agent.tools import SimulationAgentTools
from services.config import REPO_ROOT

PROJECT = {"name": "counter4", "dir": "C:/x/counter4", "src_files": ["src/counter4.vhd"], "tb_file": "tb/counter4_tb.vhd",
           "top_entity": "counter4_tb", "spec": {"entity_name": "counter4"}, "test_vectors": ["test_vectors_in.csv", "test_vectors_out.csv"],
           "testbench_code": "entity counter4_tb is end;"}
COMPILE_OK = {"status": "ok", "results": [{"status": "ok"}], "error_class": None}
COMPILE_BAD = {"status": "error", "results": [{"status": "error", "stderr": "ERROR: syntax"}], "error_class": None}
SIM_PASS = {"status": "ok", "sim_passed": True, "sim_output": "... SIMULATION PASSED", "outcome": "passed"}
SIM_FAIL = {"status": "error", "sim_passed": False, "sim_output": "count mismatch\nSIMULATION FAILED", "outcome": "failed"}
SIM_NO_MARKER = {"status": "error", "sim_passed": False, "sim_output": "run 10us", "outcome": "no_marker"}
PASS = {"verdict": "pass", "likely_cause": None, "failures": [], "summary": "ok", "suggested_fix": None}
FAIL = {"verdict": "fail", "likely_cause": "rtl", "failures": ["count mismatch"], "summary": "wrong count", "suggested_fix": "fix"}


class FakeLoader:
    def __init__(self, project=PROJECT, error=None):
        self.project, self.error, self.calls = project, error, []

    def load(self, project_dir):
        self.calls.append(project_dir)
        if self.error:
            raise RuntimeError(self.error)
        return dict(self.project)


def fake_compiler(result):
    def compile_design(project):
        if isinstance(result, Exception):
            raise result
        return dict(result)
    return compile_design


def fake_simulator(results):
    """Queued results in order (last one repeats); records the sim_time of every run."""
    queue, calls = list(results), []

    def simulate(project, sim_time):
        calls.append(sim_time)
        out = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(out, Exception):
            raise out
        return {**out, "sim_time": sim_time}
    simulate.calls = calls
    return simulate


class FakeAnalyst(SimulationAnalyst):
    def __init__(self, verdicts):
        super().__init__(llm=object())
        self.verdicts, self.calls = list(verdicts), []

    def analyze(self, project, compile_result, sim):
        self.calls.append({"compile": compile_result, "sim": sim})
        return self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]


class FakeDecider(SimulationDecider):
    """Rule-based stand-in for the planner, with optional forced decisions."""

    def __init__(self, forced=None):
        super().__init__(llm=object())
        self.forced, self.seen = list(forced or []), []

    def decide(self, summary):
        self.seen.append(summary)
        if self.forced:
            return self.forced.pop(0)
        if not summary["project_loaded"]:
            return Decision(Action.LOAD_PROJECT, "rule")
        if summary["compile"] is None:
            return Decision(Action.COMPILE_DESIGN, "rule")
        sim = summary["sim"]
        if summary["compile"]["status"] == "ok" and sim is None:
            return Decision(Action.RUN_SIMULATION, "rule")
        if sim and sim["outcome"] == "no_marker" and summary["sim_runs"] <= MAX_SIM_RERUNS:
            return Decision(Action.RUN_SIMULATION, "rule")
        if summary["analysis"] is None:
            return Decision(Action.ANALYZE_RESULT, "rule")
        return Decision(Action.FINALIZE_SIMULATION, "rule")


def make_agent(compile_result=COMPILE_OK, sims=(SIM_PASS,), verdicts=(PASS,), loader=None, decider=None, max_iterations=10):
    tools = SimulationAgentTools(project_loader=loader or FakeLoader(), compiler=fake_compiler(compile_result),
                                 simulator=fake_simulator(sims), analyst=FakeAnalyst(verdicts),
                                 decider=decider or FakeDecider())
    return SimulationAgent(tools=tools, max_iterations=max_iterations)


# ── happy path ───────────────────────────────────────────────────────────

def test_load_compile_simulate_analyze_finalize_on_pass():
    agent = make_agent()
    out = agent.run("counter4")
    assert out["status"] == SimulationStatus.COMPLETE.value and out["verdict"] == "pass"
    assert out["project_dir"] == str((REPO_ROOT / "projects" / "counter4").resolve())
    assert agent.tools.project_loader.calls == [out["project_dir"]]
    assert agent.tools.simulator.calls == ["10us"]
    assert out["project"]["top_entity"] == "counter4_tb" and out["sim"]["outcome"] == "passed"
    assert out["iteration"] == 5 and out["error"] is None and out["warnings"] == []


def test_failed_simulation_ends_in_needs_review_with_the_analysis():
    agent = make_agent(sims=[SIM_FAIL], verdicts=[FAIL])
    out = agent.run("counter4")
    assert out["status"] == SimulationStatus.NEEDS_REVIEW.value and out["verdict"] == "fail"
    assert out["analysis"] == FAIL and "wrong count" in out["error"]
    assert agent.tools.analyst.calls[0]["sim"]["outcome"] == "failed"


def test_compile_error_skips_simulation_and_is_analyzed():
    error_verdict = {**FAIL, "verdict": "error", "likely_cause": "rtl"}
    agent = make_agent(compile_result=COMPILE_BAD, verdicts=[error_verdict])
    out = agent.run("counter4")
    assert out["status"] == SimulationStatus.NEEDS_REVIEW.value and out["verdict"] == "error"
    assert agent.tools.simulator.calls == [] and out["sim"] is None
    assert agent.tools.analyst.calls[0]["compile"] == COMPILE_BAD


# ── reruns ───────────────────────────────────────────────────────────────

def test_no_marker_reruns_with_doubled_sim_time_until_a_marker_appears():
    agent = make_agent(sims=[SIM_NO_MARKER, SIM_PASS])
    out = agent.run("counter4", "10us")
    assert out["status"] == SimulationStatus.COMPLETE.value
    assert agent.tools.simulator.calls == ["10us", "20us"]


def test_reruns_are_capped():
    agent = make_agent(sims=[SIM_NO_MARKER], verdicts=[{**FAIL, "verdict": "error"}])
    out = agent.run("counter4", "10us")
    assert out["status"] == SimulationStatus.NEEDS_REVIEW.value
    assert agent.tools.simulator.calls == ["10us", "20us", "40us"]   # 1 + MAX_SIM_RERUNS
    assert len(agent.tools.analyst.calls) == 1


def test_planner_cannot_rerun_after_a_real_result():
    forced = [Decision(Action.LOAD_PROJECT), Decision(Action.COMPILE_DESIGN), Decision(Action.RUN_SIMULATION),
              Decision(Action.RUN_SIMULATION, "wrong"), Decision(Action.FINALIZE_SIMULATION, "wrong")]
    agent = make_agent(sims=[SIM_FAIL], verdicts=[FAIL], decider=FakeDecider(forced))
    out = agent.run("counter4")
    assert out["status"] == SimulationStatus.NEEDS_REVIEW.value
    assert agent.tools.simulator.calls == ["10us"]           # the rerun was turned into analyze
    assert len(agent.tools.analyst.calls) == 1


def test_repeated_load_or_compile_never_redo_finished_steps():
    forced = [Decision(Action.LOAD_PROJECT), Decision(Action.COMPILE_DESIGN), Decision(Action.LOAD_PROJECT, "wrong"),
              Decision(Action.COMPILE_DESIGN, "wrong"), Decision(Action.LOAD_PROJECT, "wrong"), Decision(Action.COMPILE_DESIGN, "wrong")]
    compiles = []
    agent = make_agent(decider=FakeDecider(forced))
    real = agent.tools.compiler
    agent.tools.compiler = lambda project: compiles.append(1) or real(project)
    out = agent.run("counter4")
    assert out["status"] == SimulationStatus.COMPLETE.value
    assert len(compiles) == 1 and agent.tools.simulator.calls == ["10us"] and len(agent.tools.analyst.calls) == 1
    assert out["iteration"] == 5           # load, compile, (load->simulate), (compile->analyze), (load->finalize)


def test_finalize_is_blocked_without_a_verdict():
    forced = [Decision(Action.LOAD_PROJECT), Decision(Action.COMPILE_DESIGN), Decision(Action.RUN_SIMULATION),
              Decision(Action.FINALIZE_SIMULATION, "wrong")]
    agent = make_agent(decider=FakeDecider(forced))
    out = agent.run("counter4")
    assert out["status"] == SimulationStatus.COMPLETE.value and len(agent.tools.analyst.calls) == 1


# ── failures and limits ──────────────────────────────────────────────────

def test_project_load_failure_fails_the_run():
    out = make_agent(loader=FakeLoader(error="no tb")).run("missing")
    assert out["status"] == SimulationStatus.FAILED.value and "no tb" in out["error"]


def test_missing_spec_and_vectors_are_warnings():
    out = make_agent(loader=FakeLoader({**PROJECT, "spec": None, "test_vectors": None})).run("counter4")
    assert out["status"] == SimulationStatus.COMPLETE.value
    assert any("spec.json" in w for w in out["warnings"]) and any("CSV" in w for w in out["warnings"])


def test_simulator_exception_fails_the_run():
    out = make_agent(sims=[RuntimeError("vivado down")]).run("counter4")
    assert out["status"] == SimulationStatus.FAILED.value and "vivado down" in out["error"]


def test_iteration_limit_returns_failed_without_a_verdict():
    forced = [Decision(Action.LOAD_PROJECT)] * 10
    out = make_agent(decider=FakeDecider(forced), max_iterations=2).run("counter4")
    assert out["status"] == SimulationStatus.FAILED.value and "Iteration limit" in out["error"]


# ── planner fallbacks, deterministic pieces and restrictions ─────────────

def test_invalid_planner_output_falls_back_safely():
    parse = SimulationDecider.parse
    assert parse("garbage", {"project_loaded": False}).action is Action.LOAD_PROJECT
    assert parse("??", {"project_loaded": True, "compile": None}).action is Action.COMPILE_DESIGN
    assert parse("??", {"project_loaded": True, "compile": {"status": "ok"}, "sim": None}).action is Action.RUN_SIMULATION
    assert parse("??", {"project_loaded": True, "compile": {"status": "ok"}, "sim": {"outcome": "no_marker"}, "sim_runs": 1}).action is Action.RUN_SIMULATION
    assert parse("??", {"project_loaded": True, "compile": {"status": "ok"}, "sim": {"outcome": "failed"}, "analysis": None}).action is Action.ANALYZE_RESULT
    assert parse("??", {"project_loaded": True, "compile": {"status": "ok"}, "sim": {"outcome": "failed"}, "analysis": {"verdict": "fail"}}).action is Action.FINALIZE_SIMULATION
    ok = parse('```json\n{"action": "analyze_result", "reason": "r"}\n```', {})
    assert ok.action is Action.ANALYZE_RESULT and ok.reason == "r"


def test_deterministic_verdicts_and_outcomes():
    assert deterministic_verdict(COMPILE_OK, SIM_PASS)["verdict"] == "pass"
    assert deterministic_verdict(COMPILE_OK, {**SIM_NO_MARKER, "sim_time": "40us"})["verdict"] == "error"
    # the log stops early (last report at 126 ns of 40 us): a frozen handshake, not a short sim_time
    frozen = {**SIM_NO_MARKER, "sim_time": "40us",
              "sim_output": "Error: beat 0: m_axis_tdata expected 2015 got 1560\nTime: 116 ns  Iteration: 0\n"
                            "Error: beat 1: m_axis_tdata expected 1560 got 2015\nTime: 126 ns  Iteration: 0\n"}
    verdict = deterministic_verdict(COMPILE_OK, frozen)
    assert verdict["verdict"] == "error" and "126 ns" in verdict["summary"] and "waiting" in verdict["summary"]
    assert "m_axis_tready = 0" in verdict["suggested_fix"]
    # reports run on until near the end: the stimulus really is longer than sim_time
    long_run = {**frozen, "sim_output": "Error: x\nTime: 39500 ns  Iteration: 0\n"}
    assert "end of sim_time" in deterministic_verdict(COMPILE_OK, long_run)["summary"]
    assert deterministic_verdict(COMPILE_OK, SIM_FAIL) is None
    assert deterministic_verdict(COMPILE_BAD, None) is None
    assert outcome_of({"status": "ok", "sim_passed": True, "sim_output": "SIMULATION PASSED"}) == "passed"
    assert outcome_of({"status": "error", "sim_passed": False, "sim_output": "SIMULATION FAILED"}) == "failed"
    assert outcome_of({"status": "error", "sim_passed": False, "sim_output": "[xelab failed]\nERROR: x"}) == "error"
    assert outcome_of({"status": "error", "sim_passed": False, "sim_output": "", "error_class": "environment"}) == "error"
    assert outcome_of({"status": "error", "sim_passed": False, "sim_output": "run 10us\nquit"}) == "no_marker"
    assert double_sim_time("10us") == "20us" and double_sim_time("1.5 ms") == "3ms" and double_sim_time("bogus") == "bogus"


def test_got_values_that_belong_to_other_cycles_are_flagged_as_a_timing_fault():
    """llr_demap16, 2026-10-29: four attempts blamed the RTL's arithmetic while
    every 'got' was simply the expected value of an earlier cycle."""
    log = """Error: cycle 16: m_axis_tdata changed while m_axis_tready is low
Error: cycle 17: m_axis_tdata expected hi=125 lo=61440 got hi=0 lo=0
Error: cycle 18: m_axis_tdata expected hi=130 lo=0 got hi=0 lo=2015
Error: cycle 20: m_axis_tdata expected hi=0 lo=0 got hi=125 lo=61440
Error: cycle 21: m_axis_tdata expected hi=129 lo=63520 got hi=130 lo=0
Error: cycle 23: m_axis_tdata expected hi=125 lo=63520 got hi=125 lo=61440
Note: SIMULATION FAILED"""
    hint = shifted_values_hint(log)
    assert hint and "3 of the 5" in hint and "'testbench'" in hint and "not arithmetic" in hint

    # genuinely wrong values (never expected anywhere) give no hint; so do all-zero gots
    assert shifted_values_hint("Error: cycle 1: count expected 3 got 7\nError: cycle 2: count expected 4 got 9") is None
    assert shifted_values_hint("Error: cycle 1: q expected 3 got 0\nError: cycle 2: q expected 0 got 0") is None
    assert shifted_values_hint("Error: cycle 1: q expected 3 got 4") is None          # one line proves nothing
    assert shifted_values_hint(None) is None

    # the hint reaches the LLM analyst as "fixed_rule_hint"
    seen = {}

    class Llm:
        def invoke(self, messages, config=None):
            import json
            seen.update(json.loads(messages[1].content))
            return type("R", (), {"content": '{"verdict": "fail", "likely_cause": "testbench"}'})()

    SimulationAnalyst(Llm()).analyze(PROJECT, COMPILE_OK, {**SIM_FAIL, "sim_output": log})
    assert "FIXED RULE" in seen["fixed_rule_hint"]
    seen.clear()
    SimulationAnalyst(Llm()).analyze(PROJECT, COMPILE_OK, SIM_FAIL)
    assert "fixed_rule_hint" not in seen                        # a plain mismatch log carries no hint


def test_all_zero_gots_are_flagged_as_an_output_register_fault():
    """llr_demap16, 2026-10-29, later run: every beat came out as 0 (the output
    register was loaded one clock after the last valid bit) and the analyst
    blamed 'the demapping calculation or output packing'."""
    log = """Error: beat 1 (input cycle 12): m_axis_tdata expected 2131480 got 0
Error: beat 2 (input cycle 14): m_axis_tdata expected 14911000 got 0
Error: beat 3 (input cycle 16): m_axis_tdata expected 8253440 got 0
Note: SIMULATION FAILED"""
    hint = all_zero_output_hint(log)
    assert hint and "all 3" in hint and "'rtl'" in hint and "NOT arithmetic" in hint
    assert fixed_rule_hint(log) == hint and shifted_values_hint(log) is None

    assert all_zero_output_hint("Error: c 1: w expected hi=1 lo=2 got hi=0 lo=0\n"
                                "Error: c 2: w expected hi=3 lo=0 got hi=0 lo=0")      # "hi=0 lo=0" is zero too
    assert all_zero_output_hint("Error: cycle 1: q expected 3 got 0\nError: cycle 2: q expected 4 got 9") is None
    assert all_zero_output_hint("Error: cycle 1: q expected 0 got 0\nError: cycle 2: q expected 3 got 0") is None
    assert all_zero_output_hint("Error: cycle 1: q expected 3 got 0") is None       # one line proves nothing
    assert all_zero_output_hint(None) is None

    # the rule travels with the analysis, so the blame note hands it to the RTL agent verbatim
    class Llm:
        def invoke(self, messages, config=None):
            return type("R", (), {"content": '{"verdict": "fail", "likely_cause": "rtl"}'})()

    analysis = SimulationAnalyst(Llm()).analyze(PROJECT, COMPILE_OK, {**SIM_FAIL, "sim_output": log})
    assert analysis["fixed_rule"] == hint
    from agents.orchestrator_agent.blame import downstream_note
    from agents.orchestrator_agent.state import Stage
    assert downstream_note(Stage.SIMULATE, analysis)["fixed_rule"] == hint
    assert "fixed_rule" not in downstream_note(Stage.SIMULATE, {"verdict": "fail"})


def test_invalid_analyst_output_becomes_a_safe_verdict():
    out = parse_analysis_response("not json", SIM_FAIL)
    assert out["verdict"] == "fail" and "did not return valid JSON" in out["failures"][0]
    assert parse_analysis_response("not json", {"outcome": "error"})["verdict"] == "error"
    assert parse_analysis_response('{"verdict": "fail", "summary": "s"}', SIM_FAIL)["summary"] == "s"


def test_resolve_project_dir():
    assert resolve_project_dir("counter4") == str((REPO_ROOT / "projects" / "counter4").resolve())
    assert resolve_project_dir("projects/counter4") == str((REPO_ROOT / "projects" / "counter4").resolve())
    assert resolve_project_dir(str(REPO_ROOT / "tests")) == str((REPO_ROOT / "tests").resolve())


def test_agent_only_has_restricted_actions():
    assert {a.value for a in Action} == {"load_project", "compile_design", "run_simulation", "analyze_result", "finalize_simulation"}
    nodes = set(make_agent().graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"decide", "load_project", "compile_design", "run_simulation", "analyze_result", "finalize_simulation"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_the_llm_sees_the_first_mismatches_and_the_end_of_a_long_log():
    from agents.simulation_agent.simulation_analyst import _tail
    warnings = "WARNING: HDL object x is of a type not supported\n" * 100
    errors = "".join(f"Error: cycle {n}: m_axis_tdata expected 1 got 0\n" for n in range(3, 400))
    log = warnings + errors + "Note: SIMULATION FAILED\n"
    cut = _tail(log, head=500, tail=300)
    assert cut.startswith(f"... [first {len(warnings)} chars omitted")      # the warnings before the first error go
    assert "Error: cycle 3:" in cut and "Error: cycle 4:" in cut               # the earliest mismatches stay
    assert cut.rstrip().endswith("SIMULATION FAILED")                          # and so does the marker
    assert "chars omitted for the LLM" in cut[len(warnings) // 10:]            # the middle is cut
    assert _tail("short log", head=500, tail=300) == "short log"
    assert _tail(None) is None


def test_the_analyst_is_handed_the_input_rows_of_the_failing_beats():
    """llr_demap16 run 170022 (2026-10-29): the golden model packed I with
    2^12 instead of 2^20, the design answered correctly for the word it was
    given, and the analyst, who never saw the CSV, blamed the RTL. With the
    stimulus rows in hand it can decode the word and blame the vectors."""
    from agents.simulation_agent.simulation_analyst import failing_input_rows
    csv_text = ("cycle,aclk,aresetn,s_axis_tvalid,s_axis_tdata,m_axis_tready\n"
                "3,1,1,0,0,1\n4,1,1,1,128,1\n5,1,1,1,1114240,1\n6,1,1,1,16711808,1\n7,1,1,1,2031744,1\n")
    log = """Error: beat 1 (input cycle 5): m_axis_tdata expected hi=1040 lo=1040 got hi=1024 lo=1055
Error: beat 2 (input cycle 6): m_axis_tdata expected hi=3120 lo=1040 got hi=3073 lo=1055
Error: beat 2 (input cycle 6): m_axis_tdata expected hi=3120 lo=1040 got hi=3073 lo=1055
Error: beat 9 (input cycle 99): m_axis_tdata expected 1 got 2
Note: SIMULATION FAILED"""
    rows = failing_input_rows(log, csv_text)
    assert rows == [{"cycle": "5", "aclk": "1", "aresetn": "1", "s_axis_tvalid": "1", "s_axis_tdata": "1114240", "m_axis_tready": "1"},
                    {"cycle": "6", "aclk": "1", "aresetn": "1", "s_axis_tvalid": "1", "s_axis_tdata": "16711808", "m_axis_tready": "1"}]
    # a fixed-cycle compare names the row directly; only "expected ... got" lines count
    assert [r["cycle"] for r in failing_input_rows("Error: cycle 7: q expected 3 got 4\nNote: cycle 4 reached", csv_text)] == ["7"]
    assert failing_input_rows(log, None) == [] and failing_input_rows(None, csv_text) == [] and failing_input_rows(log, "") == []
    many = "\n".join(f"Error: cycle {c}: q expected 1 got 2" for c in range(3, 8)) * 3
    assert len(failing_input_rows(many, csv_text)) == 5                         # distinct cycles, in log order

    # the rows reach the LLM analyst as "failing_input_rows"; without a CSV the key is absent
    seen = {}

    class Llm:
        def invoke(self, messages, config=None):
            import json
            seen.update(json.loads(messages[1].content))
            return type("R", (), {"content": '{"verdict": "fail", "likely_cause": "test_vectors"}'})()

    project = {**PROJECT, "test_vectors_in_csv": csv_text}
    analysis = SimulationAnalyst(Llm()).analyze(project, COMPILE_OK, {**SIM_FAIL, "sim_output": log})
    assert analysis["likely_cause"] == "test_vectors"
    assert [r["s_axis_tdata"] for r in seen["failing_input_rows"]] == ["1114240", "16711808"]
    seen.clear()
    SimulationAnalyst(Llm()).analyze(PROJECT, COMPILE_OK, {**SIM_FAIL, "sim_output": log})
    assert "failing_input_rows" not in seen

    from prompts.simulation_agent_prompt import SIMULATION_AGENT_SYSTEM_PROMPT
    assert '"test_vectors"' in SIMULATION_AGENT_SYSTEM_PROMPT and "failing_input_rows" in SIMULATION_AGENT_SYSTEM_PROMPT


def test_deadlocks_on_idle_rows_are_blamed_on_the_testbench_not_the_rtl():
    """llr_demap16, 2026-10-29 16:40: the hold loop waited for s_axis_tready on
    rows with s_axis_tvalid = 0 (cycles 0-1 and 50-59), reported 12 deadlocks,
    and the analyst blamed the RTL; the RTL's ready wire was high all along."""
    from agents.simulation_agent.simulation_analyst import idle_row_deadlock_hint
    csv_text = ("cycle,aresetn,s_axis_tvalid,s_axis_tdata,m_axis_tready\n"
                "0,0,0,0,1\n1,0,0,0,1\n2,1,1,123,1\n3,1,1,456,0\n50,1,0,0,1\n51,1,0,0,1\n")
    log = ("Error: cycle 0: s_axis_tready never rose (deadlock)\nError: cycle 1: s_axis_tready never rose (deadlock)\n"
           "Error: beat 5 (input cycle 14): m_axis_tdata expected 129 248 32 got 130 8 32\n"
           "Error: cycle 50: s_axis_tready never rose (deadlock)\nError: cycle 51: s_axis_tready never rose (deadlock)\n"
           "Note: SIMULATION FAILED")
    hint = idle_row_deadlock_hint(log, csv_text)
    assert hint and "all 4" in hint and "cycles 0, 1, 50, 51" in hint and "'testbench'" in hint
    assert "cur_s_axis_tvalid = 0 or dut_s_axis_tready = '1'" in hint
    assert fixed_rule_hint(log, csv_text) == hint and fixed_rule_hint(log) is None    # needs the CSV
    # a stuck REAL beat (tvalid = 1, cycle 3) is not covered by the rule
    assert idle_row_deadlock_hint("Error: cycle 3: s_axis_tready never rose (deadlock)", csv_text) is None
    assert idle_row_deadlock_hint("Error: cycle 99: s_axis_tready never rose (deadlock)", csv_text) is None
    assert idle_row_deadlock_hint("Error: cycle 1: q expected 3 got 4", csv_text) is None
    assert idle_row_deadlock_hint(log, None) is None and idle_row_deadlock_hint(None, csv_text) is None

    # the hint reaches the LLM analyst when the project carries the CSV
    seen = {}

    class CapturingLLM:
        def invoke(self, messages, config=None):
            import json as _json
            seen.update(_json.loads(messages[1].content))

            class R:
                content = '{"verdict": "fail", "likely_cause": "testbench", "failures": [], "summary": "s", "suggested_fix": "f"}'
            return R()

    SimulationAnalyst(llm=CapturingLLM()).analyze({**PROJECT, "test_vectors_in_csv": csv_text}, {"status": "ok"},
                                                  {"outcome": "failed", "sim_output": log})
    assert "idle row" in seen["fixed_rule_hint"]
    from prompts.simulation_agent_prompt import SIMULATION_AGENT_SYSTEM_PROMPT
    assert "never rose" in SIMULATION_AGENT_SYSTEM_PROMPT and "idle row" in SIMULATION_AGENT_SYSTEM_PROMPT

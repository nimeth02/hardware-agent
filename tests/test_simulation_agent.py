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


def test_the_first_run_is_at_least_as_long_as_the_test_vectors_need():
    """timing_recovery, 2026-11-05 19:02: the vectors were regenerated with
    24,000 rows (240 us at 10 ns), the agent ran 10/20/40 us, the testbench
    never reached its marker and three attempts were judged 'frozen'."""
    from agents.simulation_agent.simulation_runner import sim_time_for
    tb = "entity t is end;\narchitecture a of t is\n  constant clk_period  : time := 10 ns;\nbegin end;"
    csv_text = "cycle,aclk,s_axis_tvalid,s_axis_tdata\n" + "\n".join(f"{c},1,1,{c}" for c in range(24000)) + "\n"
    project = {**PROJECT, "testbench_code": tb, "test_vectors_in_csv": csv_text}
    assert sim_time_for(project, "10us") == "289us"                       # 24000 * 1.2 + 100 clocks of 10 ns
    assert sim_time_for(project, "500us") == "500us"                      # a longer request is kept
    assert sim_time_for({**project, "testbench_code": "no constant here", "spec": {"clock": {"frequency_mhz": 50}}}, "10us") == "578us"
    assert sim_time_for({**project, "testbench_code": "", "spec": {}}, "10us") == "289us"   # default 10 ns
    assert sim_time_for(PROJECT, "10us") == "10us"                        # no CSV text: unchanged
    assert sim_time_for({**project, "test_vectors_in_csv": "cycle,aclk\n"}, "10us") == "10us"   # header only
    assert sim_time_for(project, "bogus") == "bogus"
    short = {**project, "test_vectors_in_csv": "cycle,aclk\n" + "\n".join(f"{c},1" for c in range(50)) + "\n"}
    assert sim_time_for(short, "10us") == "10us"                          # 50 rows fit in 10 us

    agent = make_agent(sims=[SIM_NO_MARKER, SIM_PASS], loader=FakeLoader(project))
    agent.run("counter4", "10us")
    assert agent.tools.simulator.calls == ["289us", "578us"]              # first run sized to the vectors, rerun doubled


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


def test_a_wrong_number_of_output_beats_is_never_called_a_timing_fault():
    """timing_recovery, 2026-11-03 12:30: the design made 233 beats where the
    vectors expected 788 (then 28 where 198 were expected). The shifted-values
    rule fired on 4 of 20 lines, all of them the saturation value 8386559 that
    the vectors expected on many beats; the analyst blamed the testbench four
    times and it was rewritten twice to no effect, while the RTL's strobe
    logic (eta_n + to_signed(65536, 16), which wraps to 0) was never named."""
    from agents.simulation_agent.simulation_analyst import beat_count_hint
    sat = "8386559"
    lines = [f"Error: beat 0 (input cycle 12): m_axis_tdata expected {sat} got 0",
             f"Error: beat 1 (input cycle 13): m_axis_tdata expected {sat} got 0",
             f"Error: beat 2 (input cycle 14): m_axis_tdata expected {sat} got 0",
             f"Error: beat 3 (input cycle 15): m_axis_tdata expected 0 got {sat}",
             f"Error: beat 5 (input cycle 17): m_axis_tdata expected 2047 got {sat}",
             f"Error: beat 8 (input cycle 23): m_axis_tdata expected 8384512 got {sat}",
             f"Error: beat 9 (input cycle 24): m_axis_tdata expected {sat} got 3728270",
             f"Error: beat 13 (input cycle 31): m_axis_tdata expected 4095 got {sat}"]
    lines += [f"Error: beat {k} (input cycle {k + 12}): m_axis_tdata expected {1000 + k} got {2000 + k}"
              for k in range(14, 26)]                                   # 12 more lines, values never expected anywhere
    log = "\n".join(lines) + "\nError: missing output beats: expected 788 got 233\nNote: mismatches: 142\nNote: SIMULATION FAILED"
    assert len(log.splitlines()) == 23

    hint = beat_count_hint(log)
    assert hint and "233 output beats" in hint and "expect 788" in hint and "NOT a timing/ordering fault" in hint
    assert "'rtl'" in hint and "'test_vectors'" in hint and "Do not blame the testbench" in hint
    assert "strobe" in hint and "1 per 4 clocks" in hint
    assert fixed_rule_hint(log) == hint                     # it comes before, and vetoes, the shifted-values rule
    assert shifted_values_hint(log) is None                 # 4 of 20 lines, all one value the vectors expect everywhere

    # a short drain may lose a beat or two at the end of the run: no rule for a small gap
    assert beat_count_hint("Error: beat 1: q expected 5 got 6\nError: missing output beats: expected 20 got 18") is None
    assert "28 output beats" in beat_count_hint("Error: missing output beats: expected 198 got 28")
    # the design can also make too MANY beats: the testbench prints one line per extra beat
    many = "\n".join(f"Error: cycle {c}: unexpected output beat" for c in range(20, 32))
    assert "at least 12 output beats more" in beat_count_hint(many)
    assert beat_count_hint("Error: cycle 20: unexpected output beat\nError: cycle 21: unexpected output beat") is None
    # a stuck handshake explains the missing beats by itself: the deadlock rules speak, not this one
    assert beat_count_hint("Error: cycle 3: s_axis_tready never rose (deadlock)\n"
                           "Error: missing output beats: expected 20 got 2") is None
    assert beat_count_hint(None) is None and beat_count_hint("Note: SIMULATION FAILED") is None

    # the shifted-values rule itself now needs half the lines, two different values,
    # and none of them a value the vectors expect on many lines
    assert shifted_values_hint("Error: cycle 1: q expected 7 got 0\nError: cycle 2: q expected 9 got 7\n"
                               "Error: cycle 3: q expected 11 got 9")                    # 2 of 3, two values
    assert shifted_values_hint("Error: cycle 1: q expected 7 got 5\nError: cycle 2: q expected 7 got 9\n"
                               "Error: cycle 3: q expected 7 got 6\nError: cycle 4: q expected 3 got 7\n"
                               "Error: cycle 5: q expected 4 got 7") is None             # 7 is expected on 3 lines
    assert shifted_values_hint("Error: cycle 1: q expected 7 got 1\nError: cycle 2: q expected 9 got 7\n"
                               "Error: cycle 3: q expected 11 got 7") is None            # one value only

    # the hint reaches the LLM analyst, with the calculator switched off, and travels with the blame note
    seen = {}

    class CapturingLLM:
        def invoke(self, messages, config=None):
            import json as _json
            seen.update(_json.loads(messages[1].content))

            class R:
                content = '{"verdict": "fail", "likely_cause": "rtl", "failures": [], "summary": "s", "suggested_fix": "f"}'
            return R()

    out = SimulationAnalyst(llm=CapturingLLM()).analyze(PROJECT, COMPILE_OK, {"outcome": "failed", "sim_output": log})
    assert seen["fixed_rule_hint"] == hint and seen["python_check_allowed"] is False
    assert out["fixed_rule"] == hint and out["likely_cause"] == "rtl"


def test_provably_wrong_vectors_force_the_blame_onto_the_test_vectors():
    """timing_recovery, 2026-11-05 15:54: the MATLAB model divided 0 by 0, so
    s_axis_tdata was 8390656 on all 796 input beats and only ONE output beat
    was expected for a '1 per 4 clocks' spec. The RTL strobed every ~4 clocks
    as the spec says, the testbench printed 197 'unexpected output beat'
    lines, and the analyst blamed the RTL."""
    from agents.simulation_agent.simulation_analyst import (beat_count_hint, forced_vector_cause,
                                                            wrong_vectors_reason)
    spec = {"reset": {"name": "aresetn", "active": "low"},
            "constraints": ["Output strobe rate is approximately one per 4 input clocks."]}

    def csvs(values, out_every):
        in_lines = ["cycle,aclk,aresetn,s_axis_tvalid,s_axis_tdata,m_axis_tready", "0,1,0,0,0,1", "1,1,0,0,0,1"]
        out_lines = ["cycle,s_axis_tready,m_axis_tvalid,m_axis_tdata", "0,1,0,0", "1,1,0,0"]
        for i, val in enumerate(values):
            beat = 1 if out_every and (i + 1) % out_every == 0 else 0
            in_lines.append(f"{i + 2},1,1,1,{val},1")
            out_lines.append(f"{i + 2},1,{beat},{val if beat else 0}")
        return "\n".join(in_lines) + "\n", "\n".join(out_lines) + "\n"

    constant_in, one_beat_out = csvs([8390656] * 40, out_every=40)
    varied_in, few_beats_out = csvs(range(100, 140), out_every=40)
    _, good_out = csvs(range(100, 140), out_every=4)

    reason = wrong_vectors_reason(constant_in, one_beat_out, spec)
    assert reason and "8390656" in reason and "40 input beats" in reason
    reason = wrong_vectors_reason(varied_in, few_beats_out, spec)
    assert reason and "only 1 output beat" in reason and "one output per 4" in reason
    assert wrong_vectors_reason(varied_in, good_out, spec) is None                      # healthy vectors
    assert wrong_vectors_reason(varied_in, few_beats_out, {"reset": spec["reset"]}) is None   # no rate in the spec
    assert wrong_vectors_reason(None, None, spec) is None
    small_in, small_out = csvs([7] * 5, out_every=0)
    assert wrong_vectors_reason(small_in, small_out, spec) is None                      # too small to judge

    log = ("Error: beat 0 (input cycle 14): m_axis_tdata expected 8390656 got 0\n"
           + "\n".join(f"Error: cycle {c}: unexpected output beat" for c in range(18, 60, 4))
           + "\nNote: mismatches: 11\nNote: SIMULATION FAILED")
    hint = beat_count_hint(log, wrong_vectors_reason(constant_in, one_beat_out, spec))
    assert hint and "GOLDEN VECTORS ARE WRONG" in hint and "MUST be 'test_vectors'" in hint and "isfinite" in hint
    assert "likely_cause MUST" not in beat_count_hint(log)                              # without the finding: the old text
    assert fixed_rule_hint(log, constant_in, one_beat_out, spec) == hint
    assert forced_vector_cause(log, constant_in, one_beat_out, spec) == "test_vectors"
    assert forced_vector_cause(log, varied_in, good_out, spec) is None                  # good vectors: no override
    assert forced_vector_cause("Error: cycle 1: q expected 3 got 7", constant_in, one_beat_out, spec) is None  # no beat-count fault

    # the LLM still blames the RTL; the verdict that leaves the analyst blames the test vectors
    class Llm:
        def invoke(self, messages, config=None):
            class R:
                content = '{"verdict": "fail", "likely_cause": "rtl", "failures": [], "summary": "s", "suggested_fix": "f"}'
            return R()

    project = {**PROJECT, "spec": spec, "test_vectors_in_csv": constant_in, "test_vectors_out_csv": one_beat_out}
    out = SimulationAnalyst(Llm()).analyze(project, COMPILE_OK, {"outcome": "failed", "sim_output": log})
    assert out["likely_cause"] == "test_vectors" and out["overridden_cause"] == "rtl" and out["fixed_rule"] == hint

    project["test_vectors_out_csv"] = good_out                                          # healthy vectors: the LLM's call stands
    project["test_vectors_in_csv"] = varied_in
    out = SimulationAnalyst(Llm()).analyze(project, COMPILE_OK, {"outcome": "failed", "sim_output": log})
    assert out["likely_cause"] == "rtl" and "overridden_cause" not in out


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

    # the rows reach the LLM analyst inside "failing_beats"; without a CSV the key is absent
    seen = {}

    class Llm:
        def invoke(self, messages, config=None):
            import json
            seen.update(json.loads(messages[1].content))
            return type("R", (), {"content": '{"verdict": "fail", "likely_cause": "test_vectors"}'})()

    project = {**PROJECT, "test_vectors_in_csv": csv_text}
    analysis = SimulationAnalyst(Llm()).analyze(project, COMPILE_OK, {**SIM_FAIL, "sim_output": log})
    assert analysis["likely_cause"] == "test_vectors"
    assert [b["input_row"]["s_axis_tdata"] for b in seen["failing_beats"]] == ["1114240", "16711808"]
    assert seen["python_check_allowed"] is True
    seen.clear()
    SimulationAnalyst(Llm()).analyze(PROJECT, COMPILE_OK, {**SIM_FAIL, "sim_output": log})
    assert "failing_beats" not in seen and seen["python_check_allowed"] is False

    from prompts.simulation_agent_prompt import SIMULATION_AGENT_SYSTEM_PROMPT
    assert '"test_vectors"' in SIMULATION_AGENT_SYSTEM_PROMPT and "failing_beats" in SIMULATION_AGENT_SYSTEM_PROMPT
    assert "python_check" in SIMULATION_AGENT_SYSTEM_PROMPT and "CHECK beat=" in SIMULATION_AGENT_SYSTEM_PROMPT


def test_the_blamed_agent_gets_the_failing_rows_and_the_first_error_lines():
    """2026-11-06: the blame note carried the analyst's reading (summary,
    suggested_fix, a few quoted lines) but never the evidence it read: the
    decoded failing rows stayed in the analyst's payload and the log stayed
    in the run record. The blamed RTL agent could only trust the reading.
    Now the analysis keeps "failing_beats" and "log_excerpt" and the blame
    note copies both, capped."""
    from agents.orchestrator_agent.blame import MAX_EVIDENCE_CHARS, downstream_note
    from agents.orchestrator_agent.state import Stage
    from agents.simulation_agent.simulation_analyst import LOG_EXCERPT_LINES, log_excerpt
    csv_in = ("cycle,aclk,aresetn,s_axis_tvalid,s_axis_tdata,m_axis_tready\n"
              "3,1,1,0,0,1\n4,1,1,1,128,1\n5,1,1,1,1114240,1\n6,1,1,1,16711808,1\n")
    csv_out = "cycle,m_axis_tvalid,m_axis_tdata\n4,1,7\n5,1,1040\n6,1,3120\n"
    log = ("Note: stimulus loaded\n"
           "Error: beat 1 (input cycle 5): m_axis_tdata expected 1040 got 1024\n"
           "Error: beat 2 (input cycle 6): m_axis_tdata expected 3120 got 3073\n"
           "Note: SIMULATION FAILED")

    class Llm:
        def invoke(self, messages, config=None):
            return type("R", (), {"content": '{"verdict": "fail", "likely_cause": "rtl", "summary": "s", "suggested_fix": "f"}'})()

    project = {**PROJECT, "test_vectors_in_csv": csv_in, "test_vectors_out_csv": csv_out}
    analysis = SimulationAnalyst(Llm()).analyze(project, COMPILE_OK, {**SIM_FAIL, "sim_output": log})
    note = downstream_note(Stage.SIMULATE, analysis)
    assert [b["input_row"]["s_axis_tdata"] for b in note["failing_beats"]] == ["1114240", "16711808"]
    assert note["failing_beats"][0]["expected_row"]["m_axis_tdata"] == "1040"
    assert note["failing_beats"][0]["line"].endswith("expected 1040 got 1024")
    assert note["log_excerpt"].splitlines() == log.splitlines()[1:3]                # error lines only, no notes
    assert note["summary"] == "s" and note["suggested_fix"] == "f"                  # the reading still travels

    # without CSVs there are no rows, but the error lines still travel; a passing run carries nothing
    note = downstream_note(Stage.SIMULATE, SimulationAnalyst(Llm()).analyze(PROJECT, COMPILE_OK, {**SIM_FAIL, "sim_output": log}))
    assert "failing_beats" not in note and "Error: beat 1" in note["log_excerpt"]
    passed = SimulationAnalyst(Llm()).analyze(project, COMPILE_OK, SIM_PASS)
    assert "failing_beats" not in passed and "log_excerpt" not in passed
    # a rule-based verdict (no LLM) carries the evidence too
    no_marker = SimulationAnalyst(Llm()).analyze(project, COMPILE_OK, {**SIM_NO_MARKER, "sim_output": log, "sim_time": "10us"})
    assert no_marker["verdict"] == "error" and len(no_marker["failing_beats"]) == 2 and "Error: beat 2" in no_marker["log_excerpt"]

    # a failed compile quotes the compiler, not the (missing) simulation log
    assert log_excerpt(COMPILE_BAD, None) == "ERROR: syntax"
    assert log_excerpt(COMPILE_BAD, {"sim_output": "Error: never ran"}) == "ERROR: syntax"
    assert log_excerpt(COMPILE_OK, {"sim_output": "all quiet\nSIMULATION PASSED"}) is None
    assert log_excerpt(None, None) is None
    # the excerpt is the FIRST error lines, capped, with a count of the rest
    many = "\n".join(f"Error: cycle {c}: q expected 1 got 2" for c in range(50))
    excerpt = log_excerpt(COMPILE_OK, {"sim_output": many})
    assert excerpt.count("Error:") == LOG_EXCERPT_LINES and excerpt.startswith("Error: cycle 0:")
    assert f"[{50 - LOG_EXCERPT_LINES} more error lines" in excerpt
    assert len(downstream_note(Stage.SIMULATE, {"verdict": "fail", "log_excerpt": "x" * 9000})["log_excerpt"]) == MAX_EVIDENCE_CHARS
    assert "log_excerpt" not in downstream_note(Stage.SIMULATE, {"verdict": "fail"})

    # the RTL and testbench retry prompts tell the agents what the two fields are
    from prompts.rtl_agent_prompt import RTL_RETRY_ADDENDUM
    from prompts.testbench_agent_prompt import TESTBENCH_RETRY_ADDENDUM
    for addendum in (RTL_RETRY_ADDENDUM, TESTBENCH_RETRY_ADDENDUM):
        assert "failing_beats" in addendum and "log_excerpt" in addendum and "input_row" in addendum


# The real data of llr_demap16, 2026-10-30 11:51 (runs/test_vectors/llr_demap16_20261030_115142):
# the golden model wrapped m*ns with mod(., 2^24), so every negative LLR became +31.
LLR_IN_CSV = ("cycle,aclk,aresetn,s_axis_tvalid,s_axis_tdata,m_axis_tready\n"
              "0,1,0,0,0,1\n1,1,0,0,0,1\n2,1,1,1,134316048,1\n3,1,1,1,402685984,1\n"
              "4,1,1,1,4160847920,1\n5,1,1,1,3893329984,1\n6,1,1,1,537002064,1\n")
LLR_OUT_CSV = ("cycle,s_axis_tready,m_axis_tvalid,m_axis_tdata\n"
               "0,0,0,0\n1,0,0,0\n2,1,1,790595\n3,1,1,549250\n4,1,1,2486473\n5,1,1,8254212\n6,1,1,5324800\n")
LLR_LOG = """Error: beat 2 (input cycle 12): m_axis_tdata expected hi=607 lo=201 got hi=637 lo=201
Error: beat 3 (input cycle 13): m_axis_tdata expected hi=2015 lo=772 got hi=3892 lo=772
Error: beat 9 (input cycle 19): m_axis_tdata expected hi=1 lo=1 got hi=2 lo=2
Note: SIMULATION FAILED"""
# a correct check for that spec, as the analyst is asked to write it
LLR_CHECK = r'''
import re
def s12(v): return v - 4096 if v >= 2048 else v
def msb(x): return 4*x if abs(x) <= 512 else (8*x - 2048 if x > 0 else 8*x + 2048)
def lsb(x): return 2048 - 4*abs(x)
def llr(m, ns): return max(-32, min(31, (m*ns + 4096) >> 13))
for b in FAILING_BEATS:
    w = int(b["input_row"]["s_axis_tdata"]); I, Q, ns = s12(w >> 20 & 0xFFF), s12(w >> 8 & 0xFFF), w & 0xFF
    bits = [llr(msb(Q), ns), llr(msb(I), ns), llr(lsb(Q), ns), llr(lsb(I), ns)]
    v = (bits[0] & 63) << 18 | (bits[1] & 63) << 12 | (bits[2] & 63) << 6 | (bits[3] & 63)
    spec = (v >> 12, v & 0xFFF)                      # the testbench prints 12-bit halves of the 24-bit word
    m = re.search(r"expected hi=(\d+) lo=(\d+) got hi=(\d+) lo=(\d+)", b["line"]).groups()
    exp, got = (int(m[0]), int(m[1])), (int(m[2]), int(m[3]))
    print(f"beat {b['beat']}: word={w} I={I} Q={Q} ns={ns} spec={spec} expected={exp} got={got}")
    print(f"CHECK beat={b['beat']} got_matches_spec={'yes' if got == spec else 'no'} expected_matches_spec={'yes' if exp == spec else 'no'}")
'''


def test_beats_are_matched_by_counting_not_by_the_testbench_label():
    """2026-10-30 11:51: the testbench printed the cycle the output APPEARED
    (input row + 8 cycles of latency) as "input cycle", so the analyst decoded
    row 12 instead of row 4 and blamed the RTL."""
    from agents.simulation_agent.simulation_analyst import failing_beats
    spec = {"reset": {"name": "aresetn", "active": "low"}}
    beats = failing_beats(LLR_LOG, LLR_IN_CSV, LLR_OUT_CSV, spec)
    assert [b["beat"] for b in beats] == [2, 3]                                   # beat 9 does not exist: dropped
    assert beats[0]["input_cycle"] == "4" and beats[0]["input_row"]["s_axis_tdata"] == "4160847920"
    assert beats[0]["expected_row"]["m_axis_tdata"] == "2486473"               # 607 * 4096 + 201
    assert beats[1]["input_cycle"] == "5" and beats[1]["expected_row"]["cycle"] == "5"
    assert beats[0]["line"].startswith("Error: beat 2")
    # without the spec the reset column is recognised by name; without the outputs CSV expected_row is None
    assert failing_beats(LLR_LOG, LLR_IN_CSV, None)[0]["input_cycle"] == "4"
    assert failing_beats(LLR_LOG, LLR_IN_CSV, None)[0]["expected_row"] is None
    # a beat sent during reset (dropped by the design) is not counted
    during_reset = LLR_IN_CSV.replace("1,1,0,0,0,1", "1,1,0,1,999,1")
    assert failing_beats(LLR_LOG, during_reset, None, spec)[0]["input_cycle"] == "4"
    # fixed-cycle compares still name the row directly, in both files
    fixed = failing_beats("Error: cycle 5: m_axis_tdata expected 1 got 2", LLR_IN_CSV, LLR_OUT_CSV)
    assert fixed[0]["cycle"] == "5" and fixed[0]["input_row"]["cycle"] == "5" and fixed[0]["expected_row"]["cycle"] == "5"
    assert failing_beats(LLR_LOG, None, None) == [] and failing_beats(None, LLR_IN_CSV, LLR_OUT_CSV) == []


def test_the_analyst_runs_its_own_python_check_and_the_arithmetic_wins():
    """The analyst decoded 805503152 as I=128/Q=128/ns=128 once and as
    I=192/Q=192/ns=255 the next time (it is I=768, Q=768, ns=176) and blamed
    the RTL both times. Now it writes the check, code runs it, and a verdict
    that contradicts the check's CHECK lines is overridden."""
    import json
    from agents.simulation_agent.simulation_analyst import check_verdict, run_python_check

    assert check_verdict("CHECK beat=2 got_matches_spec=yes expected_matches_spec=no\n"
                         "CHECK beat=3 got_matches_spec=yes expected_matches_spec=no") == "test_vectors"
    assert check_verdict("CHECK beat=2 got_matches_spec=no expected_matches_spec=yes") == "rtl"
    assert check_verdict("CHECK beat=2 got_matches_spec=yes expected_matches_spec=no\n"
                         "CHECK beat=3 got_matches_spec=no expected_matches_spec=yes") is None   # mixed: no rule
    assert check_verdict("CHECK beat=2 got_matches_spec=yes expected_matches_spec=yes") is None  # nothing wrong?
    assert check_verdict("no check lines") is None and check_verdict(None) is None
    # timing_recovery 2026-11-05 attempt 2: the CSV was wrong on every beat and
    # the design differed too; the old rule stayed silent and the RTL was blamed
    both_wrong = ("CHECK beat=1 got_matches_spec=no expected_matches_spec=no\n"
                  "CHECK beat=2 got_matches_spec=no expected_matches_spec=no")
    assert check_verdict(both_wrong) == "test_vectors"
    assert check_verdict("CHECK beat=1 got_matches_spec=no expected_matches_spec=no\n"
                         "CHECK beat=2 got_matches_spec=yes expected_matches_spec=no") == "test_vectors"
    from agents.simulation_agent.simulation_analyst import check_rule_hint
    from prompts.simulation_agent_prompt import SIMULATION_AGENT_SYSTEM_PROMPT
    hint = check_rule_hint("test_vectors", {"stdout": both_wrong})
    assert "ALL 2 failing beats" in hint and "MUST be 'test_vectors'" in hint and "Whatever the design" in hint
    assert "expected does NOT match the spec on ANY beat" in SIMULATION_AGENT_SYSTEM_PROMPT
    assert "CLOSED-LOOP DESIGNS" in SIMULATION_AGENT_SYSTEM_PROMPT and "TOL_SYM" in SIMULATION_AGENT_SYSTEM_PROMPT

    project = {**PROJECT, "spec": {"entity_name": "llr_demap16", "reset": {"name": "aresetn", "active": "low"}},
               "test_vectors_in_csv": LLR_IN_CSV, "test_vectors_out_csv": LLR_OUT_CSV}
    sim = {**SIM_FAIL, "sim_output": LLR_LOG}
    calls = []

    class Llm:
        def __init__(self, replies):
            self.replies = list(replies)

        def invoke(self, messages, config=None):
            calls.append(json.loads(messages[1].content))
            return type("R", (), {"content": self.replies.pop(0)})()

    final = json.dumps({"verdict": "fail", "likely_cause": "rtl", "failures": ["beat 2 ..."],
                        "summary": "the RTL is wrong", "suggested_fix": "rewrite the RTL"})
    analysis = SimulationAnalyst(Llm([json.dumps({"verdict": "fail", "python_check": LLR_CHECK}), final])).analyze(
        project, COMPILE_OK, sim)
    assert len(calls) == 2
    assert calls[0]["python_check_allowed"] is True and "python_check" not in calls[0]
    check = calls[1]["python_check"]
    assert check["status"] == "ok" and check["stderr"] == "" and check["code"] == LLR_CHECK
    assert "I=-128 Q=384 ns=48" in check["stdout"]                                  # row 4 decoded, not row 12
    assert "CHECK beat=2 got_matches_spec=yes expected_matches_spec=no" in check["stdout"]
    assert "CHECK beat=3 got_matches_spec=yes expected_matches_spec=no" in check["stdout"]
    assert calls[1]["python_check_allowed"] is False and "MUST be 'test_vectors'" in calls[1]["fixed_rule_hint"]
    # the LLM still said "rtl": the check it wrote itself overrides it, and everything travels with the analysis
    assert analysis["likely_cause"] == "test_vectors" and analysis["overridden_cause"] == "rtl"
    assert analysis["python_check"]["status"] == "ok" and "FIXED RULE" in analysis["fixed_rule"]
    assert analysis["summary"] == "the RTL is wrong"
    from agents.orchestrator_agent.blame import downstream_note
    from agents.orchestrator_agent.state import Stage
    note = downstream_note(Stage.SIMULATE, analysis)
    assert note["python_check"]["code"] == LLR_CHECK and "CHECK beat=2" in note["python_check"]["stdout"]

    # a crashing check may be fixed once; a check that keeps asking is cut off and the safe verdict is used
    calls.clear()
    ok = json.dumps({"verdict": "fail", "likely_cause": "test_vectors", "failures": [], "summary": "s", "suggested_fix": "f"})
    analysis = SimulationAnalyst(Llm([json.dumps({"verdict": "fail", "python_check": "1/0"}),
                                      json.dumps({"verdict": "fail", "python_check": LLR_CHECK}), ok])).analyze(
        project, COMPILE_OK, sim)
    assert len(calls) == 3 and calls[1]["python_check"]["status"] == "error" and "ZeroDivisionError" in calls[1]["python_check"]["stderr"]
    assert calls[1]["python_check_allowed"] is True and calls[2]["python_check_allowed"] is False
    assert analysis["likely_cause"] == "test_vectors" and "overridden_cause" not in analysis
    calls.clear()
    analysis = SimulationAnalyst(Llm([json.dumps({"verdict": "fail", "python_check": "1/0"}),
                                      json.dumps({"verdict": "fail", "python_check": "1/0"}),
                                      json.dumps({"python_check": "print(1)"}), "not json"])).analyze(project, COMPILE_OK, sim)
    assert len(calls) == 4 and calls[3]["python_check_allowed"] is False and "note" in calls[3]
    assert analysis["verdict"] == "fail" and "did not return valid JSON" in analysis["failures"][0]
    assert analysis["python_check"]["status"] == "error"

    # a log rule (timing, zeros, deadlocks) settles it without arithmetic: no calculator offered
    calls.clear()
    zeros = "Error: beat 2 (input cycle 12): m_axis_tdata expected hi=607 lo=201 got hi=0 lo=0\n" \
            "Error: beat 3 (input cycle 13): m_axis_tdata expected hi=2015 lo=772 got hi=0 lo=0\nNote: SIMULATION FAILED"
    SimulationAnalyst(Llm([json.dumps({"verdict": "fail", "likely_cause": "rtl"})])).analyze(project, COMPILE_OK, {**SIM_FAIL, "sim_output": zeros})
    assert calls[0]["python_check_allowed"] is False and "failing_beats" in calls[0] and "FIXED RULE" in calls[0]["fixed_rule_hint"]

    # the runner itself: isolated interpreter, time limit, predefined data
    out = run_python_check("print(len(FAILING_BEATS), SPEC['x'])", [{"beat": 1}, {"beat": 2}], {"x": 7})
    assert out["status"] == "ok" and out["stdout"].strip() == "2 7"
    assert run_python_check("input()", [], None)["status"] == "error"                 # stdin is closed


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


def test_a_log_cut_at_the_size_limit_is_not_read_as_a_frozen_handshake():
    """timing_recovery, 2026-10-30: the testbench reported a mismatch on every
    one of 1200 cycles, the Vivado server cut the log at 200 KB, the FAILED
    marker at the end was lost, and the run was judged 'no marker, frozen
    handshake at 5036 ns' (the cut point) and rerun with 20us and 40us."""
    from agents.simulation_agent.simulation_analyst import truncated_chars
    log = ("Error: cycle 0: s_axis_tready expected 0 got '1'\nTime: 6 ns  Iteration: 0\n" * 50
           + "Error: cycle 503: m_axis_tuser expected 0 got 127\nTime: 5036 ns  Iteration: 0\n"
             "... [truncated 371544 chars] ...")
    cut = {**SIM_NO_MARKER, "sim_time": "40us", "sim_output": log}
    verdict = deterministic_verdict(COMPILE_OK, cut)
    assert verdict["verdict"] == "error" and verdict["likely_cause"] == "testbench"
    assert "cut" in verdict["summary"] and "371544" in verdict["summary"] and "51 report lines" in verdict["summary"]
    assert "waiting" not in verdict["summary"]
    assert "first 20" in verdict["suggested_fix"] and "beats in order" in verdict["suggested_fix"]
    assert "cycle by cycle" in verdict["fixed_rule"]              # the flag lines in that log: beat order, word for word
    assert truncated_chars(log) == 371544 and truncated_chars("Error: x\nTime: 10 ns") is None
    # the new server keeps the end of the log: a cut log with no marker really has none, so the usual
    # reading (stimulus longer than sim_time, or a frozen handshake) is given, with the flood noted first
    middle = ("Error: cycle 0: q expected 1 got 2\nTime: 6 ns  Iteration: 0\n"
              "... [truncated 300000 chars in the middle of the log] ...\n"
              "Error: cycle 998: q expected 1 got 2\nTime: 9986 ns  Iteration: 0\n## quit")
    verdict = deterministic_verdict(COMPILE_OK, {**SIM_NO_MARKER, "sim_time": "10us", "sim_output": middle})
    assert verdict["summary"].startswith("The simulation log was cut") and "end of sim_time" in verdict["summary"]
    assert verdict["suggested_fix"].startswith("Print at most the first 20") and "Shorten the stimulus" in verdict["suggested_fix"]
    assert "300000" in verdict["fixed_rule"] and "see fixed_rule" not in verdict["summary"]
    frozen_middle = middle.replace("Time: 9986 ns", "Time: 126 ns")
    assert "waiting" in deterministic_verdict(COMPILE_OK, {**SIM_NO_MARKER, "sim_time": "40us", "sim_output": frozen_middle})["summary"]
    # a run too short for its stimulus still shows the flag fault: it travels as fixed_rule
    short = ("Error: cycle 18: missing m_axis_tvalid\nTime: 186 ns  Iteration: 0\n"
             "Error: cycle 998: missing m_axis_tvalid\nTime: 9986 ns  Iteration: 0\n## quit")
    verdict = deterministic_verdict(COMPILE_OK, {**SIM_NO_MARKER, "sim_time": "10us", "sim_output": short})
    assert "end of sim_time" in verdict["summary"] and "see fixed_rule" in verdict["summary"]
    assert "'testbench'" in verdict["fixed_rule"] and "k-th expected beat" in verdict["fixed_rule"]
    assert "fixed_rule" not in deterministic_verdict(COMPILE_OK, {**SIM_NO_MARKER, "sim_time": "10us"})
    # the server now keeps the end of the log and reports the marker it saw on the full log
    assert outcome_of({"status": "error", "sim_passed": False, "sim_failed": True,
                       "sim_output": "... [truncated 9 chars in the middle of the log] ...\nNote: SIMULATION FAILED"}) == "failed"
    assert outcome_of({"status": "error", "sim_passed": False, "sim_failed": True, "sim_output": "run 10us"}) == "failed"
    # a design without an m_axis_tready port: the frozen-handshake text does not blame backpressure
    frozen = {**SIM_NO_MARKER, "sim_time": "40us",
              "sim_output": "Error: beat 0: m_axis_tdata expected 1 got 2\nTime: 126 ns  Iteration: 0\n"}
    no_tready = {"ports": [{"name": "s_axis_tvalid", "direction": "input"}, {"name": "m_axis_tvalid", "direction": "output"}]}
    verdict = deterministic_verdict(COMPILE_OK, frozen, no_tready)
    assert "waiting" in verdict["summary"] and "no m_axis_tready" in verdict["summary"]
    assert "m_axis_tready = 0" not in verdict["suggested_fix"] and "s_axis_tready" in verdict["suggested_fix"]
    with_tready = {"ports": no_tready["ports"] + [{"name": "m_axis_tready", "direction": "input"}]}
    assert "m_axis_tready = 0" in deterministic_verdict(COMPILE_OK, frozen, with_tready)["suggested_fix"]


def test_a_log_holding_the_full_mismatch_quota_is_not_read_as_a_frozen_handshake():
    """timing_recovery, 2026-11-05 19:02: the testbench printed its 20
    mismatches by 526 ns and, by its own cap, nothing more; the 24,000-row
    stimulus never finished inside 40 us, and the analyst said 'stopped
    making progress at 526 ns, waiting forever for a handshake. Not a
    too-short sim_time' three times. The testbench was rewritten twice for
    nothing and the RTL writer was sent after a fault that did not exist."""
    from agents.simulation_agent.simulation_analyst import mismatches_capped
    lines = [f"Error: beat {k} (input cycle {12 + 4 * k}): m_axis_tdata expected {314259 + k} got {8386559 if k else 0}\n"
             f"Time: {146 + 20 * k} ns  Iteration: 0  Process: /tb/stimulus" for k in range(20)]
    log = "\n".join(lines) + "\n"
    assert mismatches_capped(log) and not mismatches_capped("\n".join(lines[:19])) and not mismatches_capped(None)

    capped = {**SIM_NO_MARKER, "sim_time": "40us", "sim_output": log}
    assert deterministic_verdict(COMPILE_OK, capped, {"ports": []}) is None       # the LLM judges the 20 mismatches
    nineteen = {**capped, "sim_output": "\n".join(lines[:19])}
    assert "waiting" in deterministic_verdict(COMPILE_OK, nineteen, {"ports": []})["summary"]   # a real freeze still is one

    seen = {}

    class Llm:
        def invoke(self, messages, config=None):
            import json as _json
            seen.update(_json.loads(messages[1].content))

            class R:
                content = '{"verdict": "fail", "likely_cause": "rtl", "failures": ["beat 0"], "summary": "s", "suggested_fix": "f"}'
            return R()

    out = SimulationAnalyst(Llm()).analyze(PROJECT, COMPILE_OK, capped)
    assert "NOT a frozen handshake" in seen["note"] and "first 20 mismatches" in seen["note"] and "40us" in seen["note"]
    assert out["verdict"] == "fail" and out["likely_cause"] == "rtl"
    seen.clear()
    SimulationAnalyst(Llm()).analyze(PROJECT, COMPILE_OK, {**SIM_FAIL, "sim_output": log + "Note: SIMULATION FAILED"})
    assert "note" not in seen                                                      # a finished run needs no note


def test_flag_compares_per_cycle_are_blamed_on_the_testbench_not_the_rtl_timing():
    """timing_recovery attempt 2, 2026-10-30: 'unexpected m_axis_tvalid' on
    cycles 8-10, 'missing m_axis_tvalid' on every expected row, and the analyst
    asked the RTL writer to fix the NCO phase and the latency L, which the spec
    leaves to the RTL. The testbench checked WHEN instead of WHAT."""
    from agents.simulation_agent.simulation_analyst import flag_cycle_compare_hint
    log = ("Error: cycle 8: unexpected m_axis_tvalid\nError: cycle 9: unexpected m_axis_tvalid\n"
           "Error: cycle 10: m_axis_tuser expected 0 got 127\nError: cycle 10: unexpected m_axis_tvalid\n"
           "Error: cycle 18: missing m_axis_tvalid\nError: cycle 21: missing m_axis_tvalid\nNote: SIMULATION FAILED")
    hint = flag_cycle_compare_hint(log)
    assert hint and "5 line(s)" in hint and "'testbench'" in hint and "cycle 8: unexpected m_axis_tvalid" in hint
    assert "latency" in hint and "k-th expected beat" in hint and "prove nothing" in hint
    assert fixed_rule_hint(log) == hint                                  # it comes before the value rules
    # attempt 1's wording, the ready flag, and the other forms the linter rejects count too
    assert flag_cycle_compare_hint("Error: cycle 7: m_axis_tvalid expected 0 got '1'\n"
                                   "Error: cycle 0: s_axis_tready expected 0 got '1'")
    assert flag_cycle_compare_hint("Error: cycle 8: m_axis_tvalid: expected 0 got '1'\n"
                                   "Error: cycle 9: s_axis_tready mismatch: csv 0 dut '1'")
    # data mismatches, deadlocks and the beat-order reports are not flag compares
    assert flag_cycle_compare_hint("Error: cycle 17: m_axis_tdata expected 5 got 0\n"
                                   "Error: cycle 3: s_axis_tready never rose (deadlock)\n"
                                   "Error: missing output beats: expected 20 got 18\n"
                                   "Error: unexpected output beat at cycle 40") is None
    assert flag_cycle_compare_hint(None) is None

    seen = {}

    class CapturingLLM:
        def invoke(self, messages, config=None):
            import json as _json
            seen.update(_json.loads(messages[1].content))

            class R:
                content = '{"verdict": "fail", "likely_cause": "testbench", "failures": [], "summary": "s", "suggested_fix": "f"}'
            return R()

    out = SimulationAnalyst(llm=CapturingLLM()).analyze(PROJECT, {"status": "ok"}, {"outcome": "failed", "sim_output": log})
    assert "cycle by cycle" in seen["fixed_rule_hint"] and seen["python_check_allowed"] is False
    assert out["fixed_rule"] == hint                                     # travels with the blame note
    from prompts.simulation_agent_prompt import SIMULATION_AGENT_SYSTEM_PROMPT
    assert "missing" in SIMULATION_AGENT_SYSTEM_PROMPT and "log_truncated_chars" in SIMULATION_AGENT_SYSTEM_PROMPT


def test_a_cut_log_tells_the_llm_analyst_so_and_totals_do_not_feed_the_value_rules():
    """Adversarial review, 2026-10-30: _tail drops the server's cut note from
    the middle of the log, so the LLM never learned the log was cut; and the
    testbench's own total line 'missing output beats: expected N got K' was
    counted as a per-beat value and vetoed the all-zero rule."""
    from agents.simulation_agent.simulation_analyst import _expected_got_pairs, _trim_for_llm
    log = ("Error: beat 1: m_axis_tdata expected 5 got 0\n" * 3000
           + "... [truncated 123456 chars in the middle of the log] ...\n"
           + "Error: beat 2: m_axis_tdata expected 7 got 0\n" * 300
           + "Error: missing output beats: expected 20 got 18\nNote: SIMULATION FAILED")
    _, sim = _trim_for_llm(None, {"outcome": "failed", "sim_output": log})
    assert sim["log_truncated_chars"] == 123456 and sim["sim_output"].startswith("[the Vivado server cut 123456 chars")
    assert "report lines survived the cut" in sim["sim_output"] and len(sim["sim_output"]) < 12000
    assert _trim_for_llm(None, {"outcome": "failed", "sim_output": "short"})[1] == {"outcome": "failed", "sim_output": "short"}
    assert _expected_got_pairs("Error: beat 1: q expected 5 got 0\nError: missing output beats: expected 20 got 18\n"
                               "Note: mismatches: 3") == [("5", "0")]
    assert all_zero_output_hint("Error: beat 1: q expected 5 got 0\nError: beat 2: q expected 7 got 0\n"
                                "Error: missing output beats: expected 20 got 18")


def test_decimating_designs_take_the_stimulus_from_the_expected_rows_cycle():
    """timing_recovery: about one output beat per 4 input samples, so the k-th
    input beat is not the sample that produced output beat k; the golden-model
    rule puts the expected beat on the row of the sample that produced it."""
    from agents.simulation_agent.simulation_analyst import failing_beats
    in_csv = "cycle,aclk,aresetn,s_axis_tvalid,s_axis_tdata\n" + "".join(
        f"{c},1,{int(c >= 2)},{int(c >= 2)},{100 + c}\n" for c in range(12))
    out_csv = "cycle,s_axis_tready,m_axis_tvalid,m_axis_tdata,m_axis_tuser\n" + "".join(
        f"{c},1,{int(c in (5, 9))},{c * 10 if c in (5, 9) else 0},0\n" for c in range(12))
    beats = failing_beats("Error: beat 1: m_axis_tdata expected 90 got 91", in_csv, out_csv)
    assert beats[0]["expected_row"]["cycle"] == "9" and beats[0]["input_row"]["cycle"] == "9"
    assert beats[0]["input_row"]["s_axis_tdata"] == "109" and "fewer output beats" in beats[0]["input_row_note"]
    assert beats[0]["input_cycle"] == "9"
    # one beat out per beat in (the LLR demapper): the k-th input beat, as before
    one_to_one = failing_beats(LLR_LOG, LLR_IN_CSV, LLR_OUT_CSV, {"reset": {"name": "aresetn", "active": "low"}})
    assert one_to_one[0]["input_cycle"] == "4" and "input_row_note" not in one_to_one[0]
    from prompts.simulation_agent_prompt import SIMULATION_AGENT_SYSTEM_PROMPT
    assert "input_row_note" in SIMULATION_AGENT_SYSTEM_PROMPT

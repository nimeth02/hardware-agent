"""Simulation agent tests — no LLM, no MCP, no Vivado: project loader,
compiler, simulator, analyst and planner are fakes; the graph, guardrails
and rerun/limit logic are real."""

from __future__ import annotations

import pytest

from agents.simulation_agent.simulation_agent import SimulationAgent, resolve_project_dir
from agents.simulation_agent.simulation_analyst import SimulationAnalyst, deterministic_verdict, parse_analysis_response
from agents.simulation_agent.simulation_decider import Decision, SimulationDecider
from agents.simulation_agent.simulation_runner import double_sim_time, outcome_of
from agents.simulation_agent.state import MAX_SIM_RERUNS, Action, SimulationStatus
from agents.simulation_agent.tools import SimulationAgentTools
from services.config import REPO_ROOT

PROJECT = {"name": "counter4", "dir": "C:/x/counter4", "src_files": ["src/counter4.vhd"], "tb_file": "tb/counter4_tb.vhd",
           "top_entity": "counter4_tb", "spec": {"entity_name": "counter4"}, "test_vectors": "test_vectors.csv",
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
    assert deterministic_verdict(COMPILE_OK, SIM_FAIL) is None
    assert deterministic_verdict(COMPILE_BAD, None) is None
    assert outcome_of({"status": "ok", "sim_passed": True, "sim_output": "SIMULATION PASSED"}) == "passed"
    assert outcome_of({"status": "error", "sim_passed": False, "sim_output": "SIMULATION FAILED"}) == "failed"
    assert outcome_of({"status": "error", "sim_passed": False, "sim_output": "[xelab failed]\nERROR: x"}) == "error"
    assert outcome_of({"status": "error", "sim_passed": False, "sim_output": "", "error_class": "environment"}) == "error"
    assert outcome_of({"status": "error", "sim_passed": False, "sim_output": "run 10us\nquit"}) == "no_marker"
    assert double_sim_time("10us") == "20us" and double_sim_time("1.5 ms") == "3ms" and double_sim_time("bogus") == "bogus"


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

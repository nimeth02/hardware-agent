"""Implementation agent tests — no LLM, no MCP, no Vivado: project loader,
script writer, runner, metrics, analyst and planner are fakes; the graph,
guardrails and limit logic are real."""

from __future__ import annotations

import pytest

from agents.implementation_agent.implementation_agent import ImplementationAgent, resolve_project_dir
from agents.implementation_agent.implementation_analyst import (
    ImplementationAnalyst, deterministic_verdict, parse_analysis_response,
)
from agents.implementation_agent.implementation_decider import Decision, ImplementationDecider
from agents.implementation_agent.implementation_runner import metrics_from_reports, parse_timing_summary, parse_utilization
from agents.implementation_agent.script_writer import make_tcl, make_xdc, plan_scripts
from agents.implementation_agent.state import Action, ImplementationStatus
from agents.implementation_agent.tools import ImplementationAgentTools
from services.config import REPO_ROOT

SPEC = {"entity_name": "counter4", "clock": {"required": True, "name": "clk", "frequency_mhz": 100}}
PROJECT = {"name": "counter4", "dir": "C:/x/counter4", "src_files": ["src/counter4.vhd"], "top_entity": "counter4",
           "spec": SPEC, "xdc_file": None}
SCRIPTS = {"xdc": "constraints/counter4.xdc", "tcl": "scripts/synth_impl.tcl", "constraints": "create_clock ...",
           "part": "xc7a35tcpg236-1", "generated_xdc": True}
BATCH_OK = {"status": "ok", "log_excerpt": "route_design done", "reports_dir": "C:/x/counter4/impl_out", "error_class": None}
BATCH_BAD = {"status": "error", "log_excerpt": "ERROR: [Synth 8-2715] syntax error", "reports_dir": None, "error_class": None}
METRICS_OK = {"wns_ns": 7.234, "tns_ns": 0.0, "whs_ns": 0.182, "failing_endpoints": 0, "lut_used": 4, "lut_pct": 0.01,
              "ff_used": 4, "ff_pct": 0.01, "reports": ["timing_summary.rpt", "utilization.rpt"]}
METRICS_SLOW = {**METRICS_OK, "wns_ns": -1.5, "failing_endpoints": 3}
PASS = {"verdict": "pass", "likely_cause": None, "failures": [], "summary": "ok", "suggested_fix": None}
FAIL = {"verdict": "fail", "likely_cause": "rtl", "failures": ["WNS -1.5"], "summary": "timing violated", "suggested_fix": "pipeline"}


class FakeLoader:
    def __init__(self, project=PROJECT, error=None):
        self.project, self.error, self.calls = project, error, []

    def load(self, project_dir):
        self.calls.append(project_dir)
        if self.error:
            raise RuntimeError(self.error)
        return dict(self.project)


def fake_writer(scripts=SCRIPTS):
    calls = []

    def write(project, part):
        calls.append(part)
        return {**scripts, "part": part}
    write.calls = calls
    return write


def fake_runner(batch):
    calls = []

    def run(project, scripts):
        calls.append(scripts)
        if isinstance(batch, Exception):
            raise batch
        return dict(batch)
    run.calls = calls
    return run


def fake_metrics(metrics):
    calls = []

    def collect(project):
        calls.append(project["name"])
        return dict(metrics)
    collect.calls = calls
    return collect


class FakeAnalyst(ImplementationAnalyst):
    def __init__(self, verdicts):
        super().__init__(llm=object())
        self.verdicts, self.calls = list(verdicts), []

    def analyze(self, project, scripts, batch, metrics):
        self.calls.append({"scripts": scripts, "batch": batch, "metrics": metrics})
        return self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]


class FakeDecider(ImplementationDecider):
    """Rule-based stand-in for the planner, with optional forced decisions."""

    def __init__(self, forced=None):
        super().__init__(llm=object())
        self.forced, self.seen = list(forced or []), []

    def decide(self, summary):
        self.seen.append(summary)
        if self.forced:
            return self.forced.pop(0)
        return self.parse("", summary)   # the deterministic fallback policy


def make_agent(batch=BATCH_OK, metrics=METRICS_OK, verdicts=(PASS,), loader=None, decider=None, max_iterations=8):
    tools = ImplementationAgentTools(project_loader=loader or FakeLoader(), script_writer=fake_writer(),
                                     runner=fake_runner(batch), metrics=fake_metrics(metrics),
                                     analyst=FakeAnalyst(verdicts), decider=decider or FakeDecider())
    return ImplementationAgent(tools=tools, max_iterations=max_iterations)


# ── happy path ───────────────────────────────────────────────────────────

def test_load_write_run_analyze_finalize_on_pass():
    agent = make_agent()
    out = agent.run("counter4", "xc7a35tcpg236-1")
    assert out["status"] == ImplementationStatus.COMPLETE.value and out["verdict"] == "pass"
    assert out["project_dir"] == str((REPO_ROOT / "projects" / "counter4").resolve())
    assert agent.tools.script_writer.calls == ["xc7a35tcpg236-1"]
    assert agent.tools.runner.calls[0]["tcl"] == "scripts/synth_impl.tcl"
    assert agent.tools.metrics.calls == ["counter4"] and out["metrics"] == METRICS_OK
    assert out["iteration"] == 5 and out["error"] is None and out["warnings"] == []


def test_timing_violation_ends_in_needs_review_with_the_analysis():
    agent = make_agent(metrics=METRICS_SLOW, verdicts=[FAIL])
    out = agent.run("counter4")
    assert out["status"] == ImplementationStatus.NEEDS_REVIEW.value and out["verdict"] == "fail"
    assert out["analysis"] == FAIL and "timing violated" in out["error"]
    assert agent.tools.analyst.calls[0]["metrics"] == METRICS_SLOW


def test_batch_error_skips_metrics_and_is_analyzed():
    agent = make_agent(batch=BATCH_BAD, verdicts=[{**FAIL, "verdict": "error"}])
    out = agent.run("counter4")
    assert out["status"] == ImplementationStatus.NEEDS_REVIEW.value and out["verdict"] == "error"
    assert agent.tools.metrics.calls == [] and out["metrics"] == {}
    assert agent.tools.analyst.calls[0]["batch"] == BATCH_BAD


# ── guardrails ───────────────────────────────────────────────────────────

def test_planner_cannot_run_twice():
    forced = [Decision(Action.LOAD_PROJECT), Decision(Action.WRITE_SCRIPTS), Decision(Action.RUN_IMPLEMENTATION),
              Decision(Action.RUN_IMPLEMENTATION, "wrong"), Decision(Action.RUN_IMPLEMENTATION, "wrong")]
    agent = make_agent(decider=FakeDecider(forced))
    out = agent.run("counter4")
    assert out["status"] == ImplementationStatus.COMPLETE.value
    assert len(agent.tools.runner.calls) == 1 and len(agent.tools.analyst.calls) == 1


def test_run_and_finalize_are_blocked_until_their_prerequisites_exist():
    forced = [Decision(Action.RUN_IMPLEMENTATION, "wrong"), Decision(Action.FINALIZE_IMPLEMENTATION, "wrong"),
              Decision(Action.LOAD_PROJECT, "wrong"), Decision(Action.FINALIZE_IMPLEMENTATION, "wrong")]
    agent = make_agent(decider=FakeDecider(forced))
    out = agent.run("counter4")
    assert out["status"] == ImplementationStatus.COMPLETE.value
    assert [s["project_loaded"] for s in agent.tools.decider.seen][:2] == [False, True]
    assert len(agent.tools.runner.calls) == 1 and out["iteration"] == 5


# ── failures and limits ──────────────────────────────────────────────────

def test_project_load_failure_fails_the_run():
    out = make_agent(loader=FakeLoader(error="no src")).run("missing")
    assert out["status"] == ImplementationStatus.FAILED.value and "no src" in out["error"]


def test_missing_spec_and_xdc_is_a_warning():
    out = make_agent(loader=FakeLoader({**PROJECT, "spec": None})).run("counter4")
    assert out["status"] == ImplementationStatus.COMPLETE.value
    assert any("unconstrained" in w for w in out["warnings"])


def test_runner_exception_fails_the_run():
    out = make_agent(batch=RuntimeError("vivado down")).run("counter4")
    assert out["status"] == ImplementationStatus.FAILED.value and "vivado down" in out["error"]


def test_iteration_limit_returns_failed_without_a_verdict():
    out = make_agent(decider=FakeDecider([Decision(Action.LOAD_PROJECT)] * 10), max_iterations=2).run("counter4")
    assert out["status"] == ImplementationStatus.FAILED.value and "Iteration limit" in out["error"]


# ── deterministic pieces ─────────────────────────────────────────────────

def test_scripts_from_spec_and_existing_xdc_is_kept():
    xdc = make_xdc(SPEC)
    assert "create_clock -period 10.000 -name clk [get_ports clk]" in xdc
    assert "not constrained" in make_xdc(None) and "not constrained" in make_xdc({"clock": {"required": False}})
    tcl = make_tcl("counter4", ["src/a.vhd", "src/b.vhd"], "constraints/c.xdc", "xc7a35tcpg236-1")
    assert "read_vhdl {src/a.vhd}\nread_vhdl {src/b.vhd}\n" in tcl and "synth_design -top counter4 -part xc7a35tcpg236-1" in tcl
    assert "delete" not in tcl and "exec" not in tcl                  # the MCP server rejects deletion patterns

    plan = plan_scripts(PROJECT, "xc7a35tcpg236-1")
    assert plan["generated_xdc"] and set(plan["files"]) == {"constraints/counter4.xdc", "scripts/synth_impl.tcl"}
    kept = plan_scripts({**PROJECT, "xdc_file": "constraints/board.xdc"}, "xc7a35tcpg236-1")
    assert not kept["generated_xdc"] and set(kept["files"]) == {"scripts/synth_impl.tcl"}
    assert "read_xdc {constraints/board.xdc}" in kept["files"]["scripts/synth_impl.tcl"] and kept["constraints"] is None


TIMING_RPT = """
------------------------------------------------------------------------------------------------
| Design Timing Summary
| ---------------------
------------------------------------------------------------------------------------------------

    WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)  THS(ns)  THS Failing Endpoints  THS Total Endpoints
    -------  -------  ---------------------  -------------------      -------  -------  ---------------------  -------------------
      7.234    0.000                      0                    8        0.182    0.000                      0                    8
"""
UTIL_RPT = """
| Slice LUTs*             |    4 |     0 |     20800 | <0.01 |
| Slice Registers         |    5 |     0 |     41600 |  0.02 |
| Block RAM Tile          |    0 |     0 |        50 |  0.00 |
| DSPs                    |    0 |     0 |        90 |  0.00 |
"""


def test_report_parsing():
    t = parse_timing_summary(TIMING_RPT)
    assert t["wns_ns"] == 7.234 and t["tns_ns"] == 0.0 and t["whs_ns"] == 0.182 and t["failing_endpoints"] == 0
    assert parse_timing_summary("There are no user specified timing constraints.")["unconstrained"] is True
    u = parse_utilization(UTIL_RPT)
    assert (u["lut_used"], u["lut_pct"], u["ff_used"], u["ff_pct"], u["bram_used"], u["dsp_used"]) == (4, 0.01, 5, 0.02, 0, 0)
    m = metrics_from_reports({"timing_summary.rpt": TIMING_RPT, "utilization.rpt": UTIL_RPT})
    assert m["wns_ns"] == 7.234 and m["lut_used"] == 4 and m["reports"] == ["timing_summary.rpt", "utilization.rpt"]
    assert metrics_from_reports({}) == {}


def test_deterministic_verdicts():
    assert deterministic_verdict(BATCH_OK, METRICS_OK)["verdict"] == "pass"
    assert deterministic_verdict(BATCH_OK, {"unconstrained": True, "lut_pct": 1})["verdict"] == "pass"
    assert deterministic_verdict(BATCH_OK, METRICS_SLOW) is None
    assert deterministic_verdict(BATCH_OK, {**METRICS_OK, "lut_pct": 120.0}) is None
    assert deterministic_verdict(BATCH_OK, {}) is None               # no reports: not a pass
    assert deterministic_verdict(BATCH_BAD, METRICS_OK) is None


def test_invalid_outputs_fall_back_safely():
    assert parse_analysis_response("not json", BATCH_OK)["verdict"] == "fail"
    assert parse_analysis_response("not json", BATCH_BAD)["verdict"] == "error"
    assert parse_analysis_response('{"verdict": "fail", "summary": "s"}', BATCH_OK)["summary"] == "s"
    parse = ImplementationDecider.parse
    assert parse("garbage", {"project_loaded": False}).action is Action.LOAD_PROJECT
    assert parse("??", {"project_loaded": True, "scripts_written": False}).action is Action.WRITE_SCRIPTS
    assert parse("??", {"project_loaded": True, "scripts_written": True, "batch": None}).action is Action.RUN_IMPLEMENTATION
    assert parse("??", {"project_loaded": True, "scripts_written": True, "batch": {"status": "ok"}, "analysis": None}).action is Action.ANALYZE_RESULT
    assert parse("??", {"project_loaded": True, "scripts_written": True, "batch": {"status": "ok"}, "analysis": {"verdict": "pass"}}).action is Action.FINALIZE_IMPLEMENTATION
    ok = parse('```json\n{"action": "analyze_result", "reason": "r"}\n```', {})
    assert ok.action is Action.ANALYZE_RESULT and ok.reason == "r"


def test_resolve_project_dir_and_restricted_actions():
    assert resolve_project_dir("counter4") == str((REPO_ROOT / "projects" / "counter4").resolve())
    assert resolve_project_dir(str(REPO_ROOT / "tests")) == str((REPO_ROOT / "tests").resolve())
    assert {a.value for a in Action} == {"load_project", "write_scripts", "run_implementation", "analyze_result", "finalize_implementation"}
    nodes = set(make_agent().graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"decide", "load_project", "write_scripts", "run_implementation", "analyze_result", "finalize_implementation"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

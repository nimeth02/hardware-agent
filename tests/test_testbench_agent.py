"""Testbench agent tests — no LLM, no MCP, no Vivado: generator, planner,
skill loader and linter are fakes; the graph, guardrails and stall/limit
logic are real."""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from agents.testbench_agent.state import Action, TestbenchStatus
from agents.testbench_agent.testbench_agent import TestbenchAgent
from agents.testbench_agent.testbench_decider import Decision, TestbenchDecider
from agents.testbench_agent.testbench_generator import TestbenchGenerator, parse_testbench_response
from agents.testbench_agent.testbench_skill_loader import TestbenchSkillLoader
from agents.testbench_agent.tools import TestbenchAgentTools

SPEC = {"entity_name": "counter4", "ports": [{"name": "clk", "width": "1"}, {"name": "count", "width": "4"}],
        "clock": {"required": True, "name": "clk", "frequency_mhz": 100}, "behavior": ["count up"]}
RTL = {"entity_name": "counter4", "code": "entity counter4 is ... end;"}
GOOD_TB = {"entity_name": "counter4_tb", "code": "entity counter4_tb is end;", "notes": []}
BAD_TB = {"entity_name": "tb_wrong", "code": "entity tb_wrong is", "notes": []}
SKILLS = {"vhdl_2008_testbench_rules.md": "- label the DUT instance dut"}
CLEAN = {"compiler": "xvhdl", "issues": []}
DIRTY = {"compiler": "xvhdl", "issues": [{"code": "name_mismatch", "detail": "Testbench entity is 'tb_wrong'"}]}


class FakeGenerator(TestbenchGenerator):
    def __init__(self, tbs):
        super().__init__(llm=object())
        self.tbs = list(tbs)
        self.calls: list[dict] = []

    def generate(self, spec, rtl, skills=None, previous_testbench=None, lint_issues=None):
        self.calls.append({"rtl": rtl, "skills": dict(skills or {}), "previous": previous_testbench,
                           "lint_issues": list(lint_issues or [])})
        return self.tbs.pop(0) if len(self.tbs) > 1 else self.tbs[0]


class FakeDecider(TestbenchDecider):
    def __init__(self, forced=None):
        super().__init__(llm=object())
        self.forced = list(forced or [])
        self.seen: list[dict] = []

    def decide(self, summary):
        self.seen.append(summary)
        if self.forced:
            return self.forced.pop(0)
        if not summary["skills_loaded"]:
            return Decision(Action.LOAD_SKILLS, "rule")
        if not summary["has_testbench"] or (summary["lint"] or {}).get("issues"):
            return Decision(Action.GENERATE_TESTBENCH, "rule")
        return Decision(Action.FINALIZE_TESTBENCH, "rule")


class FakeSkillLoader(TestbenchSkillLoader):
    def __init__(self, skills=SKILLS, error=None):
        self.skills, self.error = skills, error

    def load(self):
        if self.error:
            raise RuntimeError(self.error)
        return dict(self.skills)


def fake_linter(results):
    queue = list(results)

    def lint(spec, rtl, testbench):
        return queue.pop(0) if len(queue) > 1 else queue[0]
    return lint


def make_agent(tbs, lints, decider=None, loader=None, max_iterations=6):
    tools = TestbenchAgentTools(generator=FakeGenerator(tbs), decider=decider or FakeDecider(),
                                skill_loader=loader or FakeSkillLoader(), linter=fake_linter(lints))
    return TestbenchAgent(tools=tools, max_iterations=max_iterations)


def test_skills_are_loaded_first_and_passed_with_the_rtl_to_the_generator():
    agent = make_agent([GOOD_TB], [CLEAN])
    out = agent.run(SPEC, RTL)
    assert out["status"] == TestbenchStatus.COMPLETE.value
    assert agent.tools.decider.seen[0]["skills_loaded"] is False
    assert agent.tools.generator.calls[0]["skills"] == SKILLS
    assert agent.tools.generator.calls[0]["rtl"] == RTL
    assert out["skills_used"] == ["vhdl_2008_testbench_rules.md"]
    assert out["iteration"] == 3


def test_clean_lint_finalizes():
    out = make_agent([GOOD_TB], [CLEAN]).run(SPEC, RTL)
    assert out["status"] == TestbenchStatus.COMPLETE.value
    assert out["testbench"] == GOOD_TB and out["lint"] == CLEAN and out["error"] is None


def test_lint_issues_trigger_a_fix_regeneration_with_feedback():
    agent = make_agent([BAD_TB, GOOD_TB], [DIRTY, CLEAN])
    out = agent.run(SPEC, RTL)
    assert out["status"] == TestbenchStatus.COMPLETE.value
    assert agent.tools.generator.calls[1]["previous"] == BAD_TB
    assert agent.tools.generator.calls[1]["lint_issues"] == DIRTY["issues"]
    assert out["iteration"] == 4


def test_fixes_that_change_nothing_end_in_needs_review():
    out = make_agent([BAD_TB], [DIRTY]).run(SPEC, RTL)
    assert out["status"] == TestbenchStatus.NEEDS_REVIEW.value
    assert "fix attempts" in out["error"] and out["testbench"] == BAD_TB


def test_finalize_is_blocked_while_lint_issues_exist():
    forced = [Decision(Action.LOAD_SKILLS), Decision(Action.GENERATE_TESTBENCH),
              Decision(Action.FINALIZE_TESTBENCH, "wrong")]
    agent = make_agent([BAD_TB, GOOD_TB], [DIRTY, CLEAN], decider=FakeDecider(forced))
    out = agent.run(SPEC, RTL)
    assert out["status"] == TestbenchStatus.COMPLETE.value
    assert agent.tools.generator.calls[1]["lint_issues"] == DIRTY["issues"]


def test_skill_loader_failure_is_a_warning_not_a_failure():
    out = make_agent([GOOD_TB], [CLEAN], loader=FakeSkillLoader(error="mcp down")).run(SPEC, RTL)
    assert out["status"] == TestbenchStatus.COMPLETE.value
    assert out["skills_used"] == [] and any("mcp down" in w for w in out["warnings"])


def test_generator_failure_fails_the_run():
    class Exploding(FakeGenerator):
        def generate(self, *a, **k):
            raise RuntimeError("llm down")
    tools = TestbenchAgentTools(generator=Exploding([GOOD_TB]), decider=FakeDecider(),
                                skill_loader=FakeSkillLoader(), linter=fake_linter([CLEAN]))
    out = TestbenchAgent(tools=tools).run(SPEC, RTL)
    assert out["status"] == TestbenchStatus.FAILED.value and "llm down" in out["error"]


def test_iteration_limit_returns_needs_review():
    issues = [{"compiler": None, "issues": [{"code": "x", "detail": f"problem {i}"}]} for i in range(10)]
    forced = [Decision(Action.LOAD_SKILLS)] + [Decision(Action.GENERATE_TESTBENCH)] * 10
    out = make_agent([BAD_TB], issues, decider=FakeDecider(forced), max_iterations=4).run(SPEC, RTL)
    assert out["status"] == TestbenchStatus.NEEDS_REVIEW.value
    assert out["iteration"] == 4 and "Iteration limit" in out["error"]


def test_invalid_planner_output_falls_back_safely():
    assert TestbenchDecider.parse("garbage", {"skills_loaded": False}).action is Action.LOAD_SKILLS
    assert TestbenchDecider.parse("??", {"skills_loaded": True, "has_testbench": False}).action is Action.GENERATE_TESTBENCH
    assert TestbenchDecider.parse("??", {"skills_loaded": True, "has_testbench": True, "lint": None}).action is Action.LINT_TESTBENCH
    assert TestbenchDecider.parse("??", {"skills_loaded": True, "has_testbench": True, "lint": DIRTY}).action is Action.GENERATE_TESTBENCH
    assert TestbenchDecider.parse("??", {"skills_loaded": True, "has_testbench": True, "lint": CLEAN}).action is Action.FINALIZE_TESTBENCH


def test_invalid_llm_output_is_kept_as_code():
    gen = TestbenchGenerator(llm=FakeListChatModel(responses=["not json at all"]))
    tb = gen.generate(SPEC, RTL)
    assert tb["entity_name"] == "counter4_tb" and tb["code"] == "not json at all"
    assert parse_testbench_response(SPEC, RTL, '{"entity_name": "counter4_tb", "code": "x"}')["code"] == "x"


def test_linter_takes_the_dut_identity_from_the_rtl_not_the_spec():
    from agents.testbench_agent.testbench_linter import _static_checks
    spec_says_counter = {**SPEC, "entity_name": "counter"}
    tb = {"entity_name": "counter4_tb", "code": """library ieee; use ieee.std_logic_1164.all;
entity counter4_tb is end entity;
architecture sim of counter4_tb is signal clk : std_logic := '0'; signal count : std_logic_vector(3 downto 0); begin
  dut: entity work.counter4 port map (clk => clk, count => count);
  process begin wait for 10 ns; assert false report "SIMULATION PASSED" severity note; wait; end process;
end architecture;"""}
    codes = {i["code"] for i in _static_checks(spec_says_counter, RTL, tb)}
    assert "dut_not_instantiated" not in codes and "name_mismatch" not in codes


def test_placeholder_rtl_fails_fast_without_iterating():
    agent = make_agent([GOOD_TB], [CLEAN])
    out = agent.run(SPEC, {"entity_name": "counter4", "code": "-- paste the RTL agent's code here"})
    assert out["status"] == TestbenchStatus.FAILED.value
    assert "no entity declaration" in out["error"]
    assert out["iteration"] == 0 and agent.tools.generator.calls == []


def test_compiler_errors_are_stable_and_quote_the_source_line():
    from agents.lint_utils import compiler_errors
    src = "line one\nclk <= not clk after period_ns/2 ns;\nline three"
    out = ("ERROR: [VRFC 10-724] found '0' definitions of operator \"/\" "
           "[C:/Users/me/AppData/Local/Temp/tmpab12cd/counter4_tb.vhd:2]\n"
           "ERROR: [VRFC 10-3782] unit 'tb' ignored due to previous errors [C:/Users/me/Temp/tmpab12cd/counter4_tb.vhd:1]\n")
    a = compiler_errors(out, src, 1, "testbench")
    b = compiler_errors(out.replace("tmpab12cd", "tmpzz99xx"), src, 1, "testbench")
    assert a == b                                           # temp dir no longer breaks stall detection
    assert a[0]["detail"].startswith("testbench: ERROR")
    assert "[counter4_tb.vhd:2]" in a[0]["detail"] and "tmpab12cd" not in a[0]["detail"]
    assert ">> line 2: clk <= not clk after period_ns/2 ns;" in a[0]["detail"]
    assert compiler_errors("", src, 3) == [{"code": "compile_error", "detail": "Compiler exited with code 3."}]


def test_ellipsis_in_prose_is_not_a_placeholder():
    from agents.testbench_agent.testbench_linter import _static_checks
    base = """library ieee; use ieee.std_logic_1164.all;
entity counter4_tb is end entity;
architecture sim of counter4_tb is signal clk : std_logic := '0'; signal count : std_logic_vector(3 downto 0); begin
  dut: entity work.counter4 port map (clk => clk, count => count);
  -- Step 3: verify counting sequence (0 -> 1 -> 2 -> ... -> 15)
  process begin wait for 10 ns; report "checking..." severity note; report "SIMULATION PASSED" severity note; wait; end process;
end architecture;"""
    tb = {"entity_name": "counter4_tb", "code": base}
    assert not [i for i in _static_checks(SPEC, RTL, tb) if i["code"] == "placeholder"]

    stub = {"entity_name": "counter4_tb",
            "code": base.replace("  process begin", "  ...\n  -- TODO stimulus\n  process begin")}
    found = [i["detail"] for i in _static_checks(SPEC, RTL, stub) if i["code"] == "placeholder"]
    assert len(found) == 2 and "line" in found[0]


def test_when_else_inside_a_process_is_caught_before_xelab_would_refuse_it():
    from agents.testbench_agent.testbench_linter import _static_checks
    code = """library ieee; use ieee.std_logic_1164.all;
entity counter4_tb is end entity;
architecture sim of counter4_tb is signal clk, rst : std_logic := '0'; signal count : std_logic_vector(3 downto 0);
  signal done : boolean := false; begin
  dut: entity work.counter4 port map (clk => clk, count => count);
  clk <= not clk after 5 ns when not done else '0';          -- concurrent: fine
  stimulus : process variable v_rst : integer := 1; begin
    rst <= '1' when v_rst = 1 else '0';                      -- sequential: xelab 2020.1 refuses
    wait for 10 ns; report "SIMULATION PASSED" severity note; done <= true; wait;
  end process;
end architecture;"""
    found = [i for i in _static_checks(SPEC, RTL, {"entity_name": "counter4_tb", "code": code})
             if i["code"] == "conditional_assignment_in_process"]
    assert len(found) == 1 and "Line 8" in found[0]["detail"] and "if c then" in found[0]["detail"]

    fixed = code.replace("rst <= '1' when v_rst = 1 else '0';", "if v_rst = 1 then rst <= '1'; else rst <= '0'; end if;")
    assert not [i for i in _static_checks(SPEC, RTL, {"entity_name": "counter4_tb", "code": fixed})
                if i["code"] == "conditional_assignment_in_process"]


def test_replay_mistakes_that_compile_but_break_the_simulation_are_caught():
    from agents.testbench_agent.testbench_linter import _replay_checks
    spec = {"entity_name": "demap", "ports": [
        {"name": "aclk", "direction": "input", "width": "1"},
        {"name": "s_axis_tdata", "direction": "input", "width": "32"},
        {"name": "m_axis_tvalid", "direction": "output", "width": "1"},
        {"name": "m_axis_tdata", "direction": "output", "width": "24"}]}
    bad = """
      variable v_cycle, v_aclk, v_s_axis_tdata : integer;   -- 32-bit port into an integer
      variable v_m_axis_tdata : integer;
      read(li, v_cycle); read(li, v_aclk); read(li, v_s_axis_tdata);
      read(lo, v_cycle); read(lo, v_m_axis_tdata);          -- m_axis_tvalid column skipped
      if v_cycle /= v_cycle then
    """
    codes = {i["code"] for i in _replay_checks(spec, bad)}
    assert codes == {"wide_port_read_as_integer", "column_count_mismatch", "self_comparison"}

    good = """
      variable v_cycle, v_cycle_out, v_aclk, v_m_axis_tvalid, v_m_axis_tdata : integer;
      variable v_s_axis_tdata : std_logic_vector(31 downto 0);
      read(li, v_cycle); read(li, v_aclk); read_wide(li, v_s_axis_tdata);
      read(lo, v_cycle_out); read(lo, v_m_axis_tvalid); read(lo, v_m_axis_tdata);
      if v_cycle /= v_cycle_out then
    """
    assert _replay_checks(spec, good) == []
    # no directions in the spec (older specs): the column count cannot be known, nothing is reported
    assert _replay_checks(SPEC, bad) == [i for i in _replay_checks(SPEC, bad) if i["code"] == "self_comparison"]


def test_agent_only_has_restricted_actions():
    assert {a.value for a in Action} == {"load_skills", "generate_testbench", "lint_testbench", "finalize_testbench"}
    nodes = set(make_agent([GOOD_TB], [CLEAN]).graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"decide", "load_skills", "generate_testbench", "lint_testbench", "finalize_testbench"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

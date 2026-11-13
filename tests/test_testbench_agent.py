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

    def generate(self, spec, rtl, skills=None, previous_testbench=None, lint_issues=None, attempt=1, attempt_history=None,
                 starting_testbench=None):
        self.calls.append({"rtl": rtl, "skills": dict(skills or {}), "previous": previous_testbench,
                           "lint_issues": list(lint_issues or []), "attempt": attempt,
                           "attempt_history": list(attempt_history or []), "starting": starting_testbench})
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
    assert out["requirement_id"] is None and out["attempt"] == 1            # called on its own: no orchestrator note


EARLIER = [{"attempt": 1, "status": "COMPLETE", "error": None, "issues": None, "likely_cause": None,
            "downstream": [{"stage": "simulate", "verdict": "fail", "failures": ["cannot open test_vectors_out.csv"],
                            "summary": "wrong file name", "suggested_fix": "open test_vectors_out.csv"}]}]


def test_the_agent_knows_which_orchestrator_call_it_is_and_tells_the_generator():
    agent = make_agent([GOOD_TB], [CLEAN])
    out = agent.run(SPEC, RTL, requirement_id="counter_T-11.35_D-16.10.2026", attempt=2, attempt_history=EARLIER)
    assert out["requirement_id"] == "counter_T-11.35_D-16.10.2026" and out["attempt"] == 2
    assert agent.tools.generator.calls[0]["attempt"] == 2                   # the writer is told it is the 2nd call
    assert agent.tools.generator.calls[0]["attempt_history"] == EARLIER      # and how the 1st one went
    first_call = make_agent([GOOD_TB], [CLEAN])
    first_call.run(SPEC, RTL)                                               # called on its own: 1st call, no history
    assert first_call.tools.generator.calls[0]["attempt"] == 1
    assert first_call.tools.generator.calls[0]["attempt_history"] == []


def test_on_a_later_attempt_the_generator_prompt_carries_the_orchestrator_feedback():
    class Spy(FakeListChatModel):
        seen: list = []

        def _call(self, messages, *args, **kwargs):
            Spy.seen.append(messages)
            return super()._call(messages, *args, **kwargs)

    Spy.seen = []
    gen = TestbenchGenerator(llm=Spy(responses=['{"entity_name": "counter4_tb", "code": "x"}'] * 2))
    gen.generate(SPEC, RTL)                                                 # 1st call: prompt unchanged
    gen.generate(SPEC, RTL, attempt=2, attempt_history=EARLIER)             # 2nd call: told what went wrong before
    first, second = Spy.seen
    assert "attempt 2" not in first[0].content and "attempt_history" not in first[1].content
    assert "This is attempt 2 for this requirement" in second[0].content and '"downstream"' in second[0].content
    assert "cannot open test_vectors_out.csv" in second[1].content and '"rtl"' in second[1].content


def test_a_retry_is_handed_the_previous_testbench_to_revise_not_rewrite():
    """2026-11-06: a blamed testbench was rewritten from zero on every retry,
    so parts that were already right (file names, DUT wiring, reset) could
    come back wrong. Like the RTL agent, the testbench agent now receives
    the testbench its previous call delivered and is told to edit it."""
    import json

    class Spy(FakeListChatModel):
        seen: list = []

        def _call(self, messages, *args, **kwargs):
            Spy.seen.append(messages)
            return super()._call(messages, *args, **kwargs)

    Spy.seen = []
    gen = TestbenchGenerator(llm=Spy(responses=['{"entity_name": "counter4_tb", "code": "x"}'] * 3))
    gen.generate(SPEC, RTL, attempt=2, attempt_history=EARLIER, starting_testbench=GOOD_TB)
    system, user = Spy.seen[0]
    assert "REVISE that code" in system.content                              # told to edit, not rewrite
    assert json.loads(user.content)["previous_code"] == GOOD_TB["code"]      # and handed the code to edit
    gen.generate(SPEC, RTL, attempt=2, attempt_history=EARLIER, starting_testbench=GOOD_TB,
                 previous_testbench=BAD_TB, lint_issues=DIRTY["issues"])     # this run's own lint fix wins
    system, user = Spy.seen[1]
    assert "REVISE that code" not in system.content and "lint_issues" in user.content
    assert json.loads(user.content)["previous_code"] == BAD_TB["code"]
    gen.generate(SPEC, RTL, attempt=1, starting_testbench=GOOD_TB)           # 1st call: nothing to revise
    assert "previous_code" not in Spy.seen[2][1].content

    # the agent passes the orchestrator's testbench through to the generator on a retry
    agent = make_agent([GOOD_TB], [CLEAN])
    agent.run(SPEC, RTL, attempt=2, attempt_history=EARLIER, previous_testbench=GOOD_TB)
    assert agent.tools.generator.calls[0]["starting"] == GOOD_TB
    assert agent.tools.generator.calls[0]["previous"] is None               # not seeded as this run's testbench
    assert make_agent([GOOD_TB], [CLEAN]).run(SPEC, RTL).__class__ is dict  # the old call shape still works


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
    from agents.testbench_agent.lint.testbench_linter import _static_checks
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
    from agents.testbench_agent.lint.testbench_linter import _static_checks
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
    from agents.testbench_agent.lint.testbench_linter import _static_checks
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


def test_read_wide_with_a_fixed_width_or_a_wrongly_sized_vector_variable_is_caught():
    """Run 17:04 on 2026-10-29: 'Array sizes do not match, left array has 24
    elements, right array has 32 elements' at time 0 inside read_wide."""
    from agents.testbench_agent.lint.testbench_linter import _replay_checks
    spec = {"entity_name": "demap", "ports": [
        {"name": "aclk", "direction": "input", "width": "1"},
        {"name": "s_axis_tdata", "direction": "input", "width": "32"},
        {"name": "m_axis_tvalid", "direction": "output", "width": "1"},
        {"name": "m_axis_tdata", "direction": "output", "width": "24"}]}
    bad = """
      variable v_cycle, v_cycle_out, v_aclk, v_m_axis_tvalid : integer;
      variable v_s_axis_tdata : std_logic_vector(31 downto 0);
      variable v_m_axis_tdata : std_logic_vector(31 downto 0);     -- port is 24 bits
      procedure read_wide(l : inout line; v : out std_logic_vector) is
        variable acc : unsigned(31 downto 0) := (others => '0');  -- fixed width
      begin
        v := std_logic_vector(acc);
      end procedure;
      read(li, v_cycle); read(li, v_aclk); read_wide(li, v_s_axis_tdata);
      read(lo, v_cycle_out); read(lo, v_m_axis_tvalid); read_wide(lo, v_m_axis_tdata);
    """
    issues = _replay_checks(spec, bad)
    assert [i["code"] for i in issues] == ["read_wide_fixed_width", "vector_variable_width"]
    assert "(31 downto 0)" in issues[0]["detail"] and "v_m_axis_tdata" in issues[1]["detail"] and "23 downto 0" in issues[1]["detail"]

    good = bad.replace("variable v_m_axis_tdata : std_logic_vector(31 downto 0)", "variable v_m_axis_tdata : std_logic_vector(23 downto 0)") \
              .replace("unsigned(31 downto 0)", "unsigned(v'length - 1 downto 0)")
    assert _replay_checks(spec, good) == []


def test_replay_mistakes_that_compile_but_break_the_simulation_are_caught():
    from agents.testbench_agent.lint.testbench_linter import _replay_checks
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


def test_reading_the_outputs_file_in_two_passes_is_not_a_column_count_mismatch():
    """The LLR demapper run: the testbench reads test_vectors_out.csv twice (collect
    expected beats, then replay). 4 + 4 reads of `lo` are two whole rows, not 8 columns."""
    from agents.testbench_agent.lint.testbench_linter import _replay_checks
    spec = {"entity_name": "llr", "ports": [
        {"name": "aclk", "direction": "input", "width": "1"},
        {"name": "aresetn", "direction": "input", "width": "1"},
        {"name": "s_axis_tvalid", "direction": "input", "width": "1"},
        {"name": "s_axis_tready", "direction": "output", "width": "1"},
        {"name": "s_axis_tdata", "direction": "input", "width": "32"},
        {"name": "m_axis_tvalid", "direction": "output", "width": "1"},
        {"name": "m_axis_tready", "direction": "input", "width": "1"},
        {"name": "m_axis_tdata", "direction": "output", "width": "24"}]}
    two_passes = """
      read(lo, v_cycle); read(lo, v_s_axis_tready); read(lo, v_m_axis_tvalid); read_wide(lo, v_m_axis_tdata);
      read(li, v_cycle); read(li, v_aclk); read(li, v_aresetn); read(li, v_s_axis_tvalid);
      read_wide(li, v_s_axis_tdata); read(li, v_m_axis_tready);
      read(lo, v_cycle); read(lo, v_s_axis_tready); read(lo, v_m_axis_tvalid); read_wide(lo, v_m_axis_tdata);
    """
    assert _replay_checks(spec, two_passes) == []
    # a column really skipped in the second pass (7 reads of `lo`) is still caught
    skipped = two_passes.replace("read(lo, v_s_axis_tready); read(lo, v_m_axis_tvalid); read_wide(lo, v_m_axis_tdata);\n    ",
                                 "read(lo, v_m_axis_tvalid); read_wide(lo, v_m_axis_tdata);\n    ", 1)
    assert {i["code"] for i in _replay_checks(spec, skipped)} == {"column_count_mismatch"}


def test_right_number_of_reads_in_the_wrong_column_order_is_caught():
    """The LLR demapper run: s_axis_tready sounds like an input, so the testbench
    read it from the inputs file. Six reads per row (correct count), but the 32-bit
    tdata landed in the integer meant for tready -> 'integer value is out of range'."""
    from agents.testbench_agent.lint.testbench_linter import _replay_checks
    spec = {"entity_name": "llr", "ports": [
        {"name": "aclk", "direction": "input", "width": "1"},
        {"name": "aresetn", "direction": "input", "width": "1"},
        {"name": "s_axis_tvalid", "direction": "input", "width": "1"},
        {"name": "s_axis_tready", "direction": "output", "width": "1"},
        {"name": "s_axis_tdata", "direction": "input", "width": "32"},
        {"name": "m_axis_tvalid", "direction": "output", "width": "1"},
        {"name": "m_axis_tready", "direction": "input", "width": "1"},
        {"name": "m_axis_tdata", "direction": "output", "width": "24"}]}
    swapped = """
      read(li, v_cycle); read(li, v_aclk); read(li, v_aresetn); read(li, v_s_axis_tvalid);
      read(li, v_s_axis_tready); read_wide(li, v_s_axis_tdata);
      read(lo, v_cycle_out); read(lo, v_m_axis_tvalid); read(lo, v_m_axis_tready); read_wide(lo, v_m_axis_tdata);
    """
    issues = [i for i in _replay_checks(spec, swapped) if i["code"] == "column_order_mismatch"]
    assert len(issues) == 2                               # both files are read in the wrong order
    assert "s_axis_tready is an OUTPUT" in issues[0]["detail"]

    right = """
      read(li, v_cycle); read(li, v_aclk); read(li, v_aresetn); read(li, v_s_axis_tvalid);
      read_wide(li, v_s_axis_tdata); read(li, v_m_axis_tready);
      read(lo, v_cycle_out); read(lo, v_s_axis_tready); read(lo, v_m_axis_tvalid); read_wide(lo, v_m_axis_tdata);
    """
    assert _replay_checks(spec, right) == []


def test_std_logic_val_on_a_csv_value_is_caught():
    from agents.testbench_agent.lint.testbench_linter import _static_checks
    code = "entity counter4_tb is end; architecture a of counter4_tb is begin process begin wait; " \
           "if detected /= std_logic'val(v_detected) then report \"x\"; end if; end process; end;"
    found = [i for i in _static_checks(SPEC, RTL, {"entity_name": "counter4_tb", "code": code})
             if i["code"] == "std_logic_val"]
    assert len(found) == 1 and "'U' for 0" in found[0]["detail"]
    ok = code.replace("detected /= std_logic'val(v_detected)", "(detected = '1') /= (v_detected = 1)")
    assert not [i for i in _static_checks(SPEC, RTL, {"entity_name": "counter4_tb", "code": ok})
                if i["code"] == "std_logic_val"]


def test_treating_a_one_bit_port_as_a_bus_is_caught():
    from agents.testbench_agent.lint.testbench_linter import _static_checks
    spec = {**SPEC, "ports": [{"name": "clk", "width": "1"}, {"name": "detected", "width": "1"},
                              {"name": "count", "width": "4"}]}
    code = "entity counter4_tb is end; architecture a of counter4_tb is begin process begin wait; " \
           "report integer'image(to_integer(unsigned(detected))); " \
           "report integer'image(to_integer(unsigned(count))); end process; end;"
    found = [i for i in _static_checks(spec, RTL, {"entity_name": "counter4_tb", "code": code})
             if i["code"] == "scalar_to_unsigned"]
    assert len(found) == 1 and "'detected'" in found[0]["detail"]      # the 4-bit count is fine


def test_agent_only_has_restricted_actions():
    assert {a.value for a in Action} == {"load_skills", "generate_testbench", "lint_testbench", "finalize_testbench"}
    nodes = set(make_agent([GOOD_TB], [CLEAN]).graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"decide", "load_skills", "generate_testbench", "lint_testbench", "finalize_testbench"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_streaming_designs_must_be_compared_in_beat_order_not_per_cycle():
    """The LLR demapper runs: the vectors put the expected output on the row of
    the input beat, the testbench compared on that same cycle, and the pipeline
    answered later -> 'cycle 2: m_axis_tdata expected 24_50_141 got 0_0_0'."""
    from agents.testbench_agent.lint.testbench_linter import _streaming_checks
    stream = {"entity_name": "llr", "ports": [{"name": n, "direction": d, "width": w} for n, d, w in [
        ("aclk", "input", "1"), ("aresetn", "input", "1"),
        ("s_axis_tvalid", "input", "1"), ("s_axis_tready", "output", "1"), ("s_axis_tdata", "input", "32"),
        ("m_axis_tvalid", "output", "1"), ("m_axis_tready", "input", "1"), ("m_axis_tdata", "output", "24")]]}
    per_cycle = """
      wait until rising_edge(aclk); wait for 1 ns;
      -- compare outputs only when output is valid
      if v_m_axis_tvalid = 1 then
        if m_axis_tdata /= v_m_axis_tdata then report "cycle mismatch" severity error; end if;
      end if;
    """
    codes = [i["code"] for i in _streaming_checks(stream, per_cycle)]
    assert codes == ["streaming_cycle_compare", "streaming_no_handshake_compare"]

    beat_order = """
      if v_m_axis_tvalid = 1 then exp_beats(n_exp) := v_m_axis_tdata; n_exp := n_exp + 1; end if;
      ...
      wait until rising_edge(aclk); wait for 1 ns;
      if m_axis_tvalid = '1' and m_axis_tready = '1' then
        if m_axis_tdata /= exp_beats(k_exp) then report "beat mismatch" severity error; end if;
        k_exp := k_exp + 1;
      end if;
    """
    assert _streaming_checks(stream, beat_order) == []

    # the rule file's sketch: sample the wires AT the edge (before `wait for
    # 1 ns`) into dut_* copies and compare those -- the checker accepts it
    sampled_at_edge = """
      if v_m_axis_tvalid = 1 then exp_beats(n_exp) := v_m_axis_tdata; n_exp := n_exp + 1; end if;
      ...
      wait until rising_edge(aclk);
      dut_m_axis_tvalid := m_axis_tvalid; dut_m_axis_tready := m_axis_tready; dut_m_axis_tdata := m_axis_tdata;
      wait for 1 ns;
      if dut_m_axis_tvalid = '1' and dut_m_axis_tready = '1' then
        if dut_m_axis_tdata /= exp_beats(k_exp) then report "beat mismatch" severity error; end if;
        k_exp := k_exp + 1;
      end if;
    """
    assert _streaming_checks(stream, sampled_at_edge) == []
    # the same guard with each term in parentheses is the same handshake
    assert _streaming_checks(stream, sampled_at_edge.replace(
        "if dut_m_axis_tvalid = '1' and dut_m_axis_tready = '1' then",
        "if (dut_m_axis_tvalid = '1') and (dut_m_axis_tready = '1') then")) == []

    # a plain counter has no handshake ports: nothing to enforce
    assert _streaming_checks(SPEC, per_cycle) == []


def test_streaming_compare_against_the_current_row_is_caught_even_inside_the_handshake():
    """llr_demap16 run of 2026-10-29 (4 attempts, RTL blamed every time): the
    compare sat inside the DUT's handshake, so the old rules were satisfied,
    but it compared m_axis_tdata with v_m_axis_tdata of the CURRENT row -> the
    output was checked against the wrong beat ('cycle 17: expected hi=125
    lo=61440 got hi=0 lo=0', then 'cycle 20: ... got hi=125 lo=61440')."""
    from agents.testbench_agent.lint.testbench_linter import _streaming_checks
    stream = {"entity_name": "llr", "ports": [{"name": n, "direction": d, "width": w} for n, d, w in [
        ("aclk", "input", "1"), ("aresetn", "input", "1"),
        ("s_axis_tvalid", "input", "1"), ("s_axis_tready", "output", "1"), ("s_axis_tdata", "input", "32"),
        ("m_axis_tvalid", "output", "1"), ("m_axis_tready", "input", "1"), ("m_axis_tdata", "output", "24")]]}
    row_compare = """
      read(lo, v_cycle_out); read(lo, v_s_axis_tready); read(lo, v_m_axis_tvalid); read_wide(lo, v_m_axis_tdata);
      wait until rising_edge(aclk); wait for 1 ns;
      if m_axis_tvalid = '1' and m_axis_tready = '1' then
        if m_axis_tdata /= v_m_axis_tdata then report "mismatch" severity error; end if;
      end if;
    """
    issues = _streaming_checks(stream, row_compare)
    assert [i["code"] for i in issues] == ["streaming_row_compare"]
    assert "v_m_axis_tdata" in issues[0]["detail"] and "exp_beats(k_exp)" in issues[0]["detail"]

    # storing the row value into the expected-beat array is the right pattern, not a compare
    beat_order = """
      read(lo, v_cycle_out); read(lo, v_s_axis_tready); read(lo, v_m_axis_tvalid); read_wide(lo, v_m_axis_tdata);
      if v_m_axis_tvalid = 1 then exp_beats(n_exp) := v_m_axis_tdata; n_exp := n_exp + 1; end if;
      wait until rising_edge(aclk); wait for 1 ns;
      if m_axis_tvalid = '1' and m_axis_tready = '1' then
        if m_axis_tdata /= exp_beats(k_exp) then report "beat mismatch" severity error; end if;
      end if;
    """
    assert _streaming_checks(stream, beat_order) == []


def test_a_hold_loop_that_an_idle_row_cannot_leave_is_rejected():
    """llr_demap16, 2026-10-29 16:40: the hold loop exited only on
    `tvalid = 1 and tready = '1'`, so every idle row (cycles 0-1, 50-59) ran
    the 64-edge limit and reported a deadlock; the analyst blamed the RTL."""
    from agents.testbench_agent.lint.testbench_linter import _idle_row_hold_issue, _streaming_checks
    stream = {"entity_name": "llr", "ports": [{"name": n, "direction": d, "width": w} for n, d, w in [
        ("aclk", "input", "1"), ("aresetn", "input", "1"),
        ("s_axis_tvalid", "input", "1"), ("s_axis_tready", "output", "1"), ("s_axis_tdata", "input", "32"),
        ("m_axis_tvalid", "output", "1"), ("m_axis_tready", "input", "1"), ("m_axis_tdata", "output", "24")]]}
    wrong = """
        loop
          wait until rising_edge(aclk);
          dut_s_axis_tready := s_axis_tready;
          if dut_m_axis_tvalid = '1' and dut_m_axis_tready = '1' then k_exp := k_exp + 1; end if;
          if cur_s_axis_tvalid = 1 and dut_s_axis_tready = '1' then
            exit;
          end if;
          stall_count := stall_count + 1;
          if stall_count > 64 then
            report "cycle " & integer'image(v_cycle) & ": s_axis_tready never rose (deadlock)" severity error;
            exit;
          end if;
        end loop;
    """
    assert "hold_on_idle_row" in [i["code"] for i in _streaming_checks(stream, wrong)]
    right = wrong.replace("if cur_s_axis_tvalid = 1 and dut_s_axis_tready = '1' then",
                          "if cur_s_axis_tvalid = 0 or dut_s_axis_tready = '1' then")
    assert "hold_on_idle_row" not in [i["code"] for i in _streaming_checks(stream, right)]
    # the other correct shape: only beat rows enter the hold loop at all
    gated = "if v_s_axis_tvalid = 1 then\n  loop\n ... never rose ...\n  end loop;\nelse wait until rising_edge(aclk); end if;"
    assert _idle_row_hold_issue(gated.splitlines()) is None
    assert _idle_row_hold_issue(["wait until rising_edge(aclk);"]) is None       # no hold loop, nothing to check


def test_streaming_rules_apply_without_an_m_axis_tready_port_and_flag_compares_are_caught():
    """timing_recovery, 2026-10-30: the spec has s_axis_tvalid/m_axis_tvalid but
    no m_axis_tready ("downstream is always ready"), the streaming checks never
    ran, and two cycle-by-cycle testbenches went through. The first compared
    s_axis_tready and m_axis_tvalid on every row (1200 error lines, log cut,
    marker lost); the second reported 'missing m_axis_tvalid' on every expected
    row and the RTL's strobe timing was blamed, although the spec leaves the
    latency L to the RTL."""
    from agents.testbench_agent.lint.testbench_linter import _streaming_checks
    stream = {"entity_name": "timing_recovery", "ports": [{"name": n, "direction": d, "width": w} for n, d, w in [
        ("aclk", "input", "1"), ("aresetn", "input", "1"),
        ("s_axis_tvalid", "input", "1"), ("s_axis_tready", "output", "1"), ("s_axis_tdata", "input", "24"),
        ("m_axis_tvalid", "output", "1"), ("m_axis_tdata", "output", "24"), ("m_axis_tuser", "output", "8")]]}
    reads = """
      read(lo, v_cycle_out); read(lo, v_s_axis_tready); read(lo, v_m_axis_tvalid); read_wide(lo, v_m_axis_tdata); read(lo, v_m_axis_tuser);
    """
    # attempt 1: cycle-exact compare of every output column, flags included
    cycle_exact = reads + """
      wait until rising_edge(aclk); wait for 1 ns;
      if (s_axis_tready = '1') /= (v_s_axis_tready = 1) then
        report "cycle " & integer'image(v_cycle) & ": s_axis_tready expected " & integer'image(v_s_axis_tready) severity error;
      end if;
      if (m_axis_tvalid = '1') /= (v_m_axis_tvalid = 1) then
        report "cycle " & integer'image(v_cycle) & ": m_axis_tvalid expected " & integer'image(v_m_axis_tvalid) severity error;
      end if;
      if m_axis_tdata /= v_m_axis_tdata then report "mismatch" severity error; end if;
    """
    codes = [i["code"] for i in _streaming_checks(stream, cycle_exact)]
    assert codes == ["streaming_row_compare", "streaming_no_handshake_compare", "streaming_flag_cycle_compare"]

    # attempt 2: hold loop and dut_ copies are there, but valid is still checked row by row
    hybrid = reads + """
      loop
        wait until rising_edge(aclk);
        dut_s_axis_tready := s_axis_tready; dut_m_axis_tvalid := m_axis_tvalid;
        dut_m_axis_tdata := m_axis_tdata; dut_m_axis_tuser := m_axis_tuser;
        wait for 1 ns;
        if cur_s_axis_tvalid = 0 or dut_s_axis_tready = '1' then exit; end if;
        wait_count := wait_count + 1;
        if wait_count >= 64 then report "cycle " & integer'image(cur_cycle) & ": s_axis_tready never rose (deadlock)" severity error; exit; end if;
      end loop;
      if dut_m_axis_tvalid = '1' then
        if to_integer(unsigned(dut_m_axis_tuser)) /= to_integer(unsigned(v_m_axis_tuser)) then
          report "cycle " & integer'image(v_cycle) & ": m_axis_tuser expected " & integer'image(to_integer(unsigned(v_m_axis_tuser))) severity error;
        end if;
      end if;
      if dut_m_axis_tvalid = '1' and v_m_axis_tvalid = 0 then
        report "cycle " & integer'image(v_cycle) & ": unexpected m_axis_tvalid" severity error;
      end if;
      if dut_m_axis_tvalid = '0' and v_m_axis_tvalid = 1 then
        report "cycle " & integer'image(v_cycle) & ": missing m_axis_tvalid" severity error;
      end if;
    """
    issues = _streaming_checks(stream, hybrid)
    assert [i["code"] for i in issues] == ["streaming_row_compare", "streaming_flag_cycle_compare"]
    assert "v_m_axis_tuser" in issues[0]["detail"]                       # any payload column, not only m_axis_tdata
    assert "no m_axis_tready port" in issues[0]["detail"]                 # the rule text knows the port is missing
    assert "v_m_axis_tvalid = 0" in issues[1]["detail"] and "unexpected m_axis_tvalid" in issues[1]["detail"]

    # the right shape for this design: beats in order, consumed on every edge with the DUT's valid high
    beat_order = reads + """
      if v_m_axis_tvalid = 1 then exp_tdata(n_exp) := v_m_axis_tdata; exp_tuser(n_exp) := v_m_axis_tuser; n_exp := n_exp + 1; end if;
      wait until rising_edge(aclk);
      dut_m_axis_tvalid := m_axis_tvalid; dut_m_axis_tdata := m_axis_tdata; dut_m_axis_tuser := m_axis_tuser;
      wait for 1 ns;
      if dut_m_axis_tvalid = '1' then
        if k_exp >= n_exp then report "unexpected output beat" severity error;
        elsif dut_m_axis_tdata /= exp_tdata(k_exp) or dut_m_axis_tuser /= exp_tuser(k_exp) then
          report "beat " & integer'image(k_exp) & ": m_axis_tdata expected " & integer'image(to_integer(unsigned(exp_tdata(k_exp)))) & " got " & integer'image(to_integer(unsigned(dut_m_axis_tdata))) severity error;
        end if;
        k_exp := k_exp + 1;
      end if;
      if k_exp < n_exp then report "missing output beats: expected " & integer'image(n_exp) & " got " & integer'image(k_exp) severity error; end if;
    """
    assert _streaming_checks(stream, beat_order) == []

    # correct shapes the checks must not mistake for compares (adversarial review, 2026-10-30):
    # a grouped declaration with an aggregate default: the '=' of '=>' is not a compare
    grouped_decl = "    variable v_m_axis_tdata, dut_m_axis_tdata : std_logic_vector(23 downto 0) := (others => '0');\n"
    assert _streaming_checks(stream, grouped_decl + beat_order) == []
    # a width attribute on the wire is a size, not the wire's value (the rule file's own to_unsigned(v, port'length))
    sized = beat_order.replace("exp_tuser(n_exp) := v_m_axis_tuser;",
                               "exp_tuser(n_exp) := std_logic_vector(to_unsigned(v_m_axis_tuser, m_axis_tuser'length));")
    assert _streaming_checks(stream, sized) == []
    ranged = "  variable v_m_axis_tdata : std_logic_vector(m_axis_tdata'range) := (others => '0');\n" + beat_order
    assert _streaming_checks(stream, ranged) == []
    # ... but the wire's VALUE next to that attribute is still a row compare
    still_bad = beat_order.replace("dut_m_axis_tuser /= exp_tuser(k_exp)",
                                   "dut_m_axis_tuser /= std_logic_vector(to_unsigned(v_m_axis_tuser, m_axis_tuser'length))")
    assert "streaming_row_compare" in [i["code"] for i in _streaming_checks(stream, still_bad)]
    # the guard with the flag as the second operand is the same guard
    for swapped in ("if k_exp < n_exp and dut_m_axis_tvalid = '1' then", "if cur_aresetn = 1 and dut_m_axis_tvalid = '1' then"):
        assert _streaming_checks(stream, beat_order.replace("if dut_m_axis_tvalid = '1' then", swapped)) == []
    # legitimate beat-order wording that merely mentions the flag is not a flag compare
    worded = beat_order.replace('report "unexpected output beat"',
                                'report "unexpected output beat (m_axis_tvalid high with nothing left to expect)"')
    assert _streaming_checks(stream, worded) == []

    # a wire the spec does not have gets a clear message instead of a compile error ...
    with_tready = beat_order.replace("if dut_m_axis_tvalid = '1' then", "if dut_m_axis_tvalid = '1' and m_axis_tready = '1' then")
    assert [i["code"] for i in _streaming_checks(stream, with_tready)] == ["no_m_axis_tready_port"]
    # ... and so does a dut_m_axis_tready copy that nothing could ever drive
    stray_var = "    variable dut_m_axis_tvalid, dut_m_axis_tready : std_logic;\n" + beat_order
    assert [i["code"] for i in _streaming_checks(stream, stray_var)] == ["no_m_axis_tready_port"]

    # the LLR shape (all four handshake wires) is unchanged: the full handshake is still required
    from prompts.testbench_agent_prompt import TESTBENCH_AGENT_SYSTEM_PROMPT
    assert "no m_axis_tready" in TESTBENCH_AGENT_SYSTEM_PROMPT and "FIRST 20" in TESTBENCH_AGENT_SYSTEM_PROMPT


def test_expected_field_read_from_the_wrong_column_is_caught():
    """timing_recovery, 2026-11-11: the testbench decoded got_q from
    m_axis_tdata(11 downto 0) but exp_q from exp_tuser (the mu column), so
    Q mismatched on every compared beat whatever the RTL did."""
    from pathlib import Path
    from agents.testbench_agent.lint.testbench_linter import _field_source_mismatch
    payload = ["m_axis_tdata", "m_axis_tuser"]
    good = """
          got_i  := to_integer(signed(dut_m_axis_tdata(23 downto 12)));
          exp_i  := to_integer(signed(exp_tdata(k_exp)(23 downto 12)));
          got_q  := to_integer(signed(dut_m_axis_tdata(11 downto 0)));
          exp_q  := to_integer(signed(exp_tdata(k_exp)(11 downto 0)));
          got_mu := to_integer(unsigned(dut_m_axis_tuser));
          exp_mu := to_integer(unsigned(exp_tuser(k_exp)));
    """
    decls = "    variable got_i, exp_i, got_q, exp_q : integer := 0;\n    variable exp_tdata : tdata_array;\n"
    assert _field_source_mismatch((decls + good).splitlines(), payload) is None

    wrong_column = good.replace("exp_tdata(k_exp)(11 downto 0)", "exp_tuser(k_exp)(7 downto 0)")
    issue = _field_source_mismatch(wrong_column.splitlines(), payload)
    assert issue["code"] == "field_source_mismatch"
    assert "exp_q" in issue["detail"] and "exp_tdata(k_exp)(11 downto 0)" in issue["detail"]   # names the fix

    wrong_bits = good.replace("exp_tdata(k_exp)(11 downto 0)", "exp_tdata(k_exp)(23 downto 12)")
    assert _field_source_mismatch(wrong_bits.splitlines(), payload)["code"] == "field_source_mismatch"

    rules = (Path(__file__).resolve().parents[1] / "skills" / "TESTBENCH" / "vhdl_2008_testbench_rules.md").read_text(encoding="utf-8")
    assert "field_source_mismatch" in rules and "exp_tdata(k_exp)(11 downto 0)" in rules


def _ports(rows):
    return [{"name": n, "direction": d, "width": w} for n, d, w in rows]


TIMING_RECOVERY_SPEC = {"entity_name": "timing_recovery", "ports": _ports([
    ("aclk", "input", "1"), ("aresetn", "input", "1"),
    ("s_axis_tvalid", "input", "1"), ("s_axis_tready", "output", "1"), ("s_axis_tdata", "input", "24"),
    ("m_axis_tvalid", "output", "1"), ("m_axis_tdata", "output", "24"), ("m_axis_tuser", "output", "8")])}
LLR_SPEC = {"entity_name": "llr_demap16", "ports": _ports([
    ("aclk", "input", "1"), ("aresetn", "input", "1"),
    ("s_axis_tvalid", "input", "1"), ("s_axis_tready", "output", "1"), ("s_axis_tdata", "input", "32"),
    ("m_axis_tvalid", "output", "1"), ("m_axis_tready", "input", "1"), ("m_axis_tdata", "output", "24")])}


def test_a_streaming_design_is_named_as_one_before_the_first_draft():
    """timing_recovery, 2026-10-30 to 2026-11-03: four first drafts in a row
    copied the counter pattern (every output and both flags compared with the
    current row), so every run opened with streaming_row_compare,
    streaming_no_handshake_compare and streaming_flag_cycle_compare. The
    writer is now told up front, with this design's own columns and wires."""
    from agents.testbench_agent.testbench_generator import streaming_hint
    hint = streaming_hint(TIMING_RECOVERY_SPEC)
    assert "THIS DESIGN IS A STREAMING DESIGN" in hint and "COMPLETE STREAMING TESTBENCH" in hint
    assert "exp_tdata for m_axis_tdata, exp_tuser for m_axis_tuser" in hint     # its own payload columns
    assert "NO m_axis_tready port" in hint and "HAS an m_axis_tready" not in hint
    assert "NO s_axis_tready" not in hint
    llr = streaming_hint(LLR_SPEC)
    assert "HAS an m_axis_tready port" in llr and "exp_tdata for m_axis_tdata." in llr
    no_s_tready = {"ports": [p for p in LLR_SPEC["ports"] if p["name"] != "s_axis_tready"]}
    assert "NO s_axis_tready port" in streaming_hint(no_s_tready)
    assert streaming_hint(SPEC) == ""                                            # a counter gets no hint
    assert "{" not in hint and "{" not in llr                                    # every placeholder filled

    # the hint is the last thing in the system prompt, on the first draft and on a fix
    class Spy(FakeListChatModel):
        seen: list = []

        def _call(self, messages, *args, **kwargs):
            Spy.seen.append(messages)
            return super()._call(messages, *args, **kwargs)

    Spy.seen = []
    gen = TestbenchGenerator(llm=Spy(responses=['{"entity_name": "timing_recovery_tb", "code": "x"}'] * 3))
    rtl = {"entity_name": "timing_recovery", "code": "entity timing_recovery is end;"}
    gen.generate(TIMING_RECOVERY_SPEC, rtl, SKILLS)
    gen.generate(TIMING_RECOVERY_SPEC, rtl, SKILLS, previous_testbench={"code": "x"},
                 lint_issues=[{"code": "streaming_row_compare", "detail": "d"}])
    gen.generate(SPEC, RTL, SKILLS)
    first, fix, counter = (call[0].content for call in Spy.seen)
    assert first.endswith(hint) and fix.endswith(hint) and "You are now FIXING" in fix
    assert "STREAMING DESIGN" not in counter.split("TESTBENCH RULES (SKILLS)")[-1]


def test_the_rules_file_has_a_complete_streaming_testbench_the_checker_accepts():
    """The rules file used to show a complete counter testbench but only a
    gappy streaming sketch (lines of '...'), and the model copied the
    complete one. The streaming example is now a whole file: copied with
    only the names changed it must pass every static check, and the change
    lists the hint points to must exist under it. (It was also simulated on
    2026-11-03: SIMULATION PASSED on llr_demap16 with the m_axis_tready
    changes applied.)"""
    import re
    from pathlib import Path
    from agents.testbench_agent.lint.testbench_linter import _static_checks
    rules = (Path(__file__).resolve().parents[1] / "skills" / "TESTBENCH" / "vhdl_2008_testbench_rules.md").read_text(encoding="utf-8")
    section = rules.split("COMPLETE STREAMING TESTBENCH. Copy this file", 1)[1].split("WITH AN m_axis_tready PORT", 1)[0]
    code_lines = [line[4:] if line.startswith("    ") else line for line in section.splitlines()]
    start = next(i for i, line in enumerate(code_lines) if line.startswith("library ieee;"))
    end = next(i for i, line in enumerate(code_lines) if line.startswith("end architecture"))
    example = "\n".join(code_lines[start:end + 1])
    assert not re.search(r"^\s*\.\.\.\s*$", example, re.MULTILINE)                # no gaps left to fill
    copied = example.replace("stream_core", "timing_recovery")
    rtl = {"entity_name": "timing_recovery", "code": "entity timing_recovery is end;"}
    assert _static_checks(TIMING_RECOVERY_SPEC, rtl, {"entity_name": "timing_recovery_tb", "code": copied}) == []
    for heading in ("WITH AN m_axis_tready PORT", "WITHOUT AN s_axis_tready PORT"):
        assert heading in rules                                                  # the hint's pointers lead somewhere
    assert "the COMPLETE STREAMING TESTBENCH under STREAMING DESIGNS instead" in rules   # the counter pattern says so


def test_the_rules_tell_closed_loop_designs_to_compare_with_a_tolerance():
    """timing_recovery, 2026-11-05 (run 3106ab33): the golden model runs the
    Gardner/NCO/Farrow loop in exact double arithmetic, the RTL rounds, beat 1
    differed by one step (I=-46/Q=-47 vs -47/-46) and the loop drifted from
    there; 207 then 181 bit-exact mismatches, the RTL blamed three times. The
    rules now say such designs compare each field with a tolerance."""
    from pathlib import Path
    rules = (Path(__file__).resolve().parents[1] / "skills" / "TESTBENCH" / "vhdl_2008_testbench_rules.md").read_text(encoding="utf-8")
    section = rules.split("CLOSED-LOOP DESIGNS: COMPARE WITH A TOLERANCE", 1)[1].split("COMPLETE STREAMING TESTBENCH.", 1)[0]
    assert "TOL_SYM" in section and "TOL_MU" in section and "abs(got_i - exp_i)" in section
    assert "4096" in section                                   # per field, never the packed word
    assert "2 %" in section                                    # the beat count may differ by a few
    assert "TED, NCO, loop filter" in section                  # how the writer recognises such a design
    assert "TOL_SYM / TOL_MU" in rules.split("CHECK YOURSELF BEFORE ANSWERING", 1)[1]   # and the checklist repeats it


# ── closed-loop designs: tolerance + settle window (closed_loop_compare.py) ──

def test_a_closed_loop_testbench_that_compares_bit_for_bit_is_sent_back():
    """timing_recovery 2026-11-11 15:04: the RTL was the proven worked example
    and the test vectors were right (0 mismatches under a tolerant testbench);
    the first-draft testbench compared the packed word bit for bit from beat 1,
    reported 256 mismatches and the RTL was blamed. The rule was only prose."""
    from agents.testbench_agent.lint.closed_loop_compare import closed_loop_compare_issues, closed_loop_hint, is_closed_loop
    ports = [{"name": n, "direction": d} for n, d in (("aclk", "input"), ("s_axis_tvalid", "input"),
             ("s_axis_tdata", "input"), ("m_axis_tvalid", "output"), ("m_axis_tdata", "output"))]
    loop = {"entity_name": "timing_recovery", "ports": ports,
            "behavior": ["Implement NCO phase eta (modulo-1).", "Update PI loop filter: acc = acc + K2 * e."]}
    plain = {"entity_name": "llr_demap16", "ports": ports, "behavior": ["Compute the LLR of each bit."]}
    strict = """
      if dut_m_axis_tvalid = '1' then
        if dut_m_axis_tdata /= exp_tdata(k_exp) then   -- abs(got - exp) > TOL and SETTLE_BEATS only in a comment
          report "beat mismatch" severity error;
        end if;
        k_exp := k_exp + 1;
      end if;"""
    tolerant = """
      constant SETTLE_BEATS : integer := 64;
      elsif k_exp >= SETTLE_BEATS then
        if abs(got_i - exp_i) > TOL_SYM or abs(got_q - exp_q) > TOL_SYM then
          report "beat mismatch" severity error;
        end if;"""
    assert is_closed_loop(loop) and not is_closed_loop(plain)
    issues = closed_loop_compare_issues(loop, strict)
    assert [i["code"] for i in issues] == ["closed_loop_exact_compare"]
    assert "no a settle window (SETTLE_BEATS) and no a tolerance compare" in issues[0]["detail"]
    assert "constant SETTLE_BEATS : integer := 64;" in issues[0]["detail"] and "TOL_MU" in issues[0]["detail"]
    assert closed_loop_compare_issues(loop, tolerant) == []
    only_window = closed_loop_compare_issues(loop, tolerant.replace("abs(got_i - exp_i) > TOL_SYM or abs(got_q - exp_q) > TOL_SYM", "got_i /= exp_i"))
    assert "no a tolerance compare" in only_window[0]["detail"] and "settle window" not in only_window[0]["detail"].split(":")[0]
    assert closed_loop_compare_issues(plain, strict) == []                    # not a loop: bit-exact is right
    assert "SETTLE_BEATS" in closed_loop_hint(loop) and closed_loop_hint(plain) == ""   # told before the first draft
    # the exact beat count: every beat matched, 197 of 200 arrived, one strict line failed the run (15:12)
    count = """
      if k_exp < n_exp then
        report "missing output beats: expected " & integer'image(n_exp)
          & " got " & integer'image(k_exp) severity error;
      end if;"""
    allowed = count.replace("if k_exp < n_exp then", "if n_exp > 0 and abs(k_exp - n_exp) > n_exp / 50 and abs(k_exp - n_exp) >= 2 then")
    exact = closed_loop_compare_issues(loop, tolerant + count)
    assert [i["code"] for i in exact] == ["closed_loop_exact_beat_count"] and "`if k_exp < n_exp then`" in exact[0]["detail"]
    assert "abs(k_exp - n_exp) > n_exp / 50" in exact[0]["detail"]
    assert closed_loop_compare_issues(loop, tolerant + allowed) == []
    assert closed_loop_compare_issues(plain, count) == []                     # not a loop: every beat must arrive
    assert "abs(k_exp - n_exp) > n_exp / 50" in closed_loop_hint(loop)


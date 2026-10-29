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

    def generate(self, spec, rtl, skills=None, previous_testbench=None, lint_issues=None, attempt=1, attempt_history=None):
        self.calls.append({"rtl": rtl, "skills": dict(skills or {}), "previous": previous_testbench,
                           "lint_issues": list(lint_issues or []), "attempt": attempt,
                           "attempt_history": list(attempt_history or [])})
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


def test_read_wide_with_a_fixed_width_or_a_wrongly_sized_vector_variable_is_caught():
    """Run 17:04 on 2026-10-29: 'Array sizes do not match, left array has 24
    elements, right array has 32 elements' at time 0 inside read_wide."""
    from agents.testbench_agent.testbench_linter import _replay_checks
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


def test_reading_the_outputs_file_in_two_passes_is_not_a_column_count_mismatch():
    """The LLR demapper run: the testbench reads test_vectors_out.csv twice (collect
    expected beats, then replay). 4 + 4 reads of `lo` are two whole rows, not 8 columns."""
    from agents.testbench_agent.testbench_linter import _replay_checks
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
    from agents.testbench_agent.testbench_linter import _replay_checks
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
    from agents.testbench_agent.testbench_linter import _static_checks
    code = "entity counter4_tb is end; architecture a of counter4_tb is begin process begin wait; " \
           "if detected /= std_logic'val(v_detected) then report \"x\"; end if; end process; end;"
    found = [i for i in _static_checks(SPEC, RTL, {"entity_name": "counter4_tb", "code": code})
             if i["code"] == "std_logic_val"]
    assert len(found) == 1 and "'U' for 0" in found[0]["detail"]
    ok = code.replace("detected /= std_logic'val(v_detected)", "(detected = '1') /= (v_detected = 1)")
    assert not [i for i in _static_checks(SPEC, RTL, {"entity_name": "counter4_tb", "code": ok})
                if i["code"] == "std_logic_val"]


def test_treating_a_one_bit_port_as_a_bus_is_caught():
    from agents.testbench_agent.testbench_linter import _static_checks
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
    from agents.testbench_agent.testbench_linter import _streaming_checks
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

    # a plain counter has no handshake ports: nothing to enforce
    assert _streaming_checks(SPEC, per_cycle) == []


def test_streaming_compare_against_the_current_row_is_caught_even_inside_the_handshake():
    """llr_demap16 run of 2026-10-29 (4 attempts, RTL blamed every time): the
    compare sat inside the DUT's handshake, so the old rules were satisfied,
    but it compared m_axis_tdata with v_m_axis_tdata of the CURRENT row -> the
    output was checked against the wrong beat ('cycle 17: expected hi=125
    lo=61440 got hi=0 lo=0', then 'cycle 20: ... got hi=125 lo=61440')."""
    from agents.testbench_agent.testbench_linter import _streaming_checks
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
    from agents.testbench_agent.testbench_linter import _idle_row_hold_issue, _streaming_checks
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

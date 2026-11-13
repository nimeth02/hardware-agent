"""RTL agent tests — no LLM, no MCP, no Vivado: generator, planner, skill
loader and linter are fakes; the graph, guardrails and stall/limit logic
are real."""

from __future__ import annotations

import json
import re

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage

from agents.rtl_agent.rtl_agent import RTLAgent
from agents.rtl_agent.rtl_decider import Decision, RTLDecider
from agents.rtl_agent.rtl_generator import RTLGenerator, parse_rtl_response
from agents.rtl_agent.rtl_skill_loader import RTLSkillLoader
from agents.rtl_agent.state import Action, RTLStatus
from agents.rtl_agent.tools import RTLAgentTools

SPEC = {
    "entity_name": "counter4",
    "ports": [{"name": "clk", "direction": "input", "width": "1"},
              {"name": "rst", "direction": "input", "width": "1"},
              {"name": "count", "direction": "output", "width": "4"}],
    "clock": {"required": True, "name": "clk", "frequency_mhz": 100},
    "reset": {"required": True, "name": "rst", "type": "synchronous", "active": "high"},
    "behavior": ["count up every clock; rst clears"],
}
GOOD_RTL = {"entity_name": "counter4", "code": "entity counter4 is ... end;", "notes": []}
BAD_RTL = {"entity_name": "wrong", "code": "entity wrong is", "notes": []}
SKILLS = {"vhdl_93_design_rules.md": "- use numeric_std"}
CLEAN = {"compiler": "xvhdl", "issues": []}
DIRTY = {"compiler": "xvhdl", "issues": [{"code": "name_mismatch", "detail": "Entity is 'wrong'"}]}


class FakeGenerator(RTLGenerator):
    def __init__(self, rtls):
        super().__init__(llm=object())
        self.rtls = list(rtls)
        self.calls: list[dict] = []

    def generate(self, spec, skills=None, previous_rtl=None, lint_issues=None, attempt=1, attempt_history=None,
                 starting_rtl=None):
        self.calls.append({"skills": dict(skills or {}), "previous": previous_rtl, "lint_issues": list(lint_issues or []),
                           "attempt": attempt, "attempt_history": list(attempt_history or []), "starting": starting_rtl})
        return self.rtls.pop(0) if len(self.rtls) > 1 else self.rtls[0]


class FakeDecider(RTLDecider):
    """Rule-based stand-in for the planner, with optional forced decisions."""

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
        if not summary["has_rtl"] or (summary["lint"] or {}).get("issues"):
            return Decision(Action.GENERATE_RTL, "rule")
        return Decision(Action.FINALIZE_RTL, "rule")


class FakeSkillLoader(RTLSkillLoader):
    def __init__(self, skills=SKILLS, error=None):
        self.skills, self.error = skills, error

    def load(self, keep=None):
        if self.error:
            raise RuntimeError(self.error)
        return {n: c for n, c in self.skills.items() if keep is None or keep(n)}


def fake_linter(results):
    """Returns the queued lint results in order (last one repeats)."""
    queue = list(results)

    def lint(spec, rtl):
        return queue.pop(0) if len(queue) > 1 else queue[0]
    return lint


def make_agent(rtls, lints, decider=None, loader=None, max_iterations=6, examples=None):
    tools = RTLAgentTools(generator=FakeGenerator(rtls), decider=decider or FakeDecider(),
                          skill_loader=loader or FakeSkillLoader(), linter=fake_linter(lints),
                          examples=examples or (lambda spec: {}))
    return RTLAgent(tools=tools, max_iterations=max_iterations)


# ── happy path ───────────────────────────────────────────────────────────

def test_skills_are_loaded_first_and_passed_to_the_generator():
    agent = make_agent([GOOD_RTL], [CLEAN])
    out = agent.run(SPEC)
    assert out["status"] == RTLStatus.COMPLETE.value
    assert agent.tools.decider.seen[0]["skills_loaded"] is False
    assert agent.tools.generator.calls[0]["skills"] == SKILLS
    assert out["skills_used"] == ["vhdl_93_design_rules.md"]
    assert out["iteration"] == 3   # load_skills, generate(+lint), finalize


def test_only_the_rulebook_for_the_spec_edition_is_loaded():
    both = {"vhdl_93_design_rules.md": "93", "vhdl_2008_design_rules.md": "2008", "style.md": "shared"}
    agent = make_agent([GOOD_RTL], [CLEAN], loader=FakeSkillLoader(both))
    assert agent.run(SPEC)["skills_used"] == ["vhdl_93_design_rules.md", "style.md"]          # no field: 93
    agent = make_agent([GOOD_RTL], [CLEAN], loader=FakeSkillLoader(both))
    assert agent.run({**SPEC, "vhdl_version": "2008"})["skills_used"] == ["vhdl_2008_design_rules.md", "style.md"]


def test_clean_lint_finalizes():
    out = make_agent([GOOD_RTL], [CLEAN]).run(SPEC)
    assert out["status"] == RTLStatus.COMPLETE.value
    assert out["rtl"] == GOOD_RTL and out["lint"] == CLEAN
    assert out["error"] is None and out["warnings"] == []
    assert out["requirement_id"] is None and out["attempt"] == 1            # called on its own: no orchestrator note


EARLIER = [{"attempt": 1, "status": "NEEDS_REVIEW", "error": "lint issues remain",
            "issues": [{"code": "name_mismatch", "detail": "Entity is 'wrong'"}]}]


def test_the_agent_knows_which_orchestrator_call_it_is_and_tells_the_generator():
    agent = make_agent([GOOD_RTL], [CLEAN])
    out = agent.run(SPEC, requirement_id="counter_T-11.35_D-16.10.2026", attempt=2, attempt_history=EARLIER)
    assert out["requirement_id"] == "counter_T-11.35_D-16.10.2026" and out["attempt"] == 2
    assert agent.tools.generator.calls[0]["attempt"] == 2                   # the writer is told it is the 2nd call
    assert agent.tools.generator.calls[0]["attempt_history"] == EARLIER      # and how the 1st one went
    first_call = make_agent([GOOD_RTL], [CLEAN])
    first_call.run(SPEC)                                                    # called on its own: 1st call, no history
    assert first_call.tools.generator.calls[0]["attempt"] == 1
    assert first_call.tools.generator.calls[0]["attempt_history"] == []


def test_on_a_retry_the_generator_prompt_carries_the_earlier_attempts():
    class Spy(FakeListChatModel):
        seen: list = []

        def _call(self, messages, *args, **kwargs):
            Spy.seen.append(messages)
            return super()._call(messages, *args, **kwargs)

    Spy.seen = []
    gen = RTLGenerator(llm=Spy(responses=['{"entity_name": "counter4", "code": "x"}'] * 2))
    gen.generate(SPEC)                                                      # 1st call: prompt unchanged
    gen.generate(SPEC, attempt=2, attempt_history=EARLIER)                  # 2nd call: told what went wrong before
    first, second = Spy.seen
    assert "attempt 2" not in first[0].content and first[1].content.startswith("{\n  \"entity_name\"")
    assert "This is attempt 2 for this requirement" in second[0].content
    assert "name_mismatch" in second[1].content and '"spec"' in second[1].content


def test_on_a_retry_the_generator_is_told_what_the_simulation_blamed_on_the_rtl():
    class Spy(FakeListChatModel):
        seen: list = []

        def _call(self, messages, *args, **kwargs):
            Spy.seen.append(messages)
            return super()._call(messages, *args, **kwargs)

    Spy.seen = []
    blamed = [{"attempt": 1, "status": "COMPLETE", "error": None, "issues": None, "likely_cause": None,
               "downstream": [{"stage": "simulate", "verdict": "fail", "failures": ["cycle 3: count expected 0011 got 0010"],
                               "summary": "count skips a value", "suggested_fix": "increment on every rising edge"}]}]
    RTLGenerator(llm=Spy(responses=['{"entity_name": "counter4", "code": "x"}'])).generate(
        SPEC, attempt=2, attempt_history=blamed)
    system, user = Spy.seen[0]
    assert '"downstream"' in system.content and "blamed this RTL" in system.content
    assert "cycle 3: count expected 0011 got 0010" in user.content and "increment on every rising edge" in user.content


def test_on_a_retry_the_generator_revises_the_previous_calls_rtl_instead_of_starting_over():
    class Spy(FakeListChatModel):
        seen: list = []

        def _call(self, messages, *args, **kwargs):
            Spy.seen.append(messages)
            return super()._call(messages, *args, **kwargs)

    Spy.seen = []
    blamed = [{"attempt": 1, "status": "COMPLETE", "error": None, "issues": None, "likely_cause": None,
               "downstream": [{"stage": "simulate", "verdict": "fail", "failures": ["cycle 3: count expected 0011 got 0010"],
                               "summary": "count skips a value", "suggested_fix": "increment on every rising edge"}]}]
    gen = RTLGenerator(llm=Spy(responses=['{"entity_name": "counter4", "code": "x"}'] * 3))
    gen.generate(SPEC, attempt=2, attempt_history=blamed, starting_rtl=GOOD_RTL)
    system, user = Spy.seen[0]
    assert "REVISE that code" in system.content                              # told to edit, not rewrite
    assert json.loads(user.content)["previous_code"] == GOOD_RTL["code"]     # and handed the code to edit
    gen.generate(SPEC, attempt=2, attempt_history=blamed, starting_rtl=GOOD_RTL,
                 previous_rtl=BAD_RTL, lint_issues=DIRTY["issues"])          # this run's own lint fix wins
    system, user = Spy.seen[1]
    assert "REVISE that code" not in system.content and "You are now FIXING" in system.content
    assert json.loads(user.content)["previous_code"] == BAD_RTL["code"]
    gen.generate(SPEC, attempt=1, starting_rtl=GOOD_RTL)                     # 1st call: nothing to revise
    assert "previous_code" not in Spy.seen[2][1].content


def test_the_agent_hands_the_previous_calls_rtl_to_the_generator_without_linting_it_first():
    agent = make_agent([GOOD_RTL], [CLEAN])
    agent.run(SPEC, attempt=2, attempt_history=EARLIER, previous_rtl=BAD_RTL)
    assert agent.tools.generator.calls[0]["starting"] == BAD_RTL           # revise this
    assert agent.tools.generator.calls[0]["previous"] is None              # but it is not this run's design yet


# ── lint-driven fix loop ─────────────────────────────────────────────────

def test_lint_issues_trigger_a_fix_regeneration_with_feedback():
    agent = make_agent([BAD_RTL, GOOD_RTL], [DIRTY, CLEAN])
    out = agent.run(SPEC)
    assert out["status"] == RTLStatus.COMPLETE.value
    fix_call = agent.tools.generator.calls[1]
    assert fix_call["previous"] == BAD_RTL
    assert fix_call["lint_issues"] == DIRTY["issues"]
    assert out["iteration"] == 4   # load, generate, generate(fix), finalize


def test_fixes_that_change_nothing_end_in_needs_review():
    agent = make_agent([BAD_RTL], [DIRTY])      # every regeneration lints identically
    out = agent.run(SPEC)
    assert out["status"] == RTLStatus.NEEDS_REVIEW.value
    assert "fix attempts" in out["error"]
    assert out["rtl"] == BAD_RTL and out["lint"]["issues"]


def test_finalize_is_blocked_while_lint_issues_exist():
    forced = [Decision(Action.LOAD_SKILLS), Decision(Action.GENERATE_RTL), Decision(Action.FINALIZE_RTL, "wrong")]
    agent = make_agent([BAD_RTL, GOOD_RTL], [DIRTY, CLEAN], decider=FakeDecider(forced))
    out = agent.run(SPEC)
    assert out["status"] == RTLStatus.COMPLETE.value
    assert agent.tools.generator.calls[1]["lint_issues"] == DIRTY["issues"]  # the guarded finalize became a fix


# ── failures and limits ──────────────────────────────────────────────────

def test_skill_loader_failure_is_a_warning_not_a_failure():
    agent = make_agent([GOOD_RTL], [CLEAN], loader=FakeSkillLoader(error="mcp down"))
    out = agent.run(SPEC)
    assert out["status"] == RTLStatus.COMPLETE.value
    assert out["skills_used"] == []
    assert any("mcp down" in w for w in out["warnings"])
    assert agent.tools.generator.calls[0]["skills"] == {}


def test_generator_failure_fails_the_run():
    class Exploding(FakeGenerator):
        def generate(self, *a, **k):
            raise RuntimeError("llm down")
    tools = RTLAgentTools(generator=Exploding([GOOD_RTL]), decider=FakeDecider(),
                          skill_loader=FakeSkillLoader(), linter=fake_linter([CLEAN]), examples=lambda spec: {})
    out = RTLAgent(tools=tools).run(SPEC)
    assert out["status"] == RTLStatus.FAILED.value and "llm down" in out["error"]


def test_iteration_limit_returns_needs_review():
    # every fix changes the lint fingerprint (no stall) but never gets clean
    issues = [{"compiler": None, "issues": [{"code": "x", "detail": f"problem {i}"}]} for i in range(10)]
    forced = [Decision(Action.LOAD_SKILLS)] + [Decision(Action.GENERATE_RTL)] * 10
    agent = make_agent([BAD_RTL], issues, decider=FakeDecider(forced), max_iterations=4)
    out = agent.run(SPEC)
    assert out["status"] == RTLStatus.NEEDS_REVIEW.value
    assert out["iteration"] == 4 and "Iteration limit" in out["error"]


# ── planner fallbacks and restrictions ───────────────────────────────────

def test_invalid_planner_output_falls_back_safely():
    assert RTLDecider.parse("garbage", {"skills_loaded": False}).action is Action.LOAD_SKILLS
    assert RTLDecider.parse("??", {"skills_loaded": True, "has_rtl": False}).action is Action.GENERATE_RTL
    assert RTLDecider.parse("??", {"skills_loaded": True, "has_rtl": True, "lint": None}).action is Action.LINT_RTL
    assert RTLDecider.parse("??", {"skills_loaded": True, "has_rtl": True, "lint": DIRTY}).action is Action.GENERATE_RTL
    assert RTLDecider.parse("??", {"skills_loaded": True, "has_rtl": True, "lint": CLEAN}).action is Action.FINALIZE_RTL
    ok = RTLDecider.parse('```json\n{"action": "lint_rtl", "reason": "r"}\n```', {})
    assert ok.action is Action.LINT_RTL and ok.reason == "r"


def test_a_reply_that_is_not_json_or_was_cut_off_has_no_code_and_says_why():
    """timing_recovery, 2026-11-02: a looping 99k-token reply was cut off mid-JSON,
    compiled as if it were RTL, and reported as markdown fences / no entity."""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from agents.rtl_agent.lint.rtl_linter import _static_checks
    rtl = RTLGenerator(llm=FakeListChatModel(responses=["here is some vhdl, not json"])).generate(SPEC)
    assert rtl["code"] == "" and "not valid JSON" in rtl["notes"][0]
    cut = AIMessage(content='```json\n{"entity_name": "counter4", "code": "entity counter4 is',
                    response_metadata={"finish_reason": "length"})
    rtl = RTLGenerator(llm=FakeMessagesListChatModel(responses=[cut])).generate(SPEC)
    assert rtl["code"] == "" and "cut off" in rtl["notes"][0]
    found = _static_checks(SPEC, rtl)
    assert [i["code"] for i in found] == ["empty_code"] and "cut off" in found[0]["detail"]
    assert parse_rtl_response(SPEC, '{"entity_name": "counter4", "code": "x"}')["code"] == "x"


def test_comparing_with_an_others_aggregate_is_caught_before_xelab_would_refuse_it():
    from agents.rtl_agent.lint.rtl_linter import _forbidden_constructs
    bad = "if count_next = (others => '0') then\nif v /= (others => '1') then"
    found = _forbidden_constructs(bad, "93")
    assert [i["code"] for i in found] == ["forbidden_construct"] * 2
    assert "line 1" in found[0]["detail"] and "xelab" in found[0]["detail"]
    ok = "count_reg <= (others => '0');\nv := (others => '1');\nif count_reg = 0 then\nif v = \"0000\" then"
    assert _forbidden_constructs(ok, "93") == [] and _forbidden_constructs(ok, "2008") == []


def test_vhdl_2008_shift_operators_are_caught_before_xvhdl_in_93_mode():
    """timing_recovery, 2026-11-02 fix round 3: `to_signed(1, 16) sra 7` and
    `v2_i sra 1` cost a whole compile round ("found '0' definitions of operator
    sra"); the rulebook already said to use shift_right."""
    from agents.rtl_agent.lint.rtl_linter import _forbidden_constructs
    bad = ("constant K1 : signed(15 downto 0) := to_signed(1, 16) sra 7;\n"
           "v2_i := v2_i SLL 1;\n"
           "acc := acc ror 2;  -- `x sra 1` inside a comment is fine")
    found = _forbidden_constructs(bad, "93")
    assert [i["code"] for i in found] == ["forbidden_construct"] * 3
    assert "line 1" in found[0]["detail"] and "sra" in found[0]["detail"] and "shift_right" in found[0]["detail"]
    assert "shift_left" in found[1]["detail"] and "rotate_right" in found[2]["detail"]
    ok = "v2_i := shift_right(v2_i, 1);\nsignal sll_count : unsigned(3 downto 0);\nk1 := k1_sra;"
    assert _forbidden_constructs(ok, "93") == [] and _forbidden_constructs(bad, "2008") == []


def test_agent_only_has_restricted_actions():
    assert {a.value for a in Action} == {"load_skills", "generate_rtl", "lint_rtl", "finalize_rtl"}
    nodes = set(make_agent([GOOD_RTL], [CLEAN]).graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"decide", "load_skills", "generate_rtl", "lint_rtl", "finalize_rtl"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_linter_catches_a_stage_that_reads_two_stages_back():
    """llr_demap16, 2026-10-29 12:18: stage 3 read i_s1/q_s1 directly while ns
    went through stage 2, so every beat was multiplied by the gap row's ns = 0
    and all 50 output beats were 0; three retries returned the same file."""
    from agents.rtl_agent.lint.rtl_linter import _pipeline_stage_skip_issues
    skipping = """
      process(aclk) begin if rising_edge(aclk) then
        i_s1 <= signed(s_axis_tdata(31 downto 20));  ns_s1 <= unsigned(s_axis_tdata(7 downto 0));
        abs_i_s2 <= resize(i_s1, 13);                ns_s2 <= ns_s1;  -- stage 2
        i_mmsb_s3 <= calc_mmsb(i_s1);                ns_s3 <= ns_s2;  -- stage 3 reads stage 1: wrong
        i_mmsb_p_s4 <= i_mmsb_s3 * signed('0' & ns_s3);
      end if; end process;
    """
    issues = _pipeline_stage_skip_issues(skipping)
    assert [i["code"] for i in issues] == ["pipeline_stage_skip"]
    assert "line 5" in issues[0]["detail"] and "i_mmsb_s3 is computed from i_s1" in issues[0]["detail"]
    assert "i_s2" in issues[0]["detail"]                      # told exactly what to read instead
    assert not _pipeline_stage_skip_issues(skipping.replace("calc_mmsb(i_s1)", "calc_mmsb(i_s2)"))
    # comments and comparisons are not reads of a stage register
    assert not _pipeline_stage_skip_issues("-- x_s3 <= y_s1\nif a_s1 <= b_s3 then c_s3 <= c_s2; end if;")


def test_a_retry_that_returns_the_same_rtl_is_refused_without_a_simulation():
    """The orchestrator hands back the RTL a simulation rejected; the model
    returns it byte for byte. That is a lint issue now, and when the fix
    returns the same file again the call ends in NEEDS_REVIEW instead of
    costing another dry run (llr_demap16, 2026-10-29: three identical answers)."""
    blamed = [{"attempt": 1, "status": "COMPLETE", "error": None, "issues": None,
               "downstream": [{"stage": "simulate", "verdict": "fail",
                               "failures": ["beat 1 (input cycle 12): m_axis_tdata expected 2131480 got 0"],
                               "summary": "every beat is 0", "suggested_fix": "multiply with ns_s3, not ns_s1"}]}]
    same = {"entity_name": "counter4", "code": GOOD_RTL["code"] + "\n", "notes": []}     # whitespace does not count
    agent = make_agent([same], [CLEAN])
    out = agent.run(SPEC, attempt=2, attempt_history=blamed, previous_rtl=GOOD_RTL)
    assert out["status"] == RTLStatus.NEEDS_REVIEW.value and "fix attempts" in out["error"]
    issue = out["lint"]["issues"][0]
    assert issue["code"] == "unchanged_on_retry"
    assert "expected 2131480 got 0" in issue["detail"] and "ns_s3" in issue["detail"]
    fix_call = agent.tools.generator.calls[1]
    assert fix_call["lint_issues"][0]["code"] == "unchanged_on_retry"    # the fix loop asked for a real change

    changed = {"entity_name": "counter4", "code": "entity counter4 is -- fixed", "notes": []}
    agent = make_agent([changed], [CLEAN])
    assert agent.run(SPEC, attempt=2, attempt_history=blamed, previous_rtl=GOOD_RTL)["status"] == RTLStatus.COMPLETE.value
    agent = make_agent([GOOD_RTL], [CLEAN])                                          # first call: nothing to compare
    assert agent.run(SPEC)["status"] == RTLStatus.COMPLETE.value


def test_linter_flags_multiplier_width_and_double_writers():
    from agents.rtl_agent.lint.rtl_linter import _multiple_driver_issues, _multiplier_width_issues

    narrow = ("signal d : signed(28 downto 0);\nsignal n : unsigned(7 downto 0);\n"
              "signal p : signed(36 downto 0);\n"
              "p <= d * resize(signed('0' & n), 37);\n")
    assert _multiplier_width_issues(narrow)[0]["code"] == "multiplier_width"
    assert not _multiplier_width_issues(narrow.replace("36 downto", "65 downto"))

    two_writers = ("a: process(clk) begin if rising_edge(clk) then x <= y; end if; end process;\n"
                   "b: process(clk) begin if rising_edge(clk) then\n"
                   "  if y <= z then x <= z; end if; end if; end process;\n")
    assert _multiple_driver_issues(two_writers)[0]["code"] == "multiple_drivers"
    one_writer = two_writers.replace("then x <= z;", "then w <= z;")
    assert not _multiple_driver_issues(one_writer)
    assert _multiple_driver_issues("a: process(clk) begin x <= y; end process;\nx <= z;\n")


def test_linter_catches_a_process_declaration_without_the_variable_keyword():
    from agents.rtl_agent.lint.rtl_linter import _process_declaration_issues
    bad = ("signal sat_i : signed(11 downto 0);\n"                       # architecture signal: fine
           "farrow: process(clk)\n"
           "  temp_i : signed(47 downto 0);\n"                           # line 3: missing `variable`
           "  variable temp_q : signed(47 downto 0);\n"
           "  constant ONE : integer := 1;\n"
           "  v2_mid_i, v2_mid_q : signed(15 downto 0);\n"               # line 6: missing `variable`
           "begin\n  temp_i := (others => '0');\nend process;\n")
    found = _process_declaration_issues(bad)
    assert [i["code"] for i in found] == ["missing_variable_keyword"] * 2
    assert "line 3" in found[0]["detail"] and "variable temp_i : ...;" in found[0]["detail"]
    assert "line 6" in found[1]["detail"] and "v2_mid_i, v2_mid_q" in found[1]["detail"]
    assert _process_declaration_issues(bad.replace("  temp_i :", "  variable temp_i :")
                                       .replace("  v2_mid_i,", "  variable v2_mid_i,")) == []


def test_linter_catches_unbalanced_parentheses_per_statement():
    from agents.rtl_agent.lint.rtl_linter import _unbalanced_parentheses_issues
    bad = ("v0_i <= resize(signed('0' & x_0_i), 16);\n"                  # balanced: fine
           "v1_i <= resize((-signed('0' & x_p2_i) + shift_left(signed('0' & x_p1_i), 1)\n"
           "         - signed('0' & x_m1_i), COEF_WIDTH);\n"             # one ')' short
           "report \"unbalanced ( in a string is fine\";\n")
    found = _unbalanced_parentheses_issues(bad)
    assert len(found) == 1 and found[0]["code"] == "unbalanced_parentheses"
    assert "line 2" in found[0]["detail"] and "6 '(' but 5 ')'" in found[0]["detail"]
    assert _unbalanced_parentheses_issues(bad.replace("COEF_WIDTH);", "COEF_WIDTH));")) == []

    entity = ("entity counter_4bit_up is\n  port (\n    clk : in  std_logic;\n    rst : in  std_logic;\n"
              "    count : out std_logic_vector(3 downto 0)\n  );\nend entity counter_4bit_up;\n"
              "architecture rtl of counter_4bit_up is\n  signal count_reg : unsigned(3 downto 0) := (others => '0');\n"
              "begin\n  count <= std_logic_vector(count_reg);\nend architecture rtl;\n")
    assert _unbalanced_parentheses_issues(entity) == []                       # ';' inside port ( ... ) is not a split
    broken = entity.replace("std_logic_vector(3 downto 0)\n", "std_logic_vector(3 downto 0\n")   # one ')' lost
    found = _unbalanced_parentheses_issues(broken)
    assert len(found) == 1 and "line 1" in found[0]["detail"] and "entity counter_4bit_up is" in found[0]["detail"]


def test_linter_names_the_if_whose_end_if_is_missing_before_the_compiler_blames_the_label():
    """timing_recovery 15:11 (2026-11-02): one `end if;` missing after an if/elsif
    in p_nco; xvhdl said 'syntax error near process' / 'mismatch on label p_nco',
    the model hunted for a label problem and returned the same file nine times."""
    from agents.rtl_agent.lint.rtl_linter import _block_balance_issues
    nco = ("p_nco : process(aclk)\n"
           "  variable mu_v : signed(16 downto 0);\n"
           "begin\n"
           "  if rising_edge(aclk) then\n"
           "    if aresetn = '0' then\n"
           "      eta_s1 <= (others => '0');\n"
           "    else\n"
           "      if eta_next_v < 0 then\n"
           "        mu_v := shift_left(eta_s1, 2);\n"
           "        if mu_v < 0 then mu_v := (others => '0');\n"
           "        elsif mu_v > to_signed(65536, 17) then mu_v := to_signed(65536, 17);\n"
           "        eta_next_v := eta_next_v + to_signed(65536, 17);  -- +1\n"
           "      end if;\n"
           "      eta_s1 <= eta_next_v;\n"
           "    end if;\n"
           "  end if;\n"
           "end process p_nco;\n")
    found = _block_balance_issues(nco)
    assert [i["code"] for i in found] == ["unclosed_block"]
    d = found[0]["detail"]
    assert "p_nco" in d and "4 `if`" in d and "closes 3" in d and "line 10" in d and "if mu_v < 0" in d
    assert "syntax error near process" in d
    fixed = nco.replace("        eta_next_v := eta_next_v", "        end if;\n        eta_next_v := eta_next_v")
    assert _block_balance_issues(fixed) == []
    # one `end if;` too many (2026-10-30 the model deleted an end if to obey a wrong message): the surplus one is named
    surplus = fixed.replace("      eta_s1 <= eta_next_v;\n", "      eta_s1 <= eta_next_v;\n      end if;\n")
    found = _block_balance_issues(surplus)
    assert len(found) == 1 and "too many" in found[0]["detail"] and "line 16" in found[0]["detail"]
    # case and loop count the same way; elsif/else/when never count; `end if` inside a string is prose
    ok = ("process(clk) begin if rising_edge(clk) then case st is when A => for i in 0 to 3 loop x(i) <= '0'; end loop;\n"
          "when others => report \"end if\"; end case; end if; end process;")
    assert _block_balance_issues(ok) == []
    assert "`loop`" in _block_balance_issues(ok.replace(" end loop;", ""))[0]["detail"]


def test_valid_chain_registers_with_odd_names_are_not_mistaken_for_the_output_flag():
    """timing_recovery 15:11 (2026-11-02): `valid_s3_f <= valid_s2;` was read as the
    output flag taken from stage 2, so an eight-deep valid chain was reported
    as two and the model could not act on the finding."""
    from agents.rtl_agent.lint.rtl_linter import _valid_data_length_issues
    clocked = "process(clk) begin if rising_edge(clk) then\n{}\nend if; end process;\n"   # one statement per line, as in real code
    chain = ("valid_s1 <= in_valid;\na_s1 <= d;\nvalid_s2 <= valid_s1;\na_s2 <= a_s1;\n"
             "valid_s3_f <= valid_s2;\na_s3 <= a_s2;\nvalid_s4 <= valid_s3_f;\na_s4 <= a_s3;")
    assert not _valid_data_length_issues(clocked.format(chain) + "m_axis_tvalid <= valid_s4;")
    short = _valid_data_length_issues(clocked.format(chain + "\na_s5 <= a_s4;") + "m_axis_tvalid <= valid_s4;")
    assert [i["code"] for i in short] == ["valid_data_length_mismatch"] and "5 registers" in short[0]["detail"]
    assert "valid flag through 4" in short[0]["detail"]                  # counted to valid_s4, not stopped at valid_s3_f


def test_compiler_follow_on_errors_are_hidden_so_the_root_causes_stand_out():
    from agents.lint_utils import compiler_errors
    src = "\n".join(f"line{n}" for n in range(1, 340))
    out = "\n".join([
        "ERROR: [VRFC 10-4982] syntax error near ';' [C:/tmp/tmpab12cd/timing_recovery.vhd:241]",
        "ERROR: [VRFC 10-4982] syntax error near ')' [C:/tmp/tmpab12cd/timing_recovery.vhd:242]",
        "ERROR: [VRFC 10-4982] syntax error near 'temp_i' [C:/tmp/tmpab12cd/timing_recovery.vhd:279]",
        "ERROR: [VRFC 10-4982] syntax error near 'begin' [C:/tmp/tmpab12cd/timing_recovery.vhd:283]",
        "ERROR: [VRFC 10-2977] 'temp_i' is not a variable [C:/tmp/tmpab12cd/timing_recovery.vhd:285]",
        "ERROR: [VRFC 10-2977] 'temp_q' is not a variable [C:/tmp/tmpab12cd/timing_recovery.vhd:286]",
        "ERROR: [VRFC 10-2977] 'result_i' is not a variable [C:/tmp/tmpab12cd/timing_recovery.vhd:293]",
        "ERROR: [VRFC 10-2942] 'result_i' is illegal in an expression [C:/tmp/tmpab12cd/timing_recovery.vhd:301]",
        "ERROR: [VRFC 10-4982] syntax error near ';' [C:/tmp/tmpab12cd/timing_recovery.vhd:316]",
        "ERROR: [VRFC 10-4982] syntax error near 'if' [C:/tmp/tmpab12cd/timing_recovery.vhd:321]",
        "ERROR: [VRFC 10-4982] syntax error near 'v2_mid_i' [C:/tmp/tmpab12cd/timing_recovery.vhd:337]",
    ])
    details = [i["detail"] for i in compiler_errors(out, src, 1)]
    assert [d.split("[timing_recovery.vhd:")[1].split("]")[0] for d in details[:-1]] == ["241", "279", "337"]
    assert details[-1].startswith("8 follow-on error(s) hidden")
    assert all(i["code"] == "compile_error" for i in compiler_errors(out, src, 1))
    independent = ("ERROR: [VRFC 10-1] 'a' is not declared [x.vhd:10]\n"
                   "ERROR: [VRFC 10-1] 'b' is not declared [x.vhd:200]")       # far apart: both are roots
    assert len(compiler_errors(independent, src, 1)) == 2


def test_linter_wants_the_stage_map_note_for_a_pipelined_design():
    """skills/RTL/pipeline_stage_map.md: the model writes the stage map
    before the code so a two-stages-back read is caught in the table, not
    in a simulation. A pipeline returned without the map goes back once."""
    from agents.rtl_agent.lint.rtl_linter import _stage_map_issues
    pipeline = "i_s1 <= signed(d); i_s2 <= i_s1; i_s3 <= i_s2;"
    assert [i["code"] for i in _stage_map_issues({"code": pipeline, "notes": ["latency 3"]})] == ["stage_map_missing"]
    assert not _stage_map_issues({"code": pipeline, "notes": ["Stage map:", "s1: i_s1 <- inputs", "s2: i_s2 <- s1"]})
    assert not _stage_map_issues({"code": "q <= d when en = '1' else q;", "notes": []})   # no stages, no map needed


def test_stage_map_skill_is_loaded_for_every_vhdl_edition():
    from agents.vhdl_version import skill_matches_version
    assert skill_matches_version("pipeline_stage_map.md", "93")
    assert skill_matches_version("pipeline_stage_map.md", "2008")


SIX_STAGES = """
  signal valid_pipe : std_logic_vector(5 downto 0) := (others => '0');
  signal m_axis_tdata_reg : std_logic_vector(23 downto 0);
begin
  advance <= m_axis_tready or (not valid_pipe(5));
  m_axis_tvalid_i <= valid_pipe(5);
  m_axis_tdata <= m_axis_tdata_reg;
  process(aclk) begin if rising_edge(aclk) then
    if aresetn = '0' then valid_pipe <= (others => '0');
    elsif advance = '1' then
      i_s1 <= signed(s_axis_tdata(31 downto 20)); valid_pipe(0) <= s_axis_tvalid;
      abs_i_s2 <= unsigned(abs(resize(i_s1, 13))); valid_pipe(1) <= valid_pipe(0);
      m_s3 <= calc(abs_i_s2);                       valid_pipe(2) <= valid_pipe(1);
      p_s4 <= m_s3 * signed('0' & ns_s3);           valid_pipe(3) <= valid_pipe(2);
      r_s5 <= shift_right(p_s4, 13);                valid_pipe(4) <= valid_pipe(3);
      b0_s6 <= sat6(r_s5);                          valid_pipe(5) <= valid_pipe(4);
      m_axis_tdata_reg <= std_logic_vector(b0_s6 & b0_s6 & b0_s6 & b0_s6);
    end if;
  end if; end process;
"""


def test_linter_counts_the_output_register_as_a_stage_the_valid_chain_must_cover():
    """llr_demap16, 2026-10-29 12:18: six stages plus m_axis_tdata_reg = seven
    data registers, but valid_pipe(5 downto 0) = six bits, so m_axis_tvalid
    rose one clock before the output register was written and every beat was
    read as 0 ("expected 8253440 got 0")."""
    from agents.rtl_agent.lint.rtl_linter import _valid_data_length_issues
    issues = _valid_data_length_issues(SIX_STAGES)
    assert [i["code"] for i in issues] == ["valid_data_length_mismatch"]
    d = issues[0]["detail"]
    assert "7 registers" in d and "`m_axis_tdata_reg`" in d and "6 (" in d and "BEFORE" in d
    assert "valid_pipe(6) <= valid_pipe(5)" in d                  # told the exact missing line
    # 13:23 shape: a seventh valid bit for the output register -> aligned
    fixed = (SIX_STAGES.replace("(5 downto 0)", "(6 downto 0)").replace("valid_pipe(5);", "valid_pipe(6);")
             .replace("m_axis_tdata_reg <= std_logic_vector(b0_s6 & b0_s6 & b0_s6 & b0_s6);",
                      "m_axis_tdata_reg <= std_logic_vector(b0_s6 & b0_s6 & b0_s6 & b0_s6); valid_pipe(6) <= valid_pipe(5);"))
    assert not _valid_data_length_issues(fixed)
    # no output register, output joined straight from stage 6 -> six and six
    direct = SIX_STAGES.replace("      m_axis_tdata_reg <= std_logic_vector(b0_s6 & b0_s6 & b0_s6 & b0_s6);\n", "") \
                       .replace("m_axis_tdata <= m_axis_tdata_reg;", "m_axis_tdata <= std_logic_vector(b0_s6 & b0_s6 & b0_s6 & b0_s6);")
    assert not _valid_data_length_issues(direct)
    # valid as stage registers instead of valid_pipe: counted the same way
    clocked = "process(clk) begin if rising_edge(clk) then {} end if; end process;"
    staged = "valid_s1 <= in_valid; a_s1 <= d; valid_s2 <= valid_s1; a_s2 <= a_s1;"
    assert not _valid_data_length_issues(clocked.format(staged))
    assert _valid_data_length_issues(clocked.format(staged + " a_s3 <= a_s2;"))[0]["code"] == "valid_data_length_mismatch"
    # the output valid flag registered next to the output register is that register's valid bit (run 27.10 17:42)
    paired = SIX_STAGES.replace("m_axis_tvalid_i <= valid_pipe(5);", "").replace(
        "m_axis_tdata_reg <= std_logic_vector(b0_s6 & b0_s6 & b0_s6 & b0_s6);",
        "m_axis_tdata_reg <= std_logic_vector(b0_s6 & b0_s6 & b0_s6 & b0_s6); m_axis_tvalid <= valid_pipe(5);")
    assert not _valid_data_length_issues(paired)
    assert not _valid_data_length_issues("q <= d when en = '1' else q;")       # no pipeline, nothing to count
    assert not _valid_data_length_issues("i_s0 <= signed(d); i_s1 <= i_s0;")   # wires outside a process: not registers
    # no valid_pipe and no valid_sK at all (record-style designs): nothing to count against, stay quiet
    assert not _valid_data_length_issues(clocked.format("a_s1 <= d; a_s2 <= a_s1; q_reg <= a_s2; m_axis_tvalid <= v;"))


# timing_recovery 17:58 (2026-11-02) in small: the output reads stage 3 through an
# output register, stages 4-5 compute the loop value fed back into stage 1
LOOP_AFTER_OUTPUT = """
entity t is port (aclk : in std_logic; s_axis_tdata : in std_logic_vector(11 downto 0); s_axis_tvalid : in std_logic;
                  m_axis_tdata : out std_logic_vector(11 downto 0); m_axis_tvalid : out std_logic); end;
architecture rtl of t is
  signal valid_pipe : std_logic_vector(2 downto 0) := (others => '0');
  signal m_axis_tdata_reg : std_logic_vector(11 downto 0);
begin
  advance <= (not valid_pipe(2));
  m_axis_tvalid_i <= valid_pipe(2);
  m_axis_tdata <= m_axis_tdata_reg;
  p_valid : process(aclk) begin if rising_edge(aclk) then
    valid_pipe(0) <= s_axis_tvalid;
    valid_pipe(1) <= valid_pipe(0);
    valid_pipe(2) <= valid_pipe(1);
  end if; end process;
  p_s1 : process(aclk) begin if rising_edge(aclk) then
    i_s1 <= signed(s_axis_tdata);
    mu_s1 <= mu_s5;
  end if; end process;
  p_s2 : process(aclk) begin if rising_edge(aclk) then
    x_s2 <= i_s1;
    mu_s2 <= mu_s1;
  end if; end process;
  p_s3 : process(aclk) begin if rising_edge(aclk) then
    y_s3 <= x_s2 + mu_s2;
    mu_s3 <= mu_s2;
  end if; end process;
  p_s4 : process(aclk) begin if rising_edge(aclk) then
    e_s4 <= y_s3 - mu_s3;
  end if; end process;
  p_s5 : process(aclk) begin if rising_edge(aclk) then
    mu_s5 <= e_s4;
  end if; end process;
  p_out : process(aclk) begin if rising_edge(aclk) then
    m_axis_tdata_reg <= std_logic_vector(y_s3);
  end if; end process;
end architecture;
"""


def test_linter_counts_the_data_path_to_the_output_not_the_loop_rows_after_it():
    """timing_recovery 17:58 (2026-11-02): the output read _s7 through
    m_axis_tdata_reg (8 registers) while _s8.._s11 carried loop state back to
    _s1. The checker said "11 stages", the model obeyed (11 valid bits), the
    finding went quiet and m_axis_tvalid rose 3 clocks late. Now the count
    stops at the stage the output reads, and the finding names the lines."""
    from agents.rtl_agent.lint.rtl_linter import _valid_data_length_issues
    issues = _valid_data_length_issues(LOOP_AFTER_OUTPUT)
    assert [i["code"] for i in issues] == ["valid_data_length_mismatch"]
    d = issues[0]["detail"]
    assert "4 registers" in d and "_s1.._s3, then `m_axis_tdata_reg`" in d and "11" not in d
    assert "_s4.._s5 come AFTER the output row and are not counted" in d and "1 clock BEFORE" in d
    assert "those rows are the old loop ring" in d and "loop_state_ring" in d
    decl_line = next(n for n, text in enumerate(LOOP_AFTER_OUTPUT.splitlines(), 1) if "signal valid_pipe" in text)
    assert f"line {decl_line}: `valid_pipe : std_logic_vector(2 downto 0)` -> `(3 downto 0)`" in d
    assert "`m_axis_tvalid_i <= valid_pipe(2)` -> `valid_pipe(3)`" in d
    assert "`advance <= (not valid_pipe(2))` -> use valid_pipe(3)" in d
    assert "in p_valid add `valid_pipe(3) <= valid_pipe(2);`" in d
    # the model's wrong fix: valid chain sized to the number of rows (5 + the output register)
    rows = (LOOP_AFTER_OUTPUT.replace("(2 downto 0)", "(5 downto 0)").replace("valid_pipe(2));", "valid_pipe(5));")
            .replace("m_axis_tvalid_i <= valid_pipe(2);", "m_axis_tvalid_i <= valid_pipe(5);")
            .replace("    valid_pipe(2) <= valid_pipe(1);\n", "    valid_pipe(2) <= valid_pipe(1);\n    valid_pipe(3) <= valid_pipe(2);\n"
                     "    valid_pipe(4) <= valid_pipe(3);\n    valid_pipe(5) <= valid_pipe(4);\n"))
    late = _valid_data_length_issues(rows)
    assert late and "2 clocks AFTER" in late[0]["detail"] and "exactly 4 bits" in late[0]["detail"]
    assert "`m_axis_tvalid_i <= valid_pipe(5)` -> `valid_pipe(3)`" in late[0]["detail"]
    assert "delete the shifts above valid_pipe(3)" in late[0]["detail"]
    # the right fix: one more bit for the output register, the flag taken from it
    fixed = (LOOP_AFTER_OUTPUT.replace("(2 downto 0)", "(3 downto 0)").replace("valid_pipe(2));", "valid_pipe(3));")
             .replace("m_axis_tvalid_i <= valid_pipe(2);", "m_axis_tvalid_i <= valid_pipe(3);")
             .replace("    valid_pipe(2) <= valid_pipe(1);\n", "    valid_pipe(2) <= valid_pipe(1);\n    valid_pipe(3) <= valid_pipe(2);\n"))
    assert not _valid_data_length_issues(fixed)
    # loop rows may keep their own valid bits to gate their updates; the output flag still comes from bit 3
    gated = fixed.replace("(3 downto 0)", "(5 downto 0)").replace(
        "    valid_pipe(3) <= valid_pipe(2);\n", "    valid_pipe(3) <= valid_pipe(2);\n    valid_pipe(4) <= valid_pipe(3);\n"
                                                "    valid_pipe(5) <= valid_pipe(4);\n")
    assert not _valid_data_length_issues(gated)
    # the input port is also named tdata: it must not be mistaken for the output
    assert "s_axis_tdata" not in d


# timing_recovery 18:25 (2026-11-02) in small: eta is updated in the last row, but
# strobe and mu only go round the ring (s1 <- s3, s2 <- s1, s3 <- s2) and are never
# computed; the output valid flag is the bare last bit although the spec says
# "output on strobe"
LOOP_RING = """
entity t is port (aclk : in std_logic; aresetn : in std_logic; s_axis_tdata : in std_logic_vector(11 downto 0);
                  s_axis_tvalid : in std_logic; m_axis_tdata : out std_logic_vector(11 downto 0);
                  m_axis_tvalid : out std_logic); end;
architecture rtl of t is
  signal valid_pipe : std_logic_vector(2 downto 0) := (others => '0');
begin
  m_axis_tvalid_i <= valid_pipe(2);
  m_axis_tdata <= std_logic_vector(y_s3);
  p_valid : process(aclk) begin if rising_edge(aclk) then
    valid_pipe(0) <= s_axis_tvalid;
    valid_pipe(1) <= valid_pipe(0);
    valid_pipe(2) <= valid_pipe(1);
  end if; end process;
  p_s1 : process(aclk) begin if rising_edge(aclk) then
    if aresetn = '0' then
      eta_s1 <= (others => '0');
      strobe_s1 <= '0';
      mu_s1 <= to_unsigned(0, 8);
    else
      i_s1 <= signed(s_axis_tdata);
      eta_s1 <= eta_s3;
      strobe_s1 <= strobe_s3;
      mu_s1 <= mu_s3;
      y_prev_s1 <= y_s3;
    end if;
  end if; end process;
  p_s2 : process(aclk) begin if rising_edge(aclk) then
    x_s2 <= i_s1;
    eta_s2 <= eta_s1;
    strobe_s2 <= strobe_s1;
    mu_s2 <= mu_s1;
  end if; end process;
  p_s3 : process(aclk) begin if rising_edge(aclk) then
    y_s3 <= x_s2 + mu_s2;
    eta_s3 <= eta_s2 - to_signed(8192, 16);
    strobe_s3 <= strobe_s2;
    mu_s3 <= mu_s2;
  end if; end process;
end architecture;
"""

SPEC_STROBE = {
    "entity_name": "t",
    "ports": [{"name": "m_axis_tvalid", "direction": "output"}, {"name": "m_axis_tdata", "direction": "output"}],
    "behavior": ["On each input sample x[n]: W = 1/N + v; eta_next = eta - W; strobe = (eta_next < 0).",
                 "If strobe: mu = clamp(eta * N, 0, 1) using eta BEFORE decrement; eta_next = eta_next + 1.",
                 "Output y_sym and mu on strobe."],
    "constraints": ["Strobe rate is approximately 1 per 4 clocks."],
}


def test_linter_reports_a_loop_value_that_only_goes_round_the_ring_and_is_never_computed():
    """timing_recovery 18:25 (2026-11-02): strobe and mu were copied through
    eleven rows and back to row 1 but never written from anything else, so
    they kept their reset value; the notes claimed "strobe is generated when
    eta_next < 0" and the dry run showed 0 on every beat."""
    from agents.rtl_agent.lint.rtl_linter import _loop_value_never_computed_issues
    issues = _loop_value_never_computed_issues(SPEC_STROBE, LOOP_RING)
    assert [i["code"] for i in issues] == ["loop_value_never_computed"] * 2
    by_name = {re.search(r"`(\w+)` only goes round", i["detail"]).group(1): i["detail"] for i in issues}
    assert set(by_name) == {"strobe", "mu"}                      # eta is computed, y_prev comes from y_s3
    strobe_line = next(n for n, t in enumerate(LOOP_RING.splitlines(), 1) if "strobe_s1 <= strobe_s3" in t)
    assert by_name["strobe"].startswith(f"line {strobe_line}: ")
    assert "keeps its reset value for the whole run" in by_name["strobe"]
    assert 'The spec says: "On each input sample x[n]: W = 1/N + v; eta_next = eta - W; strobe = (eta_next < 0)."' \
        in by_name["strobe"]
    assert "p_s3 (line" in by_name["strobe"] and "`strobe_s3 <= strobe_s2`" in by_name["strobe"]
    # the fix points at the one loop process, not at the last row of the ring (2026-11-04)
    assert "Delete the ring" in by_name["strobe"] and "`p_loop`" in by_name["strobe"]
    assert "`strobe_s2` / `mu_s2`" in by_name["strobe"]
    assert "If strobe: mu = clamp(eta * N, 0, 1)" in by_name["mu"]
    # computed in the last row (from a register, a variable, or a condition): no finding
    fixed = (LOOP_RING.replace("    strobe_s3 <= strobe_s2;\n",
                               "    if eta_s2 < 0 then strobe_s3 <= '1'; else strobe_s3 <= '0'; end if;\n")
             .replace("    mu_s3 <= mu_s2;\n", "    mu_s3 <= resize(unsigned(eta_s2), 8);\n"))
    assert not _loop_value_never_computed_issues(SPEC_STROBE, fixed)
    via_variable = LOOP_RING.replace("    strobe_s3 <= strobe_s2;\n", "    strobe_s3 <= strobe_v;\n")
    assert [i["detail"] for i in _loop_value_never_computed_issues(SPEC_STROBE, via_variable)
            if "`strobe`" in i["detail"]] == []
    # an advance/valid guard with `and` in it is still a control guard, not a computation (v73, 2026-11-03)
    guarded = (LOOP_RING.replace("  p_s3 : process(aclk) begin if rising_edge(aclk) then\n",
                                 "  p_s3 : process(aclk) begin if rising_edge(aclk) then\n"
                                 "    if advance = '1' and valid_pipe(1) = '1' then\n")
               .replace("    mu_s3 <= mu_s2;\n  end if; end process;\n", "    mu_s3 <= mu_s2;\n    end if;\n  end if; end process;\n"))
    assert len(_loop_value_never_computed_issues(SPEC_STROBE, guarded)) == 2
    # a value the spec does not mention is still reported, with a different ask
    spec_without = {**SPEC_STROBE, "behavior": ["Output y on strobe."], "constraints": []}
    mu = next(i for i in _loop_value_never_computed_issues(spec_without, LOOP_RING) if "`mu`" in i["detail"])
    assert "The spec does not name it" in mu["detail"]
    assert not _loop_value_never_computed_issues(SPEC_STROBE, SIX_STAGES)     # no loop: nothing to report


# timing_recovery 14:51 and 18:41 (2026-11-03) in small: eta, mu and v are COMPUTED in
# the last row (from the copy that went down the rows) and copied back into row 1;
# acc only goes round and is never computed
RING_COMPUTED = """
entity t is port (aclk : in std_logic; aresetn : in std_logic; s_axis_tdata : in std_logic_vector(11 downto 0);
                  s_axis_tvalid : in std_logic; m_axis_tdata : out std_logic_vector(11 downto 0);
                  m_axis_tvalid : out std_logic); end;
architecture rtl of t is
  signal valid_pipe : std_logic_vector(2 downto 0) := (others => '0');
begin
  m_axis_tvalid <= valid_pipe(2) and strobe_s3;
  m_axis_tdata <= std_logic_vector(y_s3);
  p_valid : process(aclk) begin if rising_edge(aclk) then
    valid_pipe(0) <= s_axis_tvalid;
    valid_pipe(1) <= valid_pipe(0);
    valid_pipe(2) <= valid_pipe(1);
  end if; end process;
  p_s1 : process(aclk) begin if rising_edge(aclk) then
    i_s1 <= signed(s_axis_tdata);
    eta_s1 <= eta_s3;
    mu_s1 <= resize(mu_s3, 8);
    v_s1 <= v_s3;
    acc_s1 <= acc_s3;
  end if; end process;
  p_s2 : process(aclk) begin if rising_edge(aclk) then
    x0_s2 <= i_s1;
    x1_s2 <= x0_s2;
    eta_s2 <= eta_s1;
    mu_s2 <= mu_s1;
    v_s2 <= v_s1;
    acc_s2 <= acc_s1;
  end if; end process;
  p_s3 : process(aclk)
    variable eta_next_v : signed(15 downto 0);
  begin if rising_edge(aclk) then
    if aresetn = '0' then
      eta_s3 <= (others => '0'); v_s3 <= (others => '0'); acc_s3 <= (others => '0');
      mu_s3 <= (others => '0'); strobe_s3 <= '0';
    elsif valid_pipe(1) = '1' then
      y_s3 <= x1_s2 + mu_s2;
      acc_s3 <= acc_s2;
      eta_next_v := eta_s2 - to_signed(8192, 16) - v_s2;
      if eta_next_v < 0 then
        strobe_s3 <= '1';
        eta_s3 <= eta_next_v + to_signed(32767, 16);
        mu_s3 <= resize(eta_s2, 8);
        v_s3 <= v_s2 + resize(y_s3, 16);
      else
        strobe_s3 <= '0';
        eta_s3 <= eta_next_v;
        mu_s3 <= mu_s2;
        v_s3 <= v_s2;
      end if;
    end if;
  end if; end process;
end architecture;
"""


def test_a_loop_value_computed_down_the_rows_and_copied_back_is_reported_as_one_ring():
    """timing_recovery 14:51 and 18:41 (2026-11-03): eta was computed in the
    last row from the copy that had gone down 10 (then 15) rows and copied
    back into row 1, so the design ran 11 (16) loops side by side, one beat
    came per 16 clocks instead of per 4, and no checker said a word because
    the never-computed check skips a ring that is computed."""
    from agents.rtl_agent.lint.rtl_linter import _loop_state_ring_issues, _loop_value_never_computed_issues
    issues = _loop_state_ring_issues(RING_COMPUTED)
    assert [i["code"] for i in issues] == ["loop_state_ring"]
    d = issues[0]["detail"]
    lines = {key: next(n for n, t in enumerate(RING_COMPUTED.splitlines(), 1) if key in t)
             for key in ("eta_s1 <= eta_s3", "mu_s1 <= resize(mu_s3, 8)", "v_s1 <= v_s3")}
    assert d.startswith(f"line {lines['eta_s1 <= eta_s3']}: `eta_s1 <= eta_s3;` copies `eta` from row 3 back into row 1")
    assert (f"the same ring carries `mu` (line {lines['mu_s1 <= resize(mu_s3, 8)']}), "
            f"`v` (line {lines['v_s1 <= v_s3']})") in d
    assert "`acc`" not in d                                   # never computed: the older check owns it
    assert "3 different copies of `eta`" in d and "3 samples ago" in d and "3 separate loops" in d
    # the fix is built by name: eta and v become _r registers, mu stays a carried value
    assert "every `<name>_s<K>` register of `eta`, `v`" in d and "(`eta_r`, `v_r`)" in d and "`p_loop`" in d
    assert "`mu` is decided in `p_loop` in the same update as eta and written as `strobe_s2` / `mu_s2`" in d
    assert "`mu_r`" not in d and "pipeline_stage_map.md, LOOP VALUES" in d and d.endswith(">> eta_s1 <= eta_s3;")
    assert [re.search(r"`(\w+)` only goes round", i["detail"]).group(1)
            for i in _loop_value_never_computed_issues(SPEC_STROBE, RING_COMPUTED)] == ["acc"]
    # the 2026-11-02 ring: eta computed in the last row, strobe and mu copied only -> eta is the ring finding
    only_eta = _loop_state_ring_issues(LOOP_RING)
    assert len(only_eta) == 1 and "`eta_s1 <= eta_s3;` copies `eta` from row 3 back into row 1." in only_eta[0]["detail"]
    assert "`strobe`" not in only_eta[0]["detail"] and "`mu`" not in only_eta[0]["detail"]
    # the pattern the rules teach (eta_r in p_loop, strobe_s2 / mu_s2 from p_loop): nothing
    assert not _loop_state_ring_issues(_design_around_the_rules_p_loop())
    assert not _loop_state_ring_issues(SIX_STAGES)


def _rows(*bodies: str, labels=None, concurrent: str = "", decls: str = "") -> str:
    """One clocked process per body (`p_s1`, `p_s2`, ... or the labels given)
    inside a minimal design; `decls` go before `begin`, `concurrent` before
    the processes."""
    procs = []
    for n, body in enumerate(bodies, 1):
        label = labels[n - 1] if labels else f"p_s{n}"
        inner = "\n".join("    " + l for l in body.strip().splitlines())
        procs.append(f"  {label} : process(aclk) begin if rising_edge(aclk) then\n{inner}\n  end if; end process;")
    return ("entity t is port (aclk : in std_logic; din : in std_logic_vector(11 downto 0)); end;\n"
            "architecture rtl of t is\n" + decls + "begin\n" + concurrent + "\n".join(procs) + "\nend architecture;\n")


def test_the_ring_check_follows_the_value_through_any_name_and_stays_quiet_on_plain_pipelines():
    """2026-11-04 review of the first version: a ring hid behind a second
    statement on a line, a case alternative, a label, a nested index, a
    variable, a wire or register with another name, a renamed row or a
    record field; a delay line numbered the wrong way round inside one
    process, a two-line `if` compare and a `/* */` comment set off wrong
    alarms. The value is now followed through the signals themselves."""
    from agents.rtl_agent.lint.rtl_linter import (
        _loop_state_ring_issues as ring, _loop_value_never_computed_issues as never, _pipeline_stage_skip_issues,
    )
    codes = lambda code: [i["code"] for i in ring(code)]
    chain = ["eta_s2 <= eta_s1;", "eta_s3 <= eta_s2 - w;"]
    # the same ring, written every way a model has written it
    assert codes(_rows("i_s1 <= signed(din); eta_s1 <= eta_s3;", *chain)) == ["loop_state_ring"]       # 2nd statement on a line
    assert codes(_rows("case mode is\n  when NORMAL => eta_s1 <= eta_s3;\n  when others => eta_s1 <= (others => '0');\n"
                       "end case;", *chain)) == ["loop_state_ring"]
    assert codes(_rows("l1: eta_s1 <= eta_s3;", *chain)) == ["loop_state_ring"]
    assert codes(_rows("eta_s1 <= signed(resize(unsigned(eta_s3), 16));", *chain)) == ["loop_state_ring"]
    assert codes(_rows("eta_s1 <= eta_s3 when strobe_s3 = '1' else eta_s1;", *chain)) == ["loop_state_ring"]
    assert codes(_rows("eta_s1 <= eta_s3 - step;", "eta_s2 <= eta_s1;", "eta_s3 <= eta_s2;")) == ["loop_state_ring"]
    assert codes(_rows("mem_s1(to_integer(k)) <= mem_s3(to_integer(k));", "mem_s2 <= mem_s1;",
                       "mem_s3(0) <= mem_s2(0) - w;")) == ["loop_state_ring"]
    assert codes(_rows("eta_s1 <= eta_s3;", "eta_s2 <= eta_s1;",
                       "eta_next_v := eta_s2 - w;\nif eta_next_v < 0 then strobe_s3 <= '1'; else strobe_s3 <= '0'; end if;\n"
                       "eta_s3 <= eta_next_v;")) == ["loop_state_ring"]
    one_process = ("  p_main : process(aclk)\n    variable fb_v : signed(15 downto 0);\n  begin if rising_edge(aclk) then\n"
                   "    fb_v := eta_s3;\n    eta_s1 <= fb_v;\n    eta_s2 <= eta_s1;\n    eta_s3 <= eta_s2 - w;\n"
                   "  end if; end process;\n")
    assert codes(one_process) == ["loop_state_ring"]
    # ... and through another name: a register, a wire, a renamed row, a record
    assert codes(_rows("eta_s1 <= eta_fb;", "eta_s2 <= eta_s1;", "eta_s3 <= eta_s2 - w;\neta_fb <= eta_s3;")) == ["loop_state_ring"]
    assert codes(_rows("eta_s1 <= eta_fb;", *chain, concurrent="  eta_fb <= eta_s3;\n")) == ["loop_state_ring"]
    assert codes(_rows("eta_s1 <= eta_s3;", "eta_s2 <= eta_s1;", "eta_s3 <= tmp;", concurrent="  tmp <= eta_s2 - w;\n")) == ["loop_state_ring"]
    assert codes(_rows("eta_s1 <= phase_s3;", "phase_s2 <= eta_s1;", "phase_s3 <= phase_s2 - w;")) == ["loop_state_ring"]
    assert codes(_rows("pipe_s1.eta <= pipe_s3.eta;", "pipe_s2.eta <= pipe_s1.eta;", "pipe_s3.eta <= pipe_s2.eta - w;")) == ["loop_state_ring"]
    # a value of a later row fed back under another name is the same fault (the y_prev ring of 14:51)
    assert codes(_rows("i_s1 <= signed(din);\nyprev_s1 <= y_s3;", "x_s2 <= i_s1 - yprev_s1;", "y_s3 <= x_s2 + 1;")) == ["loop_state_ring"]
    # a ring re-entering at row 2 names its own rows; an unjoined copy of the same name elsewhere hides nothing
    re_entry = ring(_rows("i_s1 <= signed(din);", "eta_s2 <= eta_s9;", *[f"eta_s{k} <= eta_s{k - 1};" for k in range(3, 9)],
                          "eta_s9 <= eta_s8 - w;"))
    assert len(re_entry) == 1 and "copies `eta` from row 9 back into row 2" in re_entry[0]["detail"]
    assert "8 different copies of `eta`" in re_entry[0]["detail"]
    hidden = ring(_rows("eta_s5 <= eta_s7;", "eta_s6 <= signed(din);", "eta_s7 <= eta_s6 + 1;", "eta_s1 <= eta_s3;", *chain,
                        labels=["p_a", "p_b", "p_c", "p_s1", "p_s2", "p_s3"]))
    assert len(hidden) == 1 and hidden[0]["detail"].split(":")[1].strip().startswith("`eta_s1 <= eta_s3;`")
    # a ring nothing computes that the older check cannot see (re-entry at row 2, a copy through resize) is reported here
    at_row_2 = ring(_rows("i_s1 <= signed(din);", "strobe_s2 <= strobe_s5;", "strobe_s3 <= strobe_s2;", "strobe_s4 <= strobe_s3;",
                          "strobe_s5 <= strobe_s4;\ny_s5 <= i_s1;"))
    assert [i["code"] for i in at_row_2] == ["loop_value_never_computed"] and "no line ever computes it" in at_row_2[0]["detail"]
    assert "`strobe_s2` / `mu_s2`" in at_row_2[0]["detail"]
    assert codes(_rows("mu_s1 <= resize(mu_s3, 8);", "mu_s2 <= mu_s1;", "mu_s3 <= mu_s2;")) == ["loop_value_never_computed"]
    # ... while the plain shape stays with the older check, which now sees both copies on one line
    two_on_a_line = _rows("strobe_s1 <= strobe_s3; mu_s1 <= mu_s3;", "strobe_s2 <= strobe_s1;\nmu_s2 <= mu_s1;",
                          "strobe_s3 <= strobe_s2;\nmu_s3 <= mu_s2;")
    assert not ring(two_on_a_line)
    assert sorted(re.search(r"`(\w+)` only goes round", i["detail"]).group(1)
                  for i in never(SPEC_STROBE, two_on_a_line)) == ["mu", "strobe"]
    # quiet on everything that is not a ring
    assert not codes(_rows("i_s1 <= signed(din);", "x0_s2 <= i_s1;\nx1_s2 <= x0_s2;\nx2_s2 <= x1_s2;", "y_s3 <= x2_s2 + x0_s2;"))
    assert not codes(_rows("a_s1 <= signed(din);", "a_s2 <= a_s1;", "a_s3 <= a_s2;", "y_s4 <= a_s3 + 1;"))
    for taps in (_rows("tap_s1 <= tap_s2;", "tap_s2 <= tap_s3;", "tap_s3 <= signed(din);", "y_s4 <= tap_s1 + tap_s3;"),
                 _rows("tap_s3 <= signed(din);\ntap_s2 <= tap_s3;\ntap_s1 <= tap_s2;\ny_s4 <= tap_s1 + tap_s3;", labels=["p_d"]),
                 _rows("tap_s3 <= signed(din); tap_s2 <= tap_s3; tap_s1 <= tap_s2;\ny_s4 <= tap_s1 + tap_s3;", labels=["p_d"])):
        assert not codes(taps)                               # a delay line numbered the wrong way round never comes back
    assert not codes(_rows("i_s1 <= signed(din);", "eta_s2 <= eta_r;\nx_s2 <= i_s1;", "w_s3 <= eta_s2 + v_r;\ny_s3 <= x_s2;",
                           "if rst = '1' then\n  eta_r <= (others => '0');\n  v_r <= (others => '0');\n"
                           "elsif valid_pipe(0) = '1' then\n  eta_r <= eta_r - W;\n  v_r <= v_r + resize(w_s3, 16);\nend if;",
                           labels=["p_s1", "p_s2", "p_s3", "p_loop"]))                     # rows reading the loop registers
    assert not codes(_rows("if rst = '1' then\n  state_s1 <= IDLE;\n  cnt_s2 <= (others => '0');\nelse\n  cnt_s2 <= cnt_s2 + 1;\n"
                           "  if cnt_s2 = 15 then state_s1 <= RUN; end if;\nend if;", labels=["p_fsm"]))
    assert not codes(_rows("pipe_s1.eta <= signed(din);\npipe_s1.v <= pipe_s1.v;", "mem_s2(3) <= mem_s1(3);\nmem_s2(0) <= signed(din);"))
    assert not codes(_rows("i_s1 <= signed(din);   -- old ring: eta_s1 <= eta_s11; eta_s2 <= eta_s1;",
                           "eta_s2 <= eta_r;\nreport \"ring check: eta_s1 <= eta_s11; eta_s11 <= eta_s10 - w\";", "eta_s3 <= eta_s2 - w;"))
    assert not codes(_rows("i_s1 <= signed(din);\n/* old version:\n   eta_s1 <= eta_s3;\n*/\neta_s1 <= eta_r;", *chain))
    two_line_if = _rows("i_s1 <= signed(din);\neta_s1 <= eta_r;", "eta_s2 <= eta_s1;",
                        "if valid_pipe(1) = '1' and\n   eta_s1 <= eta_s3 then\n  flag_s3 <= '1';\nend if;\neta_s3 <= eta_s2 - w;")
    assert not codes(two_line_if) and not _pipeline_stage_skip_issues(two_line_if)      # a compare, not a write
    assert not codes(_rows("data_s1 <= din;", "data_s2 <= data_s1;",
                           "data_s3(7 downto 0) <= data_s1(7 downto 0);\ndata_s3(11 downto 8) <= data_s2(11 downto 8);"))
    # the stage-skip check reads the second statement on a line too now
    skip = _pipeline_stage_skip_issues(_rows("a_s1 <= signed(din);", "a_s2 <= a_s1;", "b_s3 <= a_s2; c_s3 <= a_s1;"))
    assert [i["code"] for i in skip] == ["pipeline_stage_skip"] and "c_s3 is computed from a_s1" in skip[0]["detail"]


VALID_CHAIN = ("if rst = '1' then\n  valid_pipe <= (others => '0');\nelsif advance = '1' then\n"
               "  valid_pipe(0) <= s_axis_tvalid;\n  valid_pipe(1) <= valid_pipe(0);\n  valid_pipe(2) <= valid_pipe(1);\n"
               "  valid_pipe(3) <= valid_pipe(2);\nend if;")


def _loop_design(gate: str, close: str = "", extra: str = "", with_valid: bool = True) -> str:
    """A 4-row pipeline whose loop process loads `v_r` from row 4 (`e_s4`)
    under the `gate` given (`if ... then`, closed by `close`); eta_r is
    updated under valid_pipe(0) and reads v_r, so v_r is a loop register."""
    sample = "valid_pipe(0) = '1'" if with_valid else "run = '1'"
    loop = (f"if rst = '1' then\n  eta_r <= (others => '0');\n  v_r <= (others => '0');\nelsif advance = '1' then\n"
            f"  if {sample} then\n    eta_r <= eta_r - v_r;\n  end if;\n  {gate}\n"
            f"    v_r <= v_r + resize(e_s4, 16);\n  {close}\nend if;")
    bodies = [VALID_CHAIN] if with_valid else []
    bodies += ["i_s1 <= signed(din);", "x_s2 <= i_s1 + resize(eta_r, 12);", "y_s3 <= x_s2 - 1;",
               "e_s4 <= y_s3 * y_s3;", loop]
    labels = (["p_valid"] if with_valid else []) + ["p_s1", "p_s2", "p_s3", "p_s4", "p_loop"]
    return _rows(*bodies, labels=labels, concurrent=extra)


def test_a_loop_register_loaded_before_its_row_is_valid_is_reported_with_the_bit_to_wait_for():
    """timing_recovery 18:41 (2026-11-03): p_s16 updated eta, v and acc from
    row 15 under no valid bit at all; at the first clocks row 15 was still
    'U', 13 of 16 loop copies went 'X' and never recovered, one beat came
    per 16 clocks. 14:51 waited for the right bit but its history chain was
    not reset: the other half of the same fault."""
    from agents.rtl_agent.lint.rtl_linter import _history_chain_reset_issues, _state_read_before_valid_issues
    no_gate = _state_read_before_valid_issues(_loop_design(""))
    assert [i["code"] for i in no_gate] == ["state_read_before_valid"]
    d = no_gate[0]["detail"]
    line = next(n for n, t in enumerate(_loop_design("").splitlines(), 1) if "v_r <= v_r + resize(e_s4, 16);" in t)
    assert d.startswith(f"line {line}: `v_r <= v_r + resize(e_s4, 16);` in `p_loop` loads the loop register `v_r` "
                        f"from row 4 (`e_s4`) without any valid gate")
    assert "if valid_pipe(3) = '1' then v_r <= ...; end if;" in d and "and strobe_s4 = '1'" in d
    assert "never recovers" in d and d.endswith(">> v_r <= v_r + resize(e_s4, 16);")
    early = _state_read_before_valid_issues(_loop_design("if valid_pipe(1) = '1' then", "end if;"))
    assert [i["code"] for i in early] == ["state_read_before_valid"]
    assert "under valid_pipe(1) only, the bit of row 2" in early[0]["detail"] and "valid_pipe(3)" in early[0]["detail"]
    # the right bit, with or without the strobe: nothing
    assert not _state_read_before_valid_issues(_loop_design("if valid_pipe(3) = '1' then", "end if;"))
    assert not _state_read_before_valid_issues(_loop_design("if valid_pipe(3) = '1' and strobe_s4 = '1' then", "end if;"))
    # a later bit stores a younger sample's value
    late = _state_read_before_valid_issues(_loop_design("if valid_pipe(5) = '1' then", "end if;"))
    assert [i["code"] for i in late] == ["state_read_after_valid"]
    assert "under valid_pipe(5), the bit of row 6" in late[0]["detail"] and "2 tick(s) younger" in late[0]["detail"]
    assert "valid_pipe(3)" in late[0]["detail"]
    # a reset register that only feeds a port (an output register) is not a loop register
    out_reg = _loop_design("if valid_pipe(3) = '1' then", "end if;",
                           extra="  m_axis_tdata <= m_axis_tdata_reg;\n  p_o : process(aclk) begin if rising_edge(aclk) then\n"
                                 "    if rst = '1' then m_axis_tdata_reg <= (others => '0');\n"
                                 "    elsif advance = '1' then m_axis_tdata_reg <= std_logic_vector(y_s3); end if;\n"
                                 "  end if; end process;\n")
    assert not _state_read_before_valid_issues(out_reg)
    # no valid chain at all: nothing to wait for (the dry run judges that design)
    assert not _state_read_before_valid_issues(_loop_design("", with_valid=False))
    # the old shape: the loop row's else branch reads row 2 under no bit -> reported; RING_COMPUTED waits right
    old = _rows("i_s1 <= signed(din);\nvalid_pipe(0) <= s_axis_tvalid;", "x_s2 <= i_s1;",
                "if aresetn = '0' then\n  acc_s3 <= (others => '0');\nelse\n  acc_s3 <= acc_s3 + x_s2;\nend if;",
                "y_s4 <= acc_s3;")
    assert [i["code"] for i in _state_read_before_valid_issues(old)] == ["state_read_before_valid"]
    assert "`acc_s3 <= acc_s3 + x_s2;` in `p_s3`" in _state_read_before_valid_issues(old)[0]["detail"]
    assert not _state_read_before_valid_issues(RING_COMPUTED)
    # the design the rules teach waits for the right bit everywhere, and its chain is reset
    assert not _state_read_before_valid_issues(_design_around_the_rules_p_loop())
    assert not _history_chain_reset_issues(_design_around_the_rules_p_loop())
    # a history chain without reset: the taps are 'U' for as many samples as the chain is long
    chain = _rows("i_s1 <= signed(din);", "x0_s2 <= i_s1;\nx1_s2 <= x0_s2;\nx2_s2 <= x1_s2;", "y_s3 <= x2_s2 + x0_s2;")
    found = _history_chain_reset_issues(chain)
    assert [i["code"] for i in found] == ["history_chain_not_reset"]
    chain_line = next(n for n, t in enumerate(chain.splitlines(), 1) if "x1_s2 <= x0_s2;" in t)
    assert found[0]["detail"].startswith(f"line {chain_line}: the history chain in row 2 (`x0_s2`, `x1_s2`, `x2_s2`) has no reset")
    assert "`x0_s2`, `x1_s2`, `x2_s2` start undefined" in found[0]["detail"]
    assert "if rst = '1' then x0_s2 <= (others => '0'); ... elsif advance = '1' and valid_pipe(1) = '1' then" in found[0]["detail"]
    partly = _rows("i_s1 <= signed(din);",
                   "if rst = '1' then\n  x1_s2 <= (others => '0');\n  x2_s2 <= (others => '0');\n"
                   "elsif advance = '1' and valid_pipe(0) = '1' then\n  x0_s2 <= i_s1;\n  x1_s2 <= x0_s2;\n  x2_s2 <= x1_s2;\nend if;",
                   "y_s3 <= x2_s2 + x0_s2;")
    assert "`x0_s2` start undefined" in _history_chain_reset_issues(partly)[0]["detail"]      # the head counts too
    whole = partly.replace("  x1_s2 <= (others => '0');\n", "  x0_s2 <= (others => '0');\n  x1_s2 <= (others => '0');\n")
    assert not _history_chain_reset_issues(whole)
    initial = _rows("i_s1 <= signed(din);", "x0_s2 <= i_s1;\nx1_s2 <= x0_s2;\nx2_s2 <= x1_s2;", "y_s3 <= x2_s2 + x0_s2;",
                    decls="  signal x0_s2, x1_s2, x2_s2 : signed(11 downto 0) := (others => '0');\n")
    assert not _history_chain_reset_issues(initial)
    # plain forward copies and the valid chain are not history chains
    assert not _history_chain_reset_issues(SIX_STAGES) and not _history_chain_reset_issues(_loop_design(""))


def _design_around_the_rules_p_loop() -> str:
    """The p_loop process exactly as skills/RTL/pipeline_stage_map.md shows
    it, inside the smallest design that has every row it reads: a reset
    history chain in s2, copy rows s3..s7 standing in for the Farrow
    interpolator, d from yprev_*_r in s8, and the error rows s9..s10 that
    carry y_sym and mu along, so the output row is the last row s10."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "skills" / "RTL" / "pipeline_stage_map.md").read_text(encoding="utf-8")
    found = re.search(r"^( *)p_loop : process\(aclk\).*?end process p_loop;", text, re.DOTALL | re.MULTILINE)
    p_loop = "\n".join("  " + line[len(found.group(1)):] for line in found.group(0).splitlines())
    lanes = ("ysym_i", "ysym_q", "ymid_i", "ymid_q")
    decls = "".join(f"  signal strobe_s{k} : std_logic;\n" for k in range(2, 11))
    decls += "".join(f"  signal mu_s{k} : signed(MU_W-1 downto 0);\n" for k in range(2, 11))
    decls += "".join(f"  signal {', '.join(f'{n}_s{k}' for n in lanes)} : signed(X_W-1 downto 0);\n" for k in range(3, 9))
    decls += "".join(f"  signal ysym_i_s{k}, ysym_q_s{k} : signed(X_W-1 downto 0);\n" for k in (9, 10))

    def row(k: int, body: str) -> str:
        return (f"  p_s{k} : process(aclk)\n  begin\n    if rising_edge(aclk) then\n      if advance = '1' then\n"
                f"{body}      end if;\n    end if;\n  end process p_s{k};\n")

    carry = lambda k: (f"        strobe_s{k} <= strobe_s{k - 1};\n        mu_s{k} <= mu_s{k - 1};\n"
                       + ("".join(f"        {n}_s{k} <= {n}_s{k - 1};\n" for n in ("ysym_i", "ysym_q")) if k >= 9 else ""))
    first = {"ysym_i": "x0_i_s2", "ysym_q": "x0_q_s2", "ymid_i": "x1_i_s2", "ymid_q": "x1_q_s2"}
    rows = row(3, "".join(f"        {n}_s3 <= {first[n]};\n" for n in lanes) + carry(3))
    for k in range(4, 9):
        rows += row(k, "".join(f"        {n}_s{k} <= {n}_s{k - 1};\n" for n in lanes) + carry(k)
                    + ("        d_i_s8 <= resize(ysym_i_s7, D_W) - resize(yprev_i_r, D_W);\n"
                       "        d_q_s8 <= resize(ysym_q_s7, D_W) - resize(yprev_q_r, D_W);\n" if k == 8 else ""))
    rows += row(9, "        pr_i_s9 <= ymid_i_s8 * d_i_s8;\n        pr_q_s9 <= ymid_q_s8 * d_q_s8;\n" + carry(9))
    rows += row(10, "        e_s10 <= resize(pr_i_s9, E_W) + resize(pr_q_s9, E_W);\n" + carry(10))
    shifts = "".join(f"        valid_pipe({k}) <= valid_pipe({k - 1});\n" for k in range(1, 10))
    return f"""library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity t is
  port (aclk : in std_logic; aresetn : in std_logic;
        s_axis_tvalid : in std_logic; s_axis_tdata : in std_logic_vector(23 downto 0);
        m_axis_tvalid : out std_logic; m_axis_tdata : out std_logic_vector(23 downto 0));
end entity t;

architecture rtl of t is
  constant X_W     : integer := 12;
  constant D_W     : integer := X_W + 1;
  constant PR_W    : integer := X_W + D_W;
  constant E_W     : integer := PR_W + 1;
  constant MU_F    : integer := 16;
  constant MU_W    : integer := MU_F + 2;
  constant MU_ONE  : integer := 2**MU_F;
  constant ETA_F   : integer := 30;
  constant ETA_W   : integer := ETA_F + 2;
  constant ONE     : signed(ETA_W-1 downto 0) := shift_left(to_signed(1, ETA_W), ETA_F);
  constant QUARTER : signed(ETA_W-1 downto 0) := shift_left(to_signed(1, ETA_W), ETA_F - 2);
  signal rst, advance : std_logic;
  signal valid_pipe : std_logic_vector(9 downto 0) := (others => '0');
  signal eta_r, acc_r, v_r : signed(ETA_W-1 downto 0);
  signal yprev_i_r, yprev_q_r : signed(X_W-1 downto 0);
  signal i_s1, q_s1 : signed(X_W-1 downto 0);
  signal x0_i_s2, x1_i_s2, x0_q_s2, x1_q_s2 : signed(X_W-1 downto 0);
{decls}  signal d_i_s8, d_q_s8 : signed(D_W-1 downto 0);
  signal pr_i_s9, pr_q_s9 : signed(PR_W-1 downto 0);
  signal e_s10 : signed(E_W-1 downto 0);
begin
  rst     <= not aresetn;
  advance <= '1';

  p_valid : process(aclk)
  begin
    if rising_edge(aclk) then
      if rst = '1' then
        valid_pipe <= (others => '0');
      elsif advance = '1' then
        valid_pipe(0) <= s_axis_tvalid;
{shifts}      end if;
    end if;
  end process p_valid;

{p_loop}

  p_s1 : process(aclk)
  begin
    if rising_edge(aclk) then
      if advance = '1' then
        i_s1 <= signed(s_axis_tdata(23 downto 12));
        q_s1 <= signed(s_axis_tdata(11 downto 0));
      end if;
    end if;
  end process p_s1;

  p_s2 : process(aclk)
  begin
    if rising_edge(aclk) then
      if rst = '1' then
        x0_i_s2 <= (others => '0');
        x1_i_s2 <= (others => '0');
        x0_q_s2 <= (others => '0');
        x1_q_s2 <= (others => '0');
      elsif advance = '1' and valid_pipe(0) = '1' then
        x0_i_s2 <= i_s1;
        x1_i_s2 <= x0_i_s2;
        x0_q_s2 <= q_s1;
        x1_q_s2 <= x0_q_s2;
      end if;
    end if;
  end process p_s2;

{rows}
  m_axis_tdata  <= std_logic_vector(ysym_i_s10) & std_logic_vector(ysym_q_s10);
  m_axis_tvalid <= valid_pipe(9) and strobe_s10;
end architecture rtl;
"""


def test_the_loop_process_the_rules_teach_passes_every_loop_and_stage_check():
    """2026-11-04: the RTL rules replaced the ring (eta, acc, v, y_prev
    copied down 11 rows and back into row 1, which runs 11 separate loops)
    with one process p_loop that updates eta every input sample and writes
    strobe_s2 / mu_s2. The full timing_recovery design built that way
    compiled, passed the smoke dry run and locked at +100 and -1000 ppm.
    This keeps the checker from ever rejecting the pattern the rules teach."""
    from agents.rtl_agent.lint import rtl_linter as lint
    code = _design_around_the_rules_p_loop()
    assert "eta_r <= eta_next;" in code and "strobe_s2 <= '1';" in code        # the rules' p_loop is in it
    checks = [lint._process_declaration_issues(code), lint._declaration_after_begin_issues(code),
              lint._unbalanced_parentheses_issues(code), lint._block_balance_issues(code),
              lint._width_issues(code), lint._literal_width_issues(code), lint._multiple_driver_issues(code),
              lint._pipeline_stage_skip_issues(code), lint._stage_register_declaration_issues(code),
              lint._valid_data_length_issues(code), lint._constant_output_valid_issues(SPEC_STROBE, code),
              lint._loop_value_never_computed_issues(SPEC_STROBE, code), lint._loop_state_ring_issues(code),
              lint._state_read_before_valid_issues(code), lint._history_chain_reset_issues(code),
              lint._output_valid_without_strobe_issues(SPEC_STROBE, code),
              lint._self_blocking_advance_issues(SPEC_STROBE, code)]
    assert [i for found in checks for i in found] == []


SPEC_NO_TREADY = {
    "entity_name": "timing_recovery",
    "ports": [{"name": "aclk", "direction": "input", "width": "1"}, {"name": "aresetn", "direction": "input", "width": "1"},
              {"name": "s_axis_tvalid", "direction": "input", "width": "1"}, {"name": "s_axis_tready", "direction": "output", "width": "1"},
              {"name": "s_axis_tdata", "direction": "input", "width": "24"}, {"name": "m_axis_tvalid", "direction": "output", "width": "1"},
              {"name": "m_axis_tdata", "direction": "output", "width": "24"}, {"name": "m_axis_tuser", "direction": "output", "width": "8"}],
    "behavior": ["No m_axis_tready port exists; downstream is always ready."],
}

_ADVANCE_DESIGN = """architecture rtl of timing_recovery is
  signal advance : std_logic;
  signal valid_pipe : std_logic_vector(9 downto 0) := (others => '0');
begin
  rst <= not aresetn;
  {advance_line}
  s_axis_tready <= '1';
  p_valid : process(aclk) begin if rising_edge(aclk) then if rst = '1' then valid_pipe <= (others => '0');
    elsif advance = '1' then valid_pipe(0) <= s_axis_tvalid; valid_pipe(9) <= valid_pipe(8); end if; end if; end process;
  m_axis_tvalid_i <= valid_pipe(9) and strobe_s10;
end architecture rtl;
"""


def test_linter_rejects_a_pipeline_enable_that_waits_for_its_own_valid_bit():
    """timing_recovery 18:52 (2026-11-06): the spec has no m_axis_tready port,
    the rewrite turned attempt 1's `advance <= s_axis_tvalid;` into the
    recipe's handshake line minus the missing ready term, `advance <= not
    valid_pipe(9);`. Once the pipe was full (cycle 14) advance stayed '0':
    every register froze, m_axis_tvalid stuck at '1' with one constant value,
    801 mismatches, the same picture for three attempts. The checker now
    names the line and the one-line fix before any simulation."""
    from agents.rtl_agent.lint.rtl_linter import _self_blocking_advance_issues
    design = _ADVANCE_DESIGN.format

    [issue] = _self_blocking_advance_issues(SPEC_NO_TREADY, design(advance_line="advance <= not valid_pipe(9);"))
    assert issue["code"] == "self_blocking_advance"
    assert issue["detail"].startswith("line 6: the pipeline enable `advance` is computed from the pipeline's own valid flag valid_pipe(9)")
    # the fix is a pipeline that moves on every clock (equalizer 2026-11-12: `advance <= s_axis_tvalid;` never emptied)
    assert "`advance <= '1';`" in issue["detail"] and "`valid_pipe(0) <= s_axis_tvalid;`" in issue["detail"]
    assert "`advance <= s_axis_tvalid;`" not in issue["detail"] and "801 mismatches" in issue["detail"]
    assert issue["detail"].endswith(">> advance <= not valid_pipe(9);")

    # the output flag instead of the bare bit, or a when/else form: the same freeze
    [issue] = _self_blocking_advance_issues(SPEC_NO_TREADY, design(advance_line="advance <= not m_axis_tvalid_i;"))
    assert "own valid flag m_axis_tvalid_i" in issue["detail"]
    [issue] = _self_blocking_advance_issues(SPEC_NO_TREADY, design(advance_line="advance <= '1' when valid_pipe(9) = '0' else '0';"))
    assert issue["code"] == "self_blocking_advance"

    # attempt 1's line, and a free-running '1', never wait for the pipe itself
    assert _self_blocking_advance_issues(SPEC_NO_TREADY, design(advance_line="advance <= s_axis_tvalid;")) == []
    assert _self_blocking_advance_issues(SPEC_NO_TREADY, design(advance_line="advance <= '1';")) == []

    # with a downstream ready port the recipe's handshake line passes; the bare bit gets the ready term as the fix
    with_ready = {**SPEC_NO_TREADY, "ports": SPEC_NO_TREADY["ports"] + [{"name": "m_axis_tready", "direction": "input", "width": "1"}]}
    assert _self_blocking_advance_issues(with_ready, design(advance_line="advance <= m_axis_tready or (not valid_pipe(9));")) == []
    [issue] = _self_blocking_advance_issues(with_ready, design(advance_line="advance <= not valid_pipe(9);"))
    assert "`advance <= m_axis_tready or not valid_pipe(9);`" in issue["detail"]

    # an enable under another name is found through the `if <name> = '1'` guards of the processes
    renamed = design(advance_line="advance <= not valid_pipe(9);").replace("advance", "pipe_en")
    [issue] = _self_blocking_advance_issues(SPEC_NO_TREADY, renamed)
    assert "`pipe_en`" in issue["detail"] and "`pipe_en <= '1';`" in issue["detail"]

    # the real file of the 18:52 rewrite is caught through lint_rtl's static list, the attempt-1 file is not
    from agents.rtl_agent.lint import rtl_linter as lint
    real = ("architecture rtl of timing_recovery is\nbegin\n  rst <= not aresetn;\n  advance <= not valid_pipe(9);\n"
            "  s_axis_tready <= '1';\n  m_axis_tvalid_i <= valid_pipe(9) and strobe_s10;\nend architecture rtl;\n")
    assert [i["code"] for i in lint._self_blocking_advance_issues(SPEC_NO_TREADY, real)] == ["self_blocking_advance"]
    assert lint._self_blocking_advance_issues(SPEC_NO_TREADY, real.replace("not valid_pipe(9)", "s_axis_tvalid")) == []


def test_linter_requires_the_output_valid_to_carry_the_strobe_when_the_spec_says_output_on_strobe():
    """timing_recovery 18:32 (2026-11-02): m_axis_tvalid_i <= valid_pipe(10)
    raised a beat on every input sample; the testbench expected 198 strobe
    beats, saw ~785, and reported 998 mismatches with every beat "got 0"."""
    from agents.rtl_agent.lint.rtl_linter import _output_valid_without_strobe_issues, _valid_data_length_issues
    issues = _output_valid_without_strobe_issues(SPEC_STROBE, LOOP_RING)
    assert [i["code"] for i in issues] == ["output_valid_without_strobe"]
    d = issues[0]["detail"]
    line = next(n for n, t in enumerate(LOOP_RING.splitlines(), 1) if "m_axis_tvalid_i <= valid_pipe(2);" in t)
    assert d.startswith(f"line {line}: `m_axis_tvalid_i <= valid_pipe(2);` raises an output beat for EVERY input sample")
    assert '"Output y_sym and mu on strobe."' in d
    assert "Write `m_axis_tvalid_i <= valid_pipe(2) and strobe_s3;`" in d      # the strobe bit of the output row (s3)
    gated = LOOP_RING.replace("m_axis_tvalid_i <= valid_pipe(2);", "m_axis_tvalid_i <= valid_pipe(2) and strobe_s3;")
    assert not _output_valid_without_strobe_issues(SPEC_STROBE, gated)
    assert not _valid_data_length_issues(gated)                 # the gated flag still counts as bit 2 of 3
    by_if = LOOP_RING.replace("  m_axis_tvalid_i <= valid_pipe(2);\n",
                              "  p_o : process(aclk) begin if rising_edge(aclk) then\n"
                              "    if strobe_s3 = '1' then m_axis_tvalid_i <= valid_pipe(2); else m_axis_tvalid_i <= '0'; end if;\n"
                              "  end if; end process;\n")
    assert not _output_valid_without_strobe_issues(SPEC_STROBE, by_if)
    # a spec with no strobe (one beat out per beat in): the bare bit is right
    plain = {**SPEC_STROBE, "behavior": ["Output y_sym every sample."], "constraints": []}
    assert not _output_valid_without_strobe_issues(plain, LOOP_RING)
    assert not _output_valid_without_strobe_issues(plain, SIX_STAGES)


CALC_MMSB = """
  function calc_mmsb(x : signed) return signed is
    variable abs_x : signed(12 downto 0);
    variable res : signed(15 downto 0);
  begin
    abs_x := abs(resize(x, 13));
    if abs_x <= to_signed(512, 13) then
      res := resize(x, 16) * to_signed(4, 16);
    elsif x > 0 then
      res := resize(x, 16) * to_signed(8, 16) - to_signed(2048, 16);
    else
      res := shift_left(resize(x, 16), 3) + to_signed(2048, 16);
    end if;
    return res;
  end function;
"""


def test_linter_sees_width_mistakes_inside_if_branches_and_functions():
    """llr_demap16, 2026-10-29 (fourth run): `res := resize(x, 16) * to_signed(4, 16)`
    inside calc_mmsb's if-branch makes a 32-bit value for a 16-bit variable.
    The multiplier check only looked after ';', the dry run named only the
    process, and three attempts returned the same file."""
    from agents.rtl_agent.lint.rtl_linter import (
        _assignment_width_issues, _multiplier_width_issues, _power_of_two_multiply_issues, _width_issues,
    )
    mult = _multiplier_width_issues(CALC_MMSB)
    assert [i["code"] for i in mult] == ["multiplier_width"] and "line 8" in mult[0]["detail"]   # after 'then'
    pow2 = _power_of_two_multiply_issues(CALC_MMSB)
    assert [i["code"] for i in pow2] == ["power_of_two_multiply"] * 2
    assert "line 8" in pow2[0]["detail"] and "shift_left(resize(x, res'length), 2)" in pow2[0]["detail"]
    assert "line 10" in pow2[1]["detail"]
    # the sum/difference line: 32-bit product minus 16 bits is 32 bits into 16 (reported once, not twice)
    width = _assignment_width_issues(CALC_MMSB, {8})
    assert [i["code"] for i in width] == ["assignment_width"] and "line 10" in width[0]["detail"]
    assert "'res' is 16 bits but the right-hand side is 32 bits" in width[0]["detail"]
    # the shift line (12) is 16 bits both sides: clean
    assert not _assignment_width_issues(CALC_MMSB, {8, 10})
    # together, each bad line is explained once, by the message that carries the fix
    assert [(i["code"], i["detail"][:8]) for i in _width_issues(CALC_MMSB)] == \
        [("power_of_two_multiply", "line 8: "), ("power_of_two_multiply", "line 10:")]


def test_dry_run_crash_names_the_assignment_behind_an_array_size_mismatch():
    from agents.rtl_agent.lint import rtl_smoke_check as smoke
    out = ("ERROR: Array sizes do not match, left array has 16 elements, right array has 32 elements\n"
           "Time: 55 ns  Iteration: 1  Process: /smoke_tb/dut/line__107  File: x.vhd")
    code = "x\n" * 106 + "process(aclk)\n" + CALC_MMSB
    text = smoke._crash_excerpt(out, code)
    assert "[in the process starting at RTL line 107: `process(aclk)`]" in text
    assert "16-bit target given a 32-bit value" in text and "line 115: `res := resize(x, 16) * to_signed(4, 16);`" in text
    assert "line 117:" in text                                            # the sum line too
    assert "line 119" not in text                                         # the correct shift line is not blamed


# timing_recovery 11:10 (2026-11-03) in small: every width is a constant, so the
# old width check saw nothing; the 16-bit v_s12 is given a 24-bit sum
CONSTANT_WIDTHS = """
entity t is
  generic (ACC_BITS : integer := 24);
  port (aclk : in std_logic; s_axis_tdata : in std_logic_vector(23 downto 0));
end;
architecture rtl of t is
  constant E_BITS : integer := 24;
  constant V_BITS : integer := 16;
  constant P_BITS : integer := 2*E_BITS;
  signal e_s11 : signed(E_BITS-1 downto 0);
  signal acc_s11 : signed(ACC_BITS-1 downto 0);
  signal v_s12 : signed(V_BITS-1 downto 0);
  signal p : signed(P_BITS-1 downto 0);
  signal q : signed(E_BITS downto 0);
  signal unknown : signed(MYSTERY-1 downto 0);
begin
  p_s12 : process(aclk) begin if rising_edge(aclk) then
    v_s12 <= shift_right(e_s11, 7) + acc_s11;
  end if; end process;
end architecture;
"""


def test_width_checks_read_widths_given_as_constants_and_generics():
    """timing_recovery 11:10 (2026-11-03): `signed(V_BITS-1 downto 0)` everywhere,
    so the static width check never fired and the dry run crashed six rounds
    in a row with only "16 vs 24 in p_s12"; the model returned the same file."""
    from agents.rtl_agent.lint.rtl_linter import _assignment_width_issues
    from agents.rtl_agent.lint.rtl_smoke_check import _width_mismatch_hint
    from agents.rtl_agent.lint.rtl_widths import declared_widths, expr_width, int_value, scoped_widths
    w = scoped_widths(CONSTANT_WIDTHS).at(1)
    assert (w["e_s11"], w["acc_s11"], w["v_s12"], w["p"], w["q"]) == (24, 24, 16, 48, 25)
    assert "unknown" not in w and w.constants["v_bits"] == 16 and w.types["v_s12"] == "signed(V_BITS-1 downto 0)"
    assert declared_widths(CONSTANT_WIDTHS)["p"] == 48
    assert int_value("2**4 - 1", {}) == 15 and int_value("W-1", {"w": 8}) == 7 and int_value("x'length", {}) is None
    assert expr_width("resize(e_s11, V_BITS)", w) == 16 and expr_width("e_s11(E_BITS-1 downto 8)", w) == 16
    assert expr_width("e_s11 + V_BITS", w) == 24                     # an integer constant is like an integer literal
    issues = _assignment_width_issues(CONSTANT_WIDTHS, set())
    line = next(n for n, t in enumerate(CONSTANT_WIDTHS.splitlines(), 1) if "v_s12 <= shift_right" in t)
    assert [i["code"] for i in issues] == ["assignment_width"]
    assert f"line {line}: 'v_s12' is 16 bits but the right-hand side is 24 bits" in issues[0]["detail"]
    assert "resize(<that>, 16)" in issues[0]["detail"]
    hint = _width_mismatch_hint("ERROR: Array sizes do not match, left array has 16 elements, right array has 24 elements",
                                CONSTANT_WIDTHS)
    assert f"line {line}:" in hint


LITERAL_WIDTHS = """
entity t is
  port (aclk : in std_logic);
end;
architecture rtl of t is
  constant E_BITS : integer := 16;
  constant ONE : integer := 2**16;
  constant W0 : signed(15 downto 0) := to_signed(16384, 16);
  signal eta_s8 : signed(E_BITS-1 downto 0);
  signal cnt : unsigned(7 downto 0);
  signal word : std_logic_vector(7 downto 0);
  signal flag : std_logic;
begin
  p_s8 : process(aclk)
    variable eta_n : signed(15 downto 0);
    variable mu_raw : signed(15 downto 0);
  begin
    if rising_edge(aclk) then
      eta_n := eta_s8 - W0;
      mu_raw := shift_left(eta_s8, 2);
      if mu_raw > 65535 then
        flag <= '1';
      elsif mu_raw < -32768 then
        flag <= '0';
      end if;
      if cnt >= 256 then
        cnt <= to_unsigned(255, 8);
      elsif cnt <= 300 and word = "00000000" then
        cnt <= (others => '0');
      end if;
      if eta_n < 0 then
        eta_s8 <= eta_n + to_signed(65536, 16);
      else
        eta_s8 <= eta_n + to_signed(ONE, E_BITS);
      end if;
    end if;
  end process p_s8;
end architecture;
"""


def test_a_number_that_does_not_fit_its_width_is_reported_with_the_register_to_widen():
    """timing_recovery 12:30 (2026-11-03): `eta_s8 <= eta_n + to_signed(65536, 16)`
    was meant to add 1.0 to the NCO phase; numeric_std kept the low 16 bits
    (0) and only warned, the phase never wrapped back, the strobe count was
    wrong for four attempts and the testbench was blamed. `if mu_raw > 65535`
    in the same file could never be true."""
    from agents.rtl_agent.lint.rtl_linter import _literal_width_issues
    issues = _literal_width_issues(LITERAL_WIDTHS)
    assert [i["code"] for i in issues] == ["literal_width"] * 5
    by_line = {int(re.match(r"line (\d+):", i["detail"]).group(1)): i["detail"] for i in issues}
    lines = {n: t.strip() for n, t in enumerate(LITERAL_WIDTHS.splitlines(), 1)}

    def detail(key):
        n = next(n for n, t in lines.items() if key in t)
        assert n in by_line, (key, n, sorted(by_line))
        return by_line[n]

    sixty = detail("to_signed(65536, 16)")
    assert "65536 does not fit in 16 signed bits (range -32768..32767)" in sixty
    assert "silently becomes 0" in sixty and "needs at least 18 signed bits" in sixty
    assert "widen `eta_s8` (`signed(E_BITS-1 downto 0)`)" in sixty and "`signed(17 downto 0)`" in sixty
    assert "1.0 = 2**14 = 16384" in sixty and "Do not just change the number" in sixty
    assert "`to_signed(ONE, E_BITS)`: 65536 does not fit in 16 signed bits" in detail("to_signed(ONE, E_BITS)")

    gt = detail("mu_raw > 65535")
    assert "`mu_raw > 65535` is never true" in gt and "16 signed bits (range -32768..32767)" in gt and "dead" in gt
    ge = detail("cnt >= 256")
    assert "`cnt >= 256` is never true" in ge and "8 unsigned bits (range 0..255)" in ge
    le = detail("cnt <= 300")
    assert "`cnt <= 300` is always true" in le and "taken every time" in le
    # in range, a std_logic_vector, a signal on the right: no finding
    assert not any("-32768" in i["detail"] and "mu_raw <" in i["detail"] for i in issues)
    assert _literal_width_issues("signal a : signed(7 downto 0);\nsignal b : unsigned(3 downto 0);\n"
                                 "if a > b then a <= to_signed(-128, 8); end if;\n"
                                 "if b = 15 then b <= to_unsigned(15, 4); end if;\n"
                                 "if a'length > 65535 then b <= to_unsigned(x, 4); end if;") == []


def test_width_fix_follows_the_value_to_the_register_it_is_stored_in():
    """timing_recovery 11:17 (2026-11-03): the check told `v_next` (17 bits) to
    resize to 17, but v_next then went into the 16-bit v_s12 and the next
    round crashed on that line. Now the advice names the final register's
    width, and a plain name that is too wide gets the one-line fix."""
    from agents.rtl_agent.lint.rtl_linter import _assignment_width_issues
    code = (CONSTANT_WIDTHS
            .replace("p_s12 : process(aclk) begin",
                     "p_s12 : process(aclk)\n    variable v_sum : signed(24 downto 0);\n"
                     "    variable v_next : signed(16 downto 0);\n  begin")
            .replace("    v_s12 <= shift_right(e_s11, 7) + acc_s11;\n",
                     "    v_sum := resize(shift_right(e_s11, 7), 25) + resize(acc_s11, 25);\n"
                     "    v_next := v_sum;\n"
                     "    v_s12 <= v_next;\n"))
    issues = _assignment_width_issues(code, set())
    assert [i["code"] for i in issues] == ["assignment_width"] * 2
    first, second = (i["detail"] for i in issues)
    assert "'v_next' is 17 bits but the right-hand side is 25 bits" in first
    assert "write `v_next := resize(v_sum, 16);` on this line" in first
    assert "16, not 17, because `v_next` is stored in `v_s12` (16 bits, line" in first
    assert "declare `v_next` as `signed(V_BITS-1 downto 0)` like `v_s12`" in first
    assert "'v_s12' is 16 bits but the right-hand side is 17 bits" in second
    assert "write `v_s12 <= resize(v_next, 16);` on this line" in second
    assert "declare it as `signed(V_BITS-1 downto 0)` instead" in second and "`v_next := v_sum` -> resize to 16 there" in second
    # the helper declared with the register's width name, as the recipe says: nothing to report
    fixed = code.replace("variable v_next : signed(16 downto 0)", "variable v_next : signed(V_BITS-1 downto 0)") \
                .replace("v_next := v_sum;", "v_next := resize(v_sum, V_BITS);")
    assert not _assignment_width_issues(fixed, set())


def test_width_arithmetic_of_simple_expressions():
    from agents.rtl_agent.lint.rtl_widths import expr_width
    w = {"a": 12, "b": 8, "p": 20}
    assert expr_width("a * b", w) == 20 and expr_width("a * 4", w) == 24 and expr_width("4 * a", w) == 24
    assert expr_width("a + b", w) == 12 and expr_width("a - 1", w) == 12 and expr_width("-a", w) == 12
    assert expr_width("to_signed(2048, 16) - (resize(a, 16) * to_signed(4, 16))", w) == 32
    assert expr_width("shift_right(p + to_signed(4096, 20), 13)", w) == 20
    assert expr_width("abs(resize(a, 13))", w) == 13 and expr_width("abs resize(a, 13)", w) == 13
    assert expr_width("signed('0' & b)", w) == 9 and expr_width("a(11 downto 4)", w) == 8
    assert expr_width("(others => '0')", w) is None and expr_width("f(a)", w) is None
    assert expr_width("a when en = '1' else b", w) is None and expr_width("a * b * a", w) is None


def test_linter_rejects_an_absolute_value_written_as_a_bare_negation():
    """llr_demap16, 2026-10-29 13:23: `abs_i_s2 <= unsigned(-resize(i_s1, 13));`
    with no sign test (copied from the recipe's own example) negated positive
    inputs too; every m_lsb was wrong ("expected lo=1560 got lo=2080")."""
    from agents.rtl_agent.lint.rtl_linter import _abs_without_sign_check_issues
    bare = "valid_pipe(0) <= s_axis_tvalid;\nabs_i_s2 <= unsigned(-resize(i_s1, 13));\nabs_q_s2 <= unsigned(-q_s1);"
    issues = _abs_without_sign_check_issues(bare)
    assert [i["code"] for i in issues] == ["abs_without_sign_check"] * 2
    assert "line 2" in issues[0]["detail"] and "unsigned(abs(resize(i_s1, abs_i_s2'length)))" in issues[0]["detail"]
    # the correct forms are accepted
    assert not _abs_without_sign_check_issues("abs_i_s2 <= unsigned(abs(resize(i_s1, 13)));")
    assert not _abs_without_sign_check_issues(
        "if i_s1 < 0 then abs_i_s2 <= unsigned(-resize(i_s1, 13)); else abs_i_s2 <= unsigned(resize(i_s1, 13)); end if;")
    assert not _abs_without_sign_check_issues("abs_i <= unsigned(-x) when x < 0 else unsigned(x);")
    # a binary minus is a subtraction, not a negation
    assert not _abs_without_sign_check_issues("abs_diff <= to_signed(2048, 16) - resize(x, 16);")


# ── timing_recovery, 2026-10-30 12:59: seven fix rounds for one design ──────

def test_a_label_after_end_process_is_not_a_missing_variable_declaration():
    """`p_strobe : process(aclk)` after `end process p_main;` was reported as
    a declaration without `variable` in all seven rounds (the word "process"
    in `end process` opened a fake declarative region), and the model deleted
    an `end if;` trying to obey."""
    from agents.rtl_agent.lint.rtl_linter import _process_declaration_issues
    code = ("p_main : process(aclk)\n  variable w : signed(15 downto 0);\nbegin\n  w := eta;\nend process p_main;\n"
            "p_strobe : process(aclk)\nbegin\n  strobe <= '1';\nend process p_strobe;\n"
            "u_sub : entity work.sub port map (a => b);\n"
            "g_gen : for i in 0 to 3 generate\n  x(i) <= y(i);\nend generate;\n"
            "p_last : process(aclk)\n  temp : signed(3 downto 0);\nbegin\n  temp := x;\nend process;\n")
    found = _process_declaration_issues(code)
    assert [i["code"] for i in found] == ["missing_variable_keyword"]        # only the real one (line 15)
    assert "line 15" in found[0]["detail"] and "'temp'" in found[0]["detail"]


def test_multiplier_width_fix_is_one_exact_declaration_and_one_exact_line():
    """`p1 <= resize(v2, 22) * resize(signed('0' & mu_q8), 22);` into a 22-bit
    p1 was told "make p1 44 bits"; the next round, the resize() gone as the
    same message asked, "make p1 23 bits". One message now, at the natural
    widths."""
    from agents.rtl_agent.lint.rtl_linter import _multiplier_width_issues
    code = ("signal v2 : signed(13 downto 0);\nsignal mu_q8 : unsigned(7 downto 0);\nsignal p1 : signed(21 downto 0);\n"
            "p1 <= resize(v2, 22) * resize(signed('0' & mu_q8), 22);\n")
    [issue] = _multiplier_width_issues(code)
    assert issue["detail"].startswith("line 4: `p1 <= resize(v2, 22) * resize(signed('0' & mu_q8), 22);` "
                                      "puts a 44-bit product into the 22-bit 'p1'")
    assert "declare `signal p1 : signed(22 downto 0);`" in issue["detail"]
    assert "write `p1 <= v2 * signed('0' & mu_q8);` (14 + 9 = 23 bits" in issue["detail"]
    # p1 already the natural width: only the resize() wrappers are wrong
    [issue] = _multiplier_width_issues(code.replace("21 downto", "22 downto"))
    assert "'p1' is already 23 bits: write `p1 <= v2 * signed('0' & mu_q8);`" in issue["detail"]
    # the natural form, and the wrapped form into a really 44-bit target, are both accepted
    natural = code.replace("21 downto", "22 downto").replace(
        "resize(v2, 22) * resize(signed('0' & mu_q8), 22)", "v2 * signed('0' & mu_q8)")
    assert not _multiplier_width_issues(natural)
    assert not _multiplier_width_issues(code.replace("21 downto", "43 downto"))


TYPED_PRODUCT = (
    "constant MU_F : integer := 16;\nconstant MU_W : integer := MU_F+2;\n"
    "signal v2i_s4 : signed(13 downto 0);\nsignal mu_s4 : signed(MU_W-1 downto 0);\n"
    "signal p2i_s5 : signed(29 downto 0);\n"
    "p_s5 : process(aclk)\n  variable ymid_i_s7 : signed(12 downto 0);\n  variable d_i_s7 : signed(13 downto 0);\n"
    "  variable p1 : signed(25 downto 0);\nbegin\n"
    "  p2i_s5 <= v2i_s4 * mu_s4;\n  p1 := ymid_i_s7 * d_i_s7;\nend process;\n")


def test_multiplier_width_requires_the_product_declared_from_width_names():
    """2026-11-05, every run: the first draft typed `p2i_s5 : signed(29 downto 0)`
    for a 14 x 18 product (mu counted at 16 bits) and `p1 : signed(25 downto 0)`
    for 13 x 14; fix round 3 typed 31 and 26, and the same slip was back the
    next run. For `p <= a * b` with two declared names the fix now names the
    widths (`signed(A_W+B_W-1 downto 0)`), and a typed-in product width is
    reported even when the number is right, so later rounds cannot go stale."""
    from agents.rtl_agent.lint.rtl_linter import _multiplier_width_issues, _width_issues
    first, second = _multiplier_width_issues(TYPED_PRODUCT)
    assert first["detail"].startswith("line 11: `p2i_s5 <= v2i_s4 * mu_s4;` puts a 32-bit product into the 30-bit 'p2i_s5'")
    assert ("declare `constant V2I_W : integer := 14;` and change `signal v2i_s4 : signed(13 downto 0)` to "
            "`signal v2i_s4 : signed(V2I_W-1 downto 0)`") in first["detail"]           # stage suffix dropped
    assert "declare `signal p2i_s5 : signed(V2I_W+MU_W-1 downto 0);` (14 + 18 = 32 bits)" in first["detail"]
    assert "signed(31 downto 0)" not in first["detail"]                                 # no typed number offered
    assert "declare `variable p1 : signed(YMID_I_W+D_I_W-1 downto 0);` (13 + 14 = 27 bits)" in second["detail"]
    assert "`variable d_i_s7 : signed(D_I_W-1 downto 0)`" in second["detail"]          # variables keep their keyword
    # the right number, typed in: still reported, with the same fix, and the general width check stays quiet
    typed = TYPED_PRODUCT.replace("29 downto", "31 downto").replace("25 downto", "26 downto")
    [a, b] = _width_issues(typed)
    assert a["code"] == "multiplier_width" and a["detail"].startswith(
        "line 11: `p2i_s5 <= v2i_s4 * mu_s4;` stores the product in 'p2i_s5', declared with a typed-in width "
        "`signed(31 downto 0)`. The number is right today (14 + 18 = 32 bits)")
    assert "signed(V2I_W+MU_W-1 downto 0)" in a["detail"] and "line 12" in b["detail"]
    # declared from names: accepted, and a wrong named width is still a plain width error
    named = (TYPED_PRODUCT.replace("signal v2i_s4 : signed(13 downto 0)", "constant V2I_W : integer := 14;\n"
                                   "signal v2i_s4 : signed(V2I_W-1 downto 0)")
             .replace("signed(29 downto 0)", "signed(V2I_W+MU_W-1 downto 0)")
             .replace("variable ymid_i_s7 : signed(12 downto 0)", "constant Y_W : integer := 13;\n  variable ymid_i_s7 : signed(Y_W-1 downto 0)")
             .replace("variable d_i_s7 : signed(13 downto 0)", "constant D_W : integer := 14;\n  variable d_i_s7 : signed(D_W-1 downto 0)")
             .replace("signed(25 downto 0)", "signed(Y_W+D_W-1 downto 0)"))
    assert _width_issues(named) == []
    [stale] = _multiplier_width_issues(named.replace("signed(V2I_W+MU_W-1 downto 0)", "signed(V2I_W+MU_F-1 downto 0)"))
    assert "puts a 32-bit product into the 30-bit 'p2i_s5'" in stale["detail"]
    assert "declare `signal p2i_s5 : signed(V2I_W+MU_W-1 downto 0);`" in stale["detail"]
    # a wrapped factor (`signed('0' & mu)`) is not the plain shape: the exact-number fix of the older tests stays


TWO_PROCESSES_SAME_VARIABLE = (
    "signal v2_s2 : signed(12 downto 0);\nsignal mu_s2 : unsigned(7 downto 0);\n"
    "signal p1_s3 : signed(21 downto 0);\nsignal mu_s3 : unsigned(7 downto 0);\n"
    "signal p2_s4 : signed(30 downto 0);\n"
    "p_s3 : process(aclk)\n  variable prod_v : signed(21 downto 0);\nbegin\n  if rising_edge(aclk) then\n"
    "    prod_v := v2_s2 * signed('0' & mu_s2);\n    p1_s3 <= prod_v;\n  end if;\nend process p_s3;\n"
    "p_s4 : process(aclk)\n  variable prod_v : signed(30 downto 0);\nbegin\n  if rising_edge(aclk) then\n"
    "    prod_v := p1_s3 * signed('0' & mu_s3);\n    p2_s4 <= prod_v;\n  end if;\nend process p_s4;\n")


def test_width_checks_use_the_variable_declared_in_the_same_process():
    """timing_recovery 2026-11-02 17:52: `prod_v` was 22 bits in p_s3 and 31
    bits in p_s4, both right. The file-wide width map said 31 everywhere, so
    p_s3's product lines were flagged for nine fix rounds with a fix the file
    already contained. Each process is now checked against its own
    declaration, and the message quotes `:=` for a variable, not `<=`."""
    from agents.rtl_agent.lint.rtl_linter import _width_issues
    from agents.rtl_agent.lint.rtl_smoke_check import _width_mismatch_hint
    from agents.rtl_agent.lint.rtl_widths import scoped_widths
    assert _width_issues(TWO_PROCESSES_SAME_VARIABLE) == []
    scopes = scoped_widths(TWO_PROCESSES_SAME_VARIABLE)
    assert scopes.at(10)["prod_v"] == 22 and scopes.at(18)["prod_v"] == 31 and "prod_v" not in scopes.at(1)
    assert scopes.at(10)["v2_s2"] == 13                      # file-level signals are visible inside
    # a real mistake in p_s3 is still reported, at p_s3's width and with the variable's operator
    bad = TWO_PROCESSES_SAME_VARIABLE.replace("variable prod_v : signed(21 downto 0)", "variable prod_v : signed(29 downto 0)")
    [mult, *rest] = _width_issues(bad)
    assert mult["code"] == "multiplier_width" and mult["detail"].startswith(
        "line 10: `prod_v := v2_s2 * signed('0' & mu_s2);` puts a 22-bit product into the 30-bit 'prod_v'")
    assert "declare `variable prod_v : signed(21 downto 0);` and write `prod_v := v2_s2 * signed('0' & mu_s2);`" in mult["detail"]
    assert rest == []       # line 11, p1_s3 <= prod_v, fits once prod_v is the 22 bits that message asks for
    # the dry-run hint finds the line by the same scoped widths
    hint = _width_mismatch_hint("Array sizes do not match, left array has 30 elements, right array has 22 elements", bad)
    assert "line 10:" in hint and "line 18" not in hint


def test_assignment_width_fix_is_the_exact_line_when_the_right_side_is_narrower():
    """`y_sym_i <= std_logic_vector(y_sat);` (12 bits into 24) was told to
    "compute at the natural width, then resize as a separate step", the
    advice for a WIDER right side, and survived two rounds."""
    from agents.rtl_agent.lint.rtl_linter import _assignment_width_issues
    code = ("signal y_sat : signed(11 downto 0);\nsignal y_sym_i : std_logic_vector(23 downto 0);\n"
            "signal mu_q8 : unsigned(7 downto 0);\nsignal eta : signed(15 downto 0);\nsignal w : unsigned(23 downto 0);\n"
            "y_sym_i <= std_logic_vector(y_sat);\n"
            "mu_q8 <= unsigned(shift_left(resize(eta, 16), 2));\n"
            "w <= unsigned(y_sym_i(11 downto 0));\n")
    narrow, wide, conv = _assignment_width_issues(code, set())
    assert "line 6: 'y_sym_i' is 24 bits but the right-hand side is 12 bits" in narrow["detail"]
    assert "`y_sym_i <= std_logic_vector(resize(y_sat, 24));`" in narrow["detail"]
    assert "line 7: 'mu_q8' is 8 bits but the right-hand side is 16 bits" in wide["detail"]
    assert "exactly 16 bits, then `mu_q8 <= resize(<that>, 8);`" in wide["detail"]
    assert "`w <= resize(unsigned(y_sym_i(11 downto 0)), 24);`" in conv["detail"]


def test_multiple_drivers_names_the_processes_the_lines_and_the_stray_reset_line():
    """p_main reset `strobe` while p_strobe computed it; "process #1, process
    #2" was all the model got, for six rounds."""
    from agents.rtl_agent.lint.rtl_linter import _multiple_driver_issues
    code = ("p_main : process(aclk)\nbegin\n  if rising_edge(aclk) then\n    if aresetn = '0' then\n"
            "      cnt <= 0;\n      strobe <= '0';\n    end if;\n  end if;\nend process p_main;\n"
            "p_strobe : process(aclk)\nbegin\n  if rising_edge(aclk) then\n    if eta < 0 then\n"
            "      strobe <= '1';\n    else\n      strobe <= '0';\n    end if;\n  end if;\nend process p_strobe;\n")
    [issue] = _multiple_driver_issues(code)
    assert "'strobe' is written in 2 places: process p_main (line 6); process p_strobe (lines 14, 16)" in issue["detail"]
    assert "process p_main writes it only once (line 6), most likely its reset line" in issue["detail"]
    assert "reset 'strobe' in process p_strobe instead" in issue["detail"]


def test_linter_catches_a_declaration_after_the_process_begin():
    """The retry put a `variable` line after `begin` and a `signal` line inside
    the process: 'syntax error near variable' plus 13 follow-on errors, twice."""
    from agents.rtl_agent.lint.rtl_linter import _declaration_after_begin_issues
    code = ("p_main : process(aclk)\n  variable w : signed(15 downto 0);\nbegin\n"
            "  variable eta_next : signed(15 downto 0);\n  if rising_edge(aclk) then\n"
            "    signal x_m1 : signed(11 downto 0);\n    w := eta;\n  end if;\nend process p_main;\n")
    found = _declaration_after_begin_issues(code)
    assert [i["code"] for i in found] == ["declaration_after_begin"] * 2
    assert "line 4: `variable eta_next`" in found[0]["detail"]
    assert "between `process(...)` and its `begin`" in found[0]["detail"]
    assert "line 6: `signal x_m1`" in found[1]["detail"]
    assert "a signal can never be declared inside a process" in found[1]["detail"]
    fixed = code.replace("  variable eta_next : signed(15 downto 0);\n", "").replace(
        "    signal x_m1 : signed(11 downto 0);\n", "")
    assert not _declaration_after_begin_issues(fixed)


# ── timing_recovery, 2026-11-02 09:31: one unfinished file came back eight times ──

def test_linter_reads_leaked_planning_comments_as_placeholders():
    """The NCO process body was only the model's own notes ("-- I will generate
    the full code in the final answer", "-- Placeholder for the rest of the
    code"); TODO/FIXME/`...` never matched, so the file reached the dry run
    eight times and got the same canned valid_pipe hint every time."""
    from agents.rtl_agent.lint.rtl_linter import _static_checks
    spec = {"entity_name": "timing_recovery", "ports": []}
    base = ("entity timing_recovery is port (aclk : in std_logic); end entity;\n"
            "architecture rtl of timing_recovery is\nbegin\n"
            "  -- wait for the handshake before the I and Q samples are latched\n"
            "  -- this lets the counter wrap at 15 (0 -> ... -> 15)\n"
            "end architecture;\n")
    assert not [i for i in _static_checks(spec, {"code": base}) if i["code"] == "placeholder"]
    stub = base.replace("end architecture;",
                        "  -- I will generate the full code in the final answer.\n"
                        "  -- Placeholder for the rest of the code\n"
                        "  -- For now, let's just output the code with the corrected NCO process.\n"
                        "end architecture;")
    found = [i["detail"] for i in _static_checks(spec, {"code": stub}) if i["code"] == "placeholder"]
    assert len(found) == 3
    assert found[0].startswith("line 6:") and "not written yet" in found[0] and "final answer" in found[0]
    # a flood of such notes is cut to five lines plus a count
    flood = base.replace("end architecture;", "  -- placeholder\n" * 9 + "end architecture;")
    found = [i["detail"] for i in _static_checks(spec, {"code": flood}) if i["code"] == "placeholder"]
    assert len(found) == 5 and "4 more" in found[-1]


def test_linter_catches_an_output_valid_tied_to_zero_before_the_dry_run():
    """`m_axis_tvalid <= m_axis_tvalid_i;` ... `m_axis_tvalid_i <= '0';` was
    the whole output logic; the 400-cycle dry run then reported
    smoke_output_never_valid with a hint about valid_pipe/advance, names the
    file did not contain, and the model changed nothing."""
    from agents.rtl_agent.lint.rtl_linter import _constant_output_valid_issues
    spec = {"ports": [{"name": "s_axis_tvalid", "direction": "input", "width": "1"},
                      {"name": "m_axis_tvalid", "direction": "output", "width": "1"},
                      {"name": "m_axis_tdata", "direction": "output", "width": "24"}]}
    stub = ("m_axis_tvalid <= m_axis_tvalid_i;\nm_axis_tdata <= m_axis_tdata_i;\n"
            "m_axis_tvalid_i <= '0';  -- stub\nm_axis_tdata_i <= (others => '0');\n")
    [issue] = _constant_output_valid_issues(spec, stub)
    assert issue["code"] == "output_valid_stuck_at_zero"
    assert "line 3: output valid 'm_axis_tvalid'" in issue["detail"]
    assert "(through 'm_axis_tvalid_i')" in issue["detail"] and "smoke_output_never_valid" in issue["detail"]
    assert issue["detail"].endswith(">> m_axis_tvalid_i <= '0';")
    # written straight on the port
    assert _constant_output_valid_issues(spec, "m_axis_tvalid <= '0';")
    # a reset write of '0' next to a real write is fine
    ok = ("process(aclk) begin if rising_edge(aclk) then if aresetn = '0' then m_axis_tvalid_i <= '0';\n"
          "else m_axis_tvalid_i <= valid_pipe(3); end if; end if; end process;\nm_axis_tvalid <= m_axis_tvalid_i;\n")
    assert not _constant_output_valid_issues(spec, ok)
    assert not _constant_output_valid_issues(spec, "m_axis_tvalid <= '1' when busy = '1' else '0';")
    # an input valid, or a valid driven by a sub-block (no `<=` write), is not this check's business
    assert not _constant_output_valid_issues(spec, "s_axis_tvalid <= '0';")
    assert not _constant_output_valid_issues(spec, "u0 : entity work.core port map (m_axis_tvalid => m_axis_tvalid);")


# ── the generator re-asks once when the answer is the file it was asked to change ──

class RecordingLLM:
    """Stand-in chat model: hands out the queued replies and keeps every request."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def invoke(self, messages, config=None):
        self.calls.append(messages)
        return AIMessage(content=self.replies.pop(0))


OLD = json.dumps({"entity_name": "counter4", "notes": [],
                  "code": "entity counter4 is end;\n-- placeholder for the rest of the code\ncount_i <= '0';"})
NEW = json.dumps({"entity_name": "counter4", "notes": [],
                  "code": "entity counter4 is end;\ncount_i <= count_i + 1;"})
ISSUES = [{"code": "placeholder", "detail": "line 2: this comment says the code is not written yet"}]


def test_generator_re_asks_once_with_a_warmer_client_when_the_fix_is_a_copy():
    """timing_recovery, 2026-11-02 09:31: eight byte-identical answers while
    the notes said "I have rewritten the entire architecture"; each one cost
    a compile and a dry run before unchanged_on_retry / the stall rule spoke."""
    first, second = RecordingLLM(OLD), RecordingLLM(NEW)
    gen = RTLGenerator(llm=first, retry_llm=second)
    previous = json.loads(OLD)
    rtl = gen.generate(SPEC, SKILLS, previous_rtl=previous, lint_issues=ISSUES)
    assert rtl["code"] == json.loads(NEW)["code"]
    assert len(first.calls) == 1 and len(second.calls) == 1
    system, user = second.calls[0][0].content, json.loads(second.calls[0][1].content)
    assert 'returning "previous_code" unchanged is refused' in system        # the fix addendum came along
    assert [i["code"] for i in user["lint_issues"]] == ["placeholder", "unchanged_answer"]
    assert "changed 0 lines" in user["lint_issues"][-1]["detail"] and "(placeholder)" in user["lint_issues"][-1]["detail"]
    assert user["previous_code"] == previous["code"]
    assert rtl["notes"][-1].startswith("re-asked once")


def test_generator_does_not_re_ask_when_the_fix_changed_something():
    first, second = RecordingLLM(NEW), RecordingLLM()
    gen = RTLGenerator(llm=first, retry_llm=second)
    rtl = gen.generate(SPEC, SKILLS, previous_rtl=json.loads(OLD), lint_issues=ISSUES)
    assert rtl["code"] == json.loads(NEW)["code"] and len(first.calls) == 1 and not second.calls


def test_generator_re_asks_only_once_and_leaves_a_second_copy_to_the_linter():
    from agents.rtl_agent.lint.rtl_linter import unchanged_on_retry_issue
    first, second = RecordingLLM(OLD), RecordingLLM(OLD)
    gen = RTLGenerator(llm=first, retry_llm=second)
    rtl = gen.generate(SPEC, SKILLS, previous_rtl=json.loads(OLD), lint_issues=ISSUES)
    assert rtl["code"] == json.loads(OLD)["code"] and len(second.calls) == 1
    assert unchanged_on_retry_issue(rtl, json.loads(OLD), None)["code"] == "unchanged_on_retry"


def test_generator_re_asks_when_a_retry_hands_back_the_orchestrators_file_unchanged():
    """Attempts 2 and 3 this morning: the first write of each retry was the
    orchestrator's previous_code, byte for byte, before any lint issue existed."""
    first, second = RecordingLLM(OLD), RecordingLLM(NEW)
    gen = RTLGenerator(llm=first, retry_llm=second)
    history = [{"attempt": 1, "status": "NEEDS_REVIEW", "error": "stalled", "issues": ISSUES}]
    rtl = gen.generate(SPEC, SKILLS, attempt=2, attempt_history=history, starting_rtl=json.loads(OLD))
    assert rtl["code"] == json.loads(NEW)["code"] and len(second.calls) == 1
    user = json.loads(second.calls[0][1].content)
    assert user["previous_code"] == json.loads(OLD)["code"]
    assert [i["code"] for i in user["lint_issues"]] == ["unchanged_answer"]
    assert "the downstream failures" in user["lint_issues"][0]["detail"]


def test_linter_names_every_stage_register_that_is_used_but_never_declared_or_written():
    """timing_recovery 16:03 (2026-11-02): `mu_s2 <= mu_s1;` and six more
    carry lines read stage-1 registers that were never declared or written
    (loop values fed back from the last stage). xvhdl listed all seven, the
    follow-on filter showed one, and two fix rounds returned the same file."""
    from agents.rtl_agent.lint.rtl_linter import _stage_register_declaration_issues
    code = """architecture rtl of t is
  signal i_s1, q_s1 : signed(11 downto 0);
  signal mu_s2, mu_s3 : signed(16 downto 0);
  signal y_s3 : signed(11 downto 0);
begin
  p_s1 : process(clk)
  begin
    if rising_edge(clk) then
      i_s1 <= signed(s_axis_tdata(11 downto 0));
    end if;
  end process p_s1;
  p_s2 : process(clk)
  begin
    if rising_edge(clk) then
      mu_s2 <= mu_s1;                          -- mu_s1: never declared, never written
      acc_s2 <= acc_s1 + resize(i_s1, 17);     -- acc_s2 written, acc_s1 read: neither declared
    end if;
  end process p_s2;
  p_s3 : process(clk)
    variable eta_v_s3 : signed(16 downto 0);
  begin
    if rising_edge(clk) then
      eta_v_s3 := mu_s2 + 1;
      mu_s3 <= mu_s2;
      y_s3 <= q_s1;                            -- q_s1 declared but never written
    end if;
  end process p_s3;
end architecture;
"""
    issues = _stage_register_declaration_issues(code)
    assert [i["code"] for i in issues] == ["undeclared_stage_register"] * 3 + ["unwritten_stage_register"]
    assert [i["detail"].split("`")[1] for i in issues] == ["mu_s1", "acc_s2", "acc_s1", "q_s1"]
    mu, acc2, acc1, q = (i["detail"] for i in issues)
    assert "line 15" in mu and "add `signal mu_s1 : signed(16 downto 0);` next to `mu_s2` (line 3)" in mu
    # a loop value is not copied back round the rows any more: it lives in p_loop (2026-11-04)
    assert "do not copy it back from the last stage (`mu_s1 <= mu_s3;` would run 3 separate loops)" in mu
    assert "`p_loop`" in mu and "`strobe_s2` / `mu_s2`" in mu and "`p_s1`" in mu
    assert "<the type of the value it carries>" in acc2 and "write it" not in acc2    # written: only the declaration is missing
    assert "`acc_s1 <= <input>;`" in acc1
    assert "line 25" in q and "never written" in q and "`q_s1 <= <input>;`" in q
    clean = """architecture rtl of t is
  signal a_s1, c_s1 : signed(11 downto 0);
  signal b_s2 : signed(12 downto 0);
  signal mu_s1, mu_s2 : signed(16 downto 0) := (others => '0');
begin
  m_axis_tdata <= std_logic_vector(b_s2);
  u_s1 : my_comp port map (clk => clk, q => c_s1);
  p_s1 : process(clk) begin if rising_edge(clk) then a_s1 <= signed(d); mu_s1 <= mu_s2; end if; end process p_s1;
  p_s2 : process(clk) begin if rising_edge(clk) then b_s2 <= resize(a_s1, 13) + resize(c_s1, 13); mu_s2 <= mu_s1; end if; end process p_s2;
end architecture;"""
    assert _stage_register_declaration_issues(clean) == []
    assert _stage_register_declaration_issues("q <= d when en = '1' else q;") == []


def test_compiler_undeclared_lines_are_dropped_once_the_linter_explained_the_name():
    from agents.rtl_agent.lint.rtl_linter import _without_duplicate_undeclared
    static = [{"code": "undeclared_stage_register", "detail": "line 15: `mu_s1` is used but never declared ..."}]
    compiled = [{"code": "compile_error",
                 "detail": "ERROR: [VRFC 10-2989] 'mu_s1' is not declared [t.vhd:15]  >> line 15: mu_s2 <= mu_s1;"},
                {"code": "compile_error", "detail": "6 follow-on error(s) hidden: they come from the error(s) above ..."}]
    assert _without_duplicate_undeclared(static, compiled) == []                     # only the note would be left
    other = {"code": "compile_error", "detail": "ERROR: [VRFC 10-2989] 'mu' is not declared [t.vhd:30]  >> line 30: x := mu;"}
    assert _without_duplicate_undeclared(static, compiled + [other]) == [compiled[1], other]
    assert _without_duplicate_undeclared([], compiled) == compiled


def test_each_different_missing_name_is_a_root_compiler_error():
    """timing_recovery 16:03 (2026-11-02): seven stage-1 registers were never
    declared; xvhdl listed all seven on lines 278-284, the filter kept one and
    hid six as follow-ons, and the model fixed none of them."""
    from agents.lint_utils import compiler_errors
    src = "\n".join(f"line{n}" for n in range(1, 300))
    names = ["mu_s1", "eta_s1", "v_s1", "acc_s1", "y_prev_i_s1", "y_prev_q_s1", "strobe_s1"]
    out = "\n".join(f"ERROR: [VRFC 10-2989] '{n}' is not declared [C:/tmp/tmpab12cd/t.vhd:{278 + k}]"
                    for k, n in enumerate(names))
    out += "\nERROR: [VRFC 10-2989] 'mu_s1' is not declared [C:/tmp/tmpab12cd/t.vhd:290]"                # same name: follow-on
    out += "\nERROR: [VRFC 10-3782] unit 'rtl' ignored due to previous errors [C:/tmp/tmpab12cd/t.vhd:18]"  # always noise
    details = [i["detail"] for i in compiler_errors(out, src, 1)]
    assert [d.split("'")[1] for d in details[:-1]] == names
    assert details[-1].startswith("2 follow-on error(s) hidden")
    broken = ("ERROR: [VRFC 10-4982] syntax error near 'signal' [t.vhd:40]\n"      # a broken declaration ...
              "ERROR: [VRFC 10-2989] 'foo' is not declared [t.vhd:60]\n"            # ... still hides the names it broke
              "ERROR: [VRFC 10-2989] 'bar' is not declared [t.vhd:61]")
    kept = [i["detail"] for i in compiler_errors(broken, src, 1)]
    assert len(kept) == 2 and "'signal'" in kept[0] and kept[1].startswith("2 follow-on error(s) hidden")


FARROW_TAIL = """
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
entity t is port (aclk : in std_logic); end entity t;
architecture rtl of t is
  constant D_W  : integer := 12;
  constant MU_F : integer := 16;
  constant MU_W : integer := MU_F + 2;
  signal s2i_s5, s1i_s5 : signed(D_W+MU_W+1 downto 0);
  signal mu_s5 : signed(MU_W-1 downto 0);
  signal p2i_s6, p1i_s6 : signed(D_W+2*MU_W+1 downto 0);
  signal m2i_s6, m1i_s6 : signed(D_W+2*MU_W+1 downto 0);
  signal v0i_s5, v0i_s6, w0i_s6 : signed(D_W-1 downto 0);
  signal ysym_i_s7, ymid_i_s7 : signed(D_W-1 downto 0);
  signal stage1, stage2 : signed(D_W-1 downto 0);
begin
  p_s6 : process(aclk)
  begin
    if rising_edge(aclk) then
      p2i_s6 <= s2i_s5 * mu_s5;
      p1i_s6 <= s1i_s5 * mu_s5;
      v0i_s6 <= v0i_s5;
    end if;
  end process p_s6;
  p_s7 : process(aclk)
  begin
    if rising_edge(aclk) then
      ysym_i_s7 <= sat12(resize(p2i_s6, D_W+2*MU_W+2) + resize(p1i_s6, D_W+2*MU_W+2) + resize(v0i_s6, D_W+2*MU_W+2));
      ymid_i_s7 <= sat12(resize(p2i_s6, D_W+2*MU_W+2) + resize(p1i_s6, D_W+2*MU_W+2) + resize(v0i_s6, D_W+2*MU_W+2));
    end if;
  end process p_s7;
end architecture rtl;
"""


def test_two_registers_given_the_same_computed_expression_are_reported():
    """timing_recovery 2026-11-09 14:18 (run 35b45c09): ymid_i_s7 was a copy of
    ysym_i_s7's expression, the Gardner error was 0 on every strobe and mu never
    moved ("m_axis_tuser expected 249 got 0"). The same copy is in the RTL of
    five earlier runs."""
    from agents.rtl_agent.lint.rtl_linter import _duplicate_expression_issues

    found = _duplicate_expression_issues(FARROW_TAIL)
    assert [i["code"] for i in found] == ["duplicate_expression"]
    detail = found[0]["detail"]
    assert "`ymid_i_s7` is given exactly the same expression as `ysym_i_s7`" in detail
    assert "OLDER samples x[m-3]..x[m]" in detail and ">> ymid_i_s7 <= sat12(" in detail
    # its own expression (its own product registers) is fine
    own = FARROW_TAIL.replace("ymid_i_s7 <= sat12(resize(p2i_s6, D_W+2*MU_W+2) + resize(p1i_s6, D_W+2*MU_W+2)",
                              "ymid_i_s7 <= sat12(resize(m2i_s6, D_W+2*MU_W+2) + resize(m1i_s6, D_W+2*MU_W+2)")
    assert _duplicate_expression_issues(own) == []
    # not reported: plain copies of one signal, reset aggregates, and a register written in two places
    quiet = FARROW_TAIL.replace(
        "      v0i_s6 <= v0i_s5;",
        "      v0i_s6 <= v0i_s5;\n      w0i_s6 <= v0i_s5;\n"
        "      stage1 <= (others => '0');\n      stage2 <= (others => '0');\n"
        "      m2i_s6 <= s2i_s5 * mu_s5;\n      if mu_s5 < 0 then\n        m2i_s6 <= s1i_s5 * mu_s5;\n      end if;")
    assert [i["detail"].split("`")[1] for i in _duplicate_expression_issues(quiet)] == ["ymid_i_s7"]


def test_a_product_added_to_a_plain_value_without_scaling_back_is_reported():
    """timing_recovery 2026-11-09 14:18 (run 35b45c09): `p2i_s6 <= s2i_s5 * mu_s5`
    (mu: 1.0 = 2**16) was added to the 12-bit v0i_s6 with no shift, so the output
    saturated whenever mu was not 0."""
    from agents.rtl_agent.lint.rtl_linter import _unscaled_product_sum_issues

    found = _unscaled_product_sum_issues(FARROW_TAIL)
    assert {i["code"] for i in found} == {"product_added_unscaled"} and len(found) == 2    # p2i_s6 and p1i_s6, once each
    detail = found[0]["detail"]
    assert "`p2i_s6` is a product (line" in detail and "`p2i_s6 <= s2i_s5 * mu_s5`, 50 bits" in detail
    assert "plain 12-bit `v0i_s6`" in detail and "shift_right(p2i_s6, F)" in detail and "fraction bits of `mu_s5`" in detail
    # scaled back before the add, or the plain value shifted up: fine
    scaled = FARROW_TAIL.replace("resize(p2i_s6, D_W+2*MU_W+2) + resize(p1i_s6, D_W+2*MU_W+2) + resize(v0i_s6, D_W+2*MU_W+2)",
                                 "shift_right(p2i_s6, 2*MU_F) + shift_right(p1i_s6, MU_F) + resize(v0i_s6, D_W+2*MU_W+2)")
    assert _unscaled_product_sum_issues(scaled) == []
    up = FARROW_TAIL.replace("+ resize(v0i_s6, D_W+2*MU_W+2)", "+ shift_left(resize(v0i_s6, D_W+2*MU_W+2), 2*MU_F)")
    assert _unscaled_product_sum_issues(up) == []
    # a sum of products with products, and an integer multiply (`x * 3`), are fine
    only_products = FARROW_TAIL.replace(" + resize(v0i_s6, D_W+2*MU_W+2)", "")
    assert _unscaled_product_sum_issues(only_products) == []
    by_three = FARROW_TAIL.replace("p2i_s6 <= s2i_s5 * mu_s5;", "p2i_s6 <= resize(s2i_s5 * 3, D_W+2*MU_W+2);").replace(
        "p1i_s6 <= s1i_s5 * mu_s5;", "p1i_s6 <= resize(s1i_s5 * 3, D_W+2*MU_W+2);")
    assert _unscaled_product_sum_issues(by_three) == []
    # the same mistake as a variable (`:=`) inside a function: the reviewed timing_recovery RTL of 2026-11-10,
    # `p1 := v2 * mu; p2 := (resize(p1, P2_W) + resize(v1, P2_W)) * mu;`, went through a `<=`-only check
    found = _unscaled_product_sum_issues(FARROW_FUNCTION)
    assert [i["code"] for i in found] == ["product_added_unscaled"]
    p1_line = next(n for n, t in enumerate(FARROW_FUNCTION.splitlines(), 1) if "p1 := v2 * mu;" in t)
    assert f"`p1` is a product (line {p1_line}: `p1 := v2 * mu`, 32 bits) and is added to the plain 15-bit `v1`" in found[0]["detail"]
    assert found[0]["detail"].endswith(">> p2 := (resize(p1, P2_W) + resize(v1, P2_W)) * mu;")
    fixed = FARROW_FUNCTION.replace("(resize(p1, P2_W) + resize(v1, P2_W)) * mu", "(shift_right(p1, MU_F) + resize(v1, P2_W)) * mu")
    assert _unscaled_product_sum_issues(fixed) == []


FARROW_FUNCTION = """
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
entity t is port (aclk : in std_logic); end entity t;
architecture rtl of t is
  constant I_W  : integer := 12;
  constant MU_F : integer := 16;
  constant MU_W : integer := MU_F + 2;
  constant V2_W : integer := 14;
  constant V1_W : integer := 15;
  constant P1_W : integer := V2_W + MU_W;
  constant P2_W : integer := P1_W + MU_W;
  signal x0_s2, x1_s2, x2_s2, x3_s2 : signed(I_W-1 downto 0);
  signal mu_s2 : signed(MU_W-1 downto 0);
  signal y_s3 : signed(P2_W+MU_W-1 downto 0);
  function farrow(xm1, xm, xp1, xp2 : signed(I_W-1 downto 0);
                  mu : signed(MU_W-1 downto 0)) return signed is
    variable v2 : signed(V2_W-1 downto 0);
    variable v1 : signed(V1_W-1 downto 0);
    variable p1 : signed(P1_W-1 downto 0);
    variable p2 : signed(P2_W+MU_W-1 downto 0);
  begin
    v2 := resize(xp2, V2_W) - resize(xp1, V2_W) - resize(xm, V2_W) + resize(xm1, V2_W);
    v1 := shift_left(resize(xp1, V1_W), 1) + resize(xp1, V1_W) - resize(xp2, V1_W) - resize(xm, V1_W) - resize(xm1, V1_W);
    p1 := v2 * mu;
    p2 := (resize(p1, P2_W) + resize(v1, P2_W)) * mu;
    return p2;
  end function;
begin
  p_s3 : process(aclk)
  begin
    if rising_edge(aclk) then
      y_s3 <= farrow(x3_s2, x2_s2, x1_s2, x0_s2, mu_s2);
    end if;
  end process p_s3;
end architecture rtl;
"""


SPEC_WINDOW = {"behavior": ["The base point is m = n - 2.",
                            "y_sym = farrow(x[m-1], x[m], x[m+1], x[m+2], mu)",
                            "y_mid = farrow(x[m-3], x[m-2], x[m-1], x[m], mu)"]}


def test_a_history_chain_shorter_than_the_spec_window_names_the_registers_to_add():
    """timing_recovery 2026-11-10 (reviewed RTL): x0..x3 per channel, four taps,
    while y_mid reads x[m-3] = x[n-5]: six samples. The midpoint window read a
    sample the chain never stored, and pipeline_stage_map.md showed four taps too."""
    from agents.rtl_agent.lint.rtl_linter import _history_depth_issues, _spec_history_window

    assert _spec_history_window(SPEC_WINDOW) == (-5, 0, "x[m-3]..x[m+2] with m = n - 2, that is x[n-5]..x[n]")
    assert _spec_history_window({"behavior": ["y = x[n-1] + x[n]"]}) == (-1, 0, "x[n-1]..x[n]")
    chain = lambda taps, ch: "\n".join([f"x0_{ch}_s2 <= {ch}_s1;"] + [f"x{k}_{ch}_s2 <= x{k - 1}_{ch}_s2;" for k in range(1, taps)])
    four = _rows("i_s1 <= signed(din); q_s1 <= signed(din);", chain(4, "i") + "\n" + chain(4, "q"), "y_s3 <= x3_i_s2 + x0_q_s2;")
    found = _history_depth_issues(SPEC_WINDOW, four)
    assert [i["code"] for i in found] == ["history_too_short"]
    d = found[0]["detail"]
    line = next(n for n, t in enumerate(four.splitlines(), 1) if "x1_i_s2 <= x0_i_s2;" in t)
    assert d.startswith(f"line {line}: the history chain in row 2 (`x0_i_s2`..`x3_i_s2`, `x0_q_s2`..`x3_q_s2`) holds 4 samples, "
                        f"but the spec reads x[m-3]..x[m+2] with m = n - 2, that is x[n-5]..x[n]: 6 input samples side by side.")
    assert "Add 2 more register(s) to EACH chain" in d and "`x4_i_s2 <= x3_i_s2;` `x5_i_s2 <= x4_i_s2;`" in d
    assert d.endswith(">> x1_i_s2 <= x0_i_s2;")
    # six taps: fine; a spec that names no x[...] sample, or uses m without saying what m is: nothing to compare with
    assert _history_depth_issues(SPEC_WINDOW, _rows("i_s1 <= signed(din);", chain(6, "i"), "y_s3 <= x5_i_s2;")) == []
    assert _history_depth_issues({"behavior": ["y = x * 2"]}, four) == []
    assert _history_depth_issues({"behavior": ["y = x[m-3] + x[m]"]}, four) == []
    # a same-row copy whose names are not one stem plus a number (the previous symbol) is not a history chain
    assert _history_depth_issues(SPEC_WINDOW, _rows("i_s1 <= signed(din);", "ysym_s2 <= i_s1;\nyprev_s2 <= ysym_s2;")) == []


def test_a_function_of_one_signal_copied_to_two_registers_is_reported_but_reused_variables_are_not():
    """2026-11-09 18:59 (run a3133ede): `ysym_i_s7 <= sat12(t2i_s6); ymid_i_s7 <=
    sat12(t2i_s6);` slipped past the first duplicate_expression check, which only
    looked at expressions reading two or more signals."""
    from agents.rtl_agent.lint.rtl_linter import _duplicate_expression_issues

    one_signal = FARROW_TAIL.replace(
        "ysym_i_s7 <= sat12(resize(p2i_s6, D_W+2*MU_W+2) + resize(p1i_s6, D_W+2*MU_W+2) + resize(v0i_s6, D_W+2*MU_W+2));",
        "ysym_i_s7 <= sat12(p2i_s6);").replace(
        "ymid_i_s7 <= sat12(resize(p2i_s6, D_W+2*MU_W+2) + resize(p1i_s6, D_W+2*MU_W+2) + resize(v0i_s6, D_W+2*MU_W+2));",
        "ymid_i_s7 <= sat12(p2i_s6);")
    found = _duplicate_expression_issues(one_signal)
    assert len(found) == 1 and "`ymid_i_s7` is given exactly the same expression as `ysym_i_s7`" in found[0]["detail"]
    # one signal moved or re-typed into two registers is a fan-out, not a missing computation
    copies = one_signal.replace("ysym_i_s7 <= sat12(p2i_s6);", "ysym_i_s7 <= resize(p2i_s6, D_W);").replace(
        "ymid_i_s7 <= sat12(p2i_s6);", "ymid_i_s7 <= resize(p2i_s6, D_W);")
    assert _duplicate_expression_issues(copies) == []
    slices = one_signal.replace("sat12(p2i_s6)", "p2i_s6(D_W-1 downto 0)")
    assert _duplicate_expression_issues(slices) == []
    # a process variable reassigned between the two lines (older demapper RTL): same text, different value
    reused = """
architecture rtl of t is
  signal a, b, c, d : signed(11 downto 0);
  signal d0, d1 : signed(11 downto 0);
begin
  process(aclk)
    variable diff : signed(11 downto 0);
  begin
    if rising_edge(aclk) then
      diff := a - b;
      d0 <= abs(diff);
      diff := c - d;
      d1 <= abs(diff);
    end if;
  end process;
end architecture rtl;
"""
    assert _duplicate_expression_issues(reused) == []


# timing_recovery 14:43 (2026-11-10) in small: `mu : signed` has no width, so
# `v2 * mu` (13 + 18 = 31 bits into the 30-bit p1) was invisible to every static
# check and four fix rounds got only "Array sizes do not match ... in process p_s7"
FARROW_UNSIZED_MU = """\
entity t is
  port (aclk : in std_logic);
end;
architecture rtl of t is
  constant I_W  : integer := 12;
  constant MU_W : integer := 18;
  signal x1_s6, x2_s6, x3_s6, x4_s6 : signed(I_W-1 downto 0);
  signal mu_s6 : signed(MU_W-1 downto 0);
  signal y_s7 : signed(I_W-1 downto 0);
  signal s5r_s : signed(48 downto 0);
  function sat12(x : signed) return signed is
  begin
    if x > 2047 then return to_signed(2047, I_W);
    else return resize(x, I_W);
    end if;
  end function;
  function farrow(xm1 : signed; xm : signed; xp1 : signed; xp2 : signed; mu : signed) return signed is
    variable v2 : signed(12 downto 0);
    variable p1 : signed(29 downto 0);
  begin
    v2 := resize(xp2, 13) - resize(xm1, 13);
    p1 := v2 * mu;
    return sat12(p1);
  end function;
begin
  p_s7 : process(aclk)
  begin
    if rising_edge(aclk) then
      y_s7 <= farrow(x1_s6, x2_s6, x3_s6, x4_s6, mu_s6);
    end if;
  end process p_s7;
  p_x : process(aclk) begin if rising_edge(aclk) then s5r_s <= resize(sat12(s5r_s), 49); end if; end process;
end architecture;
"""


def test_function_inputs_without_a_width_take_the_width_the_calls_pass():
    """The width tool reads `mu : signed` from the one call (mu_s6, MU_W = 18
    bits), so the multiplier check names line 22 with the exact fix in round
    1, before any dry run. An input the calls size differently (sat12's x:
    30 and 49 bits) stays unknown and is never reported."""
    from agents.rtl_agent.lint.rtl_linter import _multiplier_width_issues, _unconstrained_parameter_issues, lint_rtl
    from agents.rtl_agent.lint.rtl_widths import scoped_widths
    scopes = scoped_widths(FARROW_UNSIZED_MU)
    assert scopes.functions["farrow"] == (17, 24) and scopes.functions["sat12"] == (11, 16)
    assert scopes.at(22).get("mu") == 18 and scopes.at(22).get("xp2") == 12
    assert scopes.at(22).types["mu"] == "signed(MU_W-1 downto 0)"
    assert scopes.at(14).get("x") is None                                   # 30 bits from farrow, 49 from p_x
    mult = _multiplier_width_issues(FARROW_UNSIZED_MU)
    assert [i["code"] for i in mult] == ["multiplier_width"]
    assert "line 22: `p1 := v2 * mu;` puts a 31-bit product into the 30-bit 'p1' (v2 is 13 bits, mu is 18 bits" \
        in mult[0]["detail"]
    assert "declare `variable p1 : signed(V2_W+MU_W-1 downto 0);` (13 + 18 = 31 bits)" in mult[0]["detail"]
    # the header itself is reported, with the width the call passes written as its name
    param = _unconstrained_parameter_issues(FARROW_UNSIZED_MU)
    assert [i["code"] for i in param] == ["unconstrained_function_parameter"]
    assert param[0]["detail"].startswith("line 17: function `farrow` takes `mu : signed` with no width, and line 22 "
                                         "multiplies by it (`p1 := v2 * mu;`)")
    assert "Every call passes `mu_s6` (18 bits): write `mu : signed(MU_W-1 downto 0)` in the function header" \
        in param[0]["detail"]
    # sat12's unsized x is only compared and resized: the recipe's helper shape, not an issue
    assert "sat12" not in " ".join(i["detail"] for i in param)
    codes = [i["code"] for i in lint_rtl({"entity_name": "t", "ports": []}, {"code": FARROW_UNSIZED_MU})["issues"]]
    assert "multiplier_width" in codes and "unconstrained_function_parameter" in codes

    # a header that gives the width declares it for the body, and is not reported
    sized = FARROW_UNSIZED_MU.replace("mu : signed)", "mu : signed(MU_W-1 downto 0))")
    assert scoped_widths(sized).at(22).get("mu") == 18
    assert _unconstrained_parameter_issues(sized) == []
    # calls that disagree leave the input unknown; the issue then asks for the width, naming the calls
    two = FARROW_UNSIZED_MU.replace("  signal mu_s6 : signed(MU_W-1 downto 0);",
                                    "  signal mu_s6 : signed(MU_W-1 downto 0);\n  signal mu_s5 : signed(19 downto 0);\n"
                                    "  signal y_s6 : signed(I_W-1 downto 0);")
    two = two.replace("  end process p_s7;", "  end process p_s7;\n  p_s6 : process(aclk) begin if rising_edge(aclk) "
                                             "then y_s6 <= farrow(x1_s6, x2_s6, x3_s6, x4_s6, mu_s5); end if; end process;")
    assert scoped_widths(two).at(24).get("mu") is None
    assert _multiplier_width_issues(two) == []
    detail = _unconstrained_parameter_issues(two)[0]["detail"]
    assert "Give it the width of the value the call passes (line 31 passes `mu_s6`; line 34 passes `mu_s5`)" in detail


def test_xelab_runs_after_xvhdl_and_explains_a_wrong_width_start_value(monkeypatch):
    """timing_recovery 2026-11-11, attempt 3: `to_signed(1, V2_W-1)` as the
    start value of a 16-bit constant passes xvhdl (grammar only) and stopped
    the real simulation in xelab, where it was explained as a missing comma.
    The compile step now runs xelab on the RTL alone, every round."""
    from agents.rtl_agent.lint import rtl_linter

    code = ("entity t is end;\narchitecture rtl of t is\n"
            "  constant V2_HALF : signed(15 downto 0) := to_signed(1, 15);\nbegin\nend;")
    calls = []
    monkeypatch.setattr(rtl_linter, "mcp_write_files", lambda *a, **k: None)
    monkeypatch.setattr(rtl_linter, "mcp_run_xvhdl", lambda *a, **k: [{"status": "ok", "stdout": ""}])

    def xelab(project_dir, top, file, std):
        calls.append((top, file, std))
        return {"status": "error", "errors_found": 2, "stdout":
                "ERROR: [VRFC 10-664] expression has 15 elements ; expected 16 [C:/tmp x/tmpab12/t.vhd:3]\n"
                "ERROR: [XSIM 43-3321] Static elaboration of top level VHDL design unit t in library work failed.\n"}
    monkeypatch.setattr(rtl_linter, "mcp_run_xelab", xelab)
    compiler, issues = rtl_linter._compile(code, "t", "2008")
    assert compiler == "xvhdl" and calls == [("t", "t.vhd", "2008")]
    assert [i["code"] for i in issues] == ["elaboration_error"]          # the closing summary line is not a cause
    detail = issues[0]["detail"]
    assert "[t.vhd:3]" in detail and ">> line 3: constant V2_HALF" in detail
    assert "a 15-bit value is given to a 16-bit name" in detail and "not a missing comma" in detail

    # xelab is not run on RTL that xvhdl rejects, and a clean build adds nothing
    calls.clear()
    monkeypatch.setattr(rtl_linter, "mcp_run_xvhdl", lambda *a, **k: [{"status": "error", "stdout": "ERROR: bad [t.vhd:1]"}])
    assert [i["code"] for i in rtl_linter._compile(code, "t", "2008")[1]] == ["compile_error"] and not calls
    monkeypatch.setattr(rtl_linter, "mcp_run_xvhdl", lambda *a, **k: [{"status": "ok", "stdout": ""}])
    monkeypatch.setattr(rtl_linter, "mcp_run_xelab", lambda *a, **k: {"status": "ok", "stdout": "", "errors_found": 0})
    assert rtl_linter._compile(code, "t", "2008") == ("xvhdl", [])


def test_xelab_that_cannot_run_leaves_the_xvhdl_result(monkeypatch):
    from agents.rtl_agent.lint import rtl_linter

    monkeypatch.setattr(rtl_linter, "mcp_write_files", lambda *a, **k: None)
    monkeypatch.setattr(rtl_linter, "mcp_run_xvhdl", lambda *a, **k: [{"status": "ok", "stdout": ""}])
    monkeypatch.setattr(rtl_linter, "mcp_run_xelab", lambda *a, **k: {
        "status": "error", "stderr": "xelab timed out after 180s.", "error_class": "environment"})
    assert rtl_linter._compile("entity t is end;", "t", "93") == ("xvhdl", [])

    def boom(*a, **k):
        raise KeyError("run_xelab")                 # a Vivado server started before the helper existed
    monkeypatch.setattr(rtl_linter, "mcp_run_xelab", boom)
    assert rtl_linter._compile("entity t is end;", "t", "93") == ("xvhdl", [])


FARROW_WIDTHS = (
    "constant I_W : integer := 12;\nconstant D_W : integer := 13;\nconstant E_W : integer := 26;\n"
    "constant V2_W : integer := 16;\n"
    "constant V2_ONE : signed(V2_W-1 downto 0) := to_signed(1, V2_W);\n"
    "signal ymid_i_s8 : signed(I_W-1 downto 0);\nsignal d_i_s8 : signed(D_W-1 downto 0);\n"
    "signal e_s9 : signed(E_W-1 downto 0);\n"
    "function farrow(xm : signed(I_W-1 downto 0)) return signed is\n"
    "  variable v2m1 : signed(V2_W-1 downto 0);\n  variable v2m1_2 : signed(V2_W-1 downto 0);\n"
    "  variable c0 : signed(V2_W-1 downto 0);\n  variable p0 : signed(I_W+V2_W-1 downto 0);\nbegin\n"
    "  v2m1_2 := v2m1 * v2m1;\n"                                   # line 15
    "  c0 := V2_ONE + V2_ONE * v2m1_2;\n"                           # line 16
    "  p0 := resize(xm, I_W+V2_W-1) * c0;\n"                        # line 17
    "  return p0;\nend function;\n"
    "p_s9 : process(aclk)\n  variable e_v : signed(E_W-1 downto 0);\nbegin\n"
    "  e_v := resize(ymid_i_s8, E_W) * resize(d_i_s8, E_W);\n"      # line 23
    "  e_s9 <= e_v;\nend process;\n")                               # line 24


def test_multiplier_width_takes_off_a_resize_whose_width_is_a_name():
    """timing_recovery 2026-11-11 12:15: `p0 := resize(xm, I_W+V2_W-1) * c0;`
    into a p0 that was already 12 + 16 = 28 bits. The wrapper was only taken
    off for a typed number, so three rounds were told "declare p0 as
    signed(42 downto 0) ... no resize() around any factor" next to the line
    with the resize() kept, and the line never changed."""
    from agents.rtl_agent.lint.rtl_linter import _width_issues
    details = {int(re.match(r"line (\d+)", i["detail"]).group(1)): i["detail"] for i in _width_issues(FARROW_WIDTHS)}
    assert "'p0' is already 28 bits: write `p0 := xm * c0;` (12 + 16 = 28 bits" in details[17]
    assert "42 downto 0" not in details[17]
    # the target is the wrong size too: declared from the factors' width names, never a typed number
    assert ("declare `variable e_v : signed(I_W+D_W-1 downto 0);` and write `e_v := ymid_i_s8 * d_i_s8;` "
            "(12 + 13 = 25 bits") in details[23]
    assert "downto 0);` and write `e_v := resize(" not in details[23]


def test_other_lines_are_counted_with_the_width_a_product_is_told_to_take():
    """Same run, round 4 -> 5: `v2m1_2` (16 bits) was told to become 32, and
    `c0 := V2_ONE + V2_QUARTER * v2m1_2` was told, on the same round, "a
    variable of exactly 32 bits". Both done, the product was 48 bits and the
    new helper was the next round's mistake."""
    from agents.rtl_agent.lint.rtl_linter import _width_issues
    issues = _width_issues(FARROW_WIDTHS)
    assert all(set(i) == {"code", "detail"} for i in issues)        # the hand-over between the checks stays inside
    details = {int(re.match(r"line (\d+)", i["detail"]).group(1)): i["detail"] for i in issues}
    assert "declare `variable v2m1_2 : signed(V2_W+V2_W-1 downto 0);`" in details[15]
    assert details[16].startswith("line 16: 'c0' is 16 bits but the right-hand side is 48 bits (counted with "
                                  "`v2m1_2` at 32 bits, as the message for line 15 asks)")
    assert "a signal or variable of exactly 48 bits" in details[16]
    # the register a resized product is copied into hears about it on the same round
    assert "counted with `e_v` at 25 bits, as the message for line 23 asks" in details[24]
    assert "`e_s9 <= resize(e_v, 26);`" in details[24]
    # a same-named variable of another process keeps its own width
    other = FARROW_WIDTHS + ("p_x : process(aclk)\n  variable e_v : signed(E_W-1 downto 0);\nbegin\n"
                             "  e_s9 <= e_v;\nend process;\n")
    assert sorted(n for n in (int(re.match(r"line (\d+)", i["detail"]).group(1)) for i in _width_issues(other))) == \
        [15, 16, 17, 23, 24]


# -- values packed in one port: {I[11:0], Q[11:0]} ------------------------------
PACKED_SPEC = {
    "entity_name": "iq",
    "ports": [{"name": "s_axis_tdata", "direction": "input", "width": "24", "type": "std_logic_vector"},
              {"name": "m_axis_tdata", "direction": "output", "width": "24", "type": "std_logic_vector"}],
    "behavior": ["Input s_axis_tdata is 24 bits: {I[11:0], Q[11:0]}, signed Q1.11.",
                 "Output m_axis_tdata is 24 bits: {I[11:0], Q[11:0]}, signed Q1.11, saturated."],
}
PACKED_RTL = """
architecture rtl of iq is
  signal i_s1, q_s1 : signed(11 downto 0);
begin
  p_s1 : process(aclk)
  begin
    if rising_edge(aclk) then
      i_s1 <= signed(s_axis_tdata(23 downto 12));
      q_s1 <= signed(s_axis_tdata(11 downto 0));
    end if;
  end process p_s1;
  m_axis_tdata <= std_logic_vector(sat12(i_s1) & sat12(q_s1));
end architecture rtl;
"""


def test_values_read_from_the_other_values_bits_are_reported():
    """timing_recovery 2026-11-11 13:09: `i_s1 <= signed(s_axis_tdata(11 downto 0))`
    with the spec's {I[11:0], Q[11:0]} (I in the top bits)."""
    from agents.rtl_agent.lint.rtl_linter import _packed_field_order_issues, _packed_ports

    assert _packed_ports(PACKED_SPEC)["s_axis_tdata"][1] == [("i", 23, 12), ("q", 11, 0)]
    assert _packed_field_order_issues(PACKED_SPEC, PACKED_RTL) == []
    swapped = PACKED_RTL.replace("i_s1 <= signed(s_axis_tdata(23 downto 12));", "i_s1 <= signed(s_axis_tdata(11 downto 0));") \
                        .replace("q_s1 <= signed(s_axis_tdata(11 downto 0));", "q_s1 <= signed(s_axis_tdata(23 downto 12));", 1)
    found = _packed_field_order_issues(PACKED_SPEC, swapped)
    assert [i["code"] for i in found] == ["packed_field_order"] * 2
    assert "`i_s1` takes bits 11 downto 0 of `s_axis_tdata`, and those bits are Q, not I" in found[0]["detail"]
    assert "Use `s_axis_tdata(23 downto 12)` for I" in found[0]["detail"]
    # names without a word of their own count when the twin exists: xi_s1 / xq_s1
    twins = swapped.replace("i_s1", "xi_s1").replace("q_s1", "xq_s1")
    assert len(_packed_field_order_issues(PACKED_SPEC, twins)) == 2
    # a whole-word read and a name that says nothing are left alone
    assert _packed_field_order_issues(PACKED_SPEC, swapped.replace("i_s1", "a_s1").replace("q_s1", "b_s1")) == []
    assert _packed_field_order_issues({**PACKED_SPEC, "behavior": ["Input is I then Q."]}, swapped) == []


def test_an_output_joined_in_the_other_order_is_reported():
    from agents.rtl_agent.lint.rtl_linter import _packed_field_order_issues

    joined = PACKED_RTL.replace("sat12(i_s1) & sat12(q_s1)", "sat12(q_s1) & sat12(i_s1)")
    found = _packed_field_order_issues(PACKED_SPEC, joined)
    assert len(found) == 1 and "`m_axis_tdata` is joined as {Q, I}" in found[0]["detail"]
    sliced = PACKED_RTL.replace("m_axis_tdata <= std_logic_vector(sat12(i_s1) & sat12(q_s1));",
                                "m_axis_tdata(23 downto 12) <= std_logic_vector(q_s1);\n"
                                "  m_axis_tdata(11 downto 0) <= std_logic_vector(q_s1);")
    found = _packed_field_order_issues(PACKED_SPEC, sliced)
    assert len(found) == 1 and "bits 23 downto 12 of `m_axis_tdata`, and those bits are I, not Q" in found[0]["detail"]


# ── worked examples (skills/RTL_EXAMPLES, chosen by rtl_examples.py) ─────

TIMING_SPEC = {**SPEC, "entity_name": "timing_recovery",
               "description": "Symbol timing recovery using Gardner TED and a parabolic Farrow interpolator."}


def test_a_worked_example_is_shown_only_for_the_kind_of_design_it_is_for():
    """timing_recovery 2026-11-11 13:01: p_loop, given in full by the rules, came
    back right; the Farrow rows, given as one summary line, came back wrong in a
    different way on each attempt. The complete design is now shown, but only to
    a spec of that kind: a counter is never handed it."""
    from agents.rtl_agent.rtl_examples import EXAMPLE_LABEL, keywords_of, select_example
    files = {"timing.md": "applies_when: Farrow, gardner, timing recovery\n\n# title\ncode",
             "fir.md": "applies_when: fir filter\nfir code", "plain.md": "no header line"}
    assert keywords_of(files["timing.md"]) == ["farrow", "gardner", "timing recovery"]
    assert keywords_of(files["plain.md"]) == []
    assert select_example(files, TIMING_SPEC) == {EXAMPLE_LABEL + "timing.md": "# title\ncode"}   # header line not shown
    assert select_example(files, SPEC) == {}                                # a counter: no example
    assert select_example({}, TIMING_SPEC) == {}
    both = {"description": "FIR filter in front of a Gardner timing recovery with a Farrow interpolator"}
    assert list(select_example(files, both)) == [EXAMPLE_LABEL + "timing.md"]   # one at most: the best fit


def test_the_worked_example_reaches_the_generator_after_the_rules():
    example = {"WORKED EXAMPLE timing.md": "entity timing_recovery ..."}
    agent = make_agent([GOOD_RTL], [CLEAN], examples=lambda spec: example if "Farrow" in str(spec) else {})
    out = agent.run(TIMING_SPEC)
    assert list(agent.tools.generator.calls[0]["skills"]) == ["vhdl_93_design_rules.md", "WORKED EXAMPLE timing.md"]
    assert out["skills_used"] == ["vhdl_93_design_rules.md", "WORKED EXAMPLE timing.md"]
    agent = make_agent([GOOD_RTL], [CLEAN], examples=lambda spec: example if "Farrow" in str(spec) else {})
    assert agent.run(SPEC)["skills_used"] == ["vhdl_93_design_rules.md"]


def test_a_missing_example_folder_is_a_warning_and_the_rules_are_still_used():
    def broken(spec):
        raise RuntimeError("cannot list skills/RTL_EXAMPLES")
    agent = make_agent([GOOD_RTL], [CLEAN], examples=broken)
    out = agent.run(TIMING_SPEC)
    assert out["status"] == RTLStatus.COMPLETE.value and out["skills_used"] == ["vhdl_93_design_rules.md"]
    assert any("worked example not loaded" in w for w in out["warnings"])


def test_the_shipped_timing_recovery_example_fits_the_task_spec_and_follows_the_static_rules():
    """The file on disk: it is picked for the Task 2 spec text, its stage map
    is there, and the five mistakes it exists to prevent are not in its code."""
    from pathlib import Path
    from agents.rtl_agent.rtl_examples import EXAMPLES_DIR, select_example
    root = Path(__file__).resolve().parents[1]
    files = {p.name: p.read_text(encoding="utf-8") for p in (root / EXAMPLES_DIR).glob("*.md")}
    picked = select_example(files, TIMING_SPEC)
    assert list(picked) == ["WORKED EXAMPLE timing_recovery_farrow_gardner.md"]
    text = next(iter(picked.values()))
    assert not text.lower().startswith("applies_when") and "stage map:" in text
    code = re.search(r"```vhdl\n(.*?)```", text, re.S).group(1)
    assert "i_s1 <= signed(s_axis_tdata(23 downto 12));" in code            # I is the high half
    assert "p1_i_s4  <= v2_i_s3 * mu_s3;" in code and "p2_i_s6  <= h_i_s5 * mu_s5;" in code   # two multiplies by mu
    assert "ymid_i_s7 <= resize(shift_right(p2m_i_s6, MU_F + 1), Y_W)" in code                # y_mid has its own path
    assert "v2m_i_s3 <= resize(x2_i_s2, V_W) - resize(x3_i_s2, V_W) - resize(x4_i_s2, V_W) + resize(x5_i_s2, V_W);" in code
    from agents.rtl_agent.lint.rtl_linter import _static_checks, _stage_map_issues, _pipeline_stage_skip_issues
    spec = {"entity_name": "timing_recovery", "ports": [{"name": n} for n in ("aclk", "aresetn", "s_axis_tdata", "m_axis_tuser")]}
    rtl = {"code": code, "notes": [text[text.index("stage map:"):text.index("What each row does")]]}
    assert _static_checks(spec, rtl) == [] and _stage_map_issues(rtl) == [] and _pipeline_stage_skip_issues(code) == []


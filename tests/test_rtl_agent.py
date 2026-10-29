"""RTL agent tests — no LLM, no MCP, no Vivado: generator, planner, skill
loader and linter are fakes; the graph, guardrails and stall/limit logic
are real."""

from __future__ import annotations

import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

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


def make_agent(rtls, lints, decider=None, loader=None, max_iterations=6):
    tools = RTLAgentTools(generator=FakeGenerator(rtls), decider=decider or FakeDecider(),
                          skill_loader=loader or FakeSkillLoader(), linter=fake_linter(lints))
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
                          skill_loader=FakeSkillLoader(), linter=fake_linter([CLEAN]))
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


def test_invalid_llm_rtl_output_is_kept_as_code():
    gen = RTLGenerator(llm=FakeListChatModel(responses=["here is some vhdl, not json"]))
    rtl = gen.generate(SPEC)
    assert rtl["code"].startswith("here is")
    assert "did not return valid JSON" in rtl["notes"][0]
    assert parse_rtl_response(SPEC, '{"entity_name": "counter4", "code": "x"}')["code"] == "x"


def test_comparing_with_an_others_aggregate_is_caught_before_xelab_would_refuse_it():
    from agents.rtl_agent.rtl_linter import _forbidden_constructs
    bad = "if count_next = (others => '0') then\nif v /= (others => '1') then"
    found = _forbidden_constructs(bad, "93")
    assert [i["code"] for i in found] == ["forbidden_construct"] * 2
    assert "line 1" in found[0]["detail"] and "xelab" in found[0]["detail"]
    ok = "count_reg <= (others => '0');\nv := (others => '1');\nif count_reg = 0 then\nif v = \"0000\" then"
    assert _forbidden_constructs(ok, "93") == [] and _forbidden_constructs(ok, "2008") == []


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
    from agents.rtl_agent.rtl_linter import _pipeline_stage_skip_issues
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
    from agents.rtl_agent.rtl_linter import _multiple_driver_issues, _multiplier_width_issues

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
    from agents.rtl_agent.rtl_linter import _process_declaration_issues
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
    from agents.rtl_agent.rtl_linter import _unbalanced_parentheses_issues
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
    from agents.rtl_agent.rtl_linter import _stage_map_issues
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
    from agents.rtl_agent.rtl_linter import _valid_data_length_issues
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
    from agents.rtl_agent.rtl_linter import (
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
    from agents.rtl_agent import rtl_smoke_check as smoke
    out = ("ERROR: Array sizes do not match, left array has 16 elements, right array has 32 elements\n"
           "Time: 55 ns  Iteration: 1  Process: /smoke_tb/dut/line__107  File: x.vhd")
    code = "x\n" * 106 + "process(aclk)\n" + CALC_MMSB
    text = smoke._crash_excerpt(out, code)
    assert "[in the process starting at RTL line 107: `process(aclk)`]" in text
    assert "16-bit target given a 32-bit value" in text and "line 115: `res := resize(x, 16) * to_signed(4, 16);`" in text
    assert "line 117:" in text                                            # the sum line too
    assert "line 119" not in text                                         # the correct shift line is not blamed


def test_width_arithmetic_of_simple_expressions():
    from agents.rtl_agent.rtl_widths import expr_width
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
    from agents.rtl_agent.rtl_linter import _abs_without_sign_check_issues
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

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

"""RTL agent tests — no LLM, no MCP, no Vivado: generator, planner, skill
loader and linter are fakes; the graph, guardrails and stall/limit logic
are real."""

from __future__ import annotations

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

    def generate(self, spec, skills=None, previous_rtl=None, lint_issues=None):
        self.calls.append({"skills": dict(skills or {}), "previous": previous_rtl, "lint_issues": list(lint_issues or [])})
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

    def load(self):
        if self.error:
            raise RuntimeError(self.error)
        return dict(self.skills)


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


def test_clean_lint_finalizes():
    out = make_agent([GOOD_RTL], [CLEAN]).run(SPEC)
    assert out["status"] == RTLStatus.COMPLETE.value
    assert out["rtl"] == GOOD_RTL and out["lint"] == CLEAN
    assert out["error"] is None and out["warnings"] == []


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


def test_agent_only_has_restricted_actions():
    assert {a.value for a in Action} == {"load_skills", "generate_rtl", "lint_rtl", "finalize_rtl"}
    nodes = set(make_agent([GOOD_RTL], [CLEAN]).graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"decide", "load_skills", "generate_rtl", "lint_rtl", "finalize_rtl"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

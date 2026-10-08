"""Test Vector agent tests — no LLM, no MCP, no MATLAB: generator, planner,
skill loader and validator are fakes; the graph, guardrails and
stall/limit logic are real."""

from __future__ import annotations

import pytest

from agents.test_vector_agent.state import Action, TestVectorStatus
from agents.test_vector_agent.test_vector_agent import TestVectorAgent
from agents.test_vector_agent.test_vector_decider import Decision, TestVectorDecider
from agents.test_vector_agent.test_vector_generator import TestVectorGenerator, parse_test_vector_response
from agents.test_vector_agent.test_vector_skill_loader import TestVectorSkillLoader
from agents.test_vector_agent.tools import TestVectorAgentTools

SPEC = {"entity_name": "counter4", "ports": [{"name": "clk", "width": "1"}, {"name": "count", "width": "4"}],
        "behavior": ["count up"]}
GOOD = {"script": "gen_test_vectors.m", "csv_file": "test_vectors.csv", "columns": ["cycle", "clk", "count"],
        "num_vectors": 40, "scenarios": ["reset", "count"], "notes": []}
BAD = {**GOOD, "columns": ["cycle", "clk"], "num_vectors": 0}
SKILLS = {"matlab_golden_model_rules.md": "- preallocate one matrix"}
CLEAN = {"csv": "x/test_vectors.csv", "num_vectors": 40, "columns": ["cycle", "clk", "count"], "issues": []}
DIRTY = {"csv": "x/test_vectors.csv", "num_vectors": 0, "columns": ["cycle", "clk"],
         "issues": [{"code": "port_column_missing", "detail": "No column for port 'count'."}]}


class FakeGenerator(TestVectorGenerator):
    def __init__(self, results):
        super().__init__(llm=object())
        self.results = list(results)
        self.calls: list[dict] = []

    def generate(self, spec, run_dir, skills=None, previous_result=None, validation_issues=None):
        self.calls.append({"run_dir": run_dir, "skills": dict(skills or {}), "previous": previous_result,
                           "issues": list(validation_issues or [])})
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class FakeDecider(TestVectorDecider):
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
        if not summary["has_result"] or (summary["validation"] or {}).get("issues"):
            return Decision(Action.GENERATE_VECTORS, "rule")
        return Decision(Action.FINALIZE_VECTORS, "rule")


class FakeSkillLoader(TestVectorSkillLoader):
    def __init__(self, skills=SKILLS, error=None):
        self.skills, self.error = skills, error

    def load(self):
        if self.error:
            raise RuntimeError(self.error)
        return dict(self.skills)


def fake_validator(results):
    queue = list(results)

    def validate(spec, run_dir, result):
        return queue.pop(0) if len(queue) > 1 else queue[0]
    return validate


def make_agent(results, validations, decider=None, loader=None, max_iterations=6):
    tools = TestVectorAgentTools(generator=FakeGenerator(results), decider=decider or FakeDecider(),
                                 skill_loader=loader or FakeSkillLoader(), validator=fake_validator(validations))
    return TestVectorAgent(tools=tools, max_iterations=max_iterations)


def test_skills_are_loaded_first_and_passed_to_the_generator(tmp_path):
    agent = make_agent([GOOD], [CLEAN])
    out = agent.run(SPEC, str(tmp_path))
    assert out["status"] == TestVectorStatus.COMPLETE.value
    assert agent.tools.decider.seen[0]["skills_loaded"] is False
    call = agent.tools.generator.calls[0]
    assert call["skills"] == SKILLS and call["run_dir"] == str(tmp_path)
    assert out["skills_used"] == ["matlab_golden_model_rules.md"] and out["run_dir"] == str(tmp_path)
    assert out["iteration"] == 3


def test_clean_validation_finalizes(tmp_path):
    out = make_agent([GOOD], [CLEAN]).run(SPEC, str(tmp_path))
    assert out["status"] == TestVectorStatus.COMPLETE.value
    assert out["result"] == GOOD and out["validation"] == CLEAN and out["error"] is None


def test_validation_issues_trigger_a_fix_with_feedback(tmp_path):
    agent = make_agent([BAD, GOOD], [DIRTY, CLEAN])
    out = agent.run(SPEC, str(tmp_path))
    assert out["status"] == TestVectorStatus.COMPLETE.value
    assert agent.tools.generator.calls[1]["previous"] == BAD
    assert agent.tools.generator.calls[1]["issues"] == DIRTY["issues"]
    assert out["iteration"] == 4


def test_fixes_that_change_nothing_end_in_needs_review(tmp_path):
    out = make_agent([BAD], [DIRTY]).run(SPEC, str(tmp_path))
    assert out["status"] == TestVectorStatus.NEEDS_REVIEW.value
    assert "fix attempts" in out["error"] and out["result"] == BAD


def test_finalize_is_blocked_while_issues_exist(tmp_path):
    forced = [Decision(Action.LOAD_SKILLS), Decision(Action.GENERATE_VECTORS), Decision(Action.FINALIZE_VECTORS, "wrong")]
    agent = make_agent([BAD, GOOD], [DIRTY, CLEAN], decider=FakeDecider(forced))
    out = agent.run(SPEC, str(tmp_path))
    assert out["status"] == TestVectorStatus.COMPLETE.value
    assert agent.tools.generator.calls[1]["issues"] == DIRTY["issues"]


def test_skill_loader_failure_is_a_warning_not_a_failure(tmp_path):
    out = make_agent([GOOD], [CLEAN], loader=FakeSkillLoader(error="mcp down")).run(SPEC, str(tmp_path))
    assert out["status"] == TestVectorStatus.COMPLETE.value
    assert out["skills_used"] == [] and any("mcp down" in w for w in out["warnings"])


def test_generator_failure_fails_the_run(tmp_path):
    class Exploding(FakeGenerator):
        def generate(self, *a, **k):
            raise RuntimeError("matlab down")
    tools = TestVectorAgentTools(generator=Exploding([GOOD]), decider=FakeDecider(),
                                 skill_loader=FakeSkillLoader(), validator=fake_validator([CLEAN]))
    out = TestVectorAgent(tools=tools).run(SPEC, str(tmp_path))
    assert out["status"] == TestVectorStatus.FAILED.value and "matlab down" in out["error"]


def test_iteration_limit_returns_needs_review(tmp_path):
    validations = [{**DIRTY, "issues": [{"code": "x", "detail": f"problem {i}"}]} for i in range(10)]
    forced = [Decision(Action.LOAD_SKILLS)] + [Decision(Action.GENERATE_VECTORS)] * 10
    out = make_agent([BAD], validations, decider=FakeDecider(forced), max_iterations=4).run(SPEC, str(tmp_path))
    assert out["status"] == TestVectorStatus.NEEDS_REVIEW.value
    assert out["iteration"] == 4 and "Iteration limit" in out["error"]


def test_run_dir_is_named_from_the_entity_when_not_given():
    out = make_agent([GOOD], [CLEAN]).run(SPEC)
    from pathlib import Path
    run_dir = Path(out["run_dir"])
    assert run_dir.parent.name == "test_vectors" and run_dir.name.startswith("counter4_")
    assert not run_dir.exists()       # created by the MATLAB MCP tool on first write, not by the agent


def test_invalid_planner_output_falls_back_safely():
    assert TestVectorDecider.parse("garbage", {"skills_loaded": False}).action is Action.LOAD_SKILLS
    assert TestVectorDecider.parse("??", {"skills_loaded": True, "has_result": False}).action is Action.GENERATE_VECTORS
    assert TestVectorDecider.parse("??", {"skills_loaded": True, "has_result": True, "validation": None}).action is Action.VALIDATE_VECTORS
    assert TestVectorDecider.parse("??", {"skills_loaded": True, "has_result": True, "validation": DIRTY}).action is Action.GENERATE_VECTORS
    assert TestVectorDecider.parse("??", {"skills_loaded": True, "has_result": True, "validation": CLEAN}).action is Action.FINALIZE_VECTORS


def test_invalid_llm_output_becomes_a_failure_summary():
    out = parse_test_vector_response("not json")
    assert out["num_vectors"] == 0 and "did not return valid JSON" in out["notes"][0]
    assert parse_test_vector_response('{"csv_file": "test_vectors.csv", "num_vectors": 3}')["num_vectors"] == 3


def test_agent_only_has_restricted_actions():
    assert {a.value for a in Action} == {"load_skills", "generate_vectors", "validate_vectors", "finalize_vectors"}
    nodes = set(make_agent([GOOD], [CLEAN]).graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"decide", "load_skills", "generate_vectors", "validate_vectors", "finalize_vectors"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

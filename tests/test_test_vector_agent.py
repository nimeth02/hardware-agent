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
GOOD = {"script": "gen_test_vectors.m", "input_csv": "test_vectors_in.csv", "output_csv": "test_vectors_out.csv",
        "input_columns": ["cycle", "clk"], "output_columns": ["cycle", "count"],
        "num_vectors": 40, "scenarios": ["reset", "count"], "notes": []}
BAD = {**GOOD, "output_columns": ["cycle"], "num_vectors": 0}
SKILLS = {"matlab_golden_model_rules.md": "- preallocate two matrices"}
CLEAN = {"csv": {"inputs": "x/test_vectors_in.csv", "outputs": "x/test_vectors_out.csv"}, "num_vectors": 40,
         "columns": {"inputs": ["cycle", "clk"], "outputs": ["cycle", "count"]}, "issues": []}
DIRTY = {**CLEAN, "num_vectors": 0, "columns": {"inputs": ["cycle", "clk"], "outputs": ["cycle"]},
         "issues": [{"code": "port_column_missing", "detail": "test_vectors_out.csv: no column for output port 'count'."}]}


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
    assert out["input_csv"] == "test_vectors_in.csv" and out["output_csv"] == "test_vectors_out.csv"
    assert parse_test_vector_response('{"input_csv": "test_vectors_in.csv", "num_vectors": 3}')["num_vectors"] == 3


def test_validator_checks_both_files_their_layout_and_that_they_line_up(monkeypatch):
    import agents.test_vector_agent.test_vector_validator as v
    spec = {"ports": [{"name": "clk", "direction": "input", "width": "1"},
                      {"name": "rst", "direction": "input", "width": "1"},
                      {"name": "count", "direction": "output", "width": "4"}]}
    files = {"test_vectors_in.csv": "cycle,clk,rst\n0,1,1\n1,1,0\n2,1,0\n",
             "test_vectors_out.csv": "cycle,count\n0,0\n1,1\n2,2\n"}
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: files.get(name))

    out = v.validate_test_vectors(spec, "x", {})
    assert out["issues"] == [] and out["num_vectors"] == 3
    assert out["columns"] == {"inputs": ["cycle", "clk", "rst"], "outputs": ["cycle", "count"]}

    files["test_vectors_in.csv"] = "cycle,rst,clk\n0,1,1\n1,0,1\n"           # wrong order + fewer rows
    files["test_vectors_out.csv"] = "cycle,count\n0,0\n5,1\n2,16\n"          # cycle 5 != 1, 16 does not fit 4 bits
    codes = [i["code"] for i in v.validate_test_vectors(spec, "x", {})["issues"]]
    assert "column_order" in codes and "row_count_mismatch" in codes and "cycle_mismatch" in codes
    assert "out_of_range" in codes

    del files["test_vectors_out.csv"]
    codes = [i["code"] for i in v.validate_test_vectors(spec, "x", {})["issues"]]
    assert "csv_missing" in codes


def test_run_guard_rejects_unknown_scripts_without_counting_a_failure():
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator import RunAttemptsExceeded, _guard_run_tool

    runs: list[str] = []

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        runs.append(script_name)
        return '{"status": "error", "error": "boom"}'

    tools = _guard_run_tool([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="write"),
                             StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="run")],
                            max_failures=2)
    gen, run = {t.name: t for t in tools}["generate_matlab_script"], {t.name: t for t in tools}["run_matlab_script"]

    async def scenario():
        out = await run.ainvoke({"script_name": "dir", "project_dir": "x"})       # never written: misuse
        assert "REJECTED" in out and runs == []
        await gen.ainvoke({"filename": "gen_test_vectors.m", "code": "x", "project_dir": "x"})
        assert '"error"' in await run.ainvoke({"script_name": "gen_test_vectors", "project_dir": "x"})   # failure 1
        assert "REJECTED" in await run.ainvoke({"script_name": "gen_test_vectors", "project_dir": "x"})  # unchanged
        await gen.ainvoke({"filename": "gen_test_vectors", "code": "y", "project_dir": "x"})
        with pytest.raises(RunAttemptsExceeded):                                                            # failure 2 = cap
            await run.ainvoke({"script_name": "gen_test_vectors", "project_dir": "x"})
    asyncio.run(scenario())
    assert runs == ["gen_test_vectors", "gen_test_vectors"]


def test_scripts_using_integer_classes_are_rejected_before_being_written():
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator import _forbidden_matlab, _guard_run_tool

    assert _forbidden_matlab("x = int16(a) + bitshift(b, -2); % uint8( in a comment is fine") == ["bitshift", "int16"]
    assert _forbidden_matlab("v = mod(x, 2^12); if v >= 2^11, v = v - 2^12; end") == []

    written: list[str] = []

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        written.append(filename)
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "ok", "output": "wrote 40 rows"}'

    tools = _guard_run_tool([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                             StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")])
    gen = {t.name: t for t in tools}["generate_matlab_script"]

    async def scenario():
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": "y = uint32(5);", "project_dir": "x"})
        assert "REJECTED" in out and "uint32" in out and written == []
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": "y = mod(5, 2^32);", "project_dir": "x"})
        assert "REJECTED" not in out and written == ["gen_test_vectors.m"]
    asyncio.run(scenario())


def test_agent_only_has_restricted_actions():
    assert {a.value for a in Action} == {"load_skills", "generate_vectors", "validate_vectors", "finalize_vectors"}
    nodes = set(make_agent([GOOD], [CLEAN]).graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"decide", "load_skills", "generate_vectors", "validate_vectors", "finalize_vectors"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

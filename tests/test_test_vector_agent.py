"""Test Vector agent tests — no LLM, no MCP, no MATLAB: generator, planner,
skill loader and validator are fakes; the graph, guardrails and
stall/limit logic are real."""

from __future__ import annotations

import pytest

from agents.test_vector_agent.state import Action, TestVectorStatus
from agents.test_vector_agent.test_vector_agent import TestVectorAgent
from agents.test_vector_agent.test_vector_decider import Decision, TestVectorDecider
from agents.test_vector_agent.test_vector_generator_agent.test_vector_generator import TestVectorGenerator, parse_test_vector_response
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

    def generate(self, spec, run_dir, skills=None, previous_result=None, validation_issues=None,
                 attempt=1, attempt_history=None):
        self.calls.append({"run_dir": run_dir, "skills": dict(skills or {}), "previous": previous_result,
                           "issues": list(validation_issues or []), "attempt": attempt,
                           "attempt_history": list(attempt_history or [])})
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
    assert out["requirement_id"] is None and out["attempt"] == 1            # called on its own: no orchestrator note


EARLIER = [{"attempt": 1, "status": "NEEDS_REVIEW", "error": "fix attempts left the validation issues unchanged",
            "issues": DIRTY["issues"], "likely_cause": None}]


def test_the_agent_knows_which_orchestrator_call_it_is_and_tells_the_generator(tmp_path):
    agent = make_agent([GOOD], [CLEAN])
    out = agent.run(SPEC, str(tmp_path), requirement_id="counter_T-11.35_D-16.10.2026", attempt=2, attempt_history=EARLIER)
    assert out["requirement_id"] == "counter_T-11.35_D-16.10.2026" and out["attempt"] == 2
    assert agent.tools.generator.calls[0]["attempt"] == 2                   # the generator is told it is the 2nd call
    assert agent.tools.generator.calls[0]["attempt_history"] == EARLIER      # and how the 1st one went
    first_call = make_agent([GOOD], [CLEAN])
    first_call.run(SPEC, str(tmp_path))                                     # called on its own: 1st call, no history
    assert first_call.tools.generator.calls[0]["attempt"] == 1
    assert first_call.tools.generator.calls[0]["attempt_history"] == []


def test_on_a_later_attempt_the_generator_prompt_carries_the_orchestrator_feedback():
    system1, payload1 = TestVectorGenerator._prompt(SPEC, "x", None, None, None)
    system2, payload2 = TestVectorGenerator._prompt(SPEC, "x", None, None, None, attempt=2, attempt_history=EARLIER)
    assert "attempt 2" not in system1 and "attempt_history" not in payload1   # 1st call: prompt unchanged
    assert "This is attempt 2 for this requirement" in system2
    assert payload2["attempt_history"] == EARLIER and payload2["spec"] == SPEC


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
    import agents.test_vector_agent.lint.test_vector_validator as v
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
    issues = v.validate_test_vectors(spec, "x", {})["issues"]
    missing = next(i for i in issues if i["code"] == "csv_missing")
    assert "['cycle', 'count']" in missing["detail"]                           # the fix prompt gets the exact header

    files["test_vectors_out.csv"] = "cycle,cnt\n0,0\n1,1\n"                   # wrong port name
    issues = v.validate_test_vectors(spec, "x", {})["issues"]
    column = next(i for i in issues if i["code"] == "port_column_missing")
    assert "'count'" in column["detail"] and "['cycle', 'count']" in column["detail"] and "['cycle', 'cnt']" in column["detail"]


def test_an_input_beat_on_a_blocked_row_is_rejected(monkeypatch):
    """llr_demap16 run 2026-10-29 12:00: rows with s_axis_tvalid = 1 and
    m_axis_tready = 0 made the testbench wait forever (no_marker)."""
    import agents.test_vector_agent.lint.test_vector_validator as v
    spec = {"ports": [{"name": n, "direction": d, "width": w} for n, d, w in [
        ("aclk", "input", "1"), ("s_axis_tvalid", "input", "1"), ("s_axis_tdata", "input", "8"),
        ("m_axis_tready", "input", "1"), ("s_axis_tready", "output", "1"),
        ("m_axis_tvalid", "output", "1"), ("m_axis_tdata", "output", "8")]]}
    files = {"test_vectors_in.csv": "cycle,aclk,s_axis_tvalid,s_axis_tdata,m_axis_tready\n"
                                    "0,1,1,5,1\n1,1,0,0,0\n2,1,1,6,0\n3,1,1,7,0\n4,1,1,8,1\n",
             "test_vectors_out.csv": "cycle,s_axis_tready,m_axis_tvalid,m_axis_tdata\n"
                                     "0,1,1,10\n1,1,0,0\n2,1,1,12\n3,1,1,14\n4,1,1,16\n"}
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: files.get(name))

    issues = v.validate_test_vectors(spec, "x", {})["issues"]
    blocked = [i for i in issues if i["code"] == "beat_while_blocked"]
    assert len(blocked) == 1 and "row 4" in blocked[0]["detail"]            # first offending row only

    files["test_vectors_in.csv"] = ("cycle,aclk,s_axis_tvalid,s_axis_tdata,m_axis_tready\n"
                                    "0,1,1,5,1\n1,1,0,0,0\n2,1,0,0,0\n3,1,0,0,0\n4,1,1,8,1\n")
    assert [i["code"] for i in v.validate_test_vectors(spec, "x", {})["issues"]] == []


def _stream_spec(constraints=()):
    return {"ports": [{"name": n, "direction": d, "width": w} for n, d, w in [
        ("aclk", "input", "1"), ("aresetn", "input", "1"), ("s_axis_tvalid", "input", "1"),
        ("s_axis_tdata", "input", "24"), ("m_axis_tready", "input", "1"), ("s_axis_tready", "output", "1"),
        ("m_axis_tvalid", "output", "1"), ("m_axis_tdata", "output", "24")]],
        "reset": {"name": "aresetn", "active": "low"},
        "constraints": list(constraints)}


def _stream_files(in_values, out_beat_every, out_value=None):
    """Rows 0-1 hold reset; one input beat per later row with the given data
    values; an expected output beat on every `out_beat_every`-th input row
    (0 = never). The beat's m_axis_tdata is the input value of that row, or
    `out_value(beat_index)` when given."""
    in_lines, out_lines = ["cycle,aclk,aresetn,s_axis_tvalid,s_axis_tdata,m_axis_tready"], ["cycle,s_axis_tready,m_axis_tvalid,m_axis_tdata"]
    for c in range(2):
        in_lines.append(f"{c},1,0,0,0,1")
        out_lines.append(f"{c},1,0,0")
    nbeat = 0
    for i, val in enumerate(in_values):
        c = i + 2
        beat = 1 if out_beat_every and (i + 1) % out_beat_every == 0 else 0
        data = (out_value(nbeat) if out_value else val) if beat else 0
        nbeat += beat
        in_lines.append(f"{c},1,1,1,{val},1")
        out_lines.append(f"{c},1,{beat},{data}")
    return {"test_vectors_in.csv": "\n".join(in_lines) + "\n", "test_vectors_out.csv": "\n".join(out_lines) + "\n"}


def test_a_constant_input_column_is_rejected(monkeypatch):
    """timing_recovery run 2026-11-05 15:54: the golden model divided 0 by 0,
    every input became 8390656 and only one output beat was expected; every
    layout check passed and the analyst blamed the RTL."""
    import agents.test_vector_agent.lint.test_vector_validator as v
    spec = _stream_spec(["Output strobe rate is approximately one per 4 input clocks."])
    files = _stream_files([8390656] * 40, out_beat_every=4)
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: files.get(name))

    issues = v.validate_test_vectors(spec, "x", {})["issues"]
    constant = [i for i in issues if i["code"] == "constant_input"]
    assert len(constant) == 1 and "8390656" in constant[0]["detail"] and "isfinite" in constant[0]["detail"]
    assert "beat_rate_mismatch" not in [i["code"] for i in issues]         # 10 beats for 40 inputs is right

    files = _stream_files(list(range(100, 140)), out_beat_every=4)           # varying input: clean
    assert v.validate_test_vectors(spec, "x", {})["issues"] == []

    files = _stream_files([7] * 5, out_beat_every=0)                          # too small to judge
    assert v.validate_test_vectors(spec, "x", {})["issues"] == []


def test_an_expected_beat_count_far_from_the_spec_rate_is_rejected(monkeypatch):
    import agents.test_vector_agent.lint.test_vector_validator as v
    spec = _stream_spec(["Output strobe rate is approximately one per 4 input clocks."])
    files = _stream_files(list(range(100, 140)), out_beat_every=40)          # 1 beat for 40 inputs
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: files.get(name))

    issues = v.validate_test_vectors(spec, "x", {})["issues"]
    rate = [i for i in issues if i["code"] == "beat_rate_mismatch"]
    assert len(rate) == 1 and "40 input beats but 1 expected" in rate[0]["detail"] and "per 4 input" in rate[0]["detail"]
    # The fix text names the real cause seen on 2026-11-06 (loop run on
    # integer codes) and the two asserts that make MATLAB catch it itself.
    assert "raw integer codes" in rate[0]["detail"] and "abs(v) < 1/N" in rate[0]["detail"]

    files = _stream_files(list(range(100, 140)), out_beat_every=1)           # 40 beats: too many
    assert "beat_rate_mismatch" in [i["code"] for i in v.validate_test_vectors(spec, "x", {})["issues"]]

    files = _stream_files(list(range(100, 140)), out_beat_every=3)           # 13 beats for ~10: within 2x
    assert v.validate_test_vectors(spec, "x", {})["issues"] == []

    # No rate in the spec: only "never any output beat" is reported.
    no_rate = _stream_spec(["Output is saturated to 12 bits."])
    files = _stream_files(list(range(100, 140)), out_beat_every=40)
    assert v.validate_test_vectors(no_rate, "x", {})["issues"] == []
    files = _stream_files(list(range(100, 140)), out_beat_every=0)
    assert [i["code"] for i in v.validate_test_vectors(no_rate, "x", {})["issues"]] == ["no_output_beats"]


def test_a_nearly_constant_input_is_rejected_for_rated_designs(monkeypatch):
    """timing_recovery run 2026-11-06 11:45: 99 identical symbols plus one
    flipped symbol (396 of 400 beats the same) slipped past the exact
    'one constant' check and the loop had nothing to lock onto."""
    import agents.test_vector_agent.lint.test_vector_validator as v
    spec = _stream_spec(["Output strobe rate is approximately one per 4 input clocks."])
    files = _stream_files([4195328] * 39 + [12585984], out_beat_every=4, out_value=lambda k: 100 + k)
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: files.get(name))

    issues = v.validate_test_vectors(spec, "x", {})["issues"]
    constant = [i for i in issues if i["code"] == "constant_input"]
    assert len(constant) == 1 and "39 of 40 input beats" in constant[0]["detail"] and "randi" in constant[0]["detail"]

    # 8 of 40 identical (80 %) is a normal stimulus.
    files = _stream_files([5] * 8 + list(range(100, 132)), out_beat_every=4, out_value=lambda k: 100 + k)
    assert v.validate_test_vectors(spec, "x", {})["issues"] == []

    # No rate in the spec (a plain filter): a step response is allowed to sit on one value.
    no_rate = _stream_spec(["Output is saturated to 12 bits."])
    files = _stream_files([4195328] * 39 + [12585984], out_beat_every=1)
    assert v.validate_test_vectors(no_rate, "x", {})["issues"] == []


def test_a_nearly_constant_output_is_rejected_for_rated_designs(monkeypatch):
    """Same run: 190 of 193 expected beats were 4195328 because the loop
    repeated the first symbol forever; every earlier check passed."""
    import agents.test_vector_agent.lint.test_vector_validator as v
    spec = _stream_spec(["Output strobe rate is approximately one per 4 input clocks."])
    files = _stream_files(list(range(100, 140)), out_beat_every=2, out_value=lambda k: 4195328 if k else 0)
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: files.get(name))

    issues = v.validate_test_vectors(spec, "x", {})["issues"]
    constant = [i for i in issues if i["code"] == "constant_output"]
    assert len(constant) == 1 and "4195328 on 19 of 20" in constant[0]["detail"] and "abs(v) < 1/N" in constant[0]["detail"]

    # Mostly-zero output (impulse response, warm-up zeros) is not a loop blow-up.
    files = _stream_files(list(range(100, 140)), out_beat_every=2, out_value=lambda k: 0 if k < 19 else 777)
    assert v.validate_test_vectors(spec, "x", {})["issues"] == []

    # 9 of 10 identical (90 %) stays under the 95 % line.
    files = _stream_files(list(range(100, 140)), out_beat_every=4, out_value=lambda k: 4195328 if k else 0)
    assert v.validate_test_vectors(spec, "x", {})["issues"] == []

    files = _stream_files(list(range(100, 140)), out_beat_every=4)              # varying output: clean
    assert v.validate_test_vectors(spec, "x", {})["issues"] == []


def test_an_all_zero_expected_output_is_rejected_when_the_input_varies(monkeypatch):
    """timing_recovery 2026-11-06 18:47: the pulse filter ran on the symbols
    BEFORE the x4 upsample, so 3 of 4 input samples were 0 and every strobe
    (mu = 0) read a zero sample: 99 expected beats, all 0, all mu 0. The
    warm-up exemption for zeros above let it through and attempt 1
    ("expected 99 got 97") tested nothing. Every beat the same number while
    the input varies is a test that proves nothing, zero included."""
    import agents.test_vector_agent.lint.test_vector_validator as v
    spec = _stream_spec(["Output strobe rate is approximately one per 4 input clocks."])
    files = _stream_files(list(range(100, 140)), out_beat_every=2, out_value=lambda k: 0)
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: files.get(name))

    issues = v.validate_test_vectors(spec, "x", {})["issues"]
    constant = [i for i in issues if i["code"] == "constant_output"]
    assert len(constant) == 1 and "0 on ALL 20 expected output beats" in constant[0]["detail"]
    assert "40 different values" in constant[0]["detail"] and "Upsample first" in constant[0]["detail"]

    # a constant input with an all-zero output is the constant_input case, reported once
    files = _stream_files([7] * 40, out_beat_every=2, out_value=lambda k: 0)
    codes = [i["code"] for i in v.validate_test_vectors(spec, "x", {})["issues"]]
    assert codes.count("constant_input") == 1 and "constant_output" not in codes

    # 19 of 20 zero then a real value (warm-up) still passes, as the test above checks; 20 of 20 does not
    files = _stream_files(list(range(100, 140)), out_beat_every=2, out_value=lambda k: 0 if k < 19 else 777)
    assert v.validate_test_vectors(spec, "x", {})["issues"] == []


def test_output_beats_on_many_consecutive_rows_are_rejected(monkeypatch):
    """Same run: the strobe fired on every cycle from cycle 12 on (193 beats
    for 400 inputs), which the 2x beat-count tolerance accepted."""
    import agents.test_vector_agent.lint.test_vector_validator as v
    spec = _stream_spec(["Output strobe rate is approximately one per 4 input clocks."])
    # 400 inputs, 193 beats: 8 spaced beats then one on every row -> count passes, run fails.
    in_lines, out_lines = ["cycle,aclk,aresetn,s_axis_tvalid,s_axis_tdata,m_axis_tready"], ["cycle,s_axis_tready,m_axis_tvalid,m_axis_tdata"]
    for i in range(400):
        beat = 1 if (i < 32 and i % 4 == 3) or 200 <= i < 385 else 0
        in_lines.append(f"{i},1,1,1,{1000 + i},1")
        out_lines.append(f"{i},1,{beat},{2000 + i if beat else 0}")
    files = {"test_vectors_in.csv": "\n".join(in_lines) + "\n", "test_vectors_out.csv": "\n".join(out_lines) + "\n"}
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: files.get(name))

    issues = v.validate_test_vectors(spec, "x", {})["issues"]
    assert "beat_rate_mismatch" not in [i["code"] for i in issues]            # 193 for 400 is inside 2x
    run = [i for i in issues if i["code"] == "consecutive_output_beats"]
    assert len(run) == 1 and "185 consecutive rows" in run[0]["detail"] and "real values" in run[0]["detail"]

    # Two neighbouring beats now and then (rate error) are fine; 8 in a row are not.
    files = _stream_files(list(range(100, 140)), out_beat_every=3)
    assert v.validate_test_vectors(spec, "x", {})["issues"] == []


def test_spec_output_rate_reads_the_common_phrasings():
    from agents.test_vector_agent.lint.test_vector_validator import spec_output_rate
    assert spec_output_rate({"constraints": ["Output strobe rate is approximately one per 4 input clocks."]}) == 4
    assert spec_output_rate({"behavior": "Emit 1 symbol every 8 samples."}) == 8
    assert spec_output_rate({"description": "Decimate by 16 then filter."}) == 16
    assert spec_output_rate({"description": "A 2:1 decimation stage."}) == 2
    assert spec_output_rate({"behavior": "Produces every 5th cycle one output"}) == 5
    assert spec_output_rate({"description": "A plain FIR filter with 12-bit output."}) is None
    assert spec_output_rate(None) is None


def test_run_guard_rejects_unknown_scripts_without_counting_a_failure():
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import guard_matlab_tools
    from agents.test_vector_agent.test_vector_generator_agent.state import RunAttemptsExceeded

    runs: list[str] = []

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        runs.append(script_name)
        return '{"status": "error", "error": "boom"}'

    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="write"),
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
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import forbidden_matlab, guard_matlab_tools

    assert forbidden_matlab("x = int16(a) + bitshift(b, -2); % uint8( in a comment is fine") == ["bitshift", "int16"]
    assert forbidden_matlab("v = mod(x, 2^12); if v >= 2^11, v = v - 2^12; end") == []

    written: list[str] = []

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        written.append(filename)
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "ok", "output": "wrote 40 rows"}'

    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")])
    gen = {t.name: t for t in tools}["generate_matlab_script"]

    async def scenario():
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": "y = uint32(5);", "project_dir": "x"})
        assert "REJECTED" in out and "uint32" in out and written == []
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": "y = mod(5, 2^32);", "project_dir": "x"})
        assert "REJECTED" not in out and written == ["gen_test_vectors.m"]
    asyncio.run(scenario())


def test_scripts_using_non_matlab_operators_are_rejected_before_being_written():
    """2026-10-29 llr_demap16: every first attempt copied the spec's `>>> 13`
    into the script and MATLAB stopped with 'Invalid use of operator'."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import forbidden_operators, guard_matlab_tools

    assert forbidden_operators("r = (p + 4096) >>> 13;") == [">>>"]
    assert forbidden_operators("if a != b, n += 1; end  % x >>> 2 in a comment is fine") == ["!=", "+="]
    assert forbidden_operators("fprintf('rows >> %d\\n', n); r = floor((p + 4096) / 2^13); if a ~= b, n = n + 1; end") == []

    written: list[str] = []

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        written.append(filename)
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "ok", "output": "wrote 40 rows"}'

    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")])
    gen = {t.name: t for t in tools}["generate_matlab_script"]

    async def scenario():
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": "r = (p + 4096) >>> 13;", "project_dir": "x"})
        assert "REJECTED" in out and ">>>" in out and "floor(x / 2^k)" in out and written == []
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": "r = floor((p + 4096) / 2^13);", "project_dir": "x"})
        assert "REJECTED" not in out and written == ["gen_test_vectors.m"]
    asyncio.run(scenario())


def test_scripts_that_wrap_a_signed_value_before_a_shift_or_saturation_are_rejected():
    """llr_demap16, 2026-10-30 11:51: `min(max(floor((mod(mm*nsv, 2^24) + 4096)
    / 2^13), -32), 31)` meant "signed 24-bit product" but mod() made every
    negative LLR +31; 14 of 20 expected beats were wrong, the RTL was right."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import guard_matlab_tools, wrapped_signed_math

    bad = "llr = @(m, ns) arrayfun(@(mm, nsv) min(max(floor((mod(mm*nsv, 2^24) + 4096) / 2^13), -32), 31), m, ns);"
    assert wrapped_signed_math(bad) == ["mod(mm*nsv, 2^24)"]
    assert wrapped_signed_math("v = min(max(mod(8*256 - 4*abs(x), 2^16), -32), 31);") == ["mod(8*256 - 4*abs(x), 2^16)"]
    assert wrapped_signed_math("r = floor(mod(a - b, 2^12) / 4);") == ["mod(a - b, 2^12)"]
    # the right way, and the legitimate uses of mod(): pack, wrap a counter, extract a field, stimulus patterns
    assert wrapped_signed_math("r = floor((m * ns + 4096) / 2^13); v = min(max(r, -32), 31); w = mod(v, 2^6) * 2^12;") == []
    assert wrapped_signed_math("tdata = mod(I, 2^12) * 2^20 + mod(Q, 2^12) * 2^8 + mod(ns, 2^8);") == []
    assert wrapped_signed_math("state = mod(state + 1, 16); f = mod(floor(word / 2^20), 2^12); en = floor(mod(c, 7) / 2);") == []
    assert wrapped_signed_math("x = floor(mod(-v, 2^8) / 2);") == []                  # unary minus is a sign, not a difference
    assert wrapped_signed_math("% r = floor(mod(a*b, 2^24) / 8) in a comment is fine\nr = floor((a*b + 4096) / 2^13);") == []

    written: list[str] = []

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        written.append(filename)
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "ok", "output": "wrote 40 rows"}'

    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")])
    gen = {t.name: t for t in tools}["generate_matlab_script"]

    async def scenario():
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": bad, "project_dir": "x"})
        assert "REJECTED" in out and "mod(mm*nsv, 2^24)" in out and "16752640" in out and written == []
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": "r = floor((m * ns + 4096) / 2^13);", "project_dir": "x"})
        assert "REJECTED" not in out and written == ["gen_test_vectors.m"]
    asyncio.run(scenario())

    from pathlib import Path
    from services.config import REPO_ROOT
    rules = Path(REPO_ROOT, "skills", "TEST_VECTOR", "matlab_golden_model_rules.md").read_text(encoding="utf-8")
    assert "NEVER wrap a signed intermediate value with mod" in rules


def test_scripts_that_pack_a_spec_field_at_the_wrong_bit_are_rejected():
    """Run 170022 on 2026-10-29: `mod(I,4096)*2^12` for a field the spec puts
    at [31:20]; every expected beat was wrong while the RTL was right."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import (
        guard_matlab_tools, missing_field_positions, spec_fields)

    spec = {"ports": [{"name": "s_axis_tdata", "direction": "input", "width": "32"},
                      {"name": "m_axis_tdata", "direction": "output", "width": "24"}],
            "behavior": ["Input format: s_axis_tdata[31:20] = I (signed), [19:8] = Q, [7:0] = ns",
                         "packed into m_axis_tdata[23:18]=b3, [17:12]=b2, [11:6]=b1, [5:0]=b0",
                         "saturate to range [-32, +31]; other_signal[15:4] = x is not a port"]}
    assert [(p, hi, lo, n) for p, hi, lo, n in spec_fields(spec)] == [
        ("s_axis_tdata", 31, 20, "I"), ("m_axis_tdata", 23, 18, "b3")]
    wrong = "tdata = mod(I,4096)*2^12 + mod(Q,4096)*2^8 + ns; out = u6(b3)*2^18 + u6(b2)*2^12 + u6(b1)*2^6 + u6(b0);"
    missing = missing_field_positions(spec, wrong)
    assert len(missing) == 1 and missing[0].startswith("s_axis_tdata[31:20] = I starts at bit 20")
    right = ("assert(all(I >= -2^11 & I <= 2^11-1), 'I does not fit 12 bits');\n"
             + wrong.replace("mod(I,4096)*2^12", "mod(I,4096)*2^20"))
    assert missing_field_positions(spec, right) == []
    assert missing_field_positions(spec, "x = 1; % s_axis_tdata[31:20] = I * 2^20 only in a comment") != []
    assert missing_field_positions(None, wrong) == []                      # no spec, nothing to check

    written: list[str] = []

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        written.append(filename)
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")], spec=spec)
    gen = {t.name: t for t in tools}["generate_matlab_script"]

    async def scenario():
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": wrong, "project_dir": "x"})
        assert "REJECTED" in out and "2^20" in out and written == []
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": right, "project_dir": "x"})
        assert "REJECTED" not in out and written == ["gen_test_vectors.m"]
    asyncio.run(scenario())


def test_failed_script_is_kept_under_a_numbered_name(tmp_path):
    """The next attempt overwrites gen_test_vectors.m, so the crashed version
    was unreadable afterwards (run 164152)."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import guard_matlab_tools

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        (tmp_path / filename).write_text(code)
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "error", "output": "", "error": "Line: 14 Column: 27 Invalid use of operator."}'

    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")], max_failures=5)
    by = {t.name: t for t in tools}

    async def scenario():
        await by["generate_matlab_script"].ainvoke({"filename": "gen_test_vectors.m", "code": "bad one", "project_dir": str(tmp_path)})
        await by["run_matlab_script"].ainvoke({"script_name": "gen_test_vectors", "project_dir": str(tmp_path)})
        await by["generate_matlab_script"].ainvoke({"filename": "gen_test_vectors.m", "code": "second", "project_dir": str(tmp_path)})
        assert (tmp_path / "gen_test_vectors.failed1.m").read_text() == "bad one"
        assert (tmp_path / "gen_test_vectors.m").read_text() == "second"
    asyncio.run(scenario())


def test_agent_only_has_restricted_actions():
    assert {a.value for a in Action} == {"load_skills", "generate_vectors", "validate_vectors", "finalize_vectors"}
    nodes = set(make_agent([GOOD], [CLEAN]).graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"decide", "load_skills", "generate_vectors", "validate_vectors", "finalize_vectors"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_old_csvs_are_deleted_before_every_matlab_run_and_the_last_run_is_recorded(tmp_path):
    """Run 161047: the script was rewritten, its new run crashed, and the CSVs of
    the earlier (different) script stayed on disk and passed validation."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import guard_matlab_tools
    from agents.test_vector_agent.test_vector_generator_agent.state import new_run_state

    stale = tmp_path / "test_vectors_in.csv"
    stale.write_text("cycle,old\n0,0\n")
    (tmp_path / "gen_test_vectors.m").write_text("x = 1;")
    seen_at_run: list[bool] = []

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        seen_at_run.append(stale.exists())
        return '{"status": "error", "error": "Undefined function at line 163"}' if len(seen_at_run) == 1 \
            else '{"status": "ok", "output": "wrote 60 rows"}'

    state = new_run_state()
    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")],
                               state=state)
    gen, run = {t.name: t for t in tools}["generate_matlab_script"], {t.name: t for t in tools}["run_matlab_script"]

    async def scenario():
        assert state["last_run"] is None
        await gen.ainvoke({"filename": "gen_test_vectors", "code": "x", "project_dir": str(tmp_path)})
        await run.ainvoke({"script_name": "gen_test_vectors", "project_dir": str(tmp_path)})
        assert state["last_run"]["status"] == "error"
        assert state["last_run"]["error"].startswith('{"status": "error", "error": "Undefined function at line 163"}')
        await gen.ainvoke({"filename": "gen_test_vectors", "code": "y", "project_dir": str(tmp_path)})
        await run.ainvoke({"script_name": "gen_test_vectors", "project_dir": str(tmp_path)})
        assert state["last_run"] == {"status": "ok", "error": ""}
    asyncio.run(scenario())
    assert seen_at_run == [False, False]          # the stale CSV was gone before MATLAB started, both times
    assert (tmp_path / "gen_test_vectors.m").exists()   # only CSVs are cleared, never the script


def test_a_beat_during_reset_is_rejected(monkeypatch):
    """llr_demap16 run 2026-10-29: the golden model wrote the first beat on
    cycle 3 while aresetn was still 0, so the design dropped it and every
    later beat was compared with the wrong expected value."""
    import agents.test_vector_agent.lint.test_vector_validator as v
    spec = {"reset": {"name": "aresetn", "active": "low"},
            "ports": [{"name": n, "direction": d, "width": w} for n, d, w in [
                ("aclk", "input", "1"), ("aresetn", "input", "1"), ("s_axis_tvalid", "input", "1"),
                ("s_axis_tdata", "input", "8"), ("m_axis_tready", "input", "1"), ("s_axis_tready", "output", "1"),
                ("m_axis_tvalid", "output", "1"), ("m_axis_tdata", "output", "8")]]}
    header_in = "cycle,aclk,aresetn,s_axis_tvalid,s_axis_tdata,m_axis_tready\n"
    header_out = "cycle,s_axis_tready,m_axis_tvalid,m_axis_tdata\n"
    files = {"test_vectors_in.csv": header_in + "0,1,0,0,0,1\n1,1,0,1,5,1\n2,1,0,1,6,1\n3,1,1,1,7,1\n",
             "test_vectors_out.csv": header_out + "0,0,0,0\n1,0,1,10\n2,0,1,12\n3,1,1,14\n"}
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: files.get(name))

    issues = [i for i in v.validate_test_vectors(spec, "x", {})["issues"] if i["code"] == "beat_during_reset"]
    assert len(issues) == 1                                                     # first offending row only
    assert "row 3" in issues[0]["detail"] and "s_axis_tvalid = 1" in issues[0]["detail"]
    assert "aresetn = 0" in issues[0]["detail"]

    # an expected output beat on a reset row is just as wrong
    files["test_vectors_in.csv"] = header_in + "0,1,0,0,0,1\n1,1,0,0,0,1\n2,1,1,1,6,1\n"
    files["test_vectors_out.csv"] = header_out + "0,0,0,0\n1,0,1,10\n2,1,1,12\n"
    issues = [i for i in v.validate_test_vectors(spec, "x", {})["issues"] if i["code"] == "beat_during_reset"]
    assert len(issues) == 1 and "row 3" in issues[0]["detail"] and "m_axis_tvalid = 1" in issues[0]["detail"]

    # beats only after the reset is released: clean
    files["test_vectors_in.csv"] = header_in + "0,1,0,0,0,1\n1,1,0,0,0,1\n2,1,1,1,6,1\n"
    files["test_vectors_out.csv"] = header_out + "0,0,0,0\n1,0,0,0\n2,1,1,12\n"
    assert [i["code"] for i in v.validate_test_vectors(spec, "x", {})["issues"]] == []

    # an active-high reset named rst: 1 means "in reset"
    spec_hi = {**spec, "reset": {"name": "rst", "active": "high"},
               "ports": [{**p, "name": "rst"} if p["name"] == "aresetn" else p for p in spec["ports"]]}
    files["test_vectors_in.csv"] = header_in.replace("aresetn", "rst") + "0,1,1,1,5,1\n1,1,0,1,6,1\n"
    files["test_vectors_out.csv"] = header_out + "0,0,1,10\n1,1,1,12\n"
    issues = [i for i in v.validate_test_vectors(spec_hi, "x", {})["issues"] if i["code"] == "beat_during_reset"]
    assert len(issues) == 1 and "row 2" in issues[0]["detail"] and "rst = 1" in issues[0]["detail"]


def test_scripts_that_never_check_a_field_fits_its_width_are_rejected():
    """llr_demap16, 2026-10-29 16:41: gen_test_vectors.m drew I = +2048 for a
    12-bit signed field; mod(I, 4096) wrapped it to -2048 on the wire and
    beats 5-6 were reported wrong while the RTL was right."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import (
        guard_matlab_tools, missing_field_range_asserts)

    spec = {"ports": [{"name": "s_axis_tdata", "direction": "input", "width": "32"},
                      {"name": "m_axis_tdata", "direction": "output", "width": "24"}],
            "behavior": ["s_axis_tdata[31:20] = I (signed), s_axis_tdata[19:8] = Q (signed), "
                         "s_axis_tdata[7:0] = ns (unsigned)",
                         "m_axis_tdata[23:18] = b3 is an output field: no stimulus assert needed"]}
    unchecked = ("I = randi([-2048, 2048], 1, 8); tdata = mod(I,4096)*2^20 + mod(Q,4096)*2^8 + mod(ns,256); "
                 "out = u6(b3)*2^18;")
    missing = missing_field_range_asserts(spec, unchecked)
    assert [m.split(" (")[0] for m in missing] == ["s_axis_tdata[31:20] = I", "s_axis_tdata[19:8] = Q",
                                                   "s_axis_tdata[7:0] = ns"]
    assert "2^11" in missing[0] and "12 bits" in missing[0]
    checked = ("assert(all(I >= -2^11 & I <= 2^11-1), 'I');\n"
               "assert(all(Q >= -2048 & Q <= 2047), 'Q');\n"          # literal bounds count too
               "assert(all(ns >= 0 & ns <= 2^8-1), 'ns');\n" + unchecked)
    assert missing_field_range_asserts(spec, checked) == []
    # an assert with the wrong bound, or only in a comment, does not count
    assert len(missing_field_range_asserts(spec, "assert(all(I <= 2^15), 'I');\n" + unchecked)) == 3
    assert len(missing_field_range_asserts(spec, "% assert(all(I <= 2^11-1))\n" + unchecked)) == 3
    assert missing_field_range_asserts(None, unchecked) == []

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")], spec=spec)
    gen = {t.name: t for t in tools}["generate_matlab_script"]

    async def scenario():
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": unchecked, "project_dir": "x"})
        assert "REJECTED" in out and "fit their bit width" in out and "assert(all(I" in out
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": checked, "project_dir": "x"})
        assert "REJECTED" not in out
    asyncio.run(scenario())


def test_scripts_for_a_rated_stream_must_assert_their_beat_count():
    """timing_recovery, 2026-11-06 run 2a912a0f: four scripts ran the Gardner
    loop on the integer codes, strobed 1-8 times in 400+ samples and wrote the
    CSVs; only the outer validator caught it (beat_rate_mismatch), one attempt
    later. The guard now wants the script to check its own beat count."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import (
        guard_matlab_tools, missing_beat_count_assert)

    stream = [{"name": "s_axis_tvalid", "direction": "input", "width": "1"},
              {"name": "m_axis_tvalid", "direction": "output", "width": "1"}]
    rated = {"ports": stream, "behavior": ["Output strobe rate is approximately one per 4 input clocks."]}
    unchecked = "for c = 0:399\n  if strobe, nOut = nOut + 1; end\nend\nfprintf('%d beats', nOut);\n"
    msg = missing_beat_count_assert(rated, unchecked)
    assert msg and "one output per 4" in msg and "nOut >= nIn/4/2" in msg and "abs(v) < 1/4" in msg

    checked = unchecked + "assert(nOut >= nIn/4/2 && nOut <= nIn/4*2, '%d beats for %d', nOut, nIn);\n"
    assert missing_beat_count_assert(rated, checked) is None
    other_name = unchecked + "assert(numel(strobe_cycles) > 50, 'too few strobes');\n"      # any beat-count word
    assert missing_beat_count_assert(rated, other_name) is None
    assert missing_beat_count_assert(rated, unchecked + "% assert(nOut > 50)\n") is not None  # a comment is not a check
    assert missing_beat_count_assert(rated, unchecked + "assert(all(I <= 2047), 'I');\n") is not None  # other asserts do not count
    # no rate in the spec, or not a streaming design: the rule does not apply
    assert missing_beat_count_assert({"ports": stream, "behavior": ["Output is saturated."]}, unchecked) is None
    assert missing_beat_count_assert({"ports": [], "behavior": rated["behavior"]}, unchecked) is None
    assert missing_beat_count_assert(None, unchecked) is None

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")], spec=rated)
    gen = {t.name: t for t in tools}["generate_matlab_script"]

    async def scenario():
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": unchecked, "project_dir": "x"})
        assert "REJECTED" in out and "output beat count" in out and "assert(nOut" in out
        # the finite-stimulus rule (next test) also applies to a streaming spec
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": checked + "assert(all(isfinite(x)), 'x');\n", "project_dir": "x"})
        assert "REJECTED" not in out
    asyncio.run(scenario())


def test_scripts_for_a_stream_must_assert_a_finite_stimulus():
    """timing_recovery 2026-11-06 16:06 (run bb27cb1d): a pulse tap divided by
    k^2-1 = 0, normalising made the whole pulse NaN, saturation turned every
    sample into -2048 and every input word into 8390656; the range asserts
    passed and only the validator's constant_input caught it, one attempt later."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import (
        guard_matlab_tools, missing_finite_assert)

    stream = {"ports": [{"name": "s_axis_tvalid", "direction": "input", "width": "1"},
                        {"name": "s_axis_tdata", "direction": "input", "width": "24"},
                        {"name": "m_axis_tvalid", "direction": "output", "width": "1"}],
              "behavior": ["Output strobe rate is approximately one per 4 input clocks."]}
    unchecked = ("h = h / max(abs(h));\ncI = min(max(round(xI * 2048), -2048), 2047);\n"
                 "assert(all(cI >= -2048 & cI <= 2047), 'I code');\n"
                 "assert(nOut >= nIn/4/2 && nOut <= nIn/4*2, 'beats');\n")
    msg = missing_finite_assert(stream, unchecked)
    assert msg and "isfinite" in msg and "8390656" in msg and "before any saturate/pack" in msg

    checked = "assert(all(isfinite(xI)) && all(isfinite(xQ)), 'stimulus has NaN');\n" + unchecked
    assert missing_finite_assert(stream, checked) is None
    assert missing_finite_assert(stream, "assert(isfinite(x(i)), 'x');\n" + unchecked) is None   # any isfinite assert
    assert missing_finite_assert(stream, "% assert(all(isfinite(xI)))\n" + unchecked) is not None  # comment only
    assert missing_finite_assert(stream, "ok = all(isfinite(xI));\n" + unchecked) is not None      # not an assert
    counter = {"ports": [{"name": "count", "direction": "output", "width": "4"}]}
    assert missing_finite_assert(counter, unchecked) is None                                  # not a stream
    assert missing_finite_assert(None, unchecked) is None

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")], spec=stream)
    gen = {t.name: t for t in tools}["generate_matlab_script"]

    async def scenario():
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": unchecked, "project_dir": "x"})
        assert "REJECTED" in out and "isfinite" in out
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": checked, "project_dir": "x"})
        assert "REJECTED" not in out
    asyncio.run(scenario())


def test_a_failed_run_comes_back_with_the_line_that_crashed_and_why():
    """Runs 115142 / 115908 on 2026-10-30: MATLAB said "line 43" and "line 42",
    the LLM never saw those lines, guessed, and rewrote the script with a new bug."""
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import crash_note

    code = "edgen = [ 64, 64, 64, 64, 0, 255, 127, 127 ];\nIb = [Ib; edge];\nQb = [Qb; edgeQ];\n"
    err = ('{"status": "error", "error": "\\n  File C:\\\\x\\\\gen_test_vectors.m, line 2, in gen_test_vectors\\n'
           'Dimensions of arrays being concatenated are not consistent.\\n"}')
    note = crash_note(code, err)
    assert note.startswith("WHERE IT CRASHED (gen_test_vectors.m):")
    assert "-->   2 | Ib = [Ib; edge];" in note and "      1 | edgen" in note and "      3 | Qb" in note
    assert "WHY:" in note and "row vector" in note and "a = [a, b];" in note
    assert "FIX:" in note and "do not start a new script" in note

    err = ('{"status": "error", "error": "line 2, in gen_test_vectors\\n'
           'Index exceeds the number of array elements. Index must not exceed 36."}')
    note = crash_note("x = 1;\nI = I_vals(bi);\n", err)
    assert "-->   2 | I = I_vals(bi);" in note and "same length" in note and "nbeats" in note

    # syntax errors say "Line: N Column: M"; a line number past the end of the script is not quoted
    note = crash_note("a = 1;\nr = (p + 4096) >>> 13;", '{"error": "Line: 2 Column: 20 Invalid use of operator."}')
    assert "-->   2 | r = (p + 4096) >>> 13;" in note and "floor(x / 2^k)" in note
    note = crash_note("x = 1;", '{"error": "Undefined function at line 163"}')
    assert "WHERE IT CRASHED" not in note and note.startswith("FIX:")

    # the script's own assert fired: the values are wrong, not the check
    note = crash_note("nbeats = 29;\nassert(nbeats == numel(Ivals), 'beat count mismatch');",
                      '{"error": "line 2, in gen_test_vectors\\nbeat count mismatch"}')
    assert "Your own assert" in note

    # 2026-11-06: an RRC conv() result added to a shorter preallocated list
    note = crash_note("x = zeros(1, 10);\nx = x + 1.5*a;", '{"error": "line 2\\nArrays have incompatible sizes for this operation."}')
    assert "conv(a, b) returns numel(a)+numel(b)-1" in note and "size() of BOTH sides" in note
    assert "y = y(1:N)" in note and "matlab_eval" in note

    # timing_recovery 2026-11-06 16:48: inst(i+1) with i running to nIn = numel(inst); the generic
    # list-length hint was the wrong guess, the loop and the shift are the real cause
    code = "inst = 0.25 + (0:nsym-1) * sps;\nfor i = 1:nIn\n    xs(i) = x(floor(inst(i+1)) + 1);   % sample\nend\n"
    err = '{"error": "line 3, in gen_test_vectors\\nIndex exceeds the number of array elements. Index must not exceed 100."}'
    note = crash_note(code, err)
    assert "-->   3 | " in note and "for i = 1:nIn` reads inst(i+1)" in note and "entry nIn+1 of inst" in note
    assert "for i = 1:nIn-1" in note and "inst_next = inst(2:end)" in note and "nbeats" not in note
    # the same message on a line without a shifted read keeps the list-length hint
    note = crash_note("for i = 1:nIn\n    xs(i) = x(i);\nend\n", err.replace("line 3", "line 2"))
    assert "nbeats" in note and "reads" not in note

    # timing_recovery 2026-11-06 17:12 (iteration 3): a fixed 8-slot history read with the running
    # sample count (m = nIn - 2): slot -1 on the first strobe; the loop-variable hint was the wrong guess
    code = ("xbufI = zeros(1, 8);\nfor c = 0:N-1\n    nIn = nIn + 1;\n    xbufI = [xbufI(2:8), ci];\n"
            "    m = nIn - 2;\n    xm1 = xbufI(m); xm = xbufI(m+1); xp1 = xbufI(m+2); xp2 = xbufI(m+3);\nend\n")
    err = '{"error": "line 6, in gen_test_vectors\\nArray indices must be positive integers or logical values."}'
    note = crash_note(code, err)
    assert "-->   6 | " in note and "xbufI is a fixed history of the last 8 samples" in note
    assert "reads it with xbufI(m)" in note and "x[m+2] = xbufI(8)" in note and "x[m-3] = xbufI(3)" in note
    assert "Drop m from the index" in note and "loop\nvariable" not in note
    # a history read with fixed slots, or with a for-loop variable, is not blamed
    assert crash_note(code.replace("xbufI(m)", "xbufI(5)"), err).count("fixed history") == 1   # m+1.. still flagged
    ok = "xbufI = zeros(1, 8);\nxbufI = [xbufI(2:8), ci];\nfor j = 1:8\n    s = s + xbufI(j);\nend\n"
    assert "fixed history" not in crash_note(ok, err.replace("line 6", "line 4"))


def test_scripts_that_index_a_sample_history_with_the_sample_count_are_rejected_before_they_run():
    """timing_recovery 2026-11-06 17:12 (iteration 3): xbufI(m), xbufI(m-2) with
    m = nIn - 2 crashed on the first strobe; the script was re-sent unchanged."""
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import history_index_by_counter

    bad = ("xbufQ = zeros(1, 8);\nxbufQ = [xbufQ(2:8), cq];\nm = nIn - 2;\n"
           "xm3 = xbufQ(m-2); xm2 = xbufQ(m-1);\n")
    found = history_index_by_counter(bad)
    assert len(found) == 2 and found[0].startswith("line 4 `xm3 = xbufQ(m-2); xm2 = xbufQ(m-1);`")
    assert "xbufQ(m-2)" in found[0] and "xbufQ(m-1)" in found[1] and "x[m-2] = xbufQ(4)" in found[0]
    # fixed slots, `end`, ranges, loop variables and lists that are not shifted are fine
    assert history_index_by_counter("xbufQ = zeros(1, 8);\nxbufQ = [xbufQ(2:8), cq];\ny = xbufQ(6) + xbufQ(end) + sum(xbufQ(3:5));\n") == []
    assert history_index_by_counter("xbufQ = zeros(1, 8);\nxbufQ = [xbufQ(2:8), cq];\nfor j = 1:8\n    s = s + xbufQ(j);\nend\n") == []
    assert history_index_by_counter("xI = zeros(1, 400);\nm = nIn - 2;\ny = xI(m);\n") == []
    assert history_index_by_counter("% xbufQ = zeros(1, 8); xbufQ = [xbufQ(2:8), cq]; y = xbufQ(m);\n") == []


def test_an_assert_that_compares_a_length_with_itself_plus_an_offset_is_called_a_wrong_check():
    """timing_recovery 2026-11-06 18:46 (run ce5d6a48): `Nc = numel(Ic) + sps - 1;
    assert(numel(Ic) == Nc ...)` fired; the generic hint said "fix the values, not
    the assert", the opposite of what was needed."""
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import contradicting_asserts, crash_note

    code = ("sps = 4;\nIc = conv(sym(1,:), h);\nQc = conv(sym(2,:), h);\n"
            "Nc = numel(Ic) + sps - 1;               % samples after upsampling\n"
            "assert(numel(Ic) == Nc && numel(Qc) == Nc, 'pulse length mismatch');\n% upsample by 4\n")
    err = '{"error": "line 5, in gen_test_vectors\\npulse length mismatch"}'
    note = crash_note(code, err)
    assert "-->   5 | assert(numel(Ic) == Nc" in note
    assert "This check can never pass" in note and "Nc is defined on line 4 as `Nc = numel(Ic) + sps - 1`" in note
    assert "The CHECK is wrong, not the values of Ic" in note and "Change the Nc line or the assert, not Ic" in note
    assert "Your own assert" not in note
    # the same shape with the names swapped (N == numel(x)) and length() is caught before the script runs
    found = contradicting_asserts("N = length(x) - 1;\nassert(N == length(x), 'bad');\n")
    assert len(found) == 1 and found[0].startswith("line 2 `assert(N == length(x), 'bad');`") and "never pass" in found[0]
    # a length checked against a number built from something else is a real check and keeps the old hint
    ok = "Nc = nsym*sps + numel(h) - 1;\nassert(numel(Ic) == Nc, 'pulse length mismatch');\n"
    assert contradicting_asserts(ok) == []
    note = crash_note(ok, err.replace("line 5", "line 2"))
    assert "Your own assert" in note and "never pass" not in note
    # `N = numel(x)` with no offset is a (pointless but true) check, not a contradiction; comments are ignored
    assert contradicting_asserts("N = numel(x);\nassert(numel(x) == N);\n") == []
    assert contradicting_asserts("% N = numel(x) + 1;\nN = 10;\nassert(numel(x) == N);\n") == []


def test_scripts_whose_loop_reads_past_the_end_of_a_list_are_rejected_before_they_run():
    """timing_recovery 2026-11-06 16:48 (run cb9924de): `xs(i) = x(floor(inst(i+1)) + 1)`
    inside `for i = 1:nIn` with nIn = numel(inst) crashed, the LLM rewrote the block
    and hit three more index crashes: 6 of the attempt's failed runs on one off-by-one."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import guard_matlab_tools, loop_index_overruns

    bad = "for i = 1:nIn\n    xs(i) = x(floor(inst(i+1)) + 1);\nend\n"
    found = loop_index_overruns(bad)
    assert len(found) == 1 and found[0].startswith("line 2 `xs(i) = x(floor(inst(i+1)) + 1);`")
    assert "for i = 1:nIn` reads inst(i+1)" in found[0] and "for i = 1:nIn-1" in found[0]
    assert loop_index_overruns("for k = 1:nsym\n    d(k) = sym(k+2) - sym(k);\nend\n")[0].endswith("before the loop.")
    assert "entry 0 of x" in loop_index_overruns("for i = 1:n\n    y(i) = x(i) - x(i-1);\nend\n")[0]
    # the AI's rewrite in the same run: a 0-based loop reading symI(k) directly (entry 0 on the first pass)
    # (the read sits in a nested loop, under an `if` that guards idx, not k: still found)
    rewrite = ("for k = 0:Nsym-1\n    inst = mu0 + k*Ttrue;\n    for j = 1:numel(h)\n        idx = round(inst) + (j - 1);\n"
               "        if idx >= 1 && idx <= nIn\n            xI(idx) = xI(idx) + h(j)*symI(k);\n        end\n    end\nend\n")
    found = loop_index_overruns(rewrite)
    assert len(found) == 1 and "line 6" in found[0] and "starts at 0 and reads symI(k)" in found[0]
    assert "for k = 1:Nsym-1" in found[0] and "read symI(k+1)" in found[0]
    assert loop_index_overruns("for k = 0:Nsym-1\n    a(k) = symQ(k+1);\nend\n") == []                  # k+1 from 0 is fine
    assert loop_index_overruns("for k = 0:n-1\n    y(k+1) = x(k+1) * 2;\nend\n") == []
    assert loop_index_overruns("llr = @(m) m * 2;\nfor k = 0:n-1\n    y(k+1) = llr(k);\nend\n") == []  # a function, not a list

    # room is made for the shift: a shorter range, a padded list, a later start, a guarded read
    assert loop_index_overruns("for i = 1:nIn-1\n    d(i) = inst(i+1) - inst(i);\nend\n") == []
    assert loop_index_overruns("for i = 1:numel(inst)-1\n    d(i) = inst(i+1);\nend\n") == []
    assert loop_index_overruns("for i = 1:n-2\n    d(i) = inst(i+2);\nend\n") == []
    assert loop_index_overruns("inst = zeros(1, nIn+1);\nfor i = 1:nIn\n    d(i) = inst(i+1);\nend\n") == []
    assert loop_index_overruns("for i = 2:n\n    y(i) = x(i) - x(i-1);\nend\n") == []
    assert loop_index_overruns("for i = i0:n\n    y(i) = x(i-1);\nend\n") == []                     # start unknown
    assert loop_index_overruns("for i = 1:n\n    if i < n\n        y(i) = x(i+1);\n    end\nend\n") == []
    # not a read past the end: growing a list, a function call, a different variable, a comment
    assert loop_index_overruns("for i = 1:n\n    x(i+1) = x(i) + 1;\nend\n") == []
    assert loop_index_overruns("for i = 1:n\n    y(i) = floor(i+1) + mod(i+1, 4);\nend\n") == []
    assert loop_index_overruns("for i = 1:n\n    y(i) = x(j+1);\nend\n") == []
    assert loop_index_overruns("for i = 1:n\n    y(i) = x(i);   % not x(i+1)\nend\n") == []
    assert loop_index_overruns("for v = vals\n    y(end+1) = v;\nend\n") == []                       # loop over a list
    assert loop_index_overruns("while k < n\n    y(k) = x(k+1);\n    k = k + 1;\nend\n") == []       # not a for loop

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")])
    gen = {t.name: t for t in tools}["generate_matlab_script"]

    async def scenario():
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": bad, "project_dir": "x"})
        assert "REJECTED" in out and "reads past the end" in out and "inst(i+1)" in out
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": bad.replace("1:nIn", "1:nIn-1"), "project_dir": "x"})
        assert "REJECTED" not in out
    asyncio.run(scenario())


def test_failed_matlab_eval_snippets_count_toward_the_run_cap():
    """2026-11-06: the LLM ran seven matlab_eval snippets in a row, each failing
    with the same 'incompatible sizes' error, and only the 30-step limit stopped it
    because matlab_eval was the one tool outside the failure cap."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import guard_matlab_tools
    from agents.test_vector_agent.test_vector_generator_agent.state import RunAttemptsExceeded, new_run_state

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "error", "error": "line 3\\nArrays have incompatible sizes for this operation."}'

    async def matlab_eval(code: str, project_dir: str = "") -> str:
        if "disp(size" in code:
            return '{"status": "ok", "output": " 1 5", "error": null}'
        return '{"status": "error", "output": "", "error": "Arrays have incompatible sizes for this operation."}'

    state = new_run_state()
    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r"),
                                StructuredTool.from_function(coroutine=matlab_eval, name="matlab_eval", description="e")],
                               max_failures=3, state=state)
    by_name = {t.name: t for t in tools}
    gen, run, ev = by_name["generate_matlab_script"], by_name["run_matlab_script"], by_name["matlab_eval"]

    async def scenario():
        await gen.ainvoke({"filename": "gen_test_vectors", "code": "x = zeros(1,10);\ny = conv(h, s);\nx = x + y;", "project_dir": "p"})
        assert '"error"' in await run.ainvoke({"script_name": "gen_test_vectors", "project_dir": "p"})     # failure 1
        assert state["failures"] == 1
        out = await ev.ainvoke({"code": "h = [1 2 3]; a = conv(h, [0 0 1]); disp(size(a));"})           # a snippet that works
        assert state["failures"] == 1 and "MATLAB_EVAL FAILED" not in out
        out = await ev.ainvoke({"code": "x = zeros(1, 10); a = [1 2 3 4 5]; y = x + a;"})                # failure 2
        assert state["failures"] == 2 and "MATLAB_EVAL FAILED (2 of 3 failed runs used)" in out
        assert "conv(a, b) returns numel(a)+numel(b)-1" in out and "do not shrink the problem" in out
        assert state["last_run"]["status"] == "error"
        with pytest.raises(RunAttemptsExceeded):                                                            # failure 3 = cap
            await ev.ainvoke({"code": "x = zeros(1, 10); y = x + [1 2 3 4 5];"})
    asyncio.run(scenario())
    assert state["failures"] == 3


def test_resending_the_same_crashed_script_twice_ends_the_attempt():
    """2026-11-06 17:12 (timing_recovery): after 'REJECTED: same as one that already
    crashed' the LLM re-sent the identical script seven more times (2,626 tokens each)
    until the 30-step cap. The attempt now ends on the second unchanged re-send."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import guard_matlab_tools
    from agents.test_vector_agent.test_vector_generator_agent.state import RunAttemptsExceeded, new_run_state

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return '{"status": "error", "error": "line 1, in gen_test_vectors\\nArray indices must be positive integers or logical values."}'

    state = new_run_state()
    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")],
                               state=state, max_failures=3)
    gen, run = {t.name: t for t in tools}["generate_matlab_script"], {t.name: t for t in tools}["run_matlab_script"]

    async def scenario():
        await gen.ainvoke({"filename": "gen_test_vectors", "code": "xm1 = xbuf(m);", "project_dir": "p"})
        assert '"error"' in await run.ainvoke({"script_name": "gen_test_vectors", "project_dir": "p"})
        out = await gen.ainvoke({"filename": "gen_test_vectors", "code": "% retry\nxm1 = xbuf(m);", "project_dir": "p"})
        assert "REJECTED" in out and "(1 of 2)" in out and state["identical_rejects"] == 1
        # a real change resets the count
        await gen.ainvoke({"filename": "gen_test_vectors", "code": "xm1 = xbuf(m+1);", "project_dir": "p"})
        assert state["identical_rejects"] == 0
        assert '"error"' in await run.ainvoke({"script_name": "gen_test_vectors", "project_dir": "p"})
        out = await gen.ainvoke({"filename": "gen_test_vectors", "code": "xm1 = xbuf(m+1);  % same", "project_dir": "p"})
        assert "REJECTED" in out and state["identical_rejects"] == 1
        with pytest.raises(RunAttemptsExceeded) as exc:
            await gen.ainvoke({"filename": "gen_test_vectors", "code": "xm1 = xbuf(m+1);", "project_dir": "p"})
        assert exc.value.reason.startswith("Gave up: the script that crashed was re-sent unchanged 2 times")
        assert "Array indices must be positive" in str(exc.value) and "MATLAB lists start at 1" in str(exc.value)
    asyncio.run(scenario())
    assert state["failures"] == 2


def test_the_attempt_summary_carries_the_guards_reason():
    from agents.test_vector_agent.test_vector_generator_agent.state import RunAttemptsExceeded

    assert RunAttemptsExceeded("boom").reason == "Gave up after 3 failed MATLAB runs."
    assert RunAttemptsExceeded("boom", reason="Gave up: stuck").reason == "Gave up: stuck"


def test_guard_without_a_matlab_eval_tool_still_works():
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import guard_matlab_tools

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        return "ok"

    async def run_matlab_script(script_name: str, project_dir: str) -> str:
        return "ok"

    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")])
    assert sorted(t.name for t in tools) == ["generate_matlab_script", "run_matlab_script"]


def test_a_script_identical_to_one_that_crashed_is_rejected_without_spending_a_run():
    """Run 115142 on 2026-10-30: the 2nd and 3rd failed scripts were byte-identical,
    so the 3rd run was wasted and the attempt hit its cap."""
    import asyncio
    from langchain_core.tools import StructuredTool
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import code_fingerprint, guard_matlab_tools
    from agents.test_vector_agent.test_vector_generator_agent.state import new_run_state

    assert code_fingerprint("Ib = [Ib; edge];  % append") == code_fingerprint("Ib=[Ib; edge];\n% other comment")
    assert code_fingerprint("Ib = [Ib; edge];") != code_fingerprint("Ib = [Ib, edge];")
    assert code_fingerprint("x = [1 -2];") != code_fingerprint("x = [1 - 2];")                         # two numbers vs one
    assert code_fingerprint("names = {'cycle','clk'};") != code_fingerprint("names = {'cycle','rst'};")  # a fix inside quotes counts

    written: list[str] = []
    runs: list[str] = []

    async def generate_matlab_script(filename: str, code: str, project_dir: str) -> str:
        written.append(code)
        return '{"status": "ok"}'

    async def run_matlab_script(script_name: str, project_dir: str):
        runs.append(script_name)
        # the MCP adapter hands back content blocks, not a plain string
        return [{"type": "text", "text": '{"status": "error", "output": "", "error": "line 1, in gen_test_vectors\\n'
                                         'Dimensions of arrays being concatenated are not consistent."}'}]

    state = new_run_state()
    tools = guard_matlab_tools([StructuredTool.from_function(coroutine=generate_matlab_script, name="generate_matlab_script", description="w"),
                                StructuredTool.from_function(coroutine=run_matlab_script, name="run_matlab_script", description="r")],
                               state=state, max_failures=3)
    gen, run = {t.name: t for t in tools}["generate_matlab_script"], {t.name: t for t in tools}["run_matlab_script"]

    async def scenario():
        await gen.ainvoke({"filename": "gen_test_vectors.m", "code": "Ib = [Ib; edge];", "project_dir": "x"})
        out = await run.ainvoke({"script_name": "gen_test_vectors", "project_dir": "x"})
        assert isinstance(out, list) and out[-1]["text"].startswith("WHERE IT CRASHED (gen_test_vectors.m):")
        assert "-->   1 | Ib = [Ib; edge];" in out[-1]["text"] and "WHY:" in out[-1]["text"]
        assert "WHERE IT CRASHED" in state["last_run"]["error"] and state["failures"] == 1
        assert "[{'type'" not in state["last_run"]["error"]          # readable text, not the list's Python repr
        # the same code with a comment added: not written, not run, not counted
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": "% second try\nIb = [Ib; edge];", "project_dir": "x"})
        assert "REJECTED" in out and "already crashed" in out and written == ["Ib = [Ib; edge];"]
        assert "REJECTED" in await run.ainvoke({"script_name": "gen_test_vectors", "project_dir": "x"})
        assert runs == ["gen_test_vectors"] and state["failures"] == 1
        # a real change is written and run
        out = await gen.ainvoke({"filename": "gen_test_vectors.m", "code": "Ib = [Ib, edge];", "project_dir": "x"})
        assert "REJECTED" not in str(out) and written[-1] == "Ib = [Ib, edge];"
        await run.ainvoke({"script_name": "gen_test_vectors", "project_dir": "x"})
        assert runs == ["gen_test_vectors", "gen_test_vectors"] and state["failures"] == 2
    asyncio.run(scenario())


def test_a_closed_loop_script_that_adds_noise_is_rejected_before_it_runs():
    """2026-11-09: on a noisy stimulus the golden model's loop (reacts on the next
    sample) and a pipelined RTL's loop (reacts 4..24 samples later) never settle
    onto the same path, so a CORRECT design fails the beat-by-beat compare; on the
    clean stimulus they agree within 2 steps after 64 beats (40 seeds)."""
    from pathlib import Path
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import noisy_closed_loop_stimulus

    loop_spec = {"ports": [{"name": "s_axis_tvalid"}, {"name": "m_axis_tvalid"}, {"name": "s_axis_tdata"}],
                 "behavior": ["Loop filter output v initial value 0", "On strobe: acc = acc + K2 * e",
                              "NCO phase eta is modulo-1, initial value 0"]}
    noisy = "sym = 2*randi([0 1], 2, nsym) - 1;\nupI = upI + sigma*randn(1, Nup);\n"
    why = noisy_closed_loop_stimulus(loop_spec, noisy)
    assert why and why.startswith("line 2 `upI = upI + sigma*randn(1, Nup);` adds noise")
    assert "CORRECT design fails" in why and "p = [0.25 0.75 0.75 0.25]" in why and "at least 200 symbols" in why
    assert noisy_closed_loop_stimulus(loop_spec, "y = awgn(x, 12);\n")
    # clean stimulus, noise only named in a comment, randi (not randn): fine
    assert noisy_closed_loop_stimulus(loop_spec, "sym = 2*randi([0 1], 2, nsym) - 1;   % no randn here\n") is None
    # a streaming design with no loop (the demapper) may use noise
    demap = {"ports": loop_spec["ports"], "behavior": ["m_lsb = |I| - 2*d", "llr saturated to 8 bits"]}
    assert noisy_closed_loop_stimulus(demap, noisy) is None
    assert noisy_closed_loop_stimulus(None, noisy) is None and noisy_closed_loop_stimulus({}, noisy) is None

    root = Path(__file__).resolve().parents[1]
    matlab_rules = (root / "skills" / "TEST_VECTOR" / "matlab_golden_model_rules.md").read_text(encoding="utf-8")
    assert "CLOSED-LOOP STIMULUS" in matlab_rules and "p    = [0.25, 0.75, 0.75, 0.25];" in matlab_rules
    assert "NO noise (no randn, no awgn)" in matlab_rules
    tb_rules = (root / "skills" / "TESTBENCH" / "vhdl_2008_testbench_rules.md").read_text(encoding="utf-8")
    assert "constant SETTLE_BEATS : integer := 64;" in tb_rules and "elsif k_exp >= SETTLE_BEATS then" in tb_rules
    assert "if d_mu > 128 then d_mu := 256 - d_mu; end if;" in tb_rules and "constant TOL_MU : integer := 16;" in tb_rules


def test_a_closed_loop_script_without_a_loop_trace_is_rejected_before_it_runs():
    """2026-11-10: loop_trace.csv is what lets the RTL writer see the model's
    mu / e / v / acc per beat next to the chip's registers (inside_values)."""
    from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import missing_loop_trace

    loop_spec = {"ports": [{"name": "s_axis_tvalid"}, {"name": "m_axis_tvalid"}, {"name": "s_axis_tdata"}],
                 "behavior": ["Loop filter output v initial value 0", "On strobe: acc = acc + K2 * e"]}
    why = missing_loop_trace(loop_spec, "writetable(array2table(outs, 'VariableNames', out_names), 'test_vectors_out.csv');\n")
    assert why and why.startswith("the spec describes a closed loop but the script never writes loop_trace.csv")
    assert "'beat','cycle','mu8','e','v','acc','y_sym_i','y_sym_q','y_mid_i','y_mid_q'" in why
    assert missing_loop_trace(loop_spec, "writetable(array2table(trace, 'VariableNames', trace_names), 'loop_trace.csv');\n") is None
    demap = {"ports": loop_spec["ports"], "behavior": ["m_lsb = |I| - 2*d"]}
    assert missing_loop_trace(demap, "x = 1;\n") is None and missing_loop_trace(None, "x = 1;\n") is None


def test_expected_outputs_pinned_at_full_scale_are_rejected(monkeypatch):
    """timing_recovery 2026-11-11 17:15: the model ran on the raw integer
    codes, so 174 of 192 expected beats had I = 2047 or -2048 while no input
    went beyond 1536; every other check passed and the RTL was blamed."""
    import agents.test_vector_agent.lint.test_vector_validator as v
    from agents.test_vector_agent.lint.output_range_check import output_fields

    def word(i, q):
        return (i % 4096) * 4096 + (q % 4096)

    layouts = ["Output m_axis_tdata is 24 bits wide with layout: I = bits 23:12, Q = bits 11:0.",
               "m_axis_tdata[23:12] = I, m_axis_tdata[11:0] = Q",
               "m_axis_tdata carries {I[11:0], Q[11:0]}"]
    for layout in layouts:
        assert output_fields({"ports": [{"name": "m_axis_tdata", "width": "24"}], "behavior": [layout]},
                             "m_axis_tdata") == [("I", 23, 12), ("Q", 11, 0)], layout

    rate_line = "Output rate: approximately one recovered symbol per 4 input samples."     # the 17:15 spec's wording
    spec = _stream_spec([rate_line])
    spec["behavior"] = ["Input s_axis_tdata is 24 bits wide with layout: I = bits 23:12, Q = bits 11:0.", layouts[0]]
    assert v.spec_output_rate(spec) == 4
    stimulus = [word(1536 if k % 3 else -512, 512 if k % 5 else -1536) for k in range(80)]

    pinned = _stream_files(stimulus, out_beat_every=4, out_value=lambda k: word(2047 if k % 2 else -2048, 100 + k))
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: pinned.get(name))
    found = [i for i in v.validate_test_vectors(spec, "x", {})["issues"] if i["code"] == "saturated_output"]
    assert len(found) == 1 and "I (m_axis_tdata[23:12])" in found[0]["detail"]
    assert "20 of 20 expected output beats while no input value goes beyond 1536" in found[0]["detail"]

    # a real run: the expected values stay near the input's peak (1792 in the passing 15:49 run)
    real = _stream_files(stimulus, out_beat_every=4, out_value=lambda k: word(1792 if k % 2 else -1792, 1791 - k))
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: real.get(name))
    assert "saturated_output" not in [i["code"] for i in v.validate_test_vectors(spec, "x", {})["issues"]]

    # not judged: no output rate in the spec (a demapper), or a stimulus that itself sits at full scale
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: pinned.get(name))
    no_rate = {**spec, "constraints": []}
    assert "saturated_output" not in [i["code"] for i in v.validate_test_vectors(no_rate, "x", {})["issues"]]
    loud = _stream_files([word(2047 if k % 2 else -2048, k) for k in range(80)], out_beat_every=4,
                         out_value=lambda k: word(2047 if k % 2 else -2048, 100 + k))
    monkeypatch.setattr(v, "mcp_read_file", lambda run_dir, name: loud.get(name))
    assert "saturated_output" not in [i["code"] for i in v.validate_test_vectors(spec, "x", {})["issues"]]

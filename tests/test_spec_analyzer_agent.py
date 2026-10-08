"""Spec Analyzer agent tests — no LLM: generator and planner are fakes,
the evaluator is the real deterministic one, checkpoints are in memory."""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver

from agents.spec_analyser_agent.spec_analyzer_agent import SpecAnalyzerAgent
from agents.spec_analyser_agent.spec_decider import Decision, SpecDecider
from agents.spec_analyser_agent.spec_evaluator import evaluate_spec
from agents.spec_analyser_agent.spec_generator import SpecGenerator
from agents.spec_analyser_agent.state import Action, SpecStatus
from agents.spec_analyser_agent.tools import SpecAnalyzerTools

# ── fixtures ─────────────────────────────────────────────────────────────

COMPLETE_SPEC = {
    "status": "complete",
    "entity_name": "adder4",
    "description": "4-bit clocked adder",
    "ports": [
        {"name": "clk", "direction": "input", "width": "1", "type": "clock"},
        {"name": "rst", "direction": "input", "width": "1", "type": "reset"},
        {"name": "a", "direction": "input", "width": "4", "type": "unsigned"},
        {"name": "b", "direction": "input", "width": "4", "type": "unsigned"},
        {"name": "sum", "direction": "output", "width": "5", "type": "unsigned"},
    ],
    "clock": {"required": True, "name": "clk", "frequency_mhz": 500},
    "reset": {"required": True, "name": "rst", "type": "synchronous", "active": "high"},
    "behavior": ["sum <= a + b registered on the rising edge of clk"],
    "constraints": [], "assumptions": [], "ambiguities": [],
}

MISSING_RESET_SPEC = {
    **COMPLETE_SPEC,
    "status": "needs_clarification",
    "clock": {"required": True, "name": "clk", "frequency_mhz": None},
    "reset": {"required": True, "name": "rst", "type": None, "active": None},
    "ambiguities": ["Clock frequency not given", "Reset type and polarity not given"],
}


class FakeGenerator(SpecGenerator):
    """Returns the queued specs in order (last one repeats)."""

    def __init__(self, specs):
        super().__init__(llm=object())
        self.specs = list(specs)
        self.calls: list[dict] = []

    def generate(self, request, feedback=None, previous_spec=None):
        self.calls.append({"request": request, "feedback": list(feedback or []), "previous": previous_spec})
        return self.specs.pop(0) if len(self.specs) > 1 else self.specs[0]


class FakeDecider(SpecDecider):
    """Rule-based stand-in for the LLM planner, with optional forced decisions."""

    def __init__(self, forced: list[Decision] | None = None):
        super().__init__(llm=object())
        self.forced = list(forced or [])
        self.seen: list[dict] = []

    def decide(self, summary):
        self.seen.append(summary)
        if self.forced:
            return self.forced.pop(0)
        if summary["spec"] is None or not summary["answers_applied"]:
            return Decision(Action.GENERATE_SPEC, reason="rule: generate")
        if summary["issues"]:
            return Decision(Action.ASK_USER, "What are the clock frequency, reset type and polarity?", "rule: ask")
        return Decision(Action.FINALIZE_SPEC, reason="rule: finalize")


def raising_evaluator(spec):
    raise RuntimeError("evaluator exploded")


def make_agent(specs, decider=None, evaluator=None, max_iterations=8):
    tools = SpecAnalyzerTools(generator=FakeGenerator(specs), decider=decider or FakeDecider(),
                              evaluator=evaluator or evaluate_spec)
    return SpecAnalyzerAgent(tools=tools, checkpointer=InMemorySaver(), max_iterations=max_iterations)


# ── 1. complete specification ────────────────────────────────────────────

def test_complete_specification_finalizes_without_questions():
    agent = make_agent([COMPLETE_SPEC])
    out = agent.start("Create a 4-bit clocked adder at 500 MHz, sync active-high reset.")
    assert out["status"] == SpecStatus.COMPLETE.value
    assert out["spec"]["entity_name"] == "adder4"
    assert out["issues"] == [] and out["suggestions"] == []
    assert out["pending_question"] is None
    assert out["iteration"] == 2  # decide->generate(+evaluate), decide->finalize


# ── 2. missing information ───────────────────────────────────────────────

def test_missing_information_pauses_for_user():
    agent = make_agent([MISSING_RESET_SPEC, COMPLETE_SPEC])
    out = agent.start("Create a 4-bit clocked adder.")
    assert out["status"] == SpecStatus.WAITING_FOR_USER.value
    assert "clock frequency" in out["pending_question"].lower()
    codes = {i["code"] for i in out["issues"]}
    assert {"missing_reset_type", "missing_reset_polarity", "ambiguity"} <= codes
    assert out["spec"]["entity_name"] == "adder4"  # partial spec is still returned


# ── 3. clarification / resume ────────────────────────────────────────────

def test_resume_same_session_applies_answer_and_completes():
    agent = make_agent([MISSING_RESET_SPEC, COMPLETE_SPEC])
    first = agent.start("Create a 4-bit clocked adder.")
    session = first["session_id"]
    assert first["status"] == SpecStatus.WAITING_FOR_USER.value

    second = agent.resume(session, "500 MHz, synchronous, active high.")
    assert second["session_id"] == session
    assert second["status"] == SpecStatus.COMPLETE.value
    assert second["user_feedback"] == ["500 MHz, synchronous, active high."]
    assert second["issues"] == []
    # the generator was re-run with the answer and the previous spec
    gen = agent.tools.generator
    assert gen.calls[-1]["feedback"] == ["500 MHz, synchronous, active high."]
    assert gen.calls[-1]["previous"]["entity_name"] == "adder4"


# ── 4. multiple clarification rounds ─────────────────────────────────────

def test_multiple_clarification_rounds():
    still_missing_freq = {**COMPLETE_SPEC, "status": "needs_clarification",
                          "clock": {"required": True, "name": "clk", "frequency_mhz": None},
                          "ambiguities": ["Clock frequency still not given"]}
    agent = make_agent([MISSING_RESET_SPEC, still_missing_freq, COMPLETE_SPEC])
    r1 = agent.start("Create a 4-bit clocked adder.")
    assert r1["status"] == SpecStatus.WAITING_FOR_USER.value
    r2 = agent.resume(r1["session_id"], "synchronous, active high")
    assert r2["status"] == SpecStatus.WAITING_FOR_USER.value
    assert r2["user_feedback"] == ["synchronous, active high"]
    r3 = agent.resume(r1["session_id"], "500 MHz")
    assert r3["status"] == SpecStatus.COMPLETE.value
    assert r3["user_feedback"] == ["synchronous, active high", "500 MHz"]
    assert r3["iteration"] == 6  # 3 x (generate + ask/finalize)


# ── 5. evaluator errors ──────────────────────────────────────────────────

def test_evaluator_error_fails_the_run_gracefully():
    agent = make_agent([COMPLETE_SPEC], evaluator=raising_evaluator)
    out = agent.start("adder")
    assert out["status"] == SpecStatus.FAILED.value
    assert "evaluator exploded" in out["error"]
    assert out["pending_question"] is None


# ── 6. maximum iteration limit ───────────────────────────────────────────

def test_iteration_limit_returns_needs_review():
    # planner keeps regenerating and every regeneration changes *something*
    # (so the stall guard never fires), yet the spec never becomes complete
    import itertools

    variant = {**MISSING_RESET_SPEC, "ambiguities": ["Reset polarity unclear (variant)"]}

    class CyclingGenerator(FakeGenerator):
        def __init__(self, specs):
            super().__init__(specs)
            self._cycle = itertools.cycle(specs)

        def generate(self, request, feedback=None, previous_spec=None):
            return next(self._cycle)

    forced = [Decision(Action.GENERATE_SPEC, reason="loop")] * 20
    tools = SpecAnalyzerTools(generator=CyclingGenerator([MISSING_RESET_SPEC, variant]), decider=FakeDecider(forced))
    agent = SpecAnalyzerAgent(tools=tools, checkpointer=InMemorySaver(), max_iterations=4)
    out = agent.start("adder")
    assert out["status"] == SpecStatus.NEEDS_REVIEW.value
    assert out["iteration"] == 4
    assert "Iteration limit" in out["error"]
    assert out["spec"] is not None and out["issues"]


def test_iteration_limit_without_spec_is_failed():
    class NeverGenerates(FakeGenerator):
        def generate(self, *a, **k):
            raise RuntimeError("llm down")

    tools = SpecAnalyzerTools(generator=NeverGenerates([COMPLETE_SPEC]), decider=FakeDecider())
    agent = SpecAnalyzerAgent(tools=tools, checkpointer=InMemorySaver(), max_iterations=3)
    out = agent.start("adder")
    assert out["status"] == SpecStatus.FAILED.value
    assert "llm down" in out["error"]


# ── 7. successful finalization + guardrail ───────────────────────────────

def test_finalize_is_blocked_while_issues_exist():
    forced = [Decision(Action.GENERATE_SPEC), Decision(Action.FINALIZE_SPEC, reason="planner is wrong")]
    agent = make_agent([MISSING_RESET_SPEC, COMPLETE_SPEC], decider=FakeDecider(forced))
    out = agent.start("adder")
    assert out["status"] == SpecStatus.WAITING_FOR_USER.value      # guard downgraded finalize -> ask_user
    assert "guard" in out["decision_reason"]
    assert out["pending_question"].startswith("Please clarify")
    done = agent.resume(out["session_id"], "500 MHz, sync, active high")
    assert done["status"] == SpecStatus.COMPLETE.value


def test_finalized_result_shape():
    out = make_agent([COMPLETE_SPEC]).start("adder")
    assert set(out) >= {"session_id", "status", "pending_question", "spec", "issues",
                        "suggestions", "user_feedback", "iteration", "decision_reason", "error"}
    assert out["error"] is None


# ── 8. failed / invalid LLM output ───────────────────────────────────────

def test_invalid_llm_spec_output_becomes_clarification():
    gen = SpecGenerator(llm=FakeListChatModel(responses=["Sure! Here is your spec: <not json>"]))
    spec = gen.generate("adder")
    assert spec["status"] == "needs_clarification"
    assert "did not return valid JSON" in spec["ambiguities"][0]

    agent = SpecAnalyzerAgent(tools=SpecAnalyzerTools(generator=gen, decider=FakeDecider()),
                              checkpointer=InMemorySaver())
    out = agent.start("adder")
    assert out["status"] == SpecStatus.WAITING_FOR_USER.value
    assert any(i["code"] == "needs_clarification" for i in out["issues"])


def test_invalid_planner_output_falls_back_safely():
    summary = {"spec": COMPLETE_SPEC, "issues": [], "answers_applied": True}
    assert SpecDecider.parse("garbage", summary).action is Action.FINALIZE_SPEC
    summary["issues"] = [{"code": "x", "detail": "y"}]
    assert SpecDecider.parse('{"action": "launch_missiles"}', summary).action is Action.ASK_USER
    assert SpecDecider.parse("??", {"spec": None, "issues": [], "answers_applied": True}).action is Action.GENERATE_SPEC
    ok = SpecDecider.parse('```json\n{"action": "ask_user", "question": "Reset polarity?", "reason": "r"}\n```', summary)
    assert ok.action is Action.ASK_USER and ok.question == "Reset polarity?"


def test_stalled_regeneration_is_forced_to_ask_user():
    # planner insists on regenerating; the generator keeps returning the same
    # incomplete spec -> after one fruitless regeneration the guard asks the user
    forced = [Decision(Action.GENERATE_SPEC, reason="p")] * 10
    agent = make_agent([MISSING_RESET_SPEC, MISSING_RESET_SPEC, COMPLETE_SPEC], decider=FakeDecider(forced))
    out = agent.start("adder")
    assert out["status"] == SpecStatus.WAITING_FOR_USER.value
    assert out["iteration"] == 3          # generate, generate (stalled), ask_user
    assert "not resolving" in out["decision_reason"]
    assert out["pending_question"].startswith("Please clarify")


def test_symbolic_widths_are_not_flagged():
    spec = {**COMPLETE_SPEC, "ports": [
        {"name": "clk", "direction": "input", "width": "1"},
        {"name": "q", "direction": "output", "width": "N"},
        {"name": "d", "direction": "input", "width": "[N-1:0]"},
        {"name": "w", "direction": "input", "width": "DATA_WIDTH"},
        {"name": "bad", "direction": "input", "width": "8bits"},
    ]}
    odd = [i["detail"] for i in evaluate_spec(spec) if i["code"] == "port_odd_width"]
    assert len(odd) == 1 and "'bad'" in odd[0]


def test_resume_rejects_session_that_is_not_waiting():
    agent = make_agent([COMPLETE_SPEC])
    done = agent.start("adder")
    assert done["status"] == SpecStatus.COMPLETE.value
    with pytest.raises(ValueError, match="not WAITING_FOR_USER"):
        agent.resume(done["session_id"], "anything")
    with pytest.raises(ValueError, match="unknown session"):
        agent.resume("no-such-session", "anything")


def test_agent_only_has_restricted_actions():
    assert {a.value for a in Action} == {"generate_spec", "evaluate_spec", "ask_user", "finalize_spec"}
    nodes = set(make_agent([COMPLETE_SPEC]).graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"decide", "generate_spec", "evaluate_spec", "ask_user", "finalize_spec"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

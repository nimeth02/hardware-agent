"""agents/vhdl_version.py — the single decision point for the VHDL edition.
No LLM, no MCP: pure functions, plus the two places that consume them
(RTL linter static checks, RTL generator prompt/spec preparation)."""

from __future__ import annotations

import pytest

from agents.rtl_agent.rtl_generator import RTLGenerator
from agents.rtl_agent.rtl_linter import _forbidden_constructs
from agents.spec_analyser_agent.spec_evaluator import evaluate_spec
from agents.spec_analyser_agent.spec_generator import parse_spec_response
from agents.vhdl_version import (
    DEFAULT_VERSION, VHDL_93, VHDL_2008, design_rules_skill, detect_version_in_text,
    normalize_version, prepare_spec_for_rtl, resolve_vhdl_version, select_design_skills,
    skill_matches_version, with_vhdl_version, xvhdl_std,
)

SPEC = {
    "entity_name": "llr_demap16",
    "description": "Soft demapper.",
    "ports": [{"name": "aclk", "direction": "input", "width": "1"}],
    "constraints": [
        "Synthesizable RTL (Verilog-2001/SystemVerilog or VHDL-2008).",
        "Target: Xilinx Zynq-7020 (xc7z020clg400-1).",
    ],
}
SKILLS = {"vhdl_93_design_rules.md": "93 rules", "vhdl_2008_design_rules.md": "2008 rules",
          "common_style.md": "shared"}


# ── normalisation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("93", "93"), ("2008", "2008"), (93, "93"), (2008, "2008"), ("1993", "93"),
    ("VHDL-93", "93"), ("vhdl_2008", "2008"), ("IEEE 1076-2008", "2008"), ("VHDL 2008", "2008"),
    (None, None), ("", None), ("latest", None), ("VHDL-87", None),
    ("VHDL-93 or VHDL-2008", None),            # two editions: not a clear answer
])
def test_normalize_version(value, expected):
    assert normalize_version(value) == expected


# ── resolving the edition for a spec ─────────────────────────────────────

def test_explicit_field_wins_over_text():
    spec = {**SPEC, "vhdl_version": "93"}
    assert resolve_vhdl_version(spec) == VHDL_93
    assert resolve_vhdl_version({**SPEC, "vhdl_version": "VHDL-2008"}) == VHDL_2008


def test_missing_field_and_silent_text_gives_default():
    assert resolve_vhdl_version({"entity_name": "x", "constraints": ["100 MHz"]}) == DEFAULT_VERSION
    assert resolve_vhdl_version(None) == DEFAULT_VERSION
    assert resolve_vhdl_version({}) == DEFAULT_VERSION


def test_with_vhdl_version_fills_the_field_without_mutating_input():
    out = with_vhdl_version({"entity_name": "x"})
    assert out["vhdl_version"] == DEFAULT_VERSION
    assert "vhdl_version" not in {"entity_name": "x"}


def test_spec_generator_output_always_carries_the_field():
    spec = parse_spec_response('{"entity_name": "c", "ports": []}')
    assert spec["vhdl_version"] in (VHDL_93, VHDL_2008)
    assert parse_spec_response('{"entity_name": "c", "vhdl_version": "2008"}')["vhdl_version"] == VHDL_2008


def test_evaluator_flags_garbage_version_but_accepts_absent():
    codes = [i["code"] for i in evaluate_spec({**SPEC, "behavior": ["x"], "vhdl_version": "latest"})]
    assert "bad_vhdl_version" in codes
    codes = [i["code"] for i in evaluate_spec({**SPEC, "behavior": ["x"]})]
    assert "bad_vhdl_version" not in codes


# ── text detection (owner-written rule) ──────────────────────────────────

def test_detect_version_in_text():
    assert detect_version_in_text(["Synthesizable VHDL-2008 RTL."]) == VHDL_2008
    assert detect_version_in_text(["Must be plain VHDL-93."]) == VHDL_93
    assert detect_version_in_text(["Clock 100 MHz", "Latency 93 cycles"]) is None   # no "vhdl": silent
    assert detect_version_in_text(["Verilog-2001 or SystemVerilog"]) is None
    assert detect_version_in_text([]) is None
    assert detect_version_in_text(["Use VHDL-93.", "Use VHDL-2008."]) is None   # disagreement: default applies
    assert resolve_vhdl_version({"constraints": ["Synthesizable VHDL-2008 RTL."]}) == VHDL_2008
    assert resolve_vhdl_version({"constraints": ["Use VHDL-93.", "Use VHDL-2008."]}) == DEFAULT_VERSION


# ── choosing the rulebook ────────────────────────────────────────────────

def test_rulebook_per_edition():
    assert design_rules_skill("93") == "vhdl_93_design_rules.md"
    assert design_rules_skill("2008") == "vhdl_2008_design_rules.md"
    assert design_rules_skill("garbage") == design_rules_skill(DEFAULT_VERSION)


def test_select_design_skills_keeps_matching_and_neutral_files_only():
    assert select_design_skills(SKILLS, "93") == {"vhdl_93_design_rules.md": "93 rules", "common_style.md": "shared"}
    assert select_design_skills(SKILLS, "2008") == {"vhdl_2008_design_rules.md": "2008 rules", "common_style.md": "shared"}
    assert select_design_skills({}, "93") == {}
    assert skill_matches_version("vhdl_2008_design_rules.md", "93") is False
    assert skill_matches_version("vhdl_93_design_rules.md", "VHDL-93") is True
    assert skill_matches_version("anything_else.md", "2008") is True


def test_xvhdl_std():
    assert xvhdl_std("2008") == "2008" and xvhdl_std("VHDL-93") == "93" and xvhdl_std("?") == DEFAULT_VERSION


# ── what the chip-code model reads ───────────────────────────────────────

def test_prepare_spec_for_rtl_removes_rival_language_wording():
    out = prepare_spec_for_rtl({**SPEC, "vhdl_version": "93"})
    assert out["vhdl_version"] == VHDL_93
    assert not any("Verilog" in c or "2008" in c for c in out["constraints"])
    assert out["constraints"][0].startswith("Language: synthesizable VHDL-93")
    assert "Target: Xilinx Zynq-7020 (xc7z020clg400-1)." in out["constraints"]
    assert SPEC["constraints"][0].startswith("Synthesizable RTL")          # input untouched


def test_prepare_spec_for_rtl_names_the_2008_flag():
    out = prepare_spec_for_rtl({**SPEC, "vhdl_version": "2008"})
    assert "--2008" in out["constraints"][0]


def test_generator_prompt_states_the_edition():
    seen = []

    class Spy:
        def invoke(self, messages, config=None):
            seen.append(messages)
            return type("R", (), {"content": '{"entity_name": "x", "code": "y", "notes": []}'})()

    spy = RTLGenerator(llm=Spy())
    spy.generate({**SPEC, "vhdl_version": "2008"})
    system, user = seen[0][0].content, seen[0][1].content
    assert "Write synthesizable VHDL-2008." in system and "{VHDL_VERSION}" not in system
    assert SPEC["constraints"][0] in user                    # the spec itself is passed through unchanged
    spy.generate(SPEC)                                       # no field: the text says VHDL-2008, so 2008
    assert "Write synthesizable VHDL-2008." in seen[1][0].content
    spy.generate({**SPEC, "constraints": ["100 MHz"]})       # no field, silent text: default edition
    assert f"Write synthesizable VHDL-{DEFAULT_VERSION}." in seen[2][0].content


# ── forbidden-construct pre-check in the RTL linter ──────────────────────

CODE_2008_ONLY = """library ieee;
use ieee.std_logic_1164.all;
-- process(all) in a comment is fine
comb : process(all)
begin
end process;
"""
CODE_DEPRECATED = "use ieee.std_logic_unsigned.all;\n"


def test_forbidden_constructs_depend_on_edition():
    issues_93 = _forbidden_constructs(CODE_2008_ONLY, VHDL_93)
    assert len(issues_93) == 1 and "process(all)" in issues_93[0]["detail"] and "line 4" in issues_93[0]["detail"]
    assert _forbidden_constructs(CODE_2008_ONLY, VHDL_2008) == []


def test_deprecated_libraries_are_forbidden_in_both_editions():
    for version in (VHDL_93, VHDL_2008):
        issues = _forbidden_constructs(CODE_DEPRECATED, version)
        assert len(issues) == 1 and "std_logic_unsigned" in issues[0]["detail"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

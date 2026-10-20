# agents/vhdl_version.py — the ONE place that decides which VHDL edition a
# design is written and compiled in ("93" or "2008").
#
# Every other part of the system asks here instead of hard-coding "93":
#   spec analyzer   -> fills spec["vhdl_version"] (default when unstated)
#   RTL agent       -> picks the matching rulebook in skills/RTL and tells
#                      the generator which edition to write
#   RTL linter, testbench linter, simulation compile -> compiler flag
#
# Testbenches are always VHDL-2008 regardless of the design edition; that
# is a property of the testbench rules, not of the spec, so it stays where
# it is (agents/testbench_agent).

from __future__ import annotations

import copy
import re
from typing import Iterable

VHDL_93 = "93"
VHDL_2008 = "2008"
SUPPORTED_VERSIONS = (VHDL_93, VHDL_2008)
DEFAULT_VERSION = VHDL_93          # xvhdl's own default when no flag is passed

SPEC_FIELD = "vhdl_version"        # the key carried in the spec JSON

# "VHDL-93", "vhdl_1993", "1076-1993", 93, "VHDL 2008", "1076-2008", 2008 ...
_VERSION_TOKEN_RE = re.compile(r"(?:19)?93|2008")

# Design rulebooks per edition (file names inside skills/RTL).
DESIGN_RULES_SKILL = {
    VHDL_93: "vhdl_93_design_rules.md",
    VHDL_2008: "vhdl_2008_design_rules.md",
}
_VERSIONED_SKILL_RE = re.compile(r"vhdl_(93|2008)_", re.IGNORECASE)

# A spec constraint that talks about the implementation language at all.
_LANGUAGE_RE = re.compile(r"\b(verilog|systemverilog|vhdl|rtl language|hdl)\b", re.IGNORECASE)


def normalize_version(value) -> str | None:
    """'93' | '2008' for anything that clearly names one edition
    ("VHDL-2008", "vhdl_93", 1993, "IEEE 1076-2008", ...); None otherwise."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    found = {m.group(0)[-2:] if m.group(0).endswith("93") else m.group(0)
             for m in _VERSION_TOKEN_RE.finditer(text)}
    if len(found) != 1:
        return None                 # nothing, or two different editions: not a clear answer
    return found.pop()


def detect_version_in_text(texts: Iterable[str]) -> str | None:
    """Which edition the spec's free text asks for, or None when it is silent.

    `texts` are the spec's constraint/assumption/description strings. Only a
    sentence that is about VHDL counts; the edition it names wins. A text
    that mentions both editions, or only Verilog, is treated as silent.
    """
    found = {normalize_version(t) for t in texts if "vhdl" in str(t).lower()}
    found.discard(None)
    if len(found) == 1:
        return found.pop()
    return None                     # silent, or two sentences disagree: let the default decide


def resolve_vhdl_version(spec: dict | None) -> str:
    """The edition a design must be written in: the spec's explicit field
    when valid, else what its text asks for, else the default (93)."""
    spec = spec or {}
    explicit = normalize_version(spec.get(SPEC_FIELD))
    if explicit:
        return explicit
    texts = [str(t) for key in ("constraints", "assumptions") for t in (spec.get(key) or [])]
    if spec.get("description"):
        texts.append(str(spec["description"]))
    return detect_version_in_text(texts) or DEFAULT_VERSION


def with_vhdl_version(spec: dict) -> dict:
    """Copy of the spec with spec["vhdl_version"] filled in (the spec analyzer
    calls this so every downstream agent sees an explicit value)."""
    out = copy.deepcopy(spec)
    out[SPEC_FIELD] = resolve_vhdl_version(spec)
    return out


def design_rules_skill(version: str) -> str:
    """File name of the design rulebook for this edition."""
    return DESIGN_RULES_SKILL[normalize_version(version) or DEFAULT_VERSION]


def skill_matches_version(name: str, version: str) -> bool:
    """True for the rulebook of `version` and for edition-neutral skill
    files; False for a rulebook written for the other edition. Used as the
    skill loader's `keep` filter so the wrong rulebook is never even read."""
    wanted = normalize_version(version) or DEFAULT_VERSION
    m = _VERSIONED_SKILL_RE.search(name)
    return m is None or m.group(1) == wanted


def select_design_skills(skills: dict[str, str], version: str) -> dict[str, str]:
    """Same filter applied to skills that were already loaded."""
    return {name: content for name, content in skills.items() if skill_matches_version(name, version)}


def prepare_spec_for_rtl(spec: dict) -> dict:
    """What the chip-code AI should read: the spec with an explicit edition,
    and every language constraint replaced by one unambiguous line, so a
    stray "Verilog or VHDL-2008" can no longer argue with the rulebook."""
    version = resolve_vhdl_version(spec)
    out = copy.deepcopy(spec)
    out[SPEC_FIELD] = version
    constraints = [c for c in (out.get("constraints") or []) if not _LANGUAGE_RE.search(str(c))]
    constraints.insert(0, f"Language: synthesizable VHDL-{version} only (compiled with xvhdl"
                          f"{' --2008' if version == VHDL_2008 else ', default standard'}).")
    out["constraints"] = constraints
    return out


def xvhdl_std(version: str) -> str:
    """The vhdl_std argument for the Vivado MCP server's run_xvhdl."""
    return normalize_version(version) or DEFAULT_VERSION

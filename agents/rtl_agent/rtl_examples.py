# agents/rtl_agent/rtl_examples.py — the one place that decides which worked
# example (skills/RTL_EXAMPLES/*) the RTL worker is shown for a spec.
#
# skills/RTL holds RULES and is read for every design. A worked example is a
# complete, simulated design of one kind; it is shown only when the spec is
# that kind, so a counter is never handed a timing-recovery file to copy.
#
# Why it exists: timing_recovery, 2026-11-11 13:01. The rule files give p_loop
# in full and the worker returned it right every time; they give the Farrow
# rows s3..s7 as one summary line and the worker got them wrong in a different
# way on each attempt (one multiply by mu instead of two, y_mid a copy of
# y_sym, weights not halved).
#
# An example file says what it is for in its own first line:
#     applies_when: farrow, gardner, timing recovery
# so adding an example is adding a file; nothing here changes. Files are read
# through the same file-server MCP loader as the rule files.

from __future__ import annotations

import json
import re

from agents.skill_loader import SkillLoader

EXAMPLES_DIR = "skills/RTL_EXAMPLES"
EXAMPLE_LABEL = "WORKED EXAMPLE "       # prefix of the name the worker sees above the file
_APPLIES_RE = re.compile(r"\A\s*applies_when:[ \t]*(.*)\n?", re.IGNORECASE)


def keywords_of(content: str) -> list[str]:
    """The words of the file's `applies_when:` line, lower case; [] when it has none."""
    m = _APPLIES_RE.match(content or "")
    return [w.strip().lower() for w in m.group(1).split(",") if w.strip()] if m else []


def matches(content: str, spec: dict) -> int:
    """How many of the file's keywords appear in the spec's text (0 = not this kind of design)."""
    text = json.dumps(spec, default=str).lower()
    return sum(1 for word in keywords_of(content) if word in text)


def select_example(files: dict[str, str], spec: dict) -> dict[str, str]:
    """{label: content} for the ONE example that fits `spec` best, or {}.
    One at most: an example is a whole design, and two would be two answers.
    The `applies_when:` line is for this function and is not shown to the worker."""
    scored = [(matches(content, spec), name) for name, content in sorted(files.items())]
    best = max(scored, default=(0, ""))
    if best[0] == 0:
        return {}
    name = next(n for score, n in scored if score == best[0])      # first by name on a tie
    return {EXAMPLE_LABEL + name: _APPLIES_RE.sub("", files[name], count=1).lstrip("\n")}


def examples_for(spec: dict) -> dict[str, str]:
    """The worked example to show for `spec`, read from skills/RTL_EXAMPLES; {} when none fits."""
    return select_example(SkillLoader(EXAMPLES_DIR).load(), spec)

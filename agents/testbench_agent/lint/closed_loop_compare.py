# agents/testbench_agent/lint/closed_loop_compare.py — the one place that says how
# the testbench of a CLOSED-LOOP design must compare, and checks that it does.
#
# The rule itself is prose in skills/TESTBENCH (CLOSED-LOOP DESIGNS: COMPARE
# WITH A TOLERANCE, NEVER BIT FOR BIT). Nothing enforced it, and the first
# draft ignored it twice on 2026-11-11:
#   13:01  bit-exact testbench, 3 simulations lost before a rewrite added the
#          settle window and the tolerances
#   15:04  the RTL was the proven worked example and the test vectors were
#          right (the same two files give 0 mismatches under a tolerant
#          testbench); the bit-exact testbench reported 256 mismatches from
#          beat 2 and the analyst blamed the RTL
# Two users, both one line:
#   testbench_linter._static_checks       closed_loop_compare_issues  (after a draft)
#   testbench_generator.generate_raw      closed_loop_hint            (before the first draft)
# "Is this spec a closed loop" is the test-vector guard's own test, imported
# here, so the vectors (clean stimulus, loop trace) and the testbench
# (tolerance) are always decided for the same designs.

from __future__ import annotations

import re

from agents.test_vector_agent.test_vector_generator_agent.matlab_guard import _LOOP_SPEC_WORDS, _is_streaming, _spec_text

_SETTLE_RE = re.compile(r"\bsettle\w*", re.IGNORECASE)                 # SETTLE_BEATS, settle_beats, ...
_TOLERANCE_RE = re.compile(r"\babs\s*\([^;]*?\)\s*>", re.IGNORECASE)     # if abs(got_i - exp_i) > TOL_SYM

_MISSING_BEATS_RE = re.compile(r'"[^"]*missing output beats', re.IGNORECASE)
_IF_RE = re.compile(r"\bif\b", re.IGNORECASE)

COUNT_LINE = ("if n_exp > 0 and abs(k_exp - n_exp) > n_exp / 50 and abs(k_exp - n_exp) >= 2 then")


def _exact_beat_count_line(lines: list[str]) -> str | None:
    """The `if` line that reports "missing output beats" on ANY difference in
    the beat count (`if k_exp < n_exp then`, `if k_exp /= n_exp then`), or
    None when there is no such report or its condition has an allowance
    (`abs(k_exp - n_exp) > n_exp / 50`). timing_recovery 2026-11-11 15:12:
    every compared beat matched, the last 3 strobes were still inside the
    pipeline when the input stopped (197 of 200), and that one line failed
    the simulation of a correct design."""
    for n, line in enumerate(lines):
        if not _MISSING_BEATS_RE.search(line):
            continue
        start = next((k for k in range(n, max(n - 6, -1), -1) if _IF_RE.search(lines[k].split('"')[0])), None)
        if start is None:
            continue
        condition = " ".join(lines[start:n + 1]).split('"')[0]
        if not re.search(r"\babs\s*\(", condition, re.IGNORECASE):
            return lines[start].strip()
    return None


HOW = ("Declare `constant SETTLE_BEATS : integer := 64;` (a third of the expected beats when the vectors hold "
       "fewer than 192), `constant TOL_SYM : integer := 4;` and `constant TOL_MU : integer := 16;`. Count every "
       "beat, but compare a beat only when `k_exp >= SETTLE_BEATS`. Decode each field of the payload word at the "
       "spec's bit positions as a signed integer on BOTH sides (`got_i := to_integer(signed(dut_m_axis_tdata(23 "
       "downto 12))); exp_i := to_integer(signed(exp_tdata(k_exp)(23 downto 12)));`) and report a mismatch only "
       "`if abs(got_i - exp_i) > TOL_SYM`; never compare the packed word with `/=`. A fractional offset (mu on "
       "m_axis_tuser) wraps: `d := abs(got_mu - exp_mu); if d > 128 then d := 256 - d; end if; if d > TOL_MU`. "
       f"The beat count may differ by 2 % of the expected beats (at least 2) before it is reported: `{COUNT_LINE}`. See "
       "CLOSED-LOOP DESIGNS in vhdl_2008_testbench_rules.md.")

WHY = ("The golden model applies the loop filter's new value on the very next sample; a pipelined RTL applies "
       "it several samples later and rounds every product, so while the loop locks the two take different paths "
       "and meet again after it has locked. A bit-for-bit compare from the first beat fails on a CORRECT design, "
       "and the RTL is then blamed for it.")


def is_closed_loop(spec: dict | None) -> bool:
    """True for a streaming design whose spec describes a loop fed back from its
    own output (loop filter, NCO, TED, PLL, AGC, integrator, feedback)."""
    return _is_streaming(spec) and bool(_LOOP_SPEC_WORDS.search(_spec_text(spec)))


def closed_loop_compare_issues(spec: dict, code: str) -> list[dict]:
    """[{code: "closed_loop_exact_compare", detail}] when the spec is a closed
    loop and the testbench has no settle window or no tolerance compare, and
    "closed_loop_exact_beat_count" when it demands the exact number of beats;
    [] otherwise (and for every design that is not a closed loop)."""
    if not is_closed_loop(spec):
        return []
    text = "\n".join(line.split("--", 1)[0] for line in (code or "").splitlines())     # comments do not count
    missing = [name for name, found in (("a settle window (SETTLE_BEATS)", _SETTLE_RE.search(text)),
                                        ("a tolerance compare (abs(got - exp) > TOL)", _TOLERANCE_RE.search(text)))
               if not found]
    issues = []
    if missing:
        issues.append({"code": "closed_loop_exact_compare",
                       "detail": f"The spec describes a closed loop, but the testbench has no "
                                 f"{' and no '.join(missing)}: it compares bit for bit from the first beat. "
                                 f"{WHY} {HOW}"})
    exact = _exact_beat_count_line(text.splitlines())
    if exact:
        issues.append({"code": "closed_loop_exact_beat_count",
                       "detail": f"`{exact}` reports \"missing output beats\" on ANY difference in the beat count. "
                                 f"In a closed-loop design the RTL's loop delay shifts when the strobes fall, and "
                                 f"the last strobes are still inside the pipeline when the input stops, so a "
                                 f"correct design delivers a few beats fewer (197 of 200). Allow 2 % of the expected "
                                 f"beats (at least 2): `{COUNT_LINE}`."})
    return issues


def closed_loop_hint(spec: dict) -> str:
    """The up-front block for the testbench writer's system prompt, or "" for a
    design that is not a closed loop. Same test as the check above, so the
    writer is told before its first draft what the checker will demand."""
    if not is_closed_loop(spec):
        return ""
    return ("\n\nTHIS SPEC IS A CLOSED-LOOP DESIGN: COMPARE WITH A SETTLE WINDOW AND TOLERANCES.\n"
            f"{WHY}\n{HOW}\nA testbench without SETTLE_BEATS, without an `abs(got - exp) > TOL` compare, "
            "or with an exact beat-count check is rejected (closed_loop_exact_compare, "
            "closed_loop_exact_beat_count) before any simulation.\n")

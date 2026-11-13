# agents/simulation_agent/simulation_analyst.py — tool: turn compile +
# simulation results into a verdict. A clean pass and a run that never
# printed a marker are decided deterministically; everything else goes to
# the LLM analyst (prompts/simulation_agent_prompt.py). For a failed
# simulation with a test-vector CSV the analyst first writes a small Python
# check (decode the failing input, recompute the spec's answer); the check
# is run here and its output goes back to the analyst before the verdict.

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.simulation_agent.inside_values import WAVES_PATH, inside_values
from agents.simulation_agent.state import AGENT_NAME
from agents.test_vector_agent.lint.test_vector_validator import spec_output_rate
from observability.callback import ObservabilityCallbackHandler
from prompts.simulation_agent_prompt import SIMULATION_AGENT_SYSTEM_PROMPT
from services.llm_client import get_llm


# "... [truncated 371544 chars] ..." (head-only cap of older Vivado servers) or
# "... [truncated 371544 chars in the middle of the log] ..." (the server now
# keeps the end of the log): the server cut the log at its size limit.
_TRUNCATED = re.compile(r"\[truncated\s+(\d+)\s+chars")
_REPORT_LINE = re.compile(r"^(?:Error|Failure|Warning|Note):", re.MULTILINE)
MAX_REPORTED_MISMATCHES = 20          # what the testbench rules ask for (vhdl_2008_testbench_rules)


_ERROR_REPORT = re.compile(r"^Error:", re.MULTILINE)


def mismatches_capped(sim_output) -> bool:
    """Whether the testbench printed its full quota of mismatches
    (MAX_REPORTED_MISMATCHES 'Error:' lines) and then, by its own rule,
    stopped printing. After that the log is silent however long the run
    goes on, so the last report time says nothing about a freeze.
    timing_recovery, 2026-11-05 19:02: 20 errors by 526 ns, a 24,000-row
    stimulus, a 40 us run; judged 'frozen handshake' three times."""
    return isinstance(sim_output, str) and len(_ERROR_REPORT.findall(sim_output)) >= MAX_REPORTED_MISMATCHES


def truncated_chars(sim_output) -> int | None:
    """How many characters the Vivado server dropped from the log, or None
    when the log is complete."""
    m = _TRUNCATED.search(sim_output) if isinstance(sim_output, str) else None
    return int(m.group(1)) if m else None


def _has_port(spec, name: str) -> bool | None:
    """Whether the spec declares the port; None without a spec."""
    if not isinstance(spec, dict):
        return None
    return any(str(p.get("name", "")).lower() == name for p in (spec.get("ports") or []) if isinstance(p, dict))


_TAIL_KEPT = "in the middle of the log"      # the new server's note: the end of the log is present


def _no_marker_verdict(sim: dict, failure: str, spec: dict | None) -> dict:
    """Why a complete log (its end present) has no marker: a frozen
    handshake, or a stimulus longer than sim_time."""
    sim_output = sim.get("sim_output")
    stalled_at = stalled_time_ns(sim_output, sim.get("sim_time"))
    if stalled_at is not None:
        if _has_port(spec, "m_axis_tready") is False:
            # no backpressure wire: the only handshake the testbench can wait
            # for is the DUT's s_axis_tready (or an unbounded wait for
            # m_axis_tvalid)
            return {"verdict": "error", "likely_cause": "testbench", "failures": [failure],
                    "summary": f"The testbench stopped making progress at {stalled_at:g} ns, long before "
                               f"sim_time={sim.get('sim_time')}, and never printed a result marker: it is "
                               f"waiting forever for a handshake (s_axis_tready never rose while a beat row was "
                               f"held, or an unbounded wait for m_axis_tvalid). The spec has no m_axis_tready "
                               f"port, so backpressure is not the reason. Not a too-short sim_time.",
                    "suggested_fix": "Give every wait in the testbench a limit (64 edges, then report "
                                     "'deadlock' and move on) and print the marker. If the deadlock reports then "
                                     "name rows with s_axis_tvalid = 1, the RTL holds s_axis_tready low although "
                                     "the spec says it must be 1 after reset."}
        return {"verdict": "error", "likely_cause": "testbench", "failures": [failure],
                "summary": f"The testbench stopped making progress at {stalled_at:g} ns, long before "
                           f"sim_time={sim.get('sim_time')}, and never printed a result marker: it is waiting "
                           f"forever for a handshake (s_axis_tready never rose while the testbench holds "
                           f"m_axis_tready = '0', or m_axis_tvalid never came). Not a too-short sim_time.",
                "suggested_fix": "Give the testbench's 'hold the row until s_axis_tready' wait a limit (64 "
                                 "edges, then report 'deadlock' and move on) and print the marker. Also check "
                                 "test_vectors_in.csv: a row with m_axis_tready = 0 must not carry an input beat "
                                 "(s_axis_tvalid = 1); the design cannot accept while its output is blocked."}
    return {"verdict": "error", "likely_cause": "testbench", "failures": [failure],
            "summary": "The simulation ran to the end of sim_time without the testbench printing a result "
                       "marker: either the stimulus takes longer than sim_time or the testbench never "
                       "reports one.",
            "suggested_fix": "Shorten the stimulus or make the testbench print SIMULATION PASSED/FAILED "
                             "and set done <= true at the end."}


def deterministic_verdict(compile_result: dict | None, sim: dict | None, spec: dict | None = None) -> dict | None:
    """Verdict without an LLM when the evidence is unambiguous; None otherwise.
    A "fixed_rule" key travels to the blamed agent word for word
    (blame.downstream_note) when code, not a model, read the log."""
    outcome = (sim or {}).get("outcome")
    if outcome == "passed":
        return {"verdict": "pass", "likely_cause": None, "failures": [],
                "summary": "Simulation passed: the testbench reported SIMULATION PASSED with no errors.",
                "suggested_fix": None}
    if outcome == "no_marker":
        failure = f"No SIMULATION PASSED/FAILED marker within sim_time={sim.get('sim_time')}."
        sim_output = sim.get("sim_output")
        hint = flag_cycle_compare_hint(sim_output)   # a run too short for its stimulus can still show the flag fault
        dropped = truncated_chars(sim_output)
        flood = cap_fix = None
        if dropped:
            # timing_recovery, 2026-10-30: a mismatch on every one of 1200
            # cycles, the log cut at 200 KB by a head-only cap, the FAILED
            # marker at the end lost, and the run judged "frozen handshake at
            # 5036 ns" (the cut point) and rerun with 20us and 40us for nothing.
            reports = len(_REPORT_LINE.findall(sim_output))
            flood = (f"The simulation log was cut at the tool's size limit ({dropped} characters dropped, {reports} "
                     f"report lines kept): the testbench reported a mismatch on nearly every cycle, so it compares "
                     f"outputs cycle by cycle (on a streaming design that fails on every row) and never stops "
                     f"printing.")
            cap_fix = (f"Print at most the first {MAX_REPORTED_MISMATCHES} mismatches (keep counting all of them) "
                       f"and report the total before the marker. On a streaming design (s_axis_tvalid/m_axis_tvalid) "
                       f"compare output beats in order, never the valid flag or the data cycle by cycle "
                       f"(vhdl_2008_testbench_rules, STREAMING DESIGNS).")
            if _TAIL_KEPT not in sim_output:
                # head-only cap of an older Vivado server: the end of the run,
                # with its marker, went with the cut
                verdict = {"verdict": "error", "likely_cause": "testbench", "failures": [failure],
                           "summary": flood + " The end of the run, where the testbench prints SIMULATION "
                                              "PASSED/FAILED, is missing with the cut. Not a too-short sim_time and "
                                              "not a frozen handshake.",
                           "suggested_fix": cap_fix, "fixed_rule": hint or f"{flood} {cap_fix}"}
                if hint:
                    verdict["summary"] += " The log also compares a handshake flag cycle by cycle; see fixed_rule."
                return verdict
        if not dropped and mismatches_capped(sim_output):
            # the testbench printed its 20 mismatches and went quiet by its own
            # rule; the run was just shorter than the stimulus. Those 20 lines
            # are real compare results: the LLM analyst judges them as a
            # failed run (analyze() tells it why there is no marker)
            return None
        verdict = _no_marker_verdict(sim, failure, spec)
        if flood:
            # the server kept the end of the log: there really is no marker,
            # and the flood is a second fault of the same testbench
            verdict["summary"] = f"{flood} The end of the log is present and has no marker: {verdict['summary']}"
            verdict["suggested_fix"] = f"{cap_fix} Then: {verdict['suggested_fix']}"
        if hint:
            verdict["summary"] += " The log also compares a handshake flag cycle by cycle; see fixed_rule."
            verdict["fixed_rule"] = hint
        elif flood:
            verdict["fixed_rule"] = f"{flood} {cap_fix}"
        return verdict
    return None


# xsim prints "Time: 126 ns  Iteration: 0" after every report; the last one is
# the last moment the testbench did anything.
_TIME_LINE = re.compile(r"\bTime:\s*([0-9]+(?:\.[0-9]+)?)\s*(ps|ns|us|ms)\b", re.IGNORECASE)
_SIM_TIME = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(ps|ns|us|ms)\s*$", re.IGNORECASE)
_TO_NS = {"ps": 1e-3, "ns": 1.0, "us": 1e3, "ms": 1e6}


def stalled_time_ns(sim_output, sim_time) -> float | None:
    """The time of the testbench's last report when that is under half of
    sim_time (the testbench froze, typically in an endless handshake wait);
    None when the log has no report times, sim_time cannot be read, or the
    reports run on until near the end (the stimulus is just too long)."""
    if not isinstance(sim_output, str) or not isinstance(sim_time, str):
        return None
    limit = _SIM_TIME.match(sim_time)
    times = _TIME_LINE.findall(sim_output)
    if not limit or not times:
        return None
    limit_ns = float(limit.group(1)) * _TO_NS[limit.group(2).lower()]
    last_ns = max(float(v) * _TO_NS[u.lower()] for v, u in times)
    return last_ns if last_ns < limit_ns / 2 else None


def parse_analysis_response(raw: str, sim: dict | None) -> dict:
    try:
        data = extract_json(raw)
        if isinstance(data, dict) and data.get("verdict"):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    outcome = (sim or {}).get("outcome")
    return {"verdict": "fail" if outcome == "failed" else "error", "likely_cause": None,
            "failures": ["Simulation analyst did not return valid JSON; raw reply follows.", str(raw)[:500]],
            "summary": "", "suggested_fix": None}


# The Vivado MCP server now returns logs up to 200 KB so the UI can show the
# whole xsim/xvhdl output. The LLM needs the FIRST mismatches (the clearest
# evidence: the earliest cycle that went wrong) and the tail (the
# PASSED/FAILED marker), not the warnings in between, so keep both ends.
LLM_LOG_HEAD = 3000
LLM_LOG_TAIL = 6000
_FIRST_ERROR = re.compile(r"^.*\b(Error|ERROR|Failure|FAILURE|Fatal|FATAL)\b.*$", re.MULTILINE)


def _first_error_at(text: str) -> int:
    """Where the first error line starts (0 if there is none)."""
    match = _FIRST_ERROR.search(text)
    return match.start() if match else 0


def _tail(text, head: int = LLM_LOG_HEAD, tail: int = LLM_LOG_TAIL):
    """The log cut for the LLM: `head` chars from the first error line, then
    `tail` chars from the end. Short logs are returned as they are."""
    if not isinstance(text, str) or len(text) <= head + tail:
        return text
    start = _first_error_at(text)
    skipped = f"... [first {start} chars omitted for the LLM; full log in the run record]\n" if start else ""
    if len(text) - start <= head + tail:
        return skipped + text[start:]
    middle = len(text) - start - head - tail
    return (skipped + text[start:start + head]
            + f"\n... [{middle} chars omitted for the LLM; full log in the run record]\n" + text[-tail:])


# What the blamed agent gets to see of the log itself. The analyst's note
# (summary, suggested_fix) is a reading of the evidence; these are the
# evidence lines, so the agent can check that reading instead of taking it
# on trust. blame.downstream_note copies them onto the blamed attempt.
LOG_EXCERPT_LINES = 20
LOG_EXCERPT_CHARS = 4000
_ERROR_LINE = re.compile(r"\b(Error|ERROR|Failure|FAILURE|Fatal|FATAL)\b")


def log_excerpt(compile_result: dict | None, sim: dict | None) -> str | None:
    """The first LOG_EXCERPT_LINES error lines of the run, cut at
    LOG_EXCERPT_CHARS, with a count of the lines left out; None when the run
    printed no error line. On a failed compile the lines come from the
    compiler's output (stderr, then stdout, per file); otherwise from the
    simulation log."""
    if compile_result and compile_result.get("status") != "ok":
        texts = [str(r.get(k) or "") for r in (compile_result.get("results") or []) for k in ("stderr", "stdout")]
    else:
        texts = [str((sim or {}).get("sim_output") or "")]
    lines = [ln.strip() for text in texts for ln in text.splitlines() if _ERROR_LINE.search(ln)]
    if not lines:
        return None
    kept = lines[:LOG_EXCERPT_LINES]
    excerpt = "\n".join(kept)[:LOG_EXCERPT_CHARS]
    if len(lines) > len(kept):
        excerpt += f"\n... [{len(lines) - len(kept)} more error lines in the run record]"
    return excerpt


def with_evidence(analysis: dict, compile_result: dict | None, sim: dict | None, beats: list[dict],
                  inside: dict | None = None) -> dict:
    """`analysis` plus the evidence behind it, for the blamed agent:
    "failing_beats" (failing_beats(): the input row the design received, the
    expected row and the log line with the value it produced), "log_excerpt"
    (log_excerpt()) and "inside_values" (inside_values(): the chip's loop
    registers next to the golden model's values on the beats around the first
    failure). A passing verdict is returned as it is."""
    if analysis.get("verdict") == "pass":
        return analysis
    if beats:
        analysis["failing_beats"] = beats
    excerpt = log_excerpt(compile_result, sim)
    if excerpt:
        analysis["log_excerpt"] = excerpt
    if inside:
        analysis["inside_values"] = inside
    return analysis


def _trim_for_llm(compile_result: dict | None, sim: dict | None) -> tuple[dict | None, dict | None]:
    """Copies of the compile/sim dicts with long log fields cut to a tail."""
    compile_trimmed = None
    if compile_result:
        compile_trimmed = dict(compile_result)
        compile_trimmed["results"] = [
            {**r, "stdout": _tail(r.get("stdout")), "stderr": _tail(r.get("stderr"))}
            for r in (compile_result.get("results") or [])
        ]
    sim_trimmed = None
    if sim:
        sim_output = sim.get("sim_output")
        sim_trimmed = {**sim, "sim_output": _tail(sim_output)}
        dropped = truncated_chars(sim_output)
        if dropped:
            # the server's own note sits in the middle of the log, which _tail
            # drops, so say it on the first line and in a field of its own
            reports = len(_REPORT_LINE.findall(sim_output))
            sim_trimmed["log_truncated_chars"] = dropped
            sim_trimmed["sim_output"] = (f"[the Vivado server cut {dropped} chars from this log at its size limit; "
                                         f"{reports} report lines survived the cut: the testbench reported a "
                                         f"mismatch on nearly every cycle]\n{sim_trimmed['sim_output']}")
    return compile_trimmed, sim_trimmed


# "cycle 17: m_axis_tdata expected hi=125 lo=61440 got hi=0 lo=0"
_EXPECTED_GOT = re.compile(r"\bexpected\s+(.+?)\s+got\s+(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


_ZERO_VALUE = re.compile(r"^[^0-9]*0(?:[^0-9]+0)*[^0-9]*$")      # "0", "hi=0 lo=0", "0_0_0"


# The testbench's one-line totals ("missing output beats: expected 20 got 18",
# "mismatches: 5") are not per-beat values and must not feed the value rules.
_SUMMARY_LINE = re.compile(r"missing output beats|\bmismatches\s*:", re.IGNORECASE)


def _expected_got_pairs(sim_output) -> list[tuple[str, str]]:
    """(expected, got) from every per-beat 'expected X got Y' line, lower-cased; [] for no log."""
    if not isinstance(sim_output, str):
        return []
    return [(e.strip().lower(), g.strip().lower())
            for line in sim_output.splitlines() if not _SUMMARY_LINE.search(line)
            for e, g in _EXPECTED_GOT.findall(line)]


# A value the vectors expect on more lines than this (a saturation or reset
# value such as 8386559 = +2047/+2047) turns up as "got" by chance; matching
# it proves nothing about timing.
COMMON_VALUE_LINES = 2


def shifted_values_hint(sim_output) -> str | None:
    """A fixed-rule reading of the mismatch lines, or None.

    When a value the testbench reports as "got" on one cycle is exactly a
    value it "expected" on another cycle, the design computed the right
    answer but it was checked at the wrong time. That is a timing or
    ordering fault, never arithmetic: the LLM analyst blamed the RTL's
    arithmetic for this four runs in a row (llr_demap16, 2026-10-29) and
    the RTL was rewritten to no effect. The hint tells it where to look.

    The evidence has to be most of the log, not a few lines: at least half
    the lines, two different shifted values, none of them a value the
    vectors expect on many lines. timing_recovery, 2026-11-03 12:30: 4 of
    20 lines matched, all of them the saturation value 8386559, and the
    testbench was rewritten twice for a strobe fault in the RTL."""
    pairs = _expected_got_pairs(sim_output)
    if len(pairs) < 2:
        return None
    expected_on: dict[str, int] = {}
    for e, _ in pairs:
        expected_on[e] = expected_on.get(e, 0) + 1
    shifted = [g for e, g in pairs
               if g != e and 0 < expected_on.get(g, 0) <= COMMON_VALUE_LINES and not _ZERO_VALUE.match(g)]
    if len(shifted) < 2 or len(set(shifted)) < 2 or 2 * len(shifted) < len(pairs):
        return None
    return (f"FIXED RULE (not a guess): {len(shifted)} of the {len(pairs)} 'expected X got Y' lines report a Y that "
            f"is exactly the expected value of ANOTHER cycle (e.g. got {shifted[0]!r}). The design therefore "
            f"computes the right values; they are only checked at the wrong time. This is a timing/ordering fault, "
            f"not arithmetic: do not blame the RTL's arithmetic and do not ask for the test vectors to be "
            f"recomputed. If the testbench compares an output against the CSV row of the same cycle (a streaming "
            f"design answers several cycles later), likely_cause is 'testbench'. Only if the testbench already "
            f"compares beats in order is the RTL at fault: its valid flag is not aligned with its data "
            f"(valid pipeline one stage longer or shorter than the data pipeline).")


# "missing output beats: expected 788 got 233" (the testbench's one total line
# after the drain) and "cycle 20: unexpected output beat" (one line per beat
# the design produced after the last expected one)
_MISSING_BEATS = re.compile(r"missing output beats\s*:\s*expected\s+(\d+)\s+got\s+(\d+)", re.IGNORECASE)
_UNEXPECTED_BEAT = re.compile(r"unexpected output beat", re.IGNORECASE)
BEAT_COUNT_TOLERANCE = 2      # beats a short drain may lose at the end of the run, or stray extra beats


MIN_BEATS_TO_JUDGE_VECTORS = 8     # tiny vector sets are never judged
VECTOR_RATE_FLOOR = 0.10           # expected beats at or under 10% of inputs / rate: the golden model's strobe is broken


def wrong_vectors_reason(inputs_csv, outputs_csv, spec) -> str | None:
    """Why the golden vectors themselves are wrong, or None. Two decisive
    signs, read from the CSVs the testbench replayed: s_axis_tdata is one
    constant on every input beat (a 0/0 in the MATLAB model turned every
    sample into NaN, then saturation into one number), or the expected
    output beats are under 10% of inputs / rate when the spec states a rate.
    timing_recovery, 2026-11-05 15:54: 8390656 on all 796 input beats, one
    expected beat for a '1 per 4 clocks' spec; the analyst blamed the RTL for
    197 'unexpected' beats. The vector checker now rejects such files
    before simulation; this is the safety net when they still get through."""
    in_rows, out_rows = _csv_rows(inputs_csv), _csv_rows(outputs_csv)
    in_beats = _beat_rows(in_rows, "s_axis_tvalid", spec, skip_reset=True)
    if len(in_beats) < MIN_BEATS_TO_JUDGE_VECTORS or "s_axis_tdata" not in in_beats[0]:
        return None
    values = {r.get("s_axis_tdata", "") for r in in_beats}
    if len(values) == 1:
        return (f"test_vectors_in.csv carries the same s_axis_tdata value {values.pop()} on all {len(in_beats)} input "
                f"beats, so the golden model's samples were one constant (a NaN/Inf from a 0/0 saturated to one "
                f"number) and its expected outputs are meaningless.")
    rate = spec_output_rate(spec)
    out_beats = len(_beat_rows(out_rows, "m_axis_tvalid", spec, skip_reset=False))
    if rate and out_rows and out_beats <= VECTOR_RATE_FLOOR * len(in_beats) / rate:
        return (f"test_vectors_out.csv expects only {out_beats} output beat(s) for {len(in_beats)} input beats, while "
                f"the spec says about one output per {rate} input(s) (roughly {round(len(in_beats) / rate)} beats), "
                f"so the golden model's strobe stopped firing.")
    return None


def beat_count_hint(sim_output, vectors_note: str | None = None) -> str | None:
    """A fixed-rule reading of the beat totals, or None. vectors_note is
    wrong_vectors_reason()'s finding; when given, the rule names the test
    vectors as the cause instead of leaving the choice to the analyst.

    "missing output beats: expected A got B" with A and B far apart, or many
    "unexpected output beat" lines, means the design produced the wrong
    NUMBER of beats. A latency or beat-order fault cannot do that: the k-th
    beat still comes out, only later. So the RTL's strobe / valid logic or
    the golden model's strobe is wrong, and every 'expected X got Y' line
    is a beat compared out of step. timing_recovery, 2026-11-03 12:30: the
    design made 233 beats where the vectors expected 788 (then 28 where 198
    were expected), the shifted-values rule fired on the saturation value
    8386559, and the testbench was rewritten twice to no effect. A stuck
    handshake ("never rose (deadlock)") explains missing beats by itself,
    so the deadlock rules speak then, not this one."""
    if not isinstance(sim_output, str) or _NEVER_ROSE.search(sim_output):
        return None
    totals = _MISSING_BEATS.search(sim_output)
    extra = len(_UNEXPECTED_BEAT.findall(sim_output))
    if totals:
        expected, got = int(totals.group(1)), int(totals.group(2))
        if abs(expected - got) <= max(BEAT_COUNT_TOLERANCE, expected // 10):
            return None
        counts = (f"the testbench counted {got} output beats where the vectors expect {expected} "
                  f"('missing output beats: expected {expected} got {got}')")
    elif extra > BEAT_COUNT_TOLERANCE:
        counts = (f"the design produced at least {extra} output beats more than the vectors expect "
                  f"({extra} 'unexpected output beat' lines)")
    else:
        return None
    if vectors_note:
        return (f"FIXED RULE (not a guess): {counts}. A latency or beat-order fault cannot change how many beats come "
                f"out, so this is NOT a timing/ordering fault, and the 'expected X got Y' lines are beats compared out "
                f"of step. THE GOLDEN VECTORS ARE WRONG: {vectors_note} likely_cause MUST be 'test_vectors' (not "
                f"'rtl', not 'testbench'); suggested_fix must name the golden model's formula or strobe loop (check "
                f"for a division by zero, add assert(all(isfinite(x))), make sure the loop values are updated every "
                f"sample). If the RTL's own beat total also disagrees with the spec rate, say so in the summary, but "
                f"the vectors must be fixed first because nothing can be compared against them.")
    return (f"FIXED RULE (not a guess): {counts}. A latency or beat-order fault cannot change how many beats come out "
            f"(the k-th beat still arrives, only later), so this is NOT a timing/ordering fault, and the 'expected X "
            f"got Y' lines are beats compared out of step: they prove nothing about the arithmetic and must not be "
            f"read as shifted values. Do not blame the testbench's compare order or its latency handling. The "
            f"number of beats is set by the RTL's strobe / valid logic on one side and by the golden model's strobe "
            f"on the other: count the input samples sent and compare both totals with the rate the spec gives "
            f"(e.g. 'about 1 per 4 clocks'). likely_cause is 'rtl' when the design's total disagrees with the spec, "
            f"'test_vectors' when the expected total does; when both disagree, likely_cause is 'rtl' and the summary "
            f"must say the vectors' total is wrong too. suggested_fix must name the strobe / valid logic (or the "
            f"golden model's loop), never the compare order or the latency. On the RTL side the usual cause of a "
            f"wrong beat RATE (one per 16 instead of one per 4) is a loop value (eta, v, acc) copied down the rows "
            f"and back into row 1 (`eta_s1 <= eta_s11`) instead of one `eta_r` register in `p_loop` (linter code "
            f"loop_state_ring): when the RTL has such lines, suggested_fix must name them first.")


# What the RTL writer must check when every output beat came out as 0. Each
# item is one plain sentence; the list is sent to the RTL agent word for word.
ALL_ZERO_OUTPUT_CHECKS = (
    "(1) Count the registers one input bit passes through on its way to m_axis_tdata, including any output "
    "register such as m_axis_tdata_reg written inside the clocked process; valid_pipe must have exactly that "
    "many bits and m_axis_tvalid must come from its LAST bit, gated by the strobe of that same row when the spec "
    "outputs on a strobe (`valid_pipe(9) and strobe_s10`) (seven data registers with valid_pipe(5 downto 0) "
    "is the usual fault). "
    "(2) The output register must be written in the SAME clocked process, under the SAME `advance = '1'` "
    "condition, as the valid bit that announces it; a write under another condition or in another process "
    "leaves the register at its reset value when valid rises. "
    "(3) m_axis_tdata must be driven from the register the last valid bit belongs to, not from an earlier or "
    "later stage; fix the stage map (one row per register, the output register as its own row), then the code."
)


def all_zero_output_hint(sim_output) -> str | None:
    """A fixed-rule reading for "expected X got 0" on every line, or None.

    When every value the testbench reports as "got" is 0 while the expected
    values are not, the output wire never carried a computed value at the
    moment the testbench read it. Arithmetic cannot do that: a wrong formula
    gives wrong numbers, not zeros everywhere. Seen on llr_demap16
    (2026-10-29): the output register was written one clock after the last
    valid bit, so valid rose while the register still held its reset/'U'
    value, and the LLM analyst blamed "the demapping calculation or output
    packing". Note: to_integer prints 0 for a vector that still holds 'U'/'X',
    so "got 0" can also mean "got an undefined value"."""
    pairs = _expected_got_pairs(sim_output)
    if len(pairs) < 2 or any(not _ZERO_VALUE.match(g) for _, g in pairs):
        return None
    nonzero_expected = [e for e, _ in pairs if not _ZERO_VALUE.match(e)]
    if len(nonzero_expected) < 2:
        return None
    checks = f" Checks for the RTL: {ALL_ZERO_OUTPUT_CHECKS}" if ALL_ZERO_OUTPUT_CHECKS.strip() else ""
    return (f"FIXED RULE (not a guess): all {len(pairs)} 'expected X got Y' lines report got 0 (or an undefined "
            f"'U'/'X' value, which to_integer prints as 0) while {len(nonzero_expected)} of them expected a "
            f"non-zero value. The output wire never carried a computed value when the testbench read it. This is "
            f"NOT arithmetic and NOT output packing: a wrong formula gives wrong numbers, not zeros on every beat. "
            f"Do not ask for the test vectors to be recomputed. If the testbench compares only when the design's "
            f"valid signal is high (beat order), likely_cause is 'rtl': the design raised valid while its output "
            f"register still held its reset value. If the testbench compares at a fixed cycle without waiting "
            f"for valid, the output had simply not arrived yet: likely_cause is 'testbench'.{checks}")


_NEVER_ROSE = re.compile(r"\bcycle\s+(\d+)\s*:\s*s_axis_tready\s+never\s+rose", re.IGNORECASE)


def idle_row_deadlock_hint(sim_output, inputs_csv) -> str | None:
    """A fixed-rule reading of "cycle N: s_axis_tready never rose (deadlock)"
    lines, or None.

    A row with s_axis_tvalid = 0 sends no beat, so there is nothing for
    s_axis_tready to accept; a testbench that still waits for it on such a
    row runs its edge limit and reports a deadlock that is not there. When
    EVERY deadlock cycle is such an idle row in test_vectors_in.csv, the
    fault is the testbench's hold loop (llr_demap16, 2026-10-29 16:40:
    cycles 0-1 and 50-59 were idle rows, the RTL's ready was high, and the
    analyst blamed the RTL)."""
    if not isinstance(sim_output, str) or not isinstance(inputs_csv, str) or not inputs_csv.strip():
        return None
    cycles = sorted({c for c in _NEVER_ROSE.findall(sim_output)}, key=int)
    if not cycles:
        return None
    rows = list(csv.DictReader(io.StringIO(inputs_csv)))
    tvalid = {str(r.get("cycle") or "").strip(): str(r.get("s_axis_tvalid") or "").strip() for r in rows}
    if any(c not in tvalid for c in cycles) or any(tvalid[c] != "0" for c in cycles):
        return None                                     # a real beat was stuck, or no tvalid column: no rule
    shown = ", ".join(cycles[:6]) + (", ..." if len(cycles) > 6 else "")
    return (f"FIXED RULE (not a guess): all {len(cycles)} 'never rose (deadlock)' lines (cycles {shown}) are rows "
            f"of test_vectors_in.csv with s_axis_tvalid = 0. Those rows send no beat, so s_axis_tready has "
            f"nothing to accept and the design is not stuck: the testbench's hold loop waits for a handshake on an "
            f"idle row. likely_cause is 'testbench'. Do not blame the RTL and do not ask for the test vectors to "
            f"be recomputed. suggested_fix: make the hold loop's exit line `if cur_s_axis_tvalid = 0 or "
            f"dut_s_axis_tready = '1' then exit; end if;` so an idle row lasts exactly one edge "
            f"(vhdl_2008_testbench_rules, STREAMING DESIGNS). Any 'expected X got Y' lines must still be judged "
            f"on their own.")


# "cycle 8: unexpected m_axis_tvalid", "cycle 18: missing m_axis_tvalid",
# "cycle 7: m_axis_tvalid expected 0 got '1'", "cycle 0: s_axis_tready expected 0 got '1'",
# "cycle 8: m_axis_tvalid: expected 0 got '1'", "cycle 8: m_axis_tvalid mismatch ..."
# (the same forms the testbench linter's _flag_cycle_compare rejects)
_FLAG_LINE = re.compile(r"\bcycle\s+\d+\s*:\s*(?:(?:missing|unexpected)\s+(?:m_axis_tvalid|s_axis_tready)\b"
                        r"|(?:m_axis_tvalid|s_axis_tready)\s*:?\s+(?:expected|mismatch|differs|wrong)\b)", re.IGNORECASE)


def flag_cycle_compare_hint(sim_output) -> str | None:
    """A fixed-rule reading of lines that compare a handshake flag
    (m_axis_tvalid, s_axis_tready) with the CSV cycle by cycle, or None.

    On a streaming design the cycle an output appears on is the RTL's own
    (its latency, which the spec may let it declare; its strobe pattern),
    and the golden model cannot know it: the CSV's m_axis_tvalid column only
    says WHICH rows hold expected beats, in order. A testbench that reports
    "missing m_axis_tvalid" on the CSV's rows and "unexpected m_axis_tvalid"
    on the design's real beats checks WHEN instead of WHAT, and every data
    line it prints was compared on the wrong cycle. timing_recovery,
    2026-10-30: the analyst asked the RTL writer to fix the NCO phase and
    the latency L, which the spec leaves to the RTL."""
    if not isinstance(sim_output, str):
        return None
    lines = [line.strip() for line in sim_output.splitlines() if _FLAG_LINE.search(line)]
    if not lines:
        return None
    first = re.sub(r"^\w+:\s*", "", lines[0])             # drop xsim's "Error: " prefix
    return (f"FIXED RULE (not a guess): {len(lines)} line(s) of the log compare a handshake flag with the CSV cycle "
            f"by cycle (e.g. {first!r}). On a streaming design the cycle an output appears on (the RTL's latency, "
            f"its strobe pattern) and what s_axis_tready shows during reset belong to the RTL and are not in the "
            f"CSV: the CSV's m_axis_tvalid column only marks WHICH rows hold expected beats, in order. likely_cause "
            f"is 'testbench'. Do not blame the RTL's latency, strobe or valid timing, and do not ask for the test "
            f"vectors to be recomputed. suggested_fix: collect the outputs rows with m_axis_tvalid = 1 as the "
            f"expected beats (one array per payload column: m_axis_tdata, m_axis_tuser, ...) and compare the k-th "
            f"DUT beat, taken on every rising edge where the DUT's m_axis_tvalid = '1' (and m_axis_tready = '1' "
            f"when that port exists), with the k-th expected beat; never report 'missing/unexpected m_axis_tvalid' "
            f"and never compare s_axis_tready per cycle (vhdl_2008_testbench_rules, STREAMING DESIGNS). Any "
            f"'expected X got Y' data lines in this log were compared on the wrong cycle and prove nothing about "
            f"the RTL.")


# "beat 2 (input cycle 22): m_axis_tuser expected 249 got 0 (tolerance 8)"
_SIGNAL_EXPECTED_GOT = re.compile(r"\b([A-Za-z_]\w*)\s+expected\s+(.+?)\s+got\s+(.+?)\s*$", re.IGNORECASE)
_TOLERANCE_NOTE = re.compile(r"\s*\(\s*tol\w*[^)]*\)\s*$", re.IGNORECASE)
STUCK_MIN_LINES = 4           # lines of one output needed before "always the same value" means anything
STUCK_MIN_EXPECTED = 3        # different expected values on those lines


def stuck_output_hint(sim_output) -> str | None:
    """A fixed-rule reading for an output that shows ONE value on every line
    while the expected value keeps changing, or None.

    timing_recovery, 2026-11-09 14:18 (run 35b45c09, attempt 4): every line
    read "m_axis_tuser expected 249 / 246 / 229 / ... got 0". The RTL's
    error value was always 0 (its mid-point used the same expression as its
    symbol, and the I and Q products were subtracted), so the loop never
    moved and mu stayed at its reset value. In attempts 2 and 3 the analyst
    had blamed the test vectors for the same design."""
    if not isinstance(sim_output, str):
        return None
    by_signal: dict[str, list[tuple[str, str]]] = {}
    for line in sim_output.splitlines():
        if _SUMMARY_LINE.search(line):
            continue
        m = _SIGNAL_EXPECTED_GOT.search(line)
        if m:
            got = _TOLERANCE_NOTE.sub("", m.group(3)).strip().lower()
            by_signal.setdefault(m.group(1), []).append((m.group(2).strip().lower(), got))
    for name, pairs in by_signal.items():
        got_values = {g for _, g in pairs}
        expected_values = {e for e, _ in pairs}
        if len(pairs) >= STUCK_MIN_LINES and len(got_values) == 1 and len(expected_values) >= STUCK_MIN_EXPECTED:
            stuck = next(iter(got_values))
            return (f"FIXED RULE (not a guess): on all {len(pairs)} lines for {name} the design answered the same value "
                    f"({stuck}) while the expected value took {len(expected_values)} different values. The logic that "
                    f"drives {name} never updates: it still holds its reset value. Unless the spec says {name} is a "
                    f"constant, likely_cause is 'rtl'; do not ask for the test vectors to be recomputed on this "
                    f"evidence, and do not blame the testbench. For a design with a loop (timing recovery: mu, the NCO "
                    f"phase, the loop filter) this means the error value that feeds the loop is always 0 or never "
                    f"reaches it. Checks for the RTL: is the value computed from its OWN inputs (a mid-point "
                    f"interpolated from the older samples, not the same expression as the symbol)? are the I and Q "
                    f"products of the error ADDED (Re{{a*conj(b)}} = aI*bI + aQ*bQ)? is the loop filter updated on "
                    f"every strobe, and does its output reach the NCO step? Any '{name}'-independent 'expected X got Y' "
                    f"data lines follow from the same fault: a loop that never moves samples at the wrong instant.")
    return None


def fixed_rule_hint(sim_output, inputs_csv=None, outputs_csv=None, spec=None) -> str | None:
    """The first fixed rule that applies to the mismatch lines, or None. The
    flag rule comes first: when the testbench checks WHEN the design answers,
    none of its other lines can be trusted. The beat-count rule comes before
    the value rules: a wrong number of beats makes every value line a beat
    compared out of step."""
    return (flag_cycle_compare_hint(sim_output)
            or beat_count_hint(sim_output, wrong_vectors_reason(inputs_csv, outputs_csv, spec))
            or shifted_values_hint(sim_output) or all_zero_output_hint(sim_output)
            or stuck_output_hint(sim_output)
            or idle_row_deadlock_hint(sim_output, inputs_csv))


# A design whose answer on a beat depends on earlier rows cannot be judged
# from one row. timing_recovery, 2026-11-09 14:18 (run 35b45c09, attempts 2
# and 3): the python_check only receives the 8 failing beats (one input word
# each); the analyst wrote in its own check "I cannot recompute the exact
# state without the full input stream ... But I am forced to return
# python_check", treated the output as the input sample of that row, printed
# expected_matches_spec=no on every beat, and check_verdict() then FORCED the
# blame onto test vectors that were correct. They were regenerated twice.
_HISTORY_RE = re.compile(
    r"\b[a-z_]\w*\[\s*[a-z]\s*[-+]\s*\d+\s*\]"                       # x[m-1], x[n+2]: an earlier / later sample
    r"|\b\w+_prev\b|\bprevious\s+(?:\w+\s+)?(?:sample|symbol|output|value)s?\b"
    r"|\bintegrator\b|\baccumulat\w*|\bloop\s+filter\b|\bfeedback\b|\bNCO\b|\bIIR\b", re.IGNORECASE)

HISTORY_NOTE = (
    "This design's answer on a beat depends on EARLIER input samples and on values it keeps between beats (a sample "
    "history, a loop filter, an NCO phase), not only on the row shown in failing_beats. One row is not enough to "
    "recompute the spec's answer, so python_check is not available for this design; do not do that arithmetic in "
    "prose either. An expected value that differs from the input sample on the same row is NORMAL (the golden model "
    "ran the whole input stream from reset: it interpolates between samples and carries the loop state) and is NOT "
    "evidence that the test vectors are wrong. Name 'test_vectors' only when a fixed_rule_hint says so. Read the "
    "pattern of the 'got' values instead: (a) got equals the raw input sample of that row or of a neighbouring row "
    "on every beat, or one output shows the same value on every beat while its expected value changes: the design's "
    "interpolator or loop is not working, likely_cause 'rtl'; (b) got sits at the saturation values (+2047 / -2048) "
    "on most beats: a product was not scaled back, likely_cause 'rtl'; (c) otherwise decide from the log and "
    "testbench_code as usual.")


def depends_on_history(spec: dict | None, beats: list[dict] | None = None) -> bool:
    """Whether one failing row is NOT enough to recompute the spec's answer:
    the design produces fewer output beats than input beats (failing_beats
    marked the entry), or the spec's behaviour reads earlier samples or
    keeps a value between beats (x[m-1], y_prev, an integrator, a loop
    filter, an NCO)."""
    if any(b.get("input_row_note") for b in beats or []):
        return True
    spec = spec or {}
    lines = [str(spec.get("description") or "")] + [str(b) for b in spec.get("behavior") or []]
    return bool(_HISTORY_RE.search("\n".join(lines)))


def forced_vector_cause(sim_output, inputs_csv, outputs_csv, spec) -> str | None:
    """'test_vectors' when the beat-count rule fired AND the vectors are
    provably wrong (wrong_vectors_reason), else None. The analyst's own
    likely_cause is overridden with it, as the python_check's cause is."""
    note = wrong_vectors_reason(inputs_csv, outputs_csv, spec)
    if note and flag_cycle_compare_hint(sim_output) is None and beat_count_hint(sim_output, note):
        return "test_vectors"
    return None


# "beat k (input cycle N): ..." — beat k is the k-th (from 0) output beat the
# testbench compared: the k-th row of test_vectors_out.csv with
# m_axis_tvalid = 1, produced by the k-th accepted input beat, the k-th row
# of test_vectors_in.csv with s_axis_tvalid = 1 outside reset. The
# "(input cycle N)" part is NOT trusted: on 2026-10-30 11:51 the testbench
# printed the cycle the output appeared (input row + 8 cycles of latency),
# the analyst decoded row 12 instead of row 4 and blamed the RTL.
# "cycle 17: ..." (a fixed-cycle compare) names the row directly.
_BEAT = re.compile(r"\bbeat\s+(\d+)\b", re.IGNORECASE)
_CYCLE = re.compile(r"\bcycle\s+(\d+)\b", re.IGNORECASE)
MAX_FAILING_ROWS = 8
_RESET_NAMES = ("aresetn", "arst_n", "rst_n", "resetn", "reset_n", "rst", "reset", "arst")


def _csv_rows(csv_text) -> list[dict]:
    """{column: value} per data row, everything stripped; [] for no CSV."""
    if not isinstance(csv_text, str) or not csv_text.strip():
        return []
    return [{str(k).strip(): str(v).strip() for k, v in r.items() if k is not None}
            for r in csv.DictReader(io.StringIO(csv_text))]


def _reset_active(row: dict, spec: dict | None) -> bool:
    """Whether this inputs row holds the reset active (spec's reset name and
    polarity when given, else the usual column names; a name ending in n is
    active-low). False when the row has no reset column."""
    reset = (spec or {}).get("reset") or {}
    name = reset.get("name") if reset.get("name") in row else next((h for h in row if h.lower() in _RESET_NAMES), None)
    if not name:
        return False
    low = str(reset.get("active", "")).lower() == "low" if reset.get("active") else name.lower().endswith("n")
    return row[name] == ("0" if low else "1")


def _beat_rows(rows: list[dict], valid_col: str, spec: dict | None, skip_reset: bool) -> list[dict]:
    """The rows that carry a beat (valid_col = 1), in order; [] without that column."""
    if not rows or valid_col not in rows[0]:
        return []
    return [r for r in rows if r.get(valid_col) == "1" and not (skip_reset and _reset_active(r, spec))]


def failing_beats(sim_output, inputs_csv, outputs_csv=None, spec=None) -> list[dict]:
    """One entry per distinct 'expected X got Y' line (first MAX_FAILING_ROWS,
    log order): {"line", "beat" or "cycle", "input_row", "expected_row",
    "input_cycle"}. input_row is the test_vectors_in.csv row that carried the
    beat (every input port as written) and expected_row the
    test_vectors_out.csv row the testbench compared against; either is None
    when that CSV is missing or has no such row. Lines naming a beat or
    cycle neither CSV has are dropped. [] without a log or without CSVs.

    Why: on llr_demap16 (2026-10-29, run 170022) the golden model packed the
    I field with 2^12 instead of 2^20; the design answered correctly for the
    word it was given, every beat "mismatched", and the analyst, who never
    saw the CSV, blamed the RTL three times. With the row in hand it can
    decode the word with the spec's field layout and see the CSV is at fault
    (likely_cause "test_vectors")."""
    if not isinstance(sim_output, str):
        return []
    in_rows, out_rows = _csv_rows(inputs_csv), _csv_rows(outputs_csv)
    if not in_rows and not out_rows:
        return []
    in_beats = _beat_rows(in_rows, "s_axis_tvalid", spec, skip_reset=True)
    out_beats = _beat_rows(out_rows, "m_axis_tvalid", spec, skip_reset=False)
    in_by_cycle = {r.get("cycle"): r for r in in_rows}
    out_by_cycle = {r.get("cycle"): r for r in out_rows}
    # A design that produces fewer output beats than it receives input beats
    # (timing recovery: one symbol per ~4 samples) has more input beats than
    # expected beats, so the k-th input beat is NOT the sample that produced
    # output beat k. The golden-model rule puts the expected beat on the row
    # of the input sample that produced it: that row's stimulus is used.
    decimating = bool(in_beats) and bool(out_beats) and len(in_beats) != len(out_beats)
    out: list[dict] = []
    seen: set[tuple] = set()
    for line in sim_output.splitlines():
        if not _EXPECTED_GOT.search(line):
            continue
        beat, cycle = _BEAT.search(line), _CYCLE.search(line)
        entry: dict = {"line": line.strip()}
        if beat:
            k = int(beat.group(1))
            key = ("beat", k)
            entry["beat"] = k
            entry["expected_row"] = out_beats[k] if k < len(out_beats) else None
            if decimating and entry["expected_row"] is not None:
                entry["input_row"] = in_by_cycle.get(entry["expected_row"].get("cycle"))
                entry["input_row_note"] = ("the design produces fewer output beats than it receives input beats, so "
                                           "input_row is the stimulus row on the same cycle as expected_row (the "
                                           "sample that produced this beat), not the k-th input beat; a loop or "
                                           "filter output also depends on the rows before it")
            else:
                entry["input_row"] = in_beats[k] if k < len(in_beats) else None
        elif cycle:
            key = ("cycle", cycle.group(1))
            entry["cycle"] = cycle.group(1)
            entry["input_row"] = in_by_cycle.get(cycle.group(1))
            entry["expected_row"] = out_by_cycle.get(cycle.group(1))
        else:
            continue
        if key in seen or (entry["input_row"] is None and entry["expected_row"] is None):
            continue
        seen.add(key)
        entry["input_cycle"] = entry["input_row"].get("cycle") if entry["input_row"] else None
        out.append(entry)
        if len(out) >= MAX_FAILING_ROWS:
            break
    return out


def failing_input_rows(sim_output, inputs_csv, spec=None) -> list[dict]:
    """Just the input rows of failing_beats (the stimulus the design really received)."""
    return [b["input_row"] for b in failing_beats(sim_output, inputs_csv, None, spec) if b.get("input_row")]


# ── the analyst's calculator ─────────────────────────────────────────────
# The analyst used to decode the failing input word and work out the spec's
# answer in prose, and got it wrong in a different way on every attempt
# (2026-10-30: the word 805503152 was read as I=128/Q=128/ns=128 once and
# as I=192/Q=192/ns=255 the next time; it is I=768, Q=768, ns=176). A model
# is far more reliable WRITING a check than doing arithmetic in text, so for
# a "fail" with failing_beats the analyst first returns a short Python
# script; it is run here and its output goes back before the verdict.
MAX_PYTHON_CHECKS = 2                 # runs per analysis: the first, plus one fix after an error
PYTHON_CHECK_TIMEOUT_S = 20
PYTHON_CHECK_OUTPUT_CHARS = 4000
_CHECK_LINE = re.compile(r"^\s*CHECK\s+beat=(\S+)\s+got_matches_spec=(yes|no)\s+expected_matches_spec=(yes|no)\s*$",
                         re.IGNORECASE | re.MULTILINE)


def run_python_check(code: str, beats: list[dict], spec: dict | None) -> dict:
    """Run the analyst's check in a fresh folder with an isolated interpreter
    (-I: no user site, no env, no cwd on the path), stdin closed, with a time
    limit. FAILING_BEATS and SPEC are predefined so no number is retyped.
    Returns {"code", "status": "ok" | "error" | "timeout", "stdout", "stderr"}."""
    header = f"FAILING_BEATS = {beats!r}\nSPEC = {spec!r}\n\n"
    with tempfile.TemporaryDirectory(prefix="sim_check_") as folder:
        path = Path(folder) / "check.py"
        path.write_text(header + str(code), encoding="utf-8")
        try:
            proc = subprocess.run([sys.executable, "-I", "-B", str(path)], cwd=folder, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=PYTHON_CHECK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return {"code": code, "status": "timeout", "stdout": "",
                    "stderr": f"the check did not finish within {PYTHON_CHECK_TIMEOUT_S} s"}
    return {"code": code, "status": "ok" if proc.returncode == 0 else "error",
            "stdout": proc.stdout[-PYTHON_CHECK_OUTPUT_CHARS:], "stderr": proc.stderr[-PYTHON_CHECK_OUTPUT_CHARS:]}


def check_verdict(stdout) -> str | None:
    """A fixed rule read off the CHECK lines the analyst's script printed.
    "test_vectors" when the golden CSV differs from the spec's value on
    EVERY failing beat (whatever the design answered: a CSV that is wrong
    everywhere is wrong, and the RTL cannot be blamed for not matching it),
    or when the design equals the spec on every beat while the CSV differs
    on at least one; "rtl" when the golden CSV equals the spec's value on
    every beat and the design differs on at least one; None otherwise
    (mixed, or no CHECK lines).

    timing_recovery, 2026-11-05 (run 3106ab33, attempt 2): the check printed
    expected_matches_spec=no on all 8 beats and got_matches_spec=no too, so
    the old rule (design right everywhere) stayed silent and the LLM blamed
    the RTL a second time."""
    lines = _CHECK_LINE.findall(stdout or "")
    if not lines:
        return None
    got_ok = [g.lower() == "yes" for _, g, _ in lines]
    exp_ok = [e.lower() == "yes" for _, _, e in lines]
    if not any(exp_ok):
        return "test_vectors"
    if all(got_ok) and not all(exp_ok):
        return "test_vectors"
    if all(exp_ok) and not all(got_ok):
        return "rtl"
    return None


def check_rule_hint(cause: str, check: dict) -> str:
    """The fixed-rule text for a check_verdict, sent to the analyst and kept
    on the analysis so the blamed agent reads it word for word."""
    lines = _CHECK_LINE.findall(check.get("stdout") or "")
    n = len(lines)
    if cause == "test_vectors":
        if not any(e.lower() == "yes" for _, _, e in lines):
            return (f"FIXED RULE (not a guess): the Python check you wrote, run by code, shows the golden CSV's "
                    f"expected value differs from the spec's answer on ALL {n} failing beats. Whatever the design "
                    f"answered, a golden CSV that is wrong on every beat cannot judge it: likely_cause MUST be "
                    f"'test_vectors'. Do not blame the RTL or the testbench. In suggested_fix quote the decoded "
                    f"fields, the spec's value and the CSV's value of the first beat and say what the golden model "
                    f"must have computed differently (a field packed at the wrong shift, a signed value wrapped with "
                    f"mod, a loop state the model updates in a different order than the spec).")
        return (f"FIXED RULE (not a guess): the Python check you wrote, run by code, shows the design's output equals "
                f"the spec's answer on all {n} failing beats while the golden CSV differs. The RTL is right and the "
                f"test vectors are wrong: likely_cause MUST be 'test_vectors'. Do not blame the RTL or the testbench. "
                f"In suggested_fix quote the decoded fields and the spec's value of the first beat and say what the "
                f"golden model must have computed differently.")
    return (f"FIXED RULE (not a guess): the Python check you wrote, run by code, shows the golden CSV equals the "
            f"spec's answer on all {n} failing beats while the design's output differs. likely_cause MUST be 'rtl'. "
            f"Do not ask for the test vectors to be recomputed.")


class SimulationAnalyst:
    def __init__(self, llm=None):
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def _ask(self, payload: dict) -> tuple[str, dict | None]:
        """One round with the LLM: (raw reply, its JSON object when it has a
        verdict or a python_check, else None)."""
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        response = self.llm.invoke(
            [SystemMessage(content=SIMULATION_AGENT_SYSTEM_PROMPT), HumanMessage(content=json.dumps(payload, indent=2))],
            config={"callbacks": [handler]},
        )
        raw = str(response.content)
        try:
            data = extract_json(raw)
        except (json.JSONDecodeError, ValueError):
            return raw, None
        if isinstance(data, dict) and (data.get("verdict") or data.get("python_check")):
            return raw, data
        return raw, None

    def analyze(self, project: dict, compile_result: dict | None, sim: dict | None) -> dict:
        in_csv, out_csv, spec = project.get("test_vectors_in_csv"), project.get("test_vectors_out_csv"), project.get("spec")
        sim_output = (sim or {}).get("sim_output")
        beats = failing_beats(sim_output, in_csv, out_csv, spec)
        inside = self._inside_values(project, beats) if beats else None
        verdict = deterministic_verdict(compile_result, sim, spec)
        if verdict is not None:
            return with_evidence(verdict, compile_result, sim, beats, inside)
        compile_for_llm, sim_for_llm = _trim_for_llm(compile_result, sim)
        payload = {
            "spec": project.get("spec"),
            "testbench_code": project.get("testbench_code"),
            "stage": "simulate" if (compile_result or {}).get("status") == "ok" else "compile",
            "compile": compile_for_llm,
            "sim": sim_for_llm,
        }
        if (sim or {}).get("outcome") == "no_marker" and mismatches_capped(sim_output):
            payload["note"] = (f"There is no SIMULATION PASSED/FAILED marker because the run (sim_time="
                               f"{sim.get('sim_time')}) ended before the testbench reached its last test-vector row; "
                               f"the stimulus is longer than the run. This is NOT a frozen handshake: the testbench "
                               f"printed its first {MAX_REPORTED_MISMATCHES} mismatches and then stopped printing by "
                               f"its own cap. Judge those mismatch lines exactly as you would a failed run (verdict "
                               f"'fail'); never blame the testbench's waits or ask for a longer sim_time.")
        hint = fixed_rule_hint(sim_output, in_csv, out_csv, spec)
        if hint:
            payload["fixed_rule_hint"] = hint
        forced_cause = forced_vector_cause(sim_output, in_csv, out_csv, spec)
        if beats:
            payload["failing_beats"] = beats
        if inside:
            payload["inside_values"] = inside
        # a log rule (shifted values, all zeros, idle-row deadlocks) already
        # settled it without arithmetic; the calculator is for value mismatches
        # of designs whose answer can be recomputed from the one failing row
        stateful = depends_on_history(spec, beats)
        payload["python_check_allowed"] = bool(beats) and hint is None and not stateful
        if stateful and beats and hint is None:
            payload["note"] = " ".join(n for n in (payload.get("note"), HISTORY_NOTE) if n)

        raw, reply = self._ask(payload)
        check: dict | None = None
        runs = 0
        while (payload["python_check_allowed"] and reply and isinstance(reply.get("python_check"), str)
               and reply["python_check"].strip() and runs < MAX_PYTHON_CHECKS):
            runs += 1
            check = run_python_check(reply["python_check"], beats, project.get("spec"))
            payload["python_check"] = dict(check)
            payload["python_check_allowed"] = check["status"] != "ok" and runs < MAX_PYTHON_CHECKS
            cause = check_verdict(check["stdout"]) if check["status"] == "ok" else None
            if cause:
                payload["fixed_rule_hint"] = check_rule_hint(cause, check)
            raw, reply = self._ask(payload)
        if reply and not reply.get("verdict"):          # it asked for yet another check: no, decide now
            payload["python_check_allowed"] = False
            payload["note"] = "No further python_check is possible. Return the final verdict JSON now."
            raw, reply = self._ask(payload)
        elif stateful and reply and reply.get("python_check") and not reply.get("likely_cause"):
            # it sent a check although none is possible for this design; the check is not run
            payload["note"] = HISTORY_NOTE + " No further python_check is possible. Return the final verdict JSON now."
            raw, reply = self._ask(payload)

        analysis = reply if reply and reply.get("verdict") else parse_analysis_response(raw, sim)
        analysis.pop("python_check", None)
        if check:
            analysis["python_check"] = check
            cause = check_verdict(check["stdout"]) if check["status"] == "ok" else None
            if cause:
                # the check's reading travels as a FIXED RULE the analyst has already read; since
                # 2026-11-10 it no longer overrides the verdict (run 35b45c09: a check that could not
                # see the sample history forced the blame onto correct vectors, twice)
                analysis["fixed_rule"] = check_rule_hint(cause, check)
        if hint:
            analysis["fixed_rule"] = hint     # travels with the blame note, word for word (blame.downstream_note)
        # The ONE override left: the vectors are provably wrong by code that read the CSV files
        # themselves (constant input / the strobe never fires), no LLM arithmetic involved. Blaming
        # the RTL or the testbench would only send a worker to fix a file it cannot fix.
        if forced_cause and analysis.get("verdict") == "fail" and analysis.get("likely_cause") != forced_cause:
            analysis["overridden_cause"] = analysis.get("likely_cause")
            analysis["likely_cause"] = forced_cause
        return with_evidence(analysis, compile_result, sim, beats, inside)

    @staticmethod
    def _inside_values(project: dict, beats: list[dict]) -> dict | None:
        """The chip's loop registers (from the VCD the simulator wrote next to
        the project) and the golden model's loop values (loop_trace.csv) on the
        beats around the first failure; None when neither is there. The VCD is
        read straight from disk: it sits in the project folder on this machine,
        and a project without one (tests, a remote simulator) simply has none."""
        vcd_text = None
        try:
            path = Path(str(project.get("dir") or "")) / WAVES_PATH
            if project.get("dir") and path.is_file():
                vcd_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            vcd_text = None
        try:
            return inside_values(vcd_text, project.get("loop_trace_csv"), beats)
        except Exception:                                   # a malformed wave file must never stop the analysis
            return None

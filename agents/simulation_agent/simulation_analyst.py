# agents/simulation_agent/simulation_analyst.py — tool: turn compile +
# simulation results into a verdict. A clean pass and a run that never
# printed a marker are decided deterministically; everything else goes to
# the LLM analyst (prompts/simulation_agent_prompt.py).

from __future__ import annotations

import csv
import io
import json
import re

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.simulation_agent.state import AGENT_NAME
from observability.callback import ObservabilityCallbackHandler
from prompts.simulation_agent_prompt import SIMULATION_AGENT_SYSTEM_PROMPT
from services.llm_client import get_llm


def deterministic_verdict(compile_result: dict | None, sim: dict | None) -> dict | None:
    """Verdict without an LLM when the evidence is unambiguous; None otherwise."""
    outcome = (sim or {}).get("outcome")
    if outcome == "passed":
        return {"verdict": "pass", "likely_cause": None, "failures": [],
                "summary": "Simulation passed: the testbench reported SIMULATION PASSED with no errors.",
                "suggested_fix": None}
    if outcome == "no_marker":
        failure = f"No SIMULATION PASSED/FAILED marker within sim_time={sim.get('sim_time')}."
        stalled_at = stalled_time_ns(sim.get("sim_output"), sim.get("sim_time"))
        if stalled_at is not None:
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


def _trim_for_llm(compile_result: dict | None, sim: dict | None) -> tuple[dict | None, dict | None]:
    """Copies of the compile/sim dicts with long log fields cut to a tail."""
    compile_trimmed = None
    if compile_result:
        compile_trimmed = dict(compile_result)
        compile_trimmed["results"] = [
            {**r, "stdout": _tail(r.get("stdout")), "stderr": _tail(r.get("stderr"))}
            for r in (compile_result.get("results") or [])
        ]
    sim_trimmed = {**sim, "sim_output": _tail(sim.get("sim_output"))} if sim else None
    return compile_trimmed, sim_trimmed


# "cycle 17: m_axis_tdata expected hi=125 lo=61440 got hi=0 lo=0"
_EXPECTED_GOT = re.compile(r"\bexpected\s+(.+?)\s+got\s+(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


_ZERO_VALUE = re.compile(r"^[^0-9]*0(?:[^0-9]+0)*[^0-9]*$")      # "0", "hi=0 lo=0", "0_0_0"


def _expected_got_pairs(sim_output) -> list[tuple[str, str]]:
    """(expected, got) from every 'expected X got Y' line, lower-cased; [] for no log."""
    if not isinstance(sim_output, str):
        return []
    return [(e.strip().lower(), g.strip().lower()) for e, g in _EXPECTED_GOT.findall(sim_output)]


def shifted_values_hint(sim_output) -> str | None:
    """A fixed-rule reading of the mismatch lines, or None.

    When a value the testbench reports as "got" on one cycle is exactly a
    value it "expected" on another cycle, the design computed the right
    answer but it was checked at the wrong time. That is a timing or
    ordering fault, never arithmetic: the LLM analyst blamed the RTL's
    arithmetic for this four runs in a row (llr_demap16, 2026-10-29) and
    the RTL was rewritten to no effect. The hint tells it where to look."""
    pairs = _expected_got_pairs(sim_output)
    if len(pairs) < 2:
        return None
    expected = {e for e, _ in pairs}
    shifted = [g for e, g in pairs if g != e and g in expected and not _ZERO_VALUE.match(g)]
    if len(shifted) < 2:
        return None
    return (f"FIXED RULE (not a guess): {len(shifted)} of the {len(pairs)} 'expected X got Y' lines report a Y that "
            f"is exactly the expected value of ANOTHER cycle (e.g. got {shifted[0]!r}). The design therefore "
            f"computes the right values; they are only checked at the wrong time. This is a timing/ordering fault, "
            f"not arithmetic: do not blame the RTL's arithmetic and do not ask for the test vectors to be "
            f"recomputed. If the testbench compares an output against the CSV row of the same cycle (a streaming "
            f"design answers several cycles later), likely_cause is 'testbench'. Only if the testbench already "
            f"compares beats in order is the RTL at fault: its valid flag is not aligned with its data "
            f"(valid pipeline one stage longer or shorter than the data pipeline).")


# What the RTL writer must check when every output beat came out as 0. Each
# item is one plain sentence; the list is sent to the RTL agent word for word.
ALL_ZERO_OUTPUT_CHECKS = (
    "(1) Count the registers one input bit passes through on its way to m_axis_tdata, including any output "
    "register such as m_axis_tdata_reg written inside the clocked process; valid_pipe must have exactly that "
    "many bits and m_axis_tvalid must come from its LAST bit (seven data registers with valid_pipe(5 downto 0) "
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


def fixed_rule_hint(sim_output, inputs_csv=None) -> str | None:
    """The first fixed rule that applies to the mismatch lines, or None."""
    return (shifted_values_hint(sim_output) or all_zero_output_hint(sim_output)
            or idle_row_deadlock_hint(sim_output, inputs_csv))


# "beat 1 (input cycle 14): ..." names the inputs row that carried the beat;
# "cycle 17: ..." (a fixed-cycle compare) names the row directly.
_INPUT_CYCLE = re.compile(r"\binput\s+cycle\s+(\d+)|\bcycle\s+(\d+)", re.IGNORECASE)
MAX_FAILING_ROWS = 8


def failing_input_rows(sim_output, inputs_csv) -> list[dict]:
    """The test_vectors_in.csv rows ({column: value}, every input port) of the
    cycles the 'expected X got Y' lines name: the stimulus the design really
    received. First MAX_FAILING_ROWS distinct cycles, in log order; [] when
    there is no log, no CSV or no named cycle.

    Why: on llr_demap16 (2026-10-29, run 170022) the golden model packed the
    I field with 2^12 instead of 2^20, so the input word carried I = 1 where
    the model had computed with I = 256. The design answered correctly for
    the word it was given, every beat "mismatched", and the analyst, who
    never saw the CSV, blamed the RTL three times. With the row in hand it
    can decode the word with the spec's field layout and see the CSV is at
    fault (likely_cause "test_vectors")."""
    if not isinstance(sim_output, str) or not isinstance(inputs_csv, str) or not inputs_csv.strip():
        return []
    rows = list(csv.DictReader(io.StringIO(inputs_csv)))
    by_cycle = {str(r.get("cycle") or "").strip(): r for r in rows}
    out: list[dict] = []
    seen: set[str] = set()
    for line in sim_output.splitlines():
        if not _EXPECTED_GOT.search(line):
            continue
        m = _INPUT_CYCLE.search(line)
        cycle = (m.group(1) or m.group(2)) if m else None
        if cycle is None or cycle in seen or cycle not in by_cycle:
            continue
        seen.add(cycle)
        out.append({str(k).strip(): str(v).strip() for k, v in by_cycle[cycle].items() if k is not None})
        if len(out) >= MAX_FAILING_ROWS:
            break
    return out


class SimulationAnalyst:
    def __init__(self, llm=None):
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def analyze(self, project: dict, compile_result: dict | None, sim: dict | None) -> dict:
        verdict = deterministic_verdict(compile_result, sim)
        if verdict is not None:
            return verdict
        compile_for_llm, sim_for_llm = _trim_for_llm(compile_result, sim)
        payload = {
            "spec": project.get("spec"),
            "testbench_code": project.get("testbench_code"),
            "stage": "simulate" if (compile_result or {}).get("status") == "ok" else "compile",
            "compile": compile_for_llm,
            "sim": sim_for_llm,
        }
        hint = fixed_rule_hint((sim or {}).get("sim_output"), project.get("test_vectors_in_csv"))
        if hint:
            payload["fixed_rule_hint"] = hint
        rows = failing_input_rows((sim or {}).get("sim_output"), project.get("test_vectors_in_csv"))
        if rows:
            payload["failing_input_rows"] = rows
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        response = self.llm.invoke(
            [SystemMessage(content=SIMULATION_AGENT_SYSTEM_PROMPT), HumanMessage(content=json.dumps(payload, indent=2))],
            config={"callbacks": [handler]},
        )
        analysis = parse_analysis_response(response.content, sim)
        if hint:
            analysis["fixed_rule"] = hint     # travels with the blame note, word for word (blame.downstream_note)
        return analysis

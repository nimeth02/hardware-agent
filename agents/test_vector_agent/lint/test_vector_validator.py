# agents/test_vector_agent/lint/test_vector_validator.py — deterministic checks
# on the two CSVs the test-vector agent produced: both exist, each has the
# expected header (cycle, then that direction's spec ports in spec order),
# they have the same rows with the same cycle numbers, and every value is
# an integer that fits its port width. Streaming designs also get value
# checks: the input data must not be one constant and, when the spec states
# an output rate, the expected output beat count must roughly match it, the
# input/output data must not be one value on nearly every beat, and output
# beats must not fill many consecutive rows. The files are
# read through the file-server MCP. The testbench replays these files without seeing them,
# so the layout is enforced exactly.

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from agents.mcp_utils import mcp_read_file
from agents.test_vector_agent.lint.output_range_check import saturated_output_issue

INPUT_CSV = "test_vectors_in.csv"
OUTPUT_CSV = "test_vectors_out.csv"

_PLAIN_WIDTH_RE = re.compile(r"^\d+$")


def _port_bits(port: dict) -> int | None:
    width = str(port.get("width") or "").strip()
    if _PLAIN_WIDTH_RE.match(width):
        return int(width)
    m = re.match(r"^\[(\d+):(\d+)\]$", width)
    if m:
        return abs(int(m.group(1)) - int(m.group(2))) + 1
    return None


def _check_file(label: str, run_dir: str, name: str, ports: list[dict], issue) -> tuple[list[str], list[list[str]]]:
    """Header + data rows of one CSV, with its structural and value issues
    recorded. ([], []) when the file is missing or empty."""
    expected = ["cycle"] + [p["name"] for p in ports]
    layout = f"header must be exactly {expected} (cycle, then the {label} ports in spec order)"
    content = mcp_read_file(run_dir, name)
    if content is None:
        issue("csv_missing", f"{name} was not created in {run_dir}; {layout}.")
        return [], []
    rows = list(csv.reader(io.StringIO(content)))
    if not rows:
        issue("csv_empty", f"{name} is empty; {layout}.")
        return [], []
    header, data = [h.strip() for h in rows[0]], rows[1:]

    if not header or header[0] != "cycle":
        issue("no_cycle_column", f"{name}: first column must be 'cycle'.")
    for p in ports:
        if p["name"] not in header:
            issue("port_column_missing", f"{name}: no column for {label} port '{p['name']}'; {layout}; got {header}.")
    if ports and set(expected) <= set(header) and header != expected:
        issue("column_order", f"{name}: {layout}; got {header}.")
    if not data:
        issue("no_rows", f"{name}: header only — no test vectors.")

    bits = {p["name"]: _port_bits(p) for p in ports}
    bad_cells = 0
    for r, row in enumerate(data, start=2):
        if len(row) != len(header):
            issue("ragged_row", f"{name}: row {r} has {len(row)} cells, header has {len(header)}.")
            break
        for col, cell in zip(header, row):
            try:
                value = int(float(cell))
            except ValueError:
                bad_cells += 1
                if bad_cells <= 3:
                    issue("non_integer", f"{name}: row {r}, column '{col}': '{cell}' is not an integer.")
                continue
            n = bits.get(col)
            if n is not None and not (0 <= value < 2**n):
                issue("out_of_range", f"{name}: row {r}, column '{col}': {value} does not fit {n} bit(s).")
    return header, data


def _reset_row_checks(spec: dict, in_header: list[str], in_rows: list[list[str]],
                      out_header: list[str], out_rows: list[list[str]], issue) -> None:
    """Streaming designs only: a row that holds the reset active must not
    send an input beat (s_axis_tvalid = 1) or expect an output beat
    (m_axis_tvalid = 1). The design drops anything sent during reset, so
    such a beat can never be matched and the whole beat order is off by
    one from then on (llr_demap16 run, 2026-10-29: the golden model wrote
    the first beat on cycle 3 while aresetn was still 0)."""
    if "s_axis_tvalid" not in in_header or "m_axis_tvalid" not in out_header:
        return                                      # not a streaming design
    reset = spec.get("reset") or {}
    name = reset.get("name") or next((h for h in in_header if h.lower() in ("aresetn", "rst_n", "resetn", "rst", "reset")), None)
    if not name or name not in in_header:
        return
    active = "0" if str(reset.get("active", "")).lower() == "low" or name.lower().endswith("n") else "1"
    rst_col, tvalid_col, mvalid_col = in_header.index(name), in_header.index("s_axis_tvalid"), out_header.index("m_axis_tvalid")
    for r, (in_row, out_row) in enumerate(zip(in_rows, out_rows), start=2):
        if len(in_row) <= max(rst_col, tvalid_col) or len(out_row) <= mvalid_col:
            continue                                # ragged rows are reported by _check_file
        if in_row[rst_col].strip() != active:
            continue
        sent = in_row[tvalid_col].strip() == "1"
        expected = out_row[mvalid_col].strip() == "1"
        if sent or expected:
            what = "s_axis_tvalid" if sent else "m_axis_tvalid"
            issue("beat_during_reset",
                  f"row {r}: {what} = 1 while {name} = {active} (reset active). The design drops everything sent "
                  f"during reset, so this beat can never be matched and every later beat is checked against the "
                  f"wrong expected value. Keep s_axis_tvalid = 0 and m_axis_tvalid = 0 on every reset row and "
                  f"send the first beat on the first row after the reset is released.")
            break                                   # one message per mistake, not one per row


def _backpressure_row_checks(in_header: list[str], in_rows: list[list[str]], issue) -> None:
    """Streaming designs only: a row with m_axis_tready = 0 must not send an
    input beat (s_axis_tvalid = 1). The pipeline cannot accept while its
    output is blocked, so the testbench's "hold the row until accepted"
    wait never ends and the run finishes as no_marker (llr_demap16 run,
    2026-10-29 12:00: rows 12, 16 and 20 had tvalid = 1 with tready = 0)."""
    if "s_axis_tvalid" not in in_header or "m_axis_tready" not in in_header:
        return                                      # not a streaming design
    tvalid_col, tready_col = in_header.index("s_axis_tvalid"), in_header.index("m_axis_tready")
    for r, row in enumerate(in_rows, start=2):
        if len(row) > max(tvalid_col, tready_col) and row[tvalid_col].strip() == "1" and row[tready_col].strip() == "0":
            issue("beat_while_blocked",
                  f"row {r}: s_axis_tvalid = 1 while m_axis_tready = 0. The design cannot accept a beat while "
                  f"its output is blocked, so the testbench waits forever for s_axis_tready and the simulation "
                  f"never prints a result. Put s_axis_tvalid = 0 on every row with m_axis_tready = 0 and send "
                  f"that beat on the next row where m_axis_tready = 1.")
            break                                   # one message per mistake, not one per row


_MIN_BEATS_TO_JUDGE = 8                            # tiny hand-written vector sets are never judged
_NEARLY_CONSTANT_SHARE = 0.95                      # one value on >= 95 % of the beats counts as "constant"


def _dominant(values: list[str]) -> tuple[str, int]:
    """The most frequent value and how many times it occurs."""
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    value = max(counts, key=counts.get)
    return value, counts[value]


def _longest_run(flags: list[bool]) -> int:
    best = run = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best

# "one per 4 input clocks", "1 output every 4 samples", "every 4th cycle",
# "decimate by 4", "decimation factor of 4", "4:1 decimation".
_RATE_RES = [
    # up to 3 words between "one" and "per": "one recovered symbol per 4 input samples" (2026-11-11 17:15)
    re.compile(r"\b(?:one|1)\s+(?:[A-Za-z]+\s+){0,3}(?:per|every)\s+(\d+)\s+(?:input\s+)?"
               r"(?:clocks?|cycles?|samples?|inputs?|beats?|ticks?)\b", re.I),
    re.compile(r"\bevery\s+(\d+)(?:st|nd|rd|th)?\s+(?:input\s+)?(?:clocks?|cycles?|samples?|inputs?|beats?)\b", re.I),
    re.compile(r"\bdecimat\w*\s+(?:by|factor(?:\s+of)?|ratio(?:\s+of)?)\s+(\d+)\b", re.I),
    re.compile(r"\b(\d+)\s*:\s*1\s+decimat", re.I),
]


def spec_output_rate(spec: dict | None) -> int | None:
    """The 'one output per N inputs' number the spec text states, or None
    when the spec gives none (then the beat-rate check is skipped). Also
    used by the simulation analyst's beat-count rule."""
    if not isinstance(spec, dict):
        return None
    text = " ".join(str(spec.get(k) or "") for k in ("description", "behavior", "constraints", "assumptions"))
    for rx in _RATE_RES:
        m = rx.search(text)
        if m and int(m.group(1)) >= 1:
            return int(m.group(1))
    return None


def _value_sanity_checks(spec: dict, in_header: list[str], in_rows: list[list[str]],
                         out_header: list[str], out_rows: list[list[str]], issue) -> None:
    """Streaming designs only: the numbers must look like a real golden-model
    run. (a) s_axis_tdata must not be one constant on every input beat;
    (b) the expected output beats must be roughly inputs / rate when the
    spec states a rate (timing_recovery run, 2026-11-05 15:54: the MATLAB
    model divided 0 by 0, every input became NaN -> 8390656 and the model
    strobed once in 800 cycles; every layout check passed and the analyst
    blamed the RTL for 197 'unexpected' beats). When the spec states a
    rate (a decimating / closed-loop design) also: (c) the input and the
    output data must not be one value on >= 95 % of the beats, and (d) the
    output beats must not come on many consecutive rows (timing_recovery
    run 2026-11-06 11:45: 99 identical symbols + 1 flipped one dodged (a),
    the loop ran on integer codes, blew up at the first strobe and fired on
    every cycle from cycle 12 on: 193 beats for 400 inputs passed (b),
    190 of them the same number)."""
    if "s_axis_tvalid" not in in_header or "s_axis_tdata" not in in_header or "m_axis_tvalid" not in out_header:
        return                                      # not a streaming design
    tvalid_col, tdata_col, mvalid_col = in_header.index("s_axis_tvalid"), in_header.index("s_axis_tdata"), out_header.index("m_axis_tvalid")
    mdata_col = out_header.index("m_axis_tdata") if "m_axis_tdata" in out_header else None
    in_values = [row[tdata_col].strip() for row in in_rows
                 if len(row) > max(tvalid_col, tdata_col) and row[tvalid_col].strip() == "1"]
    beat_flags = [len(row) > mvalid_col and row[mvalid_col].strip() == "1" for row in out_rows]
    out_values = [row[mdata_col].strip() for row, beat in zip(out_rows, beat_flags)
                  if beat and mdata_col is not None and len(row) > mdata_col]
    out_beats = sum(beat_flags)
    in_beats = len(in_values)
    if in_beats < _MIN_BEATS_TO_JUDGE:
        return

    rate = spec_output_rate(spec)
    in_value, in_share = _dominant(in_values)
    if in_share == in_beats:
        issue("constant_input",
              f"s_axis_tdata is {in_value} on all {in_beats} input beats. A real stimulus varies, so the golden "
              f"model most likely produced NaN or Inf (a 0/0 or x/0 in the formula) and saturation turned every "
              f"sample into the same number. Find the division, give its denominator a non-zero start value, add "
              f"assert(all(isfinite(x))) before packing, and make the input a varying signal.")
    elif rate is not None and in_share >= _NEARLY_CONSTANT_SHARE * in_beats:
        issue("constant_input",
              f"s_axis_tdata is {in_value} on {in_share} of {in_beats} input beats. A stimulus that barely changes "
              f"tests nothing in a closed loop (the error e stays 0 between the few transitions). Draw the symbols "
              f"at random (randi) so every symbol can differ from the previous one.")

    if rate is None:
        if out_beats == 0:
            issue("no_output_beats",
                  f"{in_beats} input beats but m_axis_tvalid is never 1, so the test program would check nothing. "
                  f"The golden model must write at least one expected output beat.")
        return

    if len(out_values) >= _MIN_BEATS_TO_JUDGE:
        out_value, out_share = _dominant(out_values)
        if out_share >= _NEARLY_CONSTANT_SHARE * len(out_values) and out_value not in ("0", ""):
            issue("constant_output",
                  f"m_axis_tdata is {out_value} on {out_share} of {len(out_values)} expected output beats. A real "
                  f"golden-model run gives a different symbol on most beats, so either the stimulus barely varies "
                  f"(use random symbols) or the loop blew up and repeats one value. Run the whole loop on real "
                  f"values (code / 2^fraction_bits), add assert(abs(v) < 1/N, ...) at every strobe, and check the "
                  f"printed CSV before finishing.")
        elif out_share == len(out_values) and in_share < in_beats:
            # every beat the same number, zero included, while the input varies: the test proves nothing
            # (timing_recovery 2026-11-06 18:47: 99 expected beats, all 0; the warm-up exemption above let it through)
            issue("constant_output",
                  f"m_axis_tdata is {out_value or 0} on ALL {len(out_values)} expected output beats while s_axis_tdata "
                  f"takes {len(set(in_values))} different values. A test whose every answer is the same number proves "
                  f"nothing: an RTL that never computes anything passes it. In the 2026-11-06 18:47 run the pulse "
                  f"filter was applied to the symbols BEFORE the x4 upsample, so 3 of every 4 input samples were "
                  f"exactly 0 and every strobe, with mu = 0, read a zero sample. Upsample first, then filter "
                  f"(conv(upsampled, h)), check that most input samples are non-zero, and look at the printed CSV: "
                  f"the expected values must differ from beat to beat.")

    run = _longest_run(beat_flags)
    if run >= max(4, 2 * rate):
        issue("consecutive_output_beats",
              f"m_axis_tvalid is 1 on {run} consecutive rows, but the spec says about one output per {rate} "
              f"input(s), so two output beats can never sit on neighbouring rows. The loop blew up: the error e "
              f"was most likely computed on the raw integer codes, K1*e made v huge, W = 1/N + v drove eta below "
              f"zero on every sample and the strobe fired every cycle. Run the interpolator, e, acc, v and eta on "
              f"real values (code / 2^fraction_bits), add assert(abs(v) < 1/N, 'loop filter blew up: v=%g', v) at "
              f"every strobe, and assert(nOut >= nIn/N/2 && nOut <= nIn/N*2, 'wrong beat count') after the loop.")

    expected = in_beats / rate
    if not (expected / 2 <= out_beats <= expected * 2):
        issue("beat_rate_mismatch",
              f"{in_beats} input beats but {out_beats} expected output beat(s); the spec says about one output per "
              f"{rate} input(s), so expect roughly {round(expected)} (anything between {round(expected / 2)} and "
              f"{round(expected * 2)} is accepted). Most likely cause (every timing_recovery miss on 2026-11-06): the "
              f"loop error e was computed on the raw integer codes (values near 1024) instead of the real values "
              f"(code / 2^fraction_bits, near 1.0), so K1*e made v thousands of times too big and eta never crossed "
              f"zero again. Do the whole loop (interpolator, e, acc, v, eta) on real values, convert to codes only at "
              f"the pack line, add assert(abs(v) < 1/N, 'loop filter blew up: v=%g', v) at every strobe, and "
              f"assert(nOut >= nIn/N/2 && nOut <= nIn/N*2, 'wrong beat count') after the loop; also check no NaN "
              f"stops the strobe.")


def validate_test_vectors(spec: dict, run_dir: str, result: dict) -> dict:
    """Returns {"csv": {"inputs": path | None, "outputs": path | None},
    "num_vectors": int, "columns": {"inputs": [...], "outputs": [...]},
    "issues": [{code, detail}]}."""
    issues: list[dict] = []

    def issue(code: str, detail: str) -> None:
        if len(issues) < 25:
            issues.append({"code": code, "detail": detail})

    ports = [p for p in (spec.get("ports") or []) if isinstance(p, dict) and p.get("name")]
    direction = lambda p: str(p.get("direction", "")).lower()   # noqa: E731
    inputs = [p for p in ports if direction(p) == "input"]
    outputs = [p for p in ports if direction(p) == "output"]
    for p in ports:
        if direction(p) not in ("input", "output"):
            issue("port_direction_unknown", f"Port '{p['name']}' has no direction in the spec; "
                                            f"cannot tell which file it belongs to.")

    # The generator records how the LAST MATLAB run went in
    # result["last_matlab_run"] = {"status": "ok" | "error" | "never_ran", "error": "..."}.
    # The key is absent when an older caller (or a test fake) built the result.
    # A script that crashed after an earlier success would leave an old CSV
    # that passes every layout check, so a failed last run refuses the CSVs.
    last_run = result.get("last_matlab_run")
    if last_run and last_run.get("status") != "ok":
        issue("matlab_run_failed", f"The last MATLAB run did not succeed (status {last_run.get('status')}), so "
                                   f"the CSVs on disk are not its output. MATLAB said: {last_run.get('error') or ''}")

    in_name = result.get("input_csv") or INPUT_CSV
    out_name = result.get("output_csv") or OUTPUT_CSV
    in_header, in_rows = _check_file("input", run_dir, in_name, inputs, issue)
    out_header, out_rows = _check_file("output", run_dir, out_name, outputs, issue)

    if in_rows and out_rows:
        if len(in_rows) != len(out_rows):
            issue("row_count_mismatch", f"{in_name} has {len(in_rows)} rows but {out_name} has {len(out_rows)}; "
                                        f"both must describe the same cycles.")
        for r, (a, b) in enumerate(zip(in_rows, out_rows), start=2):
            if a and b and a[0].strip() != b[0].strip():
                issue("cycle_mismatch", f"row {r}: cycle {a[0]} in {in_name} but {b[0]} in {out_name}.")
                break
        _reset_row_checks(spec, in_header, in_rows, out_header, out_rows, issue)
        _backpressure_row_checks(in_header, in_rows, issue)
        _value_sanity_checks(spec, in_header, in_rows, out_header, out_rows, issue)
        saturated = saturated_output_issue(spec, spec_output_rate(spec), in_header, in_rows, out_header, out_rows)
        if saturated:                               # expected outputs pinned at full scale (output_range_check.py)
            issue(saturated["code"], saturated["detail"])

    return {
        "csv": {"inputs": str(Path(run_dir) / in_name) if in_header else None,
                "outputs": str(Path(run_dir) / out_name) if out_header else None},
        "num_vectors": min(len(in_rows), len(out_rows)) if in_rows and out_rows else 0,
        "columns": {"inputs": in_header, "outputs": out_header},
        "issues": issues,
    }

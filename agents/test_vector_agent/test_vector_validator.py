# agents/test_vector_agent/test_vector_validator.py — deterministic checks
# on the two CSVs the test-vector agent produced: both exist, each has the
# expected header (cycle, then that direction's spec ports in spec order),
# they have the same rows with the same cycle numbers, and every value is
# an integer that fits its port width. The files are read through the
# file-server MCP. The testbench replays these files without seeing them,
# so the layout is enforced exactly.

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from agents.mcp_utils import mcp_read_file

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

    return {
        "csv": {"inputs": str(Path(run_dir) / in_name) if in_header else None,
                "outputs": str(Path(run_dir) / out_name) if out_header else None},
        "num_vectors": min(len(in_rows), len(out_rows)) if in_rows and out_rows else 0,
        "columns": {"inputs": in_header, "outputs": out_header},
        "issues": issues,
    }

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
    content = mcp_read_file(run_dir, name)
    if content is None:
        issue("csv_missing", f"{name} was not created in {run_dir}.")
        return [], []
    rows = list(csv.reader(io.StringIO(content)))
    if not rows:
        issue("csv_empty", f"{name} is empty.")
        return [], []
    header, data = [h.strip() for h in rows[0]], rows[1:]

    if not header or header[0] != "cycle":
        issue("no_cycle_column", f"{name}: first column must be 'cycle'.")
    for p in ports:
        if p["name"] not in header:
            issue("port_column_missing", f"{name}: no column for {label} port '{p['name']}'.")
    expected = ["cycle"] + [p["name"] for p in ports]
    if ports and set(expected) <= set(header) and header != expected:
        issue("column_order", f"{name}: columns must be exactly {expected} (cycle, then the {label} ports "
                              f"in spec order); got {header}.")
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

    return {
        "csv": {"inputs": str(Path(run_dir) / in_name) if in_header else None,
                "outputs": str(Path(run_dir) / out_name) if out_header else None},
        "num_vectors": min(len(in_rows), len(out_rows)) if in_rows and out_rows else 0,
        "columns": {"inputs": in_header, "outputs": out_header},
        "issues": issues,
    }

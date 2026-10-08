# agents/test_vector_agent/test_vector_validator.py — deterministic checks
# on the CSV the test-vector agent produced: it exists, has the expected
# header (cycle + every spec port), has rows, and every value is an integer
# that fits its port width. The CSV is read through the file-server MCP.

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from agents.mcp_utils import mcp_read_file

_PLAIN_WIDTH_RE = re.compile(r"^\d+$")


def _port_bits(port: dict) -> int | None:
    width = str(port.get("width") or "").strip()
    if _PLAIN_WIDTH_RE.match(width):
        return int(width)
    m = re.match(r"^\[(\d+):(\d+)\]$", width)
    if m:
        return abs(int(m.group(1)) - int(m.group(2))) + 1
    return None


def validate_test_vectors(spec: dict, run_dir: str, result: dict) -> dict:
    """Returns {"csv": path | None, "num_vectors": int, "columns": [...],
    "issues": [{code, detail}]}."""
    issues: list[dict] = []

    def issue(code: str, detail: str) -> None:
        issues.append({"code": code, "detail": detail})

    csv_name = result.get("csv_file") or "test_vectors.csv"
    csv_path = Path(run_dir) / csv_name          # reported to the UI; the read itself goes through the MCP
    content = mcp_read_file(run_dir, csv_name)
    if content is None:
        issue("csv_missing", f"{csv_name} was not created in {run_dir}.")
        return {"csv": None, "num_vectors": 0, "columns": [], "issues": issues}

    rows = list(csv.reader(io.StringIO(content)))
    if not rows:
        issue("csv_empty", f"{csv_name} is empty.")
        return {"csv": str(csv_path), "num_vectors": 0, "columns": [], "issues": issues}

    header = [h.strip() for h in rows[0]]
    data = rows[1:]

    if not header or header[0] != "cycle":
        issue("no_cycle_column", "First column must be 'cycle'.")

    ports = [p for p in (spec.get("ports") or []) if isinstance(p, dict) and p.get("name")]
    for p in ports:
        if p["name"] not in header:
            issue("port_column_missing", f"No column for port '{p['name']}'.")

    if not data:
        issue("no_rows", "Header only — no test vectors.")

    bits = {p["name"]: _port_bits(p) for p in ports}
    bad_cells = 0
    for r, row in enumerate(data, start=2):
        if len(row) != len(header):
            issue("ragged_row", f"Row {r} has {len(row)} cells, header has {len(header)}.")
            break
        for name, cell in zip(header, row):
            try:
                value = int(float(cell))
            except ValueError:
                bad_cells += 1
                if bad_cells <= 3:
                    issue("non_integer", f"Row {r}, column '{name}': '{cell}' is not an integer.")
                continue
            n = bits.get(name)
            if n is not None and not (0 <= value < 2**n):
                issue("out_of_range", f"Row {r}, column '{name}': {value} does not fit {n} bit(s).")
                if len(issues) > 20:
                    return {"csv": str(csv_path), "num_vectors": len(data), "columns": header, "issues": issues}

    return {"csv": str(csv_path), "num_vectors": len(data), "columns": header, "issues": issues}

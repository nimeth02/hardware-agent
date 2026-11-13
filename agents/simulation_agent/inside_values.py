# agents/simulation_agent/inside_values.py — what the chip held INSIDE on the
# failing beats, next to what the golden model held.
#
# Until 2026-11-10 the RTL worker only ever heard "beat 64: expected I=-1792
# got I=651": the output pins. Which of its 400 lines was wrong it had to
# guess, and it guessed wrong for 33 timing_recovery runs in a row. The
# simulator already records every signal of the design (xsim_run.tcl:
# `log_vcd /*/dut/*` -> work_proj/waves.vcd), and the golden model can write
# its loop values per beat (loop_trace.csv, CLOSED-LOOP TRACE rule). This
# module reads both for the beats around the first failure and puts them side
# by side: the chip's mu / e / v / acc / eta registers and the model's.
# Everything here is code; no LLM arithmetic.

from __future__ import annotations

import csv
import io
import re

LOOP_TRACE_CSV = "loop_trace.csv"
WAVES_PATH = "work_proj/waves.vcd"
MAX_BEATS = 8                      # rows shown per side
CONTEXT_BEATS = 2                  # beats before the first failing one

# leaf signal name (lower case) -> family. The LAST pipeline row of a family
# (highest _sN, else _r) is the one shown: it is the value that reached the
# loop / the output.
_FAMILIES: tuple[tuple[str, re.Pattern], ...] = (
    ("strobe", re.compile(r"^strobe(_s\d+|_r)?$")),
    ("mu", re.compile(r"^mu(_s\d+|_r)?$")),
    ("e", re.compile(r"^e(_s\d+|_r|_w)?$")),
    ("v", re.compile(r"^v(_s\d+|_r)?$")),
    ("acc", re.compile(r"^acc(_s\d+|_r)?$")),
    ("eta", re.compile(r"^eta(_s\d+|_r)?$")),
    ("y_sym_i", re.compile(r"^y_?sym_i(_s\d+|_r)?$")),
    ("y_sym_q", re.compile(r"^y_?sym_q(_s\d+|_r)?$")),
    ("y_mid_i", re.compile(r"^y_?mid_i(_s\d+|_r)?$")),
    ("y_mid_q", re.compile(r"^y_?mid_q(_s\d+|_r)?$")),
    ("y_prev_i", re.compile(r"^y_?prev_i(_s\d+|_r)?$")),
    ("y_prev_q", re.compile(r"^y_?prev_q(_s\d+|_r)?$")),
)
_PORTS = ("aclk", "m_axis_tvalid", "m_axis_tuser", "m_axis_tdata")
_STAGE_RE = re.compile(r"_s(\d+)$")
_VAR_RE = re.compile(r"^\$var\s+\S+\s+(\d+)\s+(\S+)\s+(\S+)")
_SCOPE_RE = re.compile(r"^\$scope\s+\S+\s+(\S+)")
_TIMESCALE_RE = re.compile(r"\$timescale\s+(\d+)\s*(fs|ps|ns|us|ms|s)")
_UNITS_NS = {"fs": 1e-6, "ps": 1e-3, "ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9}


def _rank(name: str) -> tuple[int, int]:
    """Later pipeline row first: (_sN -> N, else _r -> 0), then the register."""
    m = _STAGE_RE.search(name)
    return (int(m.group(1)) if m else -1, 1 if name.endswith("_r") else 0)


def _to_int(bits: str, signed: bool, width: int | None = None) -> int | str:
    """A VCD vector value as an integer; "U" when any bit is x/u/z. width is
    the signal's declared width: VCD writers (xsim included) drop the leading
    zeros of a vector value (IEEE 1364 18.2.1: `b1010001011` for a 12-bit
    register holding 651), so the written string is zero-extended to the
    declared width before the sign bit is read. Without this every positive
    value of a signed register came out negative (651 -> -373; found by the
    2026-11-10 review)."""
    bits = bits.lower()
    if not bits or any(c not in "01" for c in bits):
        return "U"
    width = max(width or 0, len(bits))
    value = int(bits, 2)
    if signed and value >= 1 << (width - 1):
        value -= 1 << width
    return value


def _parse_header(lines: list[str]) -> tuple[dict, dict, float, int]:
    """(code -> (leaf name, width, scope depth), dut scope's leaf name -> code,
    ns per time unit, index of the first line after the header)."""
    codes: dict[str, tuple[str, int, int]] = {}
    depth, dut_depth, dut_names = 0, None, {}
    scope_vars: dict[int, dict[str, str]] = {}
    ns = 1e-3
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("$timescale"):
            text = line
            while "$end" not in text and i + 1 < len(lines):
                i += 1
                text += " " + lines[i].strip()
            m = _TIMESCALE_RE.search(text)
            if m:
                ns = int(m.group(1)) * _UNITS_NS[m.group(2)]
        elif _SCOPE_RE.match(line):
            depth += 1
            scope_vars[depth] = {}
        elif line.startswith("$upscope"):
            if dut_depth is None and all(p in scope_vars.get(depth, {}) for p in _PORTS):
                dut_depth, dut_names = depth, dict(scope_vars[depth])
            depth -= 1
        elif line.startswith("$var"):
            m = _VAR_RE.match(line)
            if m:
                width, code, name = int(m.group(1)), m.group(2), m.group(3).lower()
                codes[code] = (name, width, depth)
                scope_vars.setdefault(depth, {})[name] = code
        elif line.startswith("$enddefinitions"):
            i += 1
            break
        i += 1
    return codes, dut_names, ns, i


def _pick_signals(dut_names: dict[str, str]) -> dict[str, str]:
    """family -> leaf name of the design's register shown for it."""
    chosen: dict[str, str] = {}
    for family, pattern in _FAMILIES:
        candidates = [n for n in dut_names if pattern.match(n)]
        if candidates:
            chosen[family] = max(candidates, key=_rank)
    return chosen


def chip_inside_values(vcd_text: str, beats: list[int]) -> dict | None:
    """The design's loop registers just before the clock edge of each wanted
    output beat (the state that produced that beat), read from the VCD the
    simulator wrote. {"signals": {family: register name}, "rows": [{"beat",
    "time_ns", "m_axis_tuser", "m_axis_tdata", <family>: int | "U"}]}, or None
    when the file is not a VCD of the design."""
    if not isinstance(vcd_text, str) or "$enddefinitions" not in vcd_text:
        return None
    lines = vcd_text.splitlines()
    codes, dut_names, ns, start = _parse_header(lines)
    if not dut_names:
        return None
    chosen = _pick_signals(dut_names)
    watch = {dut_names[p]: p for p in _PORTS} | {dut_names[n]: f for f, n in chosen.items()}
    wanted = set(beats)
    cur: dict[str, str] = {c: "" for c in watch}
    rows: list[dict] = []
    clk, tvalid = dut_names["aclk"], dut_names["m_axis_tvalid"]
    beat, time, group = 0, 0, []

    def close_group() -> None:
        nonlocal beat
        before = dict(cur)
        rose = False
        for code, value in group:
            if code == clk and before.get(clk) in ("0", "") and value == "1":
                rose = True
            cur[code] = value
        if not rose:
            return
        if before.get(tvalid) == "1":
            if beat in wanted:
                row: dict = {"beat": beat, "time_ns": round(time * ns, 3)}
                for code, label in watch.items():
                    if label == "aclk" or label == "m_axis_tvalid":
                        continue
                    _, width, _ = codes[code]
                    row[label] = _to_int(before.get(code, ""),
                                         signed=width > 1 and label not in ("m_axis_tuser", "m_axis_tdata"),
                                         width=width)
                rows.append(row)
            beat += 1

    for line in lines[start:]:
        line = line.strip()
        if not line or line.startswith("$"):
            continue
        if line[0] == "#":
            close_group()
            group = []
            time = int(line[1:] or 0)
            continue
        if line[0] in "01xXzZuU":
            code, value = line[1:], line[0]
        elif line[0] in "bB":
            parts = line[1:].split()
            if len(parts) != 2:
                continue
            value, code = parts
        else:
            continue
        if code in watch:
            group.append((code, value))
    close_group()
    widths = {f: codes[dut_names[n]][1] for f, n in chosen.items()}
    return {"signals": {f: f"{n} ({widths[f]} bits)" for f, n in chosen.items()}, "rows": rows, "beats_seen": beat}


def model_inside_values(loop_trace_csv: str | None, beats: list[int]) -> list[dict]:
    """The golden model's rows of loop_trace.csv for the wanted beats
    (columns as written: beat, cycle, mu8, e, v, acc, y_sym_i, ...)."""
    if not isinstance(loop_trace_csv, str) or not loop_trace_csv.strip():
        return []
    wanted = {str(b) for b in beats}
    rows = []
    for r in csv.DictReader(io.StringIO(loop_trace_csv)):
        clean = {str(k).strip(): str(v).strip() for k, v in r.items() if k is not None}
        if clean.get("beat") in wanted:
            rows.append(clean)
    return rows


def beats_to_show(failing_beats: list[dict] | None) -> list[int]:
    """Beats 0..2 (the loop's first steps) and the two beats before the first
    failing one up to it, at most MAX_BEATS, in order."""
    failing = sorted(b["beat"] for b in failing_beats or [] if isinstance(b.get("beat"), int))
    chosen = {0, 1, 2}
    if failing:
        k = failing[0]
        chosen |= {max(0, k - CONTEXT_BEATS + j) for j in range(CONTEXT_BEATS + 1)}
        chosen |= set(failing[1:3])
    return sorted(chosen)[:MAX_BEATS]


def inside_values(vcd_text: str | None, loop_trace_csv: str | None, failing_beats: list[dict] | None) -> dict | None:
    """Side by side, for the beats around the first failure: the chip's loop
    registers (from the VCD) and the golden model's loop values (from
    loop_trace.csv). None when neither source is there."""
    beats = beats_to_show(failing_beats)
    chip = chip_inside_values(vcd_text, beats) if vcd_text else None
    model = model_inside_values(loop_trace_csv, beats)
    if not chip and not model:
        return None
    out: dict = {"beats": beats,
                 "how_to_read": ("'chip' rows are the design's registers just before the clock edge that delivered "
                                 "that output beat, as raw integers read as signed (a register declared unsigned that "
                                 "shows negative: add 2**width): divide by 2**(fraction bits) with the design's own "
                                 "constants (eta / 2**ETA_F, mu / 2**MU_F, e by the product's fraction bits) to compare "
                                 "with the 'model' rows, which are real values (mu8 = floor(mu*256), y_* are output "
                                 "codes). Find the FIRST beat where a chip value parts from the model's and fix the "
                                 "formula that produces that register; everything after it only follows.")}
    if chip:
        out["chip"] = chip
    if model:
        out["model"] = model
    first = _first_divergence(chip, model)
    if first:
        out["first_difference"] = first
    return out


MU_DIFFERENCE = 8                  # of 256: half the testbench's TOL_MU


def _first_divergence(chip: dict | None, model: list[dict]) -> str | None:
    """The first shown beat where the chip's output mu (m_axis_tuser) is more
    than MU_DIFFERENCE/256 (round the wrap) from the model's mu8, in words."""
    if not chip or not model:
        return None
    by_beat = {str(r.get("beat")): r for r in model}
    for row in chip["rows"]:
        m = by_beat.get(str(row["beat"]))
        if not m or not str(m.get("mu8", "")).lstrip("-").isdigit() or not isinstance(row.get("m_axis_tuser"), int):
            continue
        d = abs(row["m_axis_tuser"] - int(m["mu8"]))
        d = min(d, 256 - d)
        if d > MU_DIFFERENCE:
            return (f"beat {row['beat']}: the chip's mu (m_axis_tuser = {row['m_axis_tuser']}) is {d}/256 from the "
                    f"model's ({m['mu8']}); the chip's loop registers on that beat: "
                    + ", ".join(f"{f}={row.get(f)}" for f in chip["signals"] if f in row))
    return None

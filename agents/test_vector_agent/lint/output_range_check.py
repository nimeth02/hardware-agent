# agents/test_vector_agent/lint/output_range_check.py — one check on the finished
# CSVs: the expected outputs of a closed-loop / decimating design must not sit
# at the largest or smallest code of their field on most beats.
#
# Why it exists: timing_recovery, 2026-11-11 17:15. The RTL was the proven
# worked example (the same file passed simulation and implementation at
# 15:49). The MATLAB model of this run pushed the raw integer codes (up to
# 1536) into the sample history instead of code / 2048, so y_sym * 2048
# saturated: 113 of the 128 compared beats expected I = 2047 or -2048 (the
# right value is about 1792), and mu never settled. Every layout and shape
# check passed (192 beats for 800 inputs is a normal count), the simulation
# reported 245 mismatches and the analyst blamed the RTL.
#
# One user, one line: test_vector_validator.validate_test_vectors.
# The bit positions of the fields come from the spec's own text; when the spec
# does not state them the check is skipped (it never guesses a layout).

from __future__ import annotations

import re

OUT_PORT, IN_PORT = "m_axis_tdata", "s_axis_tdata"
MIN_BEATS = 8              # tiny hand-written vector sets are never judged
MIN_FIELD_BITS = 4         # a 1..3-bit field is at "its largest code" all the time
RAIL_SHARE = 0.5           # half of the expected beats at the rail = the model saturated
INPUT_RAIL_SHARE = 0.1     # a stimulus that itself sits at the rail may legitimately saturate the output

_BRACKET_RE = re.compile(r"\b(\w+)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*=\s*([A-Za-z_]\w*)")        # port[23:12] = I
_BITS_RE = re.compile(r"\b([A-Za-z_]\w*)\s*=\s*bits?\s*\[?\s*(\d+)\s*:\s*(\d+)", re.IGNORECASE)   # I = bits 23:12
_BRACE_RE = re.compile(r"\{\s*([A-Za-z_]\w*(?:\s*\[\s*\d+\s*:\s*\d+\s*\])?(?:\s*,\s*[A-Za-z_]\w*(?:\s*\[\s*\d+\s*:\s*\d+\s*\])?)+)\s*\}")
_BRACE_ITEM_RE = re.compile(r"([A-Za-z_]\w*)(?:\s*\[\s*(\d+)\s*:\s*(\d+)\s*\])?")


def _spec_lines(spec: dict | None) -> list[str]:
    lines: list[str] = []
    for key in ("description", "behavior", "constraints", "assumptions"):
        value = (spec or {}).get(key)
        lines += [str(v) for v in value] if isinstance(value, list) else [str(value or "")]
    return lines


def _port_width(spec: dict | None, port: str) -> int | None:
    for p in (spec or {}).get("ports") or []:
        if isinstance(p, dict) and str(p.get("name", "")).lower() == port and str(p.get("width", "")).strip().isdigit():
            return int(str(p["width"]).strip())
    return None


def output_fields(spec: dict | None, port: str) -> list[tuple[str, int, int]]:
    """[(field name, high bit, low bit)] of `port`, read from the spec's text in
    the three ways specs have written it: `port[23:12] = I`, `I = bits 23:12`
    and `{I[11:0], Q[11:0]}` (first value in the top bits), the last two only
    on a line that names this port and no other tdata port. [] when the spec
    does not say, or says something that does not fit the port's width."""
    width = _port_width(spec, port)
    if width is None:
        return []
    fields: list[tuple[str, int, int]] = []
    for line in _spec_lines(spec):
        low = line.lower()
        fields += [(m.group(4), int(m.group(2)), int(m.group(3))) for m in _BRACKET_RE.finditer(line)
                   if m.group(1).lower() == port]
        if fields or port not in low or any(other in low for other in (OUT_PORT, IN_PORT) if other != port):
            continue
        fields += [(m.group(1), int(m.group(2)), int(m.group(3))) for m in _BITS_RE.finditer(line)]
        brace = _BRACE_RE.search(line)
        if not fields and brace:
            items = [_BRACE_ITEM_RE.fullmatch(i.strip()) for i in brace.group(1).split(",")]
            widths = [int(i.group(2)) - int(i.group(3)) + 1 if i.group(2) else 0 for i in items]
            if not any(widths) and width % len(items) == 0:
                widths = [width // len(items)] * len(items)             # {I, Q}: equal shares
            if min(widths) >= 1 and sum(widths) == width:
                high = width - 1
                for item, w in zip(items, widths):
                    fields.append((item.group(1), high, high - w + 1))
                    high -= w
        if fields:
            break
    ok = all(width > hi >= lo >= 0 for _, hi, lo in fields)
    return fields if ok else []


def _beats(header: list[str], rows: list[list[str]], valid: str, data: str) -> list[int]:
    """The data word of every row whose valid column is 1, as an integer."""
    if valid not in header or data not in header:
        return []
    v, d = header.index(valid), header.index(data)
    words = []
    for row in rows:
        if len(row) > max(v, d) and row[v].strip() == "1":
            try:
                words.append(int(float(row[d].strip())))
            except ValueError:
                return []                                   # not plain numbers: another check reports that
    return words


def _signed(word: int, hi: int, lo: int) -> int:
    bits = hi - lo + 1
    value = (word >> lo) & ((1 << bits) - 1)
    return value - (1 << bits) if value >= 1 << (bits - 1) else value


def _at_rail(words: list[int], hi: int, lo: int) -> int:
    bits = hi - lo + 1
    top, bottom = (1 << (bits - 1)) - 1, -(1 << (bits - 1))
    return sum(1 for w in words if _signed(w, hi, lo) in (top, bottom))


def saturated_output_issue(spec: dict, rate: int | None, in_header: list[str], in_rows: list[list[str]],
                           out_header: list[str], out_rows: list[list[str]]) -> dict | None:
    """{code: "saturated_output", detail} when a field of the expected
    m_axis_tdata is at its largest or smallest code on half of the beats or
    more while the input stays inside its range; None otherwise."""
    if rate is None:
        return None                                         # no "one output per N inputs" in the spec: a demapper or an adder is not judged
    out_words = _beats(out_header, out_rows, "m_axis_tvalid", OUT_PORT)
    if len(out_words) < MIN_BEATS:
        return None
    in_words = _beats(in_header, in_rows, "s_axis_tvalid", IN_PORT)
    in_fields = [f for f in output_fields(spec, IN_PORT) if f[1] - f[2] + 1 >= MIN_FIELD_BITS]
    if in_words and any(_at_rail(in_words, hi, lo) >= INPUT_RAIL_SHARE * len(in_words) for _, hi, lo in in_fields):
        return None                                         # a full-scale stimulus: saturation may be the right answer
    for name, hi, lo in output_fields(spec, OUT_PORT):
        bits = hi - lo + 1
        hits = _at_rail(out_words, hi, lo)
        if bits < MIN_FIELD_BITS or hits < RAIL_SHARE * len(out_words):
            continue
        top = (1 << (bits - 1)) - 1
        peak = max((abs(_signed(w, h, l)) for w in in_words for _, h, l in in_fields), default=None)
        seen = f" while no input value goes beyond {peak}" if peak is not None else ""
        return {"code": "saturated_output",
                "detail": f"{name} ({OUT_PORT}[{hi}:{lo}]) is at its largest or smallest code ({top} or {-top - 1}) on "
                          f"{hits} of {len(out_words)} expected output beats{seen}. An interpolator does not turn "
                          f"inputs inside the range into full-scale outputs, so the golden model ran on the raw "
                          f"integer codes: the samples went into the history as codes (xb = [xb(2:end), Icode(n)]) and "
                          f"y * 2^F then saturated on every beat. Convert each sample to a real value FIRST "
                          f"(x = Icode(n) / 2^F, F = the field's fraction bits, 2048 for Q1.11), run the history, the "
                          f"interpolator, e, acc, v and eta on those real values, and multiply by 2^F only on the "
                          f"pack line. Then look at the printed CSV: the expected values must stay close to the "
                          f"input's peak, and mu must settle instead of stepping by the same amount on every beat."}
    return None

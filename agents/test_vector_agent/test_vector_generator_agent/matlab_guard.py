# agents/test_vector_agent/test_vector_generator_agent/matlab_guard.py — the guard around the two
# MATLAB MCP tools (generate_matlab_script, run_matlab_script) before the
# LLM gets them. Only MATLAB rules live here; the agent-level safety rules
# (when to give up across attempts) are in test_vector_guard.py.
#
# Rules:
#
#   * scripts using integer-class MATLAB functions, or operators MATLAB does
#     not have (`>>>`, `!=`, `+=` ...), are rejected before they are written
#     (every observed failed run came from these);
#   * a script that crashed is copied to <name>.failedN.m before the next
#     attempt overwrites it;
#   * a script that never uses the start bit (2^lo) of a spec field
#     `port[hi:lo] = name` is rejected: it packs the field in the wrong place;
#   * run_matlab_script only runs a script the LLM wrote in this attempt;
#   * an unchanged script is not re-run after it failed;
#   * old CSVs are deleted before every run so a half-failed run cannot
#     leave the previous run's files behind;
#   * failed runs are hard-capped (RunAttemptsExceeded).
#
# Nothing here talks to MATLAB itself; it only wraps the tools it is given.

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool

from agents.test_vector_agent.test_vector_generator_agent.state import (
    MAX_RUN_FAILURES,
    RunAttemptsExceeded,
    RunState,
    new_run_state,
)

FORBIDDEN_MATLAB = ("int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64",
                    "typecast", "bitshift", "bitsll", "bitsra", "bitsrl", "idivide", "intmax", "intmin")
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_MATLAB) + r")\s*\(")


# Symbols copied from a spec or from C/Verilog that MATLAB does not have.
# `(p + 4096) >>> 13` in a spec becomes `floor((p + 4096) / 2^13)`; written
# as-is MATLAB stops with "Invalid use of operator" (observed: every first
# attempt of the llr_demap16 runs on 2026-10-29).
FORBIDDEN_OPERATORS = (">>>", "<<<", ">>", "<<", "!=", "+=", "-=", "*=", "/=", "++")
_OPERATOR_HINT = {
    ">>>": "floor(x / 2^k)  (arithmetic shift right k)",
    ">>": "floor(x / 2^k)  (shift right k)",
    "<<<": "mod(x * 2^k, 2^N)  (shift left k)",
    "<<": "mod(x * 2^k, 2^N)  (shift left k)",
    "!=": "~=",
    "+=": "x = x + v",
    "-=": "x = x - v",
    "*=": "x = x * v",
    "/=": "x = x / v",
    "++": "x = x + 1",
}


def _code_only(code: str) -> str:
    """The script with comments and quoted strings blanked out (what MATLAB
    parses). Walks each line once: a `%` outside a string starts a comment;
    `"` or a `'` that is not a transpose starts a string ('' inside is a
    quote). So `fprintf('rows >> %d\\n', n)` hides neither `>>` nor `%d`."""
    out_lines: list[str] = []
    for line in code.splitlines():
        out: list[str] = []
        i, n, quote = 0, len(line), ""
        while i < n:
            ch = line[i]
            if quote:
                if ch == quote:
                    if ch == "'" and i + 1 < n and line[i + 1] == "'":
                        i += 2                                   # '' = one quote inside the string
                        continue
                    quote = ""
                    out.append('"')
                i += 1
                continue
            if ch == "%":
                break                                            # comment: rest of line
            if ch == '"' or (ch == "'" and not _is_transpose(out)):
                quote = ch
                out.append('"')
                i += 1
                continue
            out.append(ch)
            i += 1
        out_lines.append("".join(out))
    return "\n".join(out_lines)


def _is_transpose(before: list[str]) -> bool:
    """A `'` right after a name, number, `)`, `]`, `.` or `'` is MATLAB's
    transpose, not a string start."""
    if not before:
        return False
    prev = before[-1]
    return prev.isalnum() or prev in ")]'._"


def forbidden_matlab(code: str) -> list[str]:
    """Integer-class MATLAB calls used in the script (comments ignored)."""
    return sorted({m.group(1) for m in _FORBIDDEN_RE.finditer(_code_only(code))})


def forbidden_operators(code: str) -> list[str]:
    """Operators MATLAB does not have, as used in the script (comments and
    strings ignored). Longer symbols win: `>>>` is reported once, not as `>>`."""
    text = _code_only(code)
    found: list[str] = []
    for op in sorted(FORBIDDEN_OPERATORS, key=len, reverse=True):
        if op in text:
            found.append(op)
            text = text.replace(op, " ")
    return sorted(found)


# "s_axis_tdata[31:20] = I" in the spec: field I starts at bit 20, so the
# script must pack it with 2^20. Run 170022 on 2026-10-29 packed it with
# 2^12 (on top of Q), the CSVs were self-consistent nonsense, and the
# simulation reported every beat wrong although the RTL was right.
_FIELD_RE = re.compile(r"\b(\w+)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*=\s*([A-Za-z_]\w*)")


def spec_fields(spec: dict | None) -> list[tuple[str, int, int, str]]:
    """(port, hi, lo, field name) for every `port[hi:lo] = name` in the spec's
    text, limited to ports the spec declares."""
    spec = spec or {}
    names = {str(p.get("name", "")).lower() for p in (spec.get("ports") or []) if isinstance(p, dict)}
    text = " ".join(str(x) for key in ("description", "behavior", "constraints", "assumptions")
                    for x in (spec.get(key) if isinstance(spec.get(key), list) else [spec.get(key) or ""]))
    found = []
    for m in _FIELD_RE.finditer(text):
        if m.group(1).lower() in names:
            found.append((m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)))
    return found


def missing_field_positions(spec: dict | None, code: str) -> list[str]:
    """Spec fields whose start bit (lo > 0) never appears in the script as
    `2^lo`: the only way the NUMBER TYPES rule packs or extracts a field."""
    text = _code_only(code)
    missing = []
    for port, hi, lo, name in spec_fields(spec):
        if lo > 0 and not re.search(r"2\s*\^\s*\(?\s*%d\s*\)?(?!\d)" % lo, text):
            missing.append(f"{port}[{hi}:{lo}] = {name} starts at bit {lo}: pack it as <{name} pattern> * 2^{lo}, "
                           f"extract it as mod(floor(word / 2^{lo}), 2^{hi - lo + 1})")
    return missing


def _input_fields(spec: dict | None) -> list[tuple[str, int, int, str]]:
    """spec_fields limited to INPUT ports: the stimulus the script invents."""
    spec = spec or {}
    inputs = {str(p.get("name", "")).lower() for p in (spec.get("ports") or [])
              if isinstance(p, dict) and str(p.get("direction", "")).lower() == "input"}
    return [f for f in spec_fields(spec) if f[0].lower() in inputs]


def missing_field_range_asserts(spec: dict | None, code: str) -> list[str]:
    """Input fields of the spec with no `assert(...)` line in the script that
    names the field together with its width bound. `mod(I, 4096)` wraps an
    out-of-range value silently: on 2026-10-29 the stimulus used I = +2048
    in a 12-bit signed field, it went on the wire as -2048, and the
    expected outputs belonged to a symbol the RTL never saw (two beats
    reported wrong while the RTL was right). An assert before packing
    makes MATLAB stop on the script instead (NUMBER TYPES rule)."""
    asserts = [line for line in _code_only(code).splitlines() if re.search(r"\bassert\s*\(", line, re.IGNORECASE)]
    missing = []
    for port, hi, lo, name in _input_fields(spec):
        n = hi - lo + 1
        bounds = [rf"2\s*\^\s*\(?\s*{n - 1}\s*\)?(?!\d)", rf"2\s*\^\s*\(?\s*{n}\s*\)?(?!\d)",
                  rf"(?<![\d.]){2 ** (n - 1)}(?![\d.])", rf"(?<![\d.]){2 ** (n - 1) - 1}(?![\d.])",
                  rf"(?<![\d.]){2 ** n - 1}(?![\d.])", rf"(?<![\d.]){2 ** n}(?![\d.])"]
        name_re = rf"(?<![\w.]){re.escape(name)}(?![\w(])"
        if not any(re.search(name_re, a) and any(re.search(b, a) for b in bounds) for a in asserts):
            missing.append(f"{port}[{hi}:{lo}] = {name} ({n} bits): add `assert(all({name} >= -2^{n - 1} & {name} <= "
                           f"2^{n - 1}-1), '{name} does not fit {n} bits');` (or 0 .. 2^{n}-1 if unsigned) before "
                           f"the pack line, and draw {name} inside that range")
    return missing


def keep_failed_script(project_dir: str, stem: str, failure_no: int) -> None:
    """Copy <stem>.m to <stem>.failed<N>.m so the script that crashed can still
    be read after the LLM overwrites it with the next attempt."""
    src = Path(str(project_dir or "")) / f"{stem}.m"
    if src.is_file():
        src.with_name(f"{stem}.failed{failure_no}.m").write_bytes(src.read_bytes())


def clear_old_csvs(project_dir: str) -> None:
    """Delete every CSV in the run folder before a MATLAB run. A run that
    errors out part-way (observed: run 161047, header changed, script
    failed at line 163) would otherwise leave the previous run's CSVs on
    disk, and the validator cannot tell them from fresh ones."""
    folder = Path(str(project_dir or ""))
    if not folder.is_dir():
        return
    for f in folder.glob("*.csv"):
        f.unlink(missing_ok=True)


def guard_matlab_tools(tools: list[BaseTool], max_failures: int = MAX_RUN_FAILURES,
                       state: RunState | None = None, spec: dict | None = None) -> list[BaseTool]:
    """Wrap generate/run so the loop can't burn steps re-running an unchanged
    script (observed with local models), and so failed runs are hard-capped."""
    by_name = {t.name: t for t in tools}
    gen_tool = by_name["generate_matlab_script"]
    run_tool = by_name["run_matlab_script"]
    if state is None:
        state = new_run_state()

    def _stem(name) -> str:
        return str(name or "").strip().removesuffix(".m")

    async def generate(**kwargs):
        # MATLAB refuses to mix integer classes; every observed failed run
        # came from these. Reject the script before it is written (not a
        # counted failure) so the LLM rewrites it in double arithmetic.
        bad = forbidden_matlab(str(kwargs.get("code") or ""))
        if bad:
            return ("REJECTED: the script uses " + ", ".join(bad) + " — integer-class functions are not "
                    "allowed (they cause 'Integers can only be combined with integers of the same class'). "
                    "Do all arithmetic in double: mod(x, 2^N) to wrap, floor(x / 2^k) to shift right, "
                    "x * 2^k to shift left, hi * 2^lo_bits + lo to pack (NUMBER TYPES rule). Rewrite and call "
                    "generate_matlab_script again.")
        ops = forbidden_operators(str(kwargs.get("code") or ""))
        if ops:
            fixes = "; ".join(f"`{op}` -> {_OPERATOR_HINT[op]}" for op in ops)
            return ("REJECTED: the script uses " + ", ".join(f"`{op}`" for op in ops) + " — MATLAB has no "
                    "such operator (it stops with 'Invalid use of operator'). The spec's shift and "
                    "C-style symbols must be rewritten in plain double arithmetic: " + fixes +
                    " (NUMBER TYPES rule). Rewrite and call generate_matlab_script again.")
        missing = missing_field_positions(spec, str(kwargs.get("code") or ""))
        if missing:
            return ("REJECTED: the script never uses the start bit of these spec fields, so it packs them "
                    "in the wrong place (every expected output would then be wrong): " + "; ".join(missing) +
                    ". Write the [hi:lo] from the spec in a comment next to each term and use 2^lo. "
                    "Rewrite and call generate_matlab_script again.")
        unchecked = missing_field_range_asserts(spec, str(kwargs.get("code") or ""))
        if unchecked:
            return ("REJECTED: the script never checks that these input fields fit their bit width before "
                    "packing, so an out-of-range value (e.g. +2048 in 12 signed bits) wraps silently and the "
                    "expected outputs belong to a symbol the design never receives: " + "; ".join(unchecked) +
                    " (NUMBER TYPES rule). Rewrite and call generate_matlab_script again.")
        state["dirty"] = True
        state["scripts"].add(_stem(kwargs.get("filename")))
        return await gen_tool.ainvoke(kwargs)

    async def run(**kwargs):
        # Running anything but a script the LLM wrote (observed: `dir` to look
        # for a reference file) is a misuse, not a failed golden model: reject
        # it with a hint and do not count it against the failure cap.
        if _stem(kwargs.get("script_name")) not in state["scripts"]:
            return ("REJECTED: run_matlab_script only runs a script you wrote with "
                    "generate_matlab_script (e.g. gen_test_vectors). It cannot list folders "
                    "or run built-ins; the spec is your only input, there is no reference file.")
        if state["failures"] and not state["dirty"]:
            return ("REJECTED: the script has not changed since its last failed run. "
                    "Fix the error and call generate_matlab_script first.")
        clear_old_csvs(kwargs.get("project_dir"))
        out = await run_tool.ainvoke(kwargs)
        state["dirty"] = False
        if '"status": "error"' in str(out):
            state["failures"] += 1
            keep_failed_script(kwargs.get("project_dir"), _stem(kwargs.get("script_name")), state["failures"])
            state["last_run"] = {"status": "error", "error": str(out)[:1500]}
            if state["failures"] >= max_failures:
                raise RunAttemptsExceeded(state["last_run"]["error"])
        else:
            state["last_run"] = {"status": "ok", "error": ""}
        return out

    wrapped = {
        gen_tool.name: StructuredTool.from_function(
            coroutine=generate, name=gen_tool.name, description=gen_tool.description, args_schema=gen_tool.args_schema),
        run_tool.name: StructuredTool.from_function(
            coroutine=run, name=run_tool.name, description=run_tool.description, args_schema=run_tool.args_schema),
    }
    return [wrapped.get(t.name, t) for t in tools]

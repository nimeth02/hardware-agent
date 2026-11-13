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
#   * a script that wraps a signed product/difference with mod() inside
#     floor/min/max is rejected: the sign is gone before the shift or the
#     saturation, so every negative result turns positive;
#   * a script for a streaming spec that states an output rate ("one output
#     per 4 inputs") but never asserts its own output beat count is rejected:
#     on 2026-11-06 four scripts ran the loop on integer codes, strobed 1-8
#     times in 400+ samples and wrote the CSVs anyway;
#   * a script for a streaming spec with no `assert(... isfinite(...))` on
#     its stimulus is rejected: NaN saturates to -2048 silently and every
#     input word becomes 8390656 (2026-11-05 0/0; 2026-11-06 pulse tap /0);
#   * a script whose loop `for i = a:b` reads `list(i+k)` (or `list(i-k)`
#     from i = 1) without making room for the shift is rejected: on
#     2026-11-06 inst(i+1) with i running to numel(inst) cost 4 of the
#     attempt's failed runs; when such a line does crash, WHY names the
#     loop and the shift instead of the generic list-length hint;
#   * run_matlab_script only runs a script the LLM wrote in this attempt;
#   * an unchanged script is not re-run after it failed;
#   * a script identical to one that already crashed in this attempt (only
#     comments or spacing differ) is rejected before it is written: run
#     115142 on 2026-10-30 spent its 2nd and 3rd run on byte-identical code;
#   * a failed run comes back with WHERE IT CRASHED (the source line MATLAB's
#     message points at) and WHY (a plain reason for the messages seen so
#     far), so the LLM fixes that line instead of rewriting the script —
#     on 2026-10-30 every rewrite brought a new bug and used up the runs;
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
    MAX_IDENTICAL_REJECTS,
    MAX_RUN_FAILURES,
    SCRIPT_NAME,
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


def _scan(code: str, keep_strings: bool) -> str:
    """The script without its comments. Walks each line once: a `%` outside
    a string starts a comment; `"` or a `'` that is not a transpose starts a
    string ('' inside is a quote). With keep_strings=False the quoted text
    is blanked too (every string becomes `"`), so `fprintf('rows >> %d\\n', n)`
    hides neither `>>` nor `%d` from the operator check. With
    keep_strings=True the strings stay: a fix can live inside quotes (a
    column name in 'VariableNames'), and the fingerprint must see it."""
    out_lines: list[str] = []
    for line in code.splitlines():
        out: list[str] = []
        i, n, quote = 0, len(line), ""
        while i < n:
            ch = line[i]
            if quote:
                if ch == quote:
                    if ch == "'" and i + 1 < n and line[i + 1] == "'":
                        if keep_strings:
                            out.append("''")
                        i += 2                                   # '' = one quote inside the string
                        continue
                    quote = ""
                    out.append(ch if keep_strings else '"')
                elif keep_strings:
                    out.append(ch)
                i += 1
                continue
            if ch == "%":
                break                                            # comment: rest of line
            if ch == '"' or (ch == "'" and not _is_transpose(out)):
                quote = ch
                out.append(ch if keep_strings else '"')
                i += 1
                continue
            out.append(ch)
            i += 1
        out_lines.append("".join(out))
    return "\n".join(out_lines)


def _code_only(code: str) -> str:
    """The script with comments and quoted strings blanked out (what the
    operator / function / field checks look at)."""
    return _scan(code, keep_strings=False)


def code_fingerprint(code: str) -> str:
    """The script reduced to what MATLAB executes: comments dropped, runs of
    spaces collapsed, spaces around `=` `;` `,` dropped, strings kept. Other
    spaces stay because they can matter inside brackets (`[1 -2]` is two
    numbers, `[1 - 2]` is one). Two scripts with the same fingerprint fail
    the same way, however different they look."""
    text = re.sub(r"\s+", " ", _scan(code, keep_strings=True))
    return re.sub(r" ?([=;,]) ?", r"\1", text).strip()


# ── what to tell the LLM when a run fails ────────────────────────────────────
#
# MATLAB's message names a line ("File ...gen_test_vectors.m, line 43, in
# gen_test_vectors" for runtime errors, "Line: 14 Column: 27" for syntax
# errors) but the LLM never sees that line: on 2026-10-30 it guessed, threw
# the script away and rewrote it with a new bug (run 115908: the 37-vs-36
# length mismatch survived two rewrites). crash_note() quotes the line(s)
# and, for the messages seen so far, says in plain words what went wrong.

_LINE_RE = re.compile(r"\bline:?\s*(\d+)", re.IGNORECASE)

# (regex on MATLAB's message, plain reason + the fix). `{0}` `{1}` are the
# regex groups. Order matters: the first match wins.
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    (r"Dimensions of arrays being concatenated",
     "The lists joined with [ ] do not have the same shape: a column vector (from randi(n, k, 1) or a(:)) was "
     "joined to a row, or two rows were joined with `;`. Keep every list a row vector (randi(n, 1, k)) and "
     "append with a comma: a = [a, b];"),
    (r"Arrays have incompatible sizes|Matrix dimensions must agree",
     "The two sides of a + - .* ./ on this line do not have the same length, so MATLAB refuses. The usual "
     "cause: conv(a, b) returns numel(a)+numel(b)-1 values, longer than the list it is added to (an RRC "
     "pulse-shaped stream added to zeros(1, nsym*sps)). Print size() of BOTH sides of this exact line, then "
     "trim (y = y(1:N)) or pad (y(end+1:N) = 0) the longer one before the operation, and assert(numel(y) == N). "
     "Do not run smaller test snippets with matlab_eval: each failed snippet counts as a failed run."),
    (r"Index exceeds the number of array elements|Index exceeds|exceeds array bounds|Index in position",
     "A list is shorter than the number of beats it is indexed with: beat_cycles and the per-beat lists "
     "(I, Q, ns, symbol indexes) do not have the same length. Define nbeats once, give every list exactly "
     "nbeats entries (vals(mod(0:nbeats-1, numel(vals)) + 1) wraps a short list), and "
     "assert(numel(list) == nbeats) right after each one."),
    (r"Array indices must be positive",
     "An index of 0 or a negative number was used: MATLAB lists start at 1 (x(1) is the first entry). A loop "
     "variable that starts at 0, or `(i-1)` on the first pass of `for i = 1:n`, is the usual cause: add 1 to the "
     "index or start the loop at 2."),
    (r"Unrecognized function or variable '(\w+)'",
     "'{0}' is used before it is defined, is misspelled, or is a script variable read inside a local function "
     "(invisible there). Define it above this line in the script itself; no local functions."),
    (r"Invalid use of operator",
     "This line has a symbol MATLAB does not have (>>>, >>, <<, !=, +=, ++): write floor(x / 2^k), "
     "mod(x * 2^k, 2^N), ~=, x = x + v instead."),
    (r"size of the left side is 1-by-(\d+) and the size of the right side is 1-by-(\d+)",
     "The row has {1} values but the matrix has {0} columns: build the row from the same name list the header "
     "uses and assert(numel(row) == numel(names)) before storing it."),
    (r"Integers can only be combined with integers of the same class|of the same class",
     "Two number types are mixed: do every calculation in plain double (mod, floor, min/max); never "
     "int8/int16/uint8/uint32, typecast or bitshift."),
    (r"VariableNames",
     "The header name list and the matrix do not have the same number of columns: one name per column, "
     "cycle first, then the ports in the spec's order."),
    (r"Function definitions",
     "A local function inside the script: remove it and put the logic inline in the loop (or in an anonymous "
     "function @(...) that takes everything it needs as arguments)."),
)


# A loop that reads one entry past the end of a list. timing_recovery
# 2026-11-06 16:48: `for i = 1:nIn ... xs(i) = x(floor(inst(i+1)) + 1)` with
# inst 100 long asked for inst(101) on the last pass; the WHY hint (keyed on
# "Index exceeds" alone) blamed per-beat lists of different lengths, the LLM
# rewrote the block and hit three more index crashes before it succeeded.
_INDEX_ERROR_RE = re.compile(r"Index exceeds|exceeds array bounds|Index in position|Array indices must be positive",
                             re.IGNORECASE)
_FOR_RE = re.compile(r"^\s*(?:par)?for\s+([A-Za-z_]\w*)\s*=\s*(.+?)\s*[;,]?\s*$")
_BLOCK_OPEN_RE = re.compile(r"^\s*(if|while|switch|try|for|parfor|function)\b")
_BLOCK_END_RE = re.compile(r"^\s*end\b")
# `name(var)`, `name(var + k)` or `name(var - k)` that is read, not assigned
# (`x(i+1) = v` grows the list and is legal)
_SHIFTED_READ_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(\s*([A-Za-z_]\w*)\s*(?:([+-])\s*(\d+))?\s*\)(?!\s*=(?!=))")
_MATLAB_FUNCS = frozenset("""abs all any angle assert bitand bitor bitxor ceil conv cos cumsum diff disp error exp
    find fix floor fprintf imag isempty isfinite isnan length log log2 max min mod num2str numel ones pi rand randi
    randn real rem repmat reshape round sign sin sprintf sqrt sum tanh upsample zeros""".split())


def _split_range(header: str) -> tuple[str, str] | None:
    """(start, stop) of a `start:stop` or `start:step:stop` loop range; None
    for a loop over a list (`for v = vals`)."""
    header = header.strip()
    if header.startswith("(") and _closing_paren(header, 0) == len(header) - 1:
        header = header[1:-1]
    parts, depth, cur = [], 0, []
    for ch in header:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == ":" and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur).strip())
    return (parts[0], parts[-1]) if len(parts) in (2, 3) else None


def _enclosing_for(stack: list, idx: str) -> tuple | None:
    """The `for idx = start:stop` frame the read `name(idx ...)` belongs to,
    walking out through nested loops; None when an if/while on the way
    mentions idx (the read is guarded), when the loop runs over a list, or
    when no enclosing loop has that variable."""
    for frame in reversed(stack):
        if frame[0] == "guard":
            if idx in frame[1]:
                return None
        elif frame[0] == "for" and frame[1] == idx:
            return frame if frame[3] is not None else None
    return None


def _loop_overruns(code: str) -> list[dict]:
    """Every read of `name(var)`, `name(var + k)` or `name(var - k)` inside
    `for var = start:stop` (through nested loops, but not under an if/while
    that mentions var) whose range does not make room for the shift: for `+k`
    the stop does not end in `-k` or more and `name` is not built with
    `stop + k` or more; for `-k` (k = 0 for a bare read) the start is a
    literal below 1 + k. Comments and strings are ignored."""
    lines = _code_only(code).splitlines()
    funcs = _MATLAB_FUNCS | {f.lower() for f in re.findall(r"([A-Za-z_]\w*)\s*=\s*@\s*\(", "\n".join(lines))}   # + anonymous functions
    # ("for", var, header, start, stop) — start/stop None for a loop over a list;
    # ("guard", {names in the condition}) for if/while; ("other", set()) for switch/try/function
    stack: list[tuple] = []
    found: list[dict] = []
    for n, line in enumerate(lines, 1):
        if _BLOCK_END_RE.match(line):
            if stack:
                stack.pop()
            continue
        m = _FOR_RE.match(line)
        if m:
            rng = _split_range(m.group(2)) or (None, None)
            stack.append(("for", m.group(1), m.group(2).strip(), *rng))
            continue
        b = _BLOCK_OPEN_RE.match(line)
        if b:
            kind = "guard" if b.group(1) in ("if", "while") else "other"
            stack.append((kind, set(re.findall(r"[A-Za-z_]\w*", line[b.end():]))))
            continue
        if not stack:
            continue
        for r in _SHIFTED_READ_RE.finditer(line):
            name, idx = r.group(1), r.group(2)
            sign, k = (r.group(3), int(r.group(4))) if r.group(3) else ("-", 0)   # a bare name(var): shift 0
            frame = _enclosing_for(stack, idx)
            if frame is None or name.lower() in funcs or name == idx:
                continue
            _, var, header, start, stop = frame
            if sign == "+":
                shrunk = re.search(r"-\s*(\d+)\s*$", stop)
                if shrunk and int(shrunk.group(1)) >= k:
                    continue
                padded = re.search(rf"(?<![\w.]){re.escape(name)}\s*=(?!=).*(?<![\w.]){re.escape(stop)}\s*\+\s*(\d+)",
                                   "\n".join(lines)) if re.fullmatch(r"[A-Za-z_]\w*", stop) else None
                if padded and int(padded.group(1)) >= k:
                    continue
            else:
                if not re.fullmatch(r"\d+", start) or int(start) >= 1 + k:
                    continue
            found.append({"line": n, "text": line.strip(), "var": var, "header": header, "start": start, "stop": stop,
                          "name": name, "sign": sign, "k": k})
    return found


def _overrun_reason(o: dict) -> str:
    var, name, k, stop, start = o["var"], o["name"], o["k"], o["stop"], o["start"]
    if o["sign"] == "+":
        return (f"The loop `for {var} = {o['header']}` reads {name}({var}+{k}) on this line: on the last pass {var} = "
                f"{stop}, so it asks for entry {stop}+{k} of {name}, one past its end. Either run the loop shorter "
                f"(for {var} = {start}:{stop}-{k}), make {name} {k} entr{'y' if k == 1 else 'ies'} longer before the loop "
                f"({name}(end+1) = {name}(end)), or build the shift into the list ({name}_next = {name}({1 + k}:end)) "
                f"and read {name}_next({var}). Then assert(numel({name}) >= {stop}+{k}) before the loop.")
    if k == 0:
        return (f"The loop `for {var} = {o['header']}` starts at {start} and reads {name}({var}) on this line, so the first "
                f"pass asks for entry {start} of {name}, but MATLAB lists start at 1. Either start the loop at 1 "
                f"(for {var} = 1:{stop}, and use {var}-1 where the 0-based count is meant) or read {name}({var}+1).")
    return (f"The loop `for {var} = {o['header']}` reads {name}({var}-{k}) on this line: on the first pass {var} = {start}, "
            f"so it asks for entry {int(start) - k} of {name}, but MATLAB lists start at 1. Start the loop at {1 + k} "
            f"(for {var} = {1 + k}:{stop}) and handle the first {k} value(s) before it, or read {name}(max({var}-{k}, 1)).")


def loop_index_overruns(code: str) -> list[str]:
    """Plain description of every loop read that goes past the end (or before
    the start) of a list, for the pre-run rejection."""
    return [f"line {o['line']} `{o['text']}`: {_overrun_reason(o)}" for o in _loop_overruns(code)]


# A fixed-length sample history indexed with the running sample counter.
# timing_recovery 2026-11-06 17:12 (iteration 3): xbufI = zeros(1, 8) was
# shifted with xbufI = [xbufI(2:8), ci] (newest sample in slot 8) and then
# read as xbufI(m), xbufI(m+3), xbufI(m-2) with m = nIn - 2. The first strobe
# (nIn = 1) asked for slot -1 ("Array indices must be positive"); after 8
# samples every read would have been past the end. The generic hint blamed a
# loop variable starting at 0, so the LLM kept the m variable.
_ZEROS_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*=\s*zeros\s*\(\s*1\s*,\s*(\d+)\s*\)")
_BASE_VAR_RE = re.compile(r"^([A-Za-z_]\w*)\s*(?:[+-]\s*\d+)?$")


def _history_buffers(code: str) -> dict[str, int]:
    """name -> length of every `name = zeros(1, K)` (K <= 64) that is later
    shifted with `name = [name(2:...), x]`: a history of the last K samples,
    newest entry last."""
    code = _code_only(code)
    found: dict[str, int] = {}
    for m in _ZEROS_RE.finditer(code):
        name, k = m.group(1), int(m.group(2))
        if k <= 64 and re.search(rf"(?<![\w.]){re.escape(name)}\s*=\s*\[\s*{re.escape(name)}\s*\(\s*2\s*:", code):
            found[name] = k
    return found


def _history_reads_by_counter(code: str) -> list[dict]:
    """Every read of a history buffer whose index is a variable (plus/minus a
    number) that is not a for-loop variable: a running sample count."""
    bufs = _history_buffers(code)
    if not bufs:
        return []
    clean = _code_only(code)
    loop_vars = {m.group(1) for m in re.finditer(r"^\s*(?:par)?for\s+([A-Za-z_]\w*)\s*=", clean, re.MULTILINE)}
    found: list[dict] = []
    for n, line in enumerate(clean.splitlines(), 1):
        for name, k in bufs.items():
            for r in re.finditer(rf"(?<![\w.]){re.escape(name)}\s*\(\s*([^()]*?)\s*\)(?!\s*=(?!=))", line):
                base = _BASE_VAR_RE.match(r.group(1).strip())
                if not base or base.group(1) in loop_vars or base.group(1).lower() == "end":
                    continue
                found.append({"line": n, "text": line.strip(), "name": name, "k": k, "idx": r.group(1).strip(),
                              "var": base.group(1)})
    return found


def _history_reason(h: dict) -> str:
    name, k, idx, var = h["name"], h["k"], h["idx"], h["var"]
    return (f"{name} is a fixed history of the last {k} samples (the newest sample is {name}({k}), the one before it "
            f"{name}({k - 1}), ..., x[n-j] is {name}({k}-j)), but this line reads it with {name}({idx}), where {var} "
            f"counts samples and keeps growing: it is 0 or negative on the first strobe and past {k} after {k} samples. "
            f"Drop {var} from the index and use fixed slot numbers counted from the newest sample: for base point "
            f"m = n-2, x[m+2] = {name}({k}), x[m+1] = {name}({k - 1}), x[m] = {name}({k - 2}), x[m-1] = {name}({k - 3}), "
            f"x[m-2] = {name}({k - 4}), x[m-3] = {name}({k - 5}). Make the history long enough for the oldest sample "
            f"read (zeros(1, 8) holds x[n-7] .. x[n]).")


def history_index_by_counter(code: str) -> list[str]:
    """Plain description of every history-buffer read indexed with a running
    sample count, for the pre-run rejection."""
    return [f"line {h['line']} `{h['text']}`: {_history_reason(h)}" for h in _history_reads_by_counter(code)]


# An assert that can never pass: it compares a list's length with a number
# defined FROM that same length. timing_recovery 2026-11-06 18:46 (run
# ce5d6a48): `Nc = numel(Ic) + sps - 1; assert(numel(Ic) == Nc && numel(Qc)
# == Nc, 'pulse length mismatch')` — numel(Ic) == numel(Ic) + 3 is false
# whatever Ic holds. The generic assert hint said "fix the values, not the
# assert", the opposite of what was needed; the attempt was lost.
_LEN_EQ_RE = re.compile(r"(?:numel|length)\s*\(\s*([A-Za-z_]\w*)\s*\)\s*==\s*([A-Za-z_]\w*)(?![\w(])"
                        r"|(?<![\w.])([A-Za-z_]\w*)\s*==\s*(?:numel|length)\s*\(\s*([A-Za-z_]\w*)\s*\)")


def _assert_contradiction(code: str, crash_line: str) -> dict | None:
    """For an assert line comparing numel(X) == N: the definition `N = ...`
    that itself uses numel(X)/length(X) plus or minus something, if any."""
    if not re.match(r"\s*assert\s*\(", crash_line):
        return None
    lines = _code_only(code).splitlines()
    for m in _LEN_EQ_RE.finditer(_code_only(crash_line)):
        x, n = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        for d, line in enumerate(lines, 1):
            dm = re.match(rf"\s*{re.escape(n)}\s*=(?!=)\s*(.+?)\s*;?\s*$", line)
            if not dm:
                continue
            expr = dm.group(1)
            if re.search(rf"(?:numel|length)\s*\(\s*{re.escape(x)}\s*\)", expr) and \
                    not re.fullmatch(rf"(?:numel|length)\s*\(\s*{re.escape(x)}\s*\)", expr):
                return {"x": x, "n": n, "def_line": d, "expr": expr}
    return None


def _contradiction_reason(c: dict) -> str:
    x, n, expr, d = c["x"], c["n"], c["expr"], c["def_line"]
    return (f"This check can never pass: {n} is defined on line {d} as `{n} = {expr}`, so `numel({x}) == {n}` compares "
            f"the length of {x} with that same length changed by an offset. The CHECK is wrong, not the values of {x}. "
            f"Decide which length you mean: if {n} is the length AFTER a later step (upsampling by sps gives nsym*sps "
            f"values, conv(a, b) gives numel(a)+numel(b)-1), compute {n} from the inputs (nsym, sps, numel(h)) and "
            f"assert the RESULT of that step against it, on the line after that step; if you meant the length of {x} "
            f"as it is now, define {n} from what {x} was built from, not from numel({x}). Change the {n} line or the "
            f"assert, not {x}.")


def contradicting_asserts(code: str) -> list[str]:
    """Plain description of every assert in the script that compares a list's
    length with a number defined from that same length, for the pre-run
    rejection."""
    found: list[str] = []
    for k, (line, raw) in enumerate(zip(_code_only(code).splitlines(), code.splitlines()), 1):
        c = _assert_contradiction(code, line)
        if c:
            found.append(f"line {k} `{raw.strip()}`: {_contradiction_reason(c)}")
    return found


def _why(error: str, crash_line: str, overrun: dict | None = None, history: dict | None = None,
         contradiction: dict | None = None) -> str:
    """Plain reason for the MATLAB message, or "" when it is not one seen before.
    `overrun` is the loop read past the end found on the crashed line, if any,
    `history` a fixed history buffer read with a running sample count, and
    `contradiction` an assert that compares a length with itself plus an
    offset: each names the real cause instead of the generic hint."""
    if history and _INDEX_ERROR_RE.search(error):
        return _history_reason(history)
    if overrun and _INDEX_ERROR_RE.search(error):
        return _overrun_reason(overrun)
    if contradiction:
        return _contradiction_reason(contradiction)
    for pattern, hint in _ERROR_HINTS:
        m = re.search(pattern, error, re.IGNORECASE)
        if m:
            return hint.format(*m.groups()) if m.groups() else hint
    if re.match(r"\s*assert\s*\(", crash_line):
        return ("Your own assert on this line fired, so the message above is yours: the values it checks are wrong "
                "(a list length, a field range, a row/header count). Fix the lists or values it names, not the assert.")
    return ""


def crash_note(code: str, error: str, script: str = SCRIPT_NAME) -> str:
    """What is appended to a failed run_matlab_script result: the source
    line(s) MATLAB's message points at (with one line of context each
    side), why it stopped there when the message is a known one, and the
    instruction to fix that line only."""
    lines = code.splitlines()
    numbers: list[int] = []
    for m in _LINE_RE.finditer(error):
        n = int(m.group(1))
        if 1 <= n <= len(lines) and n not in numbers:
            numbers.append(n)
    parts: list[str] = []
    if numbers:
        parts.append(f"WHERE IT CRASHED ({script}):")
        for n in numbers[:3]:
            for k in range(max(1, n - 1), min(len(lines), n + 1) + 1):
                parts.append(f"{'-->' if k == n else '   '}{k:4d} | {lines[k - 1]}")
    overrun = next((o for o in _loop_overruns(code) if numbers and o["line"] == numbers[0]), None)
    history = next((h for h in _history_reads_by_counter(code) if numbers and h["line"] == numbers[0]), None)
    crash_line = lines[numbers[0] - 1] if numbers else ""
    why = _why(error, crash_line, overrun, history, _assert_contradiction(code, crash_line))
    if why:
        parts.append("WHY: " + why)
    parts.append("FIX: change only this line (and whatever it depends on) with generate_matlab_script; keep every other "
                 "line exactly as it is and do not start a new script. A script identical to this one is rejected.")
    return "\n".join(parts)


def _append_text(out, text: str):
    """Add `text` to a tool result whatever its shape: a plain string, or the
    list of content blocks the MCP adapter returns."""
    if isinstance(out, list):
        return [*out, {"type": "text", "text": text}]
    return f"{out}\n\n{text}"


def _plain_text(out) -> str:
    """The readable text of a tool result: content blocks joined, not the
    Python repr `[{'type': 'text', ...}]` that str() gives (the "gave up"
    message in the UI and the next attempt's payload showed that repr)."""
    if isinstance(out, list):
        return "\n\n".join(str(b.get("text", b)) if isinstance(b, dict) else str(b) for b in out)
    return str(out)


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


# A signed intermediate wrapped with mod() before a shift or a saturation:
# mod() maps every negative number onto a large positive one, so the sign is
# gone before floor/min/max ever see it. llr_demap16, 2026-10-30 11:51:
#   llr = @(m, ns) min(max(floor((mod(m*ns, 2^24) + 4096) / 2^13), -32), 31)
# meant "signed 24-bit product" but made every negative LLR +31: 14 of the
# 20 expected beats were wrong, the RTL was right and was blamed twice.
SIGN_SENSITIVE = ("floor", "ceil", "round", "fix", "min", "max")
_MOD_RE = re.compile(r"\bmod\s*\(")
_BINARY_MINUS = re.compile(r"[\w)\]]\s*-")          # `a - b`, `x) - 1`; not the sign of `-32` or `2^-3`


def _closing_paren(text: str, open_at: int) -> int | None:
    """Index of the `)` that closes the `(` at open_at, or None."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _first_argument(args: str) -> str:
    """The text before the first top-level comma."""
    depth = 0
    for i, ch in enumerate(args):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return args[:i]
    return args


def _enclosing_calls(text: str) -> list[str]:
    """Names of the calls whose `(` is still open at the end of `text`
    (lower-cased; "" for a bare parenthesis)."""
    names, depth = [], 0
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth:
                depth -= 1
            else:
                m = re.search(r"([A-Za-z_]\w*)\s*$", text[:i])
                names.append(m.group(1).lower() if m else "")
    return names


def wrapped_signed_math(code: str) -> list[str]:
    """Every `mod(<product or difference>, ...)` that sits inside a
    floor/ceil/round/fix/min/max call on the same line, as written in the
    script (comments and strings ignored). A plain variable or a sum inside
    mod() (`mod(I, 2^12)`, `mod(state + 1, 16)`) is a wrap-around and fine;
    a product or a difference is where a negative value comes from."""
    text = _code_only(code)
    found: list[str] = []
    for m in _MOD_RE.finditer(text):
        open_at = m.end() - 1
        close_at = _closing_paren(text, open_at)
        if close_at is None:
            continue
        first = _first_argument(text[open_at + 1:close_at])
        if "*" not in first and not _BINARY_MINUS.search(first):
            continue
        line_start = text.rfind("\n", 0, m.start()) + 1
        if any(name in SIGN_SENSITIVE for name in _enclosing_calls(text[line_start:m.start()])):
            snippet = re.sub(r"\s+", " ", text[m.start():close_at + 1]).strip()
            if snippet not in found:
                found.append(snippet)
    return found


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


# A decimating stream (timing recovery: about one output per N inputs) whose
# model strobes 1..8 times in 400+ samples writes self-consistent CSVs that
# only the outer validator rejects (beat_rate_mismatch), one attempt later.
# 2026-11-06 run 2a912a0f: four such scripts, all with the loop run on the
# integer codes. An assert on the beat count makes MATLAB stop the script
# itself, with the numbers, inside the generator's own loop.
_BEAT_COUNT_WORDS = re.compile(r"beat|strobe|n_?out|out_?count|mvalid|m_?axis_?tvalid|outs\s*\(\s*:", re.IGNORECASE)


def _is_streaming(spec: dict | None) -> bool:
    names = {str(p.get("name", "")).lower() for p in ((spec or {}).get("ports") or []) if isinstance(p, dict)}
    return "s_axis_tvalid" in names and "m_axis_tvalid" in names


def missing_beat_count_assert(spec: dict | None, code: str) -> str | None:
    """Why the script is rejected when the spec states an output rate but no
    `assert(...)` line in the script mentions the output beat count; None
    when the check does not apply or the assert is there."""
    from agents.test_vector_agent.lint.test_vector_validator import spec_output_rate   # no import cycle: validator imports no guard
    rate = spec_output_rate(spec)
    if rate is None or not _is_streaming(spec):
        return None
    asserts = [line for line in _code_only(code).splitlines() if re.search(r"\bassert\s*\(", line, re.IGNORECASE)]
    if any(_BEAT_COUNT_WORDS.search(a) for a in asserts):
        return None
    return (f"the spec says about one output per {rate} input(s), but the script never checks its own output beat "
            f"count, so a loop that stops strobing (1-8 beats in 400+ samples on 2026-11-06, loop run on integer "
            f"codes instead of real values) still writes CSVs and is only caught one attempt later. Count the "
            f"beats (nOut) and the input samples (nIn) and add after the loop: "
            f"assert(nOut >= nIn/{rate}/2 && nOut <= nIn/{rate}*2, '%d beats for %d inputs, want about %d', "
            f"nOut, nIn, round(nIn/{rate})); — also assert(abs(v) < 1/{rate}, 'loop filter blew up: v=%g', v) at "
            f"every strobe if the design has a loop filter (CLOSED LOOPS rule)")


# NaN in the stimulus passes every range check: min(max(NaN, -2048), 2047)
# is -2048, so every I and Q became -2048 and every input word 8390656.
# 2026-11-05 15:54 (0/0 in the formula) and 2026-11-06 16:06 (a pulse tap
# divided by k^2-1 = 0, then normalised Inf/Inf): both ran MATLAB to the end,
# wrote the CSVs and were only caught by the validator (constant_input).
_ISFINITE_ASSERT = re.compile(r"\bassert\s*\(.*\bisfinite\s*\(", re.IGNORECASE)


def missing_finite_assert(spec: dict | None, code: str) -> str | None:
    """Why the script is rejected when a streaming spec's script never asserts
    that its stimulus is finite; None when it does or the check does not apply."""
    if not _is_streaming(spec):
        return None
    if any(_ISFINITE_ASSERT.search(line) for line in _code_only(code).splitlines()):
        return None
    return ("the script never checks that the stimulus is finite before packing it. A NaN or Inf (0/0, x/0, a "
            "pulse tap divided by k^2-1 at k = 1, Inf/Inf when normalising) passes the range check because "
            "min(max(NaN, -2048), 2047) is -2048, so every input word becomes the same number (8390656 on "
            "2026-11-05 and 2026-11-06) and the validator rejects the CSVs one attempt later. Right after the "
            "stimulus is built and before any saturate/pack line add: assert(all(isfinite(xI)) && "
            "all(isfinite(xQ)), 'stimulus has NaN/Inf: check every division and normalisation'); (use your own "
            "stimulus names; one assert per input signal is fine)")


# Noise in the stimulus of a closed loop. The golden model applies the loop
# filter's value on the next sample, a pipelined RTL several samples later;
# on a clean stimulus the two settle onto the same path (within 2 steps after
# 64 beats, 40 seeds, 2026-11-09), on a noisy one they never do: a CORRECT
# design then differs by hundreds of steps on some beats and the compare
# fails whatever the RTL does (scripts of 2026-11-06 / 11-09 with randn and a
# +500 ppm rate error: up to 1361 steps and a mu 128 of 256 away).
_LOOP_SPEC_WORDS = re.compile(r"\bloop\s+filter\b|\bNCO\b|\bintegrator\b|\bfeedback\b|\bTED\b|\bPLL\b|\bAGC\b",
                              re.IGNORECASE)
_NOISE_CALL = re.compile(r"(?<![\w.])(randn|awgn)\s*\(", re.IGNORECASE)


def _spec_text(spec: dict | None) -> str:
    spec = spec or {}
    return "\n".join([str(spec.get("description") or "")] + [str(b) for b in spec.get("behavior") or []])


def noisy_closed_loop_stimulus(spec: dict | None, code: str) -> str | None:
    """Why the script is rejected when the spec describes a closed loop (a
    loop filter, an NCO, an integrator fed back) and the script adds noise to
    its stimulus; None when it does not or the spec has no loop."""
    if not _is_streaming(spec) or not _LOOP_SPEC_WORDS.search(_spec_text(spec)):
        return None
    lines = [(n, line.strip()) for n, line in enumerate(_code_only(code).splitlines(), 1) if _NOISE_CALL.search(line)]
    if not lines:
        return None
    n, text = lines[0]
    return (f"line {n} `{text}` adds noise to the stimulus of a closed-loop design. The golden model's loop reacts on "
            f"the next sample and the RTL's a few samples later; on a noisy stimulus the two never settle onto the "
            f"same path, so a CORRECT design fails the beat-by-beat compare. This simulation checks that the RTL "
            f"computes the spec's formulas, not its noise performance (the spec's Es/N0 and ppm figures belong to "
            f"the graded test). Use the clean stimulus of the CLOSED-LOOP STIMULUS rule: random +/-1 symbols drawn "
            f"separately for I and Q, each spread over the 4 samples with p = [0.25 0.75 0.75 0.25], scaled by 2048, "
            f"at least 200 symbols; no randn / awgn, no sample-rate error, no resampling, no fractional start offset")


LOOP_TRACE_CSV = "loop_trace.csv"


def missing_loop_trace(spec: dict | None, code: str) -> str | None:
    """Why the script is rejected when the spec describes a closed loop and
    the script never writes loop_trace.csv (CLOSED-LOOP TRACE rule); None
    when it does or the spec has no loop. The trace is what lets the RTL
    writer see the model's mu / e / v / acc per beat next to the chip's
    registers when the dry run fails (simulation_agent.inside_values)."""
    if not _is_streaming(spec) or not _LOOP_SPEC_WORDS.search(_spec_text(spec)):
        return None
    if re.search(rf"\b{re.escape(LOOP_TRACE_CSV)}\b", code or "", re.IGNORECASE):
        return None
    return (f"the spec describes a closed loop but the script never writes {LOOP_TRACE_CSV}. When the dry run fails, "
            f"the RTL writer is shown the model's loop values from that file next to the chip's registers for the "
            f"same beats; without it it only sees the output pins and guesses. Add the CLOSED-LOOP TRACE rule's lines: "
            f"trace_names = {{'beat','cycle','mu8','e','v','acc','y_sym_i','y_sym_q','y_mid_i','y_mid_q'}}; collect "
            f"one row per output beat inside the strobe branch (beat = nOut before it is incremented, cycle, "
            f"floor(mu*256), e, v, acc, the two output codes, the two y_mid codes) and write it after the loop with "
            f"writetable(array2table(trace, 'VariableNames', trace_names), '{LOOP_TRACE_CSV}')")


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
    script (observed with local models), and so failed runs are hard-capped.
    matlab_eval (when present) shares the same cap: a failed snippet counts
    as a failed run."""
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
        wrapped = wrapped_signed_math(str(kwargs.get("code") or ""))
        if wrapped:
            return ("REJECTED: the script wraps a signed value with mod() BEFORE a shift or a saturation: " +
                    "; ".join(f"`{w}`" for w in wrapped) + ". mod() turns every negative number into a large "
                    "positive one (mod(-24576, 2^24) = 16752640), so a negative result comes out as the positive "
                    "saturation value and every such expected output is wrong while the RTL is right (llr_demap16, "
                    "2026-10-30: 14 of 20 expected LLRs became +31). A double already carries the sign of a 'signed "
                    "24-bit product': write the formula plainly — p = m * ns; r = floor((p + 4096) / 2^13); "
                    "v = min(max(r, -32), 31) — and use mod(v, 2^N) only at the pack line / CSV write "
                    "(NUMBER TYPES rule). Rewrite and call generate_matlab_script again.")
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
        no_count = missing_beat_count_assert(spec, str(kwargs.get("code") or ""))
        if no_count:
            return "REJECTED: " + no_count + ". Rewrite and call generate_matlab_script again."
        no_finite = missing_finite_assert(spec, str(kwargs.get("code") or ""))
        if no_finite:
            return "REJECTED: " + no_finite + ". Rewrite and call generate_matlab_script again."
        noisy = noisy_closed_loop_stimulus(spec, str(kwargs.get("code") or ""))
        if noisy:
            return "REJECTED: " + noisy + ". Rewrite and call generate_matlab_script again."
        no_trace = missing_loop_trace(spec, str(kwargs.get("code") or ""))
        if no_trace:
            return "REJECTED: " + no_trace + ". Rewrite and call generate_matlab_script again."
        overruns = loop_index_overruns(str(kwargs.get("code") or ""))
        if overruns:
            return ("REJECTED: a loop reads past the end of a list, so MATLAB would stop with 'Index exceeds the "
                    "number of array elements' (timing_recovery 2026-11-06: inst(i+1) with i running to numel(inst)): "
                    + "; ".join(overruns) + ". Rewrite and call generate_matlab_script again.")
        by_counter = history_index_by_counter(str(kwargs.get("code") or ""))
        if by_counter:
            return ("REJECTED: a fixed sample history is indexed with the running sample count, so MATLAB would stop "
                    "with 'Array indices must be positive' on the first strobe (timing_recovery 2026-11-06 17:12: "
                    "xbufI(m) with m = nIn - 2): " + "; ".join(by_counter) + ". Rewrite and call generate_matlab_script again.")
        contradictions = contradicting_asserts(str(kwargs.get("code") or ""))
        if contradictions:
            return ("REJECTED: an assert compares a list's length with a number defined from that same length, so it "
                    "fails whatever the list holds (timing_recovery 2026-11-06 18:46: Nc = numel(Ic) + sps - 1; "
                    "assert(numel(Ic) == Nc)): " + "; ".join(contradictions) + ". Rewrite and call generate_matlab_script again.")
        # Same code as a script that already crashed (comments or spacing
        # aside) fails the same way: say so instead of spending a run on it.
        code = str(kwargs.get("code") or "")
        if code_fingerprint(code) in state["failed_fingerprints"]:
            # 2026-11-06 17:12: the LLM re-sent the identical script seven times
            # after this rejection (same 2,626 output tokens each time) until the
            # 30-step cap. A model that is not changing the line will not start
            # now: end the attempt so the next one begins with fresh context.
            state["identical_rejects"] += 1
            last_error = (state["last_run"] or {}).get("error", "")
            if state["identical_rejects"] >= MAX_IDENTICAL_REJECTS:
                raise RunAttemptsExceeded(
                    f"The same crashed script was sent {state['identical_rejects']} times in a row without changing "
                    f"the line that crashed.\n{last_error}",
                    reason=f"Gave up: the script that crashed was re-sent unchanged {state['identical_rejects']} times.")
            return ("REJECTED: this script is the same as one that already crashed in this attempt (only comments "
                    "or spacing differ), so running it would fail the same way. Read WHERE IT CRASHED and WHY in "
                    "that error, change that line (and whatever it depends on), and call generate_matlab_script again. "
                    f"Sending it unchanged once more ends this attempt ({state['identical_rejects']} of "
                    f"{MAX_IDENTICAL_REJECTS}).")
        state["identical_rejects"] = 0
        state["dirty"] = True
        stem = _stem(kwargs.get("filename"))
        state["scripts"].add(stem)
        state["codes"][stem] = code
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
            stem = _stem(kwargs.get("script_name"))
            keep_failed_script(kwargs.get("project_dir"), stem, state["failures"])
            code = state["codes"].get(stem, "")
            state["failed_fingerprints"].add(code_fingerprint(code))
            out = _append_text(out, crash_note(code, str(out), f"{stem}.m"))   # the line it died on + why + "fix that"
            state["last_run"] = {"status": "error", "error": _plain_text(out)[:2500]}
            if state["failures"] >= max_failures:
                raise RunAttemptsExceeded(state["last_run"]["error"])
        else:
            state["last_run"] = {"status": "ok", "error": ""}
        return out

    async def eval_snippet(**kwargs):
        # matlab_eval was the one tool with no cap (run 2026-11-06: seven
        # "smaller diagnostic" snippets in a row, every one repeating the
        # script's own length mismatch, until the 30-step limit). A failed
        # snippet is a failed MATLAB run: count it, explain the message the
        # same way, and stop the attempt at the cap.
        out = await eval_tool.ainvoke(kwargs)
        if '"status": "error"' in str(out):
            state["failures"] += 1
            why = _why(str(out), "")
            note = (f"MATLAB_EVAL FAILED ({state['failures']} of {max_failures} failed runs used)."
                    + (f"\nWHY: {why}" if why else "")
                    + "\nFIX: do not shrink the problem into more snippets; fix the line in the script with "
                      "generate_matlab_script and run it with run_matlab_script.")
            out = _append_text(out, note)
            state["last_run"] = {"status": "error", "error": _plain_text(out)[:2500]}
            if state["failures"] >= max_failures:
                raise RunAttemptsExceeded(state["last_run"]["error"])
        return out

    wrapped = {
        gen_tool.name: StructuredTool.from_function(
            coroutine=generate, name=gen_tool.name, description=gen_tool.description, args_schema=gen_tool.args_schema),
        run_tool.name: StructuredTool.from_function(
            coroutine=run, name=run_tool.name, description=run_tool.description, args_schema=run_tool.args_schema),
    }
    eval_tool = by_name.get("matlab_eval")
    if eval_tool is not None:
        wrapped[eval_tool.name] = StructuredTool.from_function(
            coroutine=eval_snippet, name=eval_tool.name, description=eval_tool.description,
            args_schema=eval_tool.args_schema)
    return [wrapped.get(t.name, t) for t in tools]

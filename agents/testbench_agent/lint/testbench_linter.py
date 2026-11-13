# agents/testbench_agent/lint/testbench_linter.py — lint check for a generated
# VHDL testbench.
#
# Static checks always run (deterministic, no tools needed): testbench
# entity present and named <entity>_tb, DUT instantiated, every spec port
# connected, stimulus and self-checks present, no placeholders. Then, when
# Vivado's xvhdl is reachable, the DUT is compiled first and the testbench
# is compiled against it, so instantiation errors are caught too.

from __future__ import annotations

import re
import tempfile

from agents.lint_utils import compiler_errors
from agents.mcp_utils import mcp_run_xvhdl, mcp_write_files
from agents.testbench_agent.lint.closed_loop_compare import closed_loop_compare_issues
from agents.vhdl_version import resolve_vhdl_version, xvhdl_std

_ENTITY_RE = re.compile(r"\bentity\s+(\w+)\s+is", re.IGNORECASE)


def _static_checks(spec: dict, rtl: dict, testbench: dict) -> list[dict]:
    issues: list[dict] = []

    def issue(code: str, detail: str) -> None:
        issues.append({"code": code, "detail": detail})

    code = testbench.get("code") or ""
    # The RTL is what gets compiled, so its entity name is the DUT's identity
    # even if the spec says something else.
    dut = rtl.get("entity_name") or spec.get("entity_name")
    expected_tb = f"{dut}_tb" if dut else None

    if not code.strip():
        issue("empty_code", "No testbench code was returned.")
        return issues

    if "```" in code:
        issue("markdown_fence", "Code contains markdown fences — strip them before compiling.")
    for n, line in enumerate(code.splitlines(), 1):
        # "..." only counts as a stub when it is the whole line; an ellipsis
        # inside a comment or report string is normal prose.
        if re.search(r"\bTODO\b|\bFIXME\b", line) or re.fullmatch(r"\s*(--)?\s*\.\.\.\s*", line):
            issue("placeholder", f"Placeholder left in the code at line {n}: {line.strip()}")

    # xvhdl accepts "sig <= a when c else b;" inside a process, but xelab
    # 2020.1 refuses it at elaboration (long after lint). Catch it here.
    in_process = False
    for n, line in enumerate(code.splitlines(), 1):
        text = line.split("--", 1)[0]
        if re.search(r"\bend\s+process\b", text, re.IGNORECASE):
            in_process = False
        elif re.search(r"\bprocess\b", text, re.IGNORECASE):
            in_process = True
        elif in_process and re.search(r"<=.*\bwhen\b.*\belse\b", text, re.IGNORECASE):
            issue("conditional_assignment_in_process",
                  f"Line {n}: 'x <= a when c else b' inside a process is not simulated by xelab 2020.1; "
                  f"use 'if c then x <= a; else x <= b; end if;'.")

    # std_logic'val(n) picks by POSITION in ('U','X','0','1',...): 0 -> 'U',
    # 1 -> 'X'. It compiles, and every expected 1-bit value is then wrong.
    # unsigned(<1-bit port>) is the matching mistake on the printing side:
    # xvhdl "cannot convert type 'std_logic' to type 'unsigned'" (observed
    # looping for 6 attempts). Both get a plain message with the right form.
    one_bit = [p["name"] for p in spec.get("ports") or []
               if isinstance(p, dict) and p.get("name") and str(p.get("width") or "").strip() in ("1", "[0:0]")]
    one_bit_re = re.compile(r"\bunsigned\s*\(\s*(" + "|".join(map(re.escape, one_bit)) + r")\s*\)",
                            re.IGNORECASE) if one_bit else None
    for n, line in enumerate(code.splitlines(), 1):
        text = line.split("--", 1)[0]
        if re.search(r"\bstd_logic'val\s*\(", text, re.IGNORECASE):
            issue("std_logic_val",
                  f"Line {n}: std_logic'val(n) gives 'U' for 0 and 'X' for 1 (position in the type), so the "
                  f"comparison always fails; use (sig = '1') /= (v = 1) and print with integer'image(v).")
        m = one_bit_re.search(text) if one_bit_re else None
        if m:
            issue("scalar_to_unsigned",
                  f"Line {n}: '{m.group(1)}' is a 1-bit std_logic; unsigned({m.group(1)}) does not compile. "
                  f"Compare with (sig = '1') /= (v = 1) and print with std_logic'image({m.group(1)}).")

    m = _ENTITY_RE.search(code)
    if not m:
        issue("no_entity", "No 'entity ... is' declaration found.")
    elif expected_tb and m.group(1) != expected_tb:
        issue("name_mismatch", f"Testbench entity is '{m.group(1)}' but expected '{expected_tb}'.")
    if not re.search(r"\barchitecture\b", code, re.IGNORECASE):
        issue("no_architecture", "No architecture body found.")
    if not re.search(r"\bwait\b", code, re.IGNORECASE):
        issue("no_stimulus", "No 'wait' statements — the testbench has no stimulus timing.")
    if not re.search(r"\bassert\b|\breport\b", code, re.IGNORECASE):
        issue("no_checks", "No assert/report — the testbench is not self-checking.")

    if dut and not re.search(rf"\b{re.escape(dut)}\b", code):
        issue("dut_not_instantiated", f"DUT '{dut}' is never referenced in the testbench.")

    for p in spec.get("ports") or []:
        name = p.get("name") if isinstance(p, dict) else None
        if name and not re.search(rf"\b{re.escape(name)}\b", code):
            issue("port_not_connected", f"Spec port '{name}' does not appear in the testbench.")

    issues += _replay_checks(spec, code)
    issues += _streaming_checks(spec, code)
    issues += closed_loop_compare_issues(spec, code)     # tolerance + settle window (closed_loop_compare.py)
    return issues


# Two-argument reads only: read_wide's own `read(l, c, ok)` character reads
# are not column reads.
_READ_RE = re.compile(r"\bread(?:_wide)?\s*\(\s*(\w+)\s*,\s*[\w.()' ]+?\s*\)", re.IGNORECASE)
_INT_VAR_RE = re.compile(r"\bvariable\s+([^:;]+?)\s*:\s*integer\b", re.IGNORECASE)
_SELF_CMP_RE = re.compile(r"\bif\s+(\w+)\s*/=\s*(\w+)\b", re.IGNORECASE)


def _port_bits(port: dict) -> int | None:
    width = str(port.get("width") or "").strip()
    if re.fullmatch(r"\d+", width):
        return int(width)
    m = re.fullmatch(r"\[(\d+):(\d+)\]", width)
    return abs(int(m.group(1)) - int(m.group(2))) + 1 if m else None


_READ_WIDE_DEF_RE = re.compile(r"\bprocedure\s+read_wide\b(.*?)\bend\s+procedure\b", re.IGNORECASE | re.DOTALL)
_FIXED_WIDTH_RE = re.compile(r"\(\s*(\d+)\s+downto\s+0\s*\)", re.IGNORECASE)
_VEC_VAR_RE = re.compile(r"\bvariable\s+([^:;]+?)\s*:\s*std_logic_vector\s*\(\s*(\d+)\s+downto\s+0\s*\)", re.IGNORECASE)


def _read_wide_checks(ports: list[dict], text: str) -> list[dict]:
    """Run 17:04 on 2026-10-29 (llr_demap16): the simulation died at time 0
    with 'Array sizes do not match, left array has 24 elements, right array
    has 32 elements' inside read_wide. The procedure is shared by every port
    it is called for, so a width written as a number inside it (31 downto 0)
    or a v_<port> variable declared with another port's width breaks the
    first call with a different width. Both compile fine."""
    issues: list[dict] = []
    for m in _READ_WIDE_DEF_RE.finditer(text):
        body = m.group(1)
        fixed = [w.group(0) for w in _FIXED_WIDTH_RE.finditer(body)]
        if fixed or "v'length" not in body.lower().replace(" ", ""):
            issues.append({"code": "read_wide_fixed_width",
                           "detail": "read_wide must size its accumulator from the parameter, exactly as the "
                                     "WIDE PORTS rule shows: `variable acc : unsigned(v'length - 1 downto 0)` and "
                                     "`v := std_logic_vector(acc)`. " +
                                     (f"It contains the fixed width(s) {fixed}; " if fixed else "It does not use v'length; ") +
                                     "the same procedure is called for every port it reads (24-bit and 32-bit alike), "
                                     "so a fixed width crashes with 'Array sizes do not match'."})
    for m in _VEC_VAR_RE.finditer(text):
        declared = int(m.group(2)) + 1
        for var in (v.strip() for v in m.group(1).split(",")):
            port_name = _port_of(var, ports)
            port = next((p for p in ports if p.get("name") == port_name), None)
            bits = _port_bits(port) if port else None
            if bits is not None and bits != declared:
                issues.append({"code": "vector_variable_width",
                               "detail": f"Variable '{var}' is std_logic_vector({declared - 1} downto 0) but port "
                                         f"'{port_name}' is {bits} bits; declare it as std_logic_vector({bits - 1} "
                                         f"downto 0). Every v_<port>/dut_<port> variable has exactly its port's width."})
    return issues


def _replay_checks(spec: dict, code: str) -> list[dict]:
    """Mistakes in replaying the CSVs that compile fine but break the
    simulation: a row read with fewer/more values than its columns (every
    later value lands in the wrong variable), a 32-bit-or-wider port read
    into an `integer` (textio stops at 2**31-1), and a cycle check that
    compares a variable with itself."""
    issues: list[dict] = []
    text = "\n".join(line.split("--", 1)[0] for line in code.splitlines())   # drop comments
    ports = [p for p in (spec.get("ports") or []) if isinstance(p, dict) and p.get("name")]
    direction = lambda p: str(p.get("direction", "")).lower()   # noqa: E731
    inputs = [p for p in ports if direction(p) == "input"]
    outputs = [p for p in ports if direction(p) == "output"]

    int_vars = [v.strip().lower() for m in _INT_VAR_RE.finditer(text) for v in m.group(1).split(",")]
    for p in ports:
        bits = _port_bits(p)
        if bits is not None and bits >= 32:
            hits = [v for v in int_vars if p["name"].lower() in v]
            if hits:
                issues.append({"code": "wide_port_read_as_integer",
                               "detail": f"Port '{p['name']}' is {bits} bits but variable(s) {hits} are 'integer' "
                                         f"(textio read stops at 2**31-1); declare it as "
                                         f"std_logic_vector({bits - 1} downto 0) and use read_wide (WIDE PORTS rule)."})

    if inputs and outputs:
        counts: dict[str, int] = {}
        for m in _READ_RE.finditer(text):
            counts[m.group(1).lower()] = counts.get(m.group(1).lower(), 0) + 1
        in_n, out_n = 1 + len(inputs), 1 + len(outputs)
        # A file may be read in several passes (collect expected beats first, then
        # replay), so a line variable's reads only have to add up to whole rows.
        whole_rows = lambda n: any((n - a * in_n) % out_n == 0   # noqa: E731
                                   for a in range(n // in_n + 1))
        if counts and not all(whole_rows(n) for n in counts.values()):
            in_cols = ", ".join(["cycle"] + [p["name"] for p in inputs])
            out_cols = ", ".join(["cycle"] + [p["name"] for p in outputs])
            issues.append({"code": "column_count_mismatch",
                           "detail": f"Reads per line variable: {counts}. test_vectors_in.csv has exactly these "
                                     f"{1 + len(inputs)} columns: [{in_cols}] and test_vectors_out.csv exactly these "
                                     f"{1 + len(outputs)}: [{out_cols}] (direction from the spec, NOT the name: "
                                     f"s_axis_tready is an output, m_axis_tready an input). Read each row's columns "
                                     f"in that order, one read per column, even ones you do not use."})

    issues += _read_wide_checks(ports, text)
    issues += _column_order_checks(inputs, outputs, text)

    for m in _SELF_CMP_RE.finditer(text):
        if m.group(1).lower() == m.group(2).lower():
            issues.append({"code": "self_comparison",
                           "detail": f"'if {m.group(1)} /= {m.group(2)}' compares a variable with itself; read the "
                                     f"outputs-row cycle into a second variable (v_cycle_out) and compare the two."})
    return issues


_READ_TARGET_RE = re.compile(r"\bread(?:_wide)?\s*\(\s*(\w+)\s*,\s*(\w+)", re.IGNORECASE)


def _port_of(target: str, ports: list[dict]) -> str | None:
    """Which port a read target like `v_s_axis_tdata` refers to: the longest
    port name contained in it (so `s_axis_tready` wins over `s_axis_t`)."""
    low = target.lower()
    hits = [p["name"] for p in ports if p["name"].lower() in low]
    return max(hits, key=len) if hits else None


def _column_order_checks(inputs: list[dict], outputs: list[dict], text: str) -> list[dict]:
    """The right NUMBER of reads per row can still put them in the wrong
    ORDER: a testbench that reads `s_axis_tready` from the inputs file
    (because the name sounds like an input) shifts every later column and
    the 32-bit tdata lands in an integer. For each line variable, map the
    read targets to port names and compare with the files' column order."""
    ports = inputs + outputs
    targets: dict[str, list[str]] = {}
    for m in _READ_TARGET_RE.finditer(text):
        if m.group(2).lower() in ("c", "ok"):           # read_wide's own character reads
            continue
        targets.setdefault(m.group(1).lower(), []).append(m.group(2))
    layouts = {"test_vectors_in.csv": inputs, "test_vectors_out.csv": outputs}
    issues: list[dict] = []
    for line_var, seq in targets.items():
        names = [_port_of(t, ports) for t in seq[1:]]       # seq[0] is the cycle column
        if not any(names):
            continue
        matched = [f for f, cols in layouts.items()
                   if len(names) == len(cols) and all(n is None or n == c["name"] for n, c in zip(names, cols))]
        if matched or len(names) not in {len(inputs), len(outputs)}:
            continue                                        # right order, or a count issue already reported
        wrong = [f for f, cols in layouts.items() if len(names) == len(cols)]
        for f in wrong:
            cols = layouts[f]
            order = ", ".join(["cycle"] + [c["name"] for c in cols])
            got = ", ".join(["cycle"] + [n or "?" for n in names])
            issues.append({"code": "column_order_mismatch",
                           "detail": f"Reads from line '{line_var}' are in the order [{got}] but {f} has the columns "
                                     f"[{order}]. Direction comes from the spec, NOT the name: s_axis_tready is an "
                                     f"OUTPUT (outputs file), m_axis_tready is an INPUT (inputs file). Read each "
                                     f"column, in file order, into the variable of that port."})
    return issues


# Every stream design has these two flags; s_axis_tready and m_axis_tready
# are optional. timing_recovery (2026-10-30) had no m_axis_tready port
# ("downstream is always ready"), the set below used to require all four
# wires, so none of the streaming checks ran and a cycle-by-cycle testbench
# went through: "missing m_axis_tvalid" on every expected row, "unexpected
# m_axis_tvalid" on every real beat, and the RTL's timing was blamed.
_STREAM_PORTS = {"s_axis_tvalid", "m_axis_tvalid"}
# The handshake flags the outputs file carries. They are never compared
# cycle by cycle: WHEN the design raises m_axis_tvalid (its latency, its
# strobe pattern) and what s_axis_tready shows during reset belong to the
# RTL, and the golden model cannot know them. Every other output column
# (m_axis_tdata, m_axis_tuser, ...) is a per-beat value: the payload.
_OUTPUT_FLAGS = ("s_axis_tready", "m_axis_tvalid")
_STRING_RE = re.compile(r'"[^"]*"')
# A relational compare: `/=`, or a `=` that is not part of `:=`, `<=`, `>=`
# or the association arrow `=>` (`(others => '0')` in a declaration default).
_CMP_RE = re.compile(r"/=|(?<![:<>/])=(?![=>])")
# `if v_m_axis_tvalid = 1`: the CSV's flag (an integer) opens a block ...
_CSV_TVALID_GATE_RE = re.compile(r"\bif\s+\w*m_axis_tvalid\w*\s*=\s*[01]\b", re.IGNORECASE)
# `m_axis_tvalid = '1' and m_axis_tready = '1'`: the DUT's own handshake decides.
# The rule file's complete streaming testbench samples the wires AT the edge into dut_m_axis_tvalid /
# dut_m_axis_tready copies and compares those, so a prefix is allowed (the
# CSV's integer flag is compared with 1, not '1', so it never matches here).
# Each term may be parenthesised: `if (a = '1') and (b = '1') then`.
_HANDSHAKE_RE = re.compile(r"\b\w*m_axis_tvalid\s*=\s*'1'\s*\)?\s+and\s+\(?\s*\w*m_axis_tready\s*=\s*'1'"
                           r"|\b\w*m_axis_tready\s*=\s*'1'\s*\)?\s+and\s+\(?\s*\w*m_axis_tvalid\s*=\s*'1'", re.IGNORECASE)
# Without an m_axis_tready port the downstream is always ready: every edge
# with the DUT's m_axis_tvalid = '1' is a consumed beat, so the compare is
# guarded by `if dut_m_axis_tvalid = '1' then` (or `... and k_exp < n_exp`, in
# either operand order: `if k_exp < n_exp and dut_m_axis_tvalid = '1' then` is
# the same guard). A flag compare such as
# `if (m_axis_tvalid = '1') /= (v_m_axis_tvalid = 1)` is not a guard.
_VALID_ONLY_RE = re.compile(r"\b(?:if|elsif)\s+(?:[^;]*?\band\s+)?\(?\s*\w*m_axis_tvalid\s*=\s*'1'\s*\)?\s+"
                            r"(?:then\b|and\b|or\b)", re.IGNORECASE)
# Any identifier that carries the name (m_axis_tready, dut_m_axis_tready,
# v_m_axis_tready): on a design without the port, none of them may exist.
_M_AXIS_TREADY_RE = re.compile(r"\b\w*m_axis_tready\w*\b", re.IGNORECASE)
_IF_RE = re.compile(r"\bif\b", re.IGNORECASE)
_END_IF_RE = re.compile(r"\bend\s+if\b", re.IGNORECASE)


def _no_strings(line: str) -> str:
    """The line with its string literals blanked, so a port name inside a
    report message does not count as a reference to the wire."""
    return _STRING_RE.sub('""', line)


def _dut_ref_re(names: list[str]) -> re.Pattern:
    """A reference to the DUT's own output wire (`m_axis_tdata`) or to its
    at-the-edge copy (`dut_m_axis_tdata`); never to a CSV row variable
    (`v_m_axis_tdata`), an expected-beat array, or a width attribute
    (`m_axis_tuser'length`, `m_axis_tdata'range`: a size, not the wire's
    value)."""
    alts = "|".join(re.escape(n) for n in names)
    return re.compile(rf"(?<![\w'])(?:dut_)?(?:{alts})\b(?!\s*')", re.IGNORECASE)


def _payload_ports(outputs: list[dict]) -> list[str]:
    """The output ports that carry a value per beat: every output except
    the handshake flags."""
    return [p["name"] for p in outputs if p["name"].lower() not in _OUTPUT_FLAGS]


def streaming_shape(spec: dict) -> dict | None:
    """For a streaming design (s_axis_tvalid AND m_axis_tvalid among the
    spec's ports) the wires the testbench has to handle, else None:
    {"has_m_axis_tready": bool, "has_s_axis_tready": bool, "payload": [...]}.
    The streaming checks below and the testbench writer's up-front hint
    (testbench_generator.streaming_hint) both use this one test, so the
    writer is told about exactly the designs the checker will hold to the
    beat-order rule."""
    ports = [p for p in (spec.get("ports") or []) if isinstance(p, dict) and p.get("name")]
    names = {str(p["name"]).lower() for p in ports}
    if not _STREAM_PORTS <= names:
        return None
    outputs = [p for p in ports if str(p.get("direction", "")).lower() == "output"]
    return {"has_m_axis_tready": "m_axis_tready" in names,
            "has_s_axis_tready": "s_axis_tready" in names,
            "payload": _payload_ports(outputs) or ["m_axis_tdata"]}


def _row_variable_compare(lines: list[str], ports: list[dict], payload: list[str]) -> str | None:
    """A compare of a DUT payload output (the wire or its dut_ copy) against
    the variable the current outputs ROW was read into
    (`if m_axis_tdata /= v_m_axis_tdata`,
    `if to_integer(unsigned(dut_m_axis_tuser)) /= to_integer(unsigned(v_m_axis_tuser))`),
    or None. Even inside the DUT's handshake, that is a cycle-by-cycle
    compare: the row's value belongs to the beat sent on that row, which the
    pipeline answers several cycles later. The beat-order pattern compares
    against an array element (`exp_beats(k_exp)`) instead, and only stores
    the row variable (`exp_beats(n) := v_m_axis_tdata`)."""
    text = "\n".join(lines)
    by_port: dict[str, set[str]] = {}
    for m in _READ_TARGET_RE.finditer(text):
        if m.group(2).lower() in ("c", "ok"):            # read_wide's own character reads
            continue
        port = _port_of(m.group(2), ports)
        if port in payload:
            by_port.setdefault(port, set()).add(m.group(2))
    for port, row_vars in by_port.items():
        var_re = re.compile(r"\b(?:" + "|".join(re.escape(v) for v in sorted(row_vars)) + r")\b", re.IGNORECASE)
        dut_re = _dut_ref_re([port])
        for line in lines:
            bare = _no_strings(line)
            if _CMP_RE.search(bare) and var_re.search(bare) and dut_re.search(bare):
                return line.strip()
    return None


def _csv_gated_compare(lines: list[str], payload: list[str]) -> str | None:
    """The `if <csv tvalid> = 1` line whose block compares a DUT payload
    output, or None. Collecting expected beats inside such a block
    (`exp_beats(n) := v_m_axis_tdata`) is the correct pattern and is not
    a compare."""
    dut_re = _dut_ref_re(payload)
    for start, line in enumerate(lines):
        if not _CSV_TVALID_GATE_RE.search(_no_strings(line)):
            continue
        depth = 0
        for inner in lines[start:]:
            bare = _no_strings(inner)
            depth += len(_IF_RE.findall(bare)) - 2 * len(_END_IF_RE.findall(bare))  # 'end if' also matches \bif\b
            if _CMP_RE.search(bare) and dut_re.search(bare):
                return line.strip()
            if depth <= 0:
                break
    return None


def _flag_cycle_compare(lines: list[str], flags: list[str]) -> str | None:
    """A line that compares a handshake flag (s_axis_tready, m_axis_tvalid)
    cycle by cycle with the CSV, or None: the CSV's integer flag and the
    DUT's std_logic flag in one condition
    (`if (m_axis_tvalid = '1') /= (v_m_axis_tvalid = 1)`,
    `if dut_m_axis_tvalid = '0' and v_m_axis_tvalid = 1`), or a report
    that calls the flag missing / unexpected / expected. timing_recovery,
    2026-10-30: the design chooses its own latency L, the golden model
    guessed another one, and the testbench reported "missing m_axis_tvalid"
    on every expected row and "unexpected m_axis_tvalid" on every real
    beat; the analyst blamed the RTL's strobe timing."""
    for flag in flags:
        csv_re = re.compile(rf"\b\w*{flag}\w*\s*=\s*[01]\b", re.IGNORECASE)
        dut_re = re.compile(rf"\b\w*{flag}\w*\s*=\s*'[01]'", re.IGNORECASE)
        # the keyword must sit NEXT to the flag ("missing m_axis_tvalid",
        # "m_axis_tvalid expected"): "unexpected output beat (m_axis_tvalid
        # high ...)" or "m_axis_tvalid beats: expected N" are legitimate
        # beat-order messages. The analyst's _FLAG_LINE reads the same forms.
        msg_re = re.compile(rf'"[^"]*(?:\b(?:missing|unexpected)\s+{flag}\b'
                            rf'|\b{flag}\s*:?\s+(?:expected|mismatch|differs|wrong)\b)[^"]*"', re.IGNORECASE)
        for line in lines:
            bare = _no_strings(line)
            if (csv_re.search(bare) and dut_re.search(bare)) or msg_re.search(line):
                return line.strip()
    return None


def _streaming_checks(spec: dict, code: str) -> list[dict]:
    """Streaming designs (s_axis_tvalid + m_axis_tvalid; the tready wires
    are optional) answer a few cycles after the input beat, and the vectors
    put the expected output on the SAME row as the input beat
    (matlab_golden_model_rules, STREAMING DESIGNS). A testbench that
    compares cycle by cycle therefore fails on every beat ("expected
    24_50_141 got 0_0_0" in the LLR runs; "missing m_axis_tvalid" on every
    expected row in timing_recovery). Require the beat-order pattern from
    vhdl_2008_testbench_rules instead."""
    shape = streaming_shape(spec)
    if shape is None:
        return []
    has_tready = shape["has_m_axis_tready"]
    payload = shape["payload"]
    lines = [line.split("--", 1)[0] for line in code.splitlines()]       # drop comments
    ports = [p for p in (spec.get("ports") or []) if isinstance(p, dict) and p.get("name")]
    names = {str(p["name"]).lower() for p in ports}
    flags = [f for f in _OUTPUT_FLAGS if f in names]
    consumed = ("m_axis_tvalid = '1' and m_axis_tready = '1'" if has_tready else
                "m_axis_tvalid = '1' (the spec has no m_axis_tready port: the downstream is always ready, so "
                "every such edge is a consumed beat)")
    rule = (f"STREAMING DESIGNS rule: collect the outputs rows with m_axis_tvalid = 1 as the expected beats "
            f"({', '.join(payload)} of each such row, one array per column), and compare them with the next "
            f"expected beat on every rising edge where the DUT's {consumed}; never on the cycle of the outputs "
            f"row, and never compare the s_axis_tready / m_axis_tvalid columns themselves cycle by cycle.")
    issues: list[dict] = []
    gate = _csv_gated_compare(lines, payload)
    if gate:
        issues.append({"code": "streaming_cycle_compare",
                       "detail": f"'{gate}' compares the output on the cycle the CSV row says, but the "
                                 f"pipeline answers several cycles later, so every beat mismatches. {rule}"})
    row_cmp = _row_variable_compare(lines, ports, payload)
    if row_cmp:
        issues.append({"code": "streaming_row_compare",
                       "detail": f"'{row_cmp}' compares a DUT output with the value read from the CURRENT "
                                 f"outputs row. That row holds the result of the beat sent on that same row, which "
                                 f"the pipeline delivers several cycles later, so the output is checked against the "
                                 f"wrong beat (seen as 'expected X got 0' and later 'got X' on another cycle). "
                                 f"Store the row's value in an array of expected beats and compare against "
                                 f"exp_beats(k_exp). {rule}"})
    if not has_tready:
        stray = next((line.strip() for line in lines if _M_AXIS_TREADY_RE.search(_no_strings(line))), None)
        if stray:
            issues.append({"code": "no_m_axis_tready_port",
                           "detail": f"'{stray}' uses m_axis_tready, but the spec has no such port (the downstream "
                                     f"is always ready), so this does not compile. Remove every m_axis_tready "
                                     f"signal, variable and column: a beat is consumed on every rising edge where "
                                     f"the DUT's m_axis_tvalid = '1'. {rule}"})
    if not (_HANDSHAKE_RE if has_tready else _VALID_ONLY_RE).search(chr(10).join(lines)):
        issues.append({"code": "streaming_no_handshake_compare",
                       "detail": f"No compare guarded by the DUT's handshake ({consumed}) was found. {rule}"})
    flag = _flag_cycle_compare(lines, flags)
    if flag:
        issues.append({"code": "streaming_flag_cycle_compare",
                       "detail": f"'{flag}' compares a handshake flag with the CSV cycle by cycle. The cycle on "
                                 f"which the design raises m_axis_tvalid (its latency, its strobe pattern) and "
                                 f"what s_axis_tready shows during reset belong to the RTL and are not in the "
                                 f"CSV: the CSV's m_axis_tvalid column only marks WHICH outputs rows hold an "
                                 f"expected beat, in order (timing_recovery, 2026-10-30: 'missing m_axis_tvalid' "
                                 f"on every expected row, 'unexpected m_axis_tvalid' on every real beat, and the "
                                 f"RTL was blamed). Never report 'missing/unexpected m_axis_tvalid' or "
                                 f"'<flag> expected X got Y' per cycle; read v_s_axis_tready and v_m_axis_tvalid "
                                 f"only to find the expected beats. {rule}"})
    idle = _idle_row_hold_issue(lines)
    if idle:
        issues.append(idle)
    field = _field_source_mismatch(lines, payload)
    if field:
        issues.append(field)
    return issues


_FIELD_ASSIGN_RE = re.compile(r"\b(got|exp)_(\w+)\s*:=\s*(.+)", re.IGNORECASE | re.DOTALL)
_SLICE_RE = re.compile(r"\(\s*(\d+)\s+downto\s+(\d+)\s*\)", re.IGNORECASE)


def _field_source_mismatch(lines: list[str], payload: list[str]) -> dict | None:
    """A decoded field whose expected value is read from another place than
    the DUT value it is compared with: `got_q := ...dut_m_axis_tdata(11 downto 0)`
    next to `exp_q := ...exp_tuser(k_exp)(7 downto 0)`. timing_recovery,
    2026-11-11: expected Q was read from the mu column, so every compared
    beat reported a Q mismatch on an RTL that could have been correct."""
    short = {p: p.lower().rsplit("_", 1)[-1] for p in payload}         # m_axis_tdata -> tdata

    def source(rhs: str) -> tuple[str | None, tuple[int, int] | None]:
        words = re.findall(r"[a-z_]\w*", rhs.lower())
        cols = [p for p, s in short.items() if any(s in w for w in words)]
        cut = _SLICE_RE.search(rhs)
        return (cols[0] if len(cols) == 1 else None), ((int(cut.group(1)), int(cut.group(2))) if cut else None)

    seen: dict[tuple[str, str], str] = {}
    for stmt in "\n".join(lines).split(";"):
        m = _FIELD_ASSIGN_RE.search(_no_strings(stmt))
        if m:
            seen.setdefault((m.group(1).lower(), m.group(2).lower()), " ".join(m.group(3).split()))
    for (kind, name), got_rhs in seen.items():
        exp_rhs = seen.get(("exp", name))
        if kind != "got" or exp_rhs is None:
            continue
        got_col, got_cut = source(got_rhs)
        exp_col, exp_cut = source(exp_rhs)
        wrong_col = got_col and exp_col and got_col != exp_col
        wrong_cut = got_cut and exp_cut and got_cut != exp_cut
        if wrong_col or wrong_cut:
            fix_col = short.get(got_col, "tdata") if got_col else "tdata"
            fix_cut = f"({got_cut[0]} downto {got_cut[1]})" if got_cut else ""
            return {"code": "field_source_mismatch",
                    "detail": f"'exp_{name} := {exp_rhs}' reads the expected value from another place than "
                              f"'got_{name} := {got_rhs}' reads the DUT value, so the compare of {name} fails on "
                              f"every beat even when the RTL is right. Both sides of one field must come from the "
                              f"same output column and the same bit range: read exp_{name} from "
                              f"exp_{fix_col}(k_exp){fix_cut}."}
    return None


# The bounded "hold the row until s_axis_tready" loop (its deadlock report
# or its edge counter) and the two ways an idle row may leave it early.
_HOLD_LOOP_RE = re.compile(r"never\s+rose|stall_count|hold_count", re.IGNORECASE)
_IDLE_ROW_TEST_RE = re.compile(r"\b\w*s_axis_tvalid\w*\s*(?:=\s*'?0'?|/=\s*'?1'?)(?![\w'])", re.IGNORECASE)
_BEAT_ROW_GATE_RE = re.compile(r"\bif\s+\w*s_axis_tvalid\w*\s*=\s*'?1'?\s+then\b", re.IGNORECASE)
_LEAVE_RE = re.compile(r"\bexit\b|\bwait\s+until\b", re.IGNORECASE)
_LOOP_RE = re.compile(r"\bloop\b", re.IGNORECASE)


def _idle_row_hold_issue(lines: list[str]) -> dict | None:
    """A hold loop that an idle row (s_axis_tvalid = 0) cannot leave early.
    Such a row has no beat to accept, so waiting for s_axis_tready is
    pointless: the loop runs its 64-edge limit and reports "s_axis_tready
    never rose (deadlock)" for every reset row, gap row and trailing row
    (llr_demap16, 2026-10-29: cycles 0-1 and 50-59), and the analyst
    blamed the RTL for a testbench fault."""
    if not any(_HOLD_LOOP_RE.search(line) for line in lines):
        return None                                     # no bounded hold loop: nothing to leave
    for i, line in enumerate(lines):
        window = "\n".join(lines[i:i + 3])
        if _IDLE_ROW_TEST_RE.search(line) and _LEAVE_RE.search(window):
            return None                                 # `if tvalid = 0 or tready = '1' then exit`
        if _BEAT_ROW_GATE_RE.search(line) and _LOOP_RE.search(window):
            return None                                 # `if tvalid = 1 then loop ... end loop; else one edge`
    return {"code": "hold_on_idle_row",
            "detail": "The hold loop ('s_axis_tready never rose') has no early exit for an idle row "
                      "(s_axis_tvalid = 0). Such a row carries no beat, so the loop waits its full limit on every "
                      "reset row, gap row and row after the last beat and reports a deadlock that is not there. "
                      "Make the loop's exit line `if cur_s_axis_tvalid = 0 or dut_s_axis_tready = '1' then exit; "
                      "end if;` so an idle row lasts exactly one edge (STREAMING DESIGNS rule)."}


def _compile(spec: dict, rtl: dict, testbench: dict) -> tuple[str | None, list[dict]]:
    """Write DUT and testbench through the file-server MCP, then compile the
    DUT (VHDL-93) and the testbench (VHDL-2008) into the same work library
    with the Vivado MCP server's run_xvhdl. Returns (compiler, issues);
    compiler is None when Vivado is unavailable (static checks only)."""
    dut = rtl.get("entity_name") or spec.get("entity_name") or "design"
    tb_name = testbench.get("entity_name") or f"{dut}_tb"
    # DUT in the spec's edition (agents/vhdl_version.py); testbench always 2008.
    sources = [(f"{dut}.vhd", xvhdl_std(resolve_vhdl_version(spec)), "DUT", rtl.get("code") or ""),
               (f"{tb_name}.vhd", "2008", "testbench", testbench.get("code") or "")]
    with tempfile.TemporaryDirectory() as tmp:
        mcp_write_files(tmp, {name: code for name, _, _, code in sources})
        results = mcp_run_xvhdl(tmp, [(name, std) for name, std, _, _ in sources])

    issues: list[dict] = []
    for (name, _, label, code), result in zip(sources, results):
        if result.get("error_class") == "environment":
            return None, issues
        output = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
        issues += compiler_errors(output, code, 0 if result.get("status") == "ok" else 1, label)
    return "xvhdl", issues


def lint_testbench(spec: dict, rtl: dict, testbench: dict) -> dict:
    """Returns {"compiler": "xvhdl" | None, "issues": [{code, detail}]}.
    compiler is None when only the static checks could run (Vivado or the
    MCP servers unavailable)."""
    issues = _static_checks(spec, rtl, testbench)
    compiler = None
    if testbench.get("code") and rtl.get("code"):
        try:
            compiler, compile_issues = _compile(spec, rtl, testbench)
            issues += compile_issues
        except Exception as e:  # MCP server could not start / write failed
            issues.append({"code": "compiler_failed", "detail": f"Compiler could not run: {e}"})
    return {"compiler": compiler, "issues": issues}

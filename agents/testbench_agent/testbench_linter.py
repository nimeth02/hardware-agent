# agents/testbench_agent/testbench_linter.py — lint check for a generated
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
        expected = sorted([1 + len(inputs), 1 + len(outputs)])
        got = sorted(counts.values(), reverse=True)[:2]
        if counts and sorted(got) != expected:
            in_cols = ", ".join(["cycle"] + [p["name"] for p in inputs])
            out_cols = ", ".join(["cycle"] + [p["name"] for p in outputs])
            issues.append({"code": "column_count_mismatch",
                           "detail": f"Reads per line variable: {counts}. test_vectors_in.csv has exactly these "
                                     f"{1 + len(inputs)} columns: [{in_cols}] and test_vectors_out.csv exactly these "
                                     f"{1 + len(outputs)}: [{out_cols}] (direction from the spec, NOT the name: "
                                     f"s_axis_tready is an output, m_axis_tready an input). Read each row's columns "
                                     f"in that order, one read per column, even ones you do not use."})

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


_STREAM_PORTS = {"s_axis_tvalid", "s_axis_tready", "m_axis_tvalid", "m_axis_tready"}
# `if v_m_axis_tvalid = 1`: the CSV's flag (an integer) opens a block ...
_CSV_TVALID_GATE_RE = re.compile(r"\bif\s+\w*m_axis_tvalid\w*\s*=\s*1\b", re.IGNORECASE)
# ... and inside it the DUT's bare m_axis_tdata wire is compared: cycle-by-cycle.
_DUT_TDATA_CMP_RE = re.compile(r"(?<![\w])m_axis_tdata\s*/?=|/?=\s*m_axis_tdata\b", re.IGNORECASE)
# `m_axis_tvalid = '1' and m_axis_tready = '1'`: the DUT's own handshake decides.
_HANDSHAKE_RE = re.compile(r"\bm_axis_tvalid\s*=\s*'1'\s+and\s+m_axis_tready\s*=\s*'1'"
                           r"|\bm_axis_tready\s*=\s*'1'\s+and\s+m_axis_tvalid\s*=\s*'1'", re.IGNORECASE)
_IF_RE = re.compile(r"\bif\b", re.IGNORECASE)
_END_IF_RE = re.compile(r"\bend\s+if\b", re.IGNORECASE)


def _csv_gated_compare(lines: list[str]) -> str | None:
    """The `if <csv tvalid> = 1` line whose block compares the DUT's
    m_axis_tdata, or None. Collecting expected beats inside such a block
    (`exp_beats(n) := v_m_axis_tdata`) is the correct pattern and is not
    a compare."""
    for start, line in enumerate(lines):
        if not _CSV_TVALID_GATE_RE.search(line):
            continue
        depth = 0
        for inner in lines[start:]:
            depth += len(_IF_RE.findall(inner)) - 2 * len(_END_IF_RE.findall(inner))  # 'end if' also matches \bif\b
            if _DUT_TDATA_CMP_RE.search(inner):
                return line.strip()
            if depth <= 0:
                break
    return None


def _streaming_checks(spec: dict, code: str) -> list[dict]:
    """Streaming designs (AXI-Stream handshake ports) answer a few cycles
    after the input beat, and the vectors put the expected output on the
    SAME row as the input beat (matlab_golden_model_rules, STREAMING
    DESIGNS). A testbench that compares cycle by cycle therefore fails on
    every beat ("expected 24_50_141 got 0_0_0" in the LLR runs). Require
    the beat-order pattern from vhdl_2008_testbench_rules instead."""
    names = {str(p.get("name", "")).lower() for p in (spec.get("ports") or []) if isinstance(p, dict)}
    if not _STREAM_PORTS <= names:
        return []
    lines = [line.split("--", 1)[0] for line in code.splitlines()]       # drop comments
    rule = ("STREAMING DESIGNS rule: collect the outputs rows with m_axis_tvalid = 1 as the expected beats, "
            "and compare m_axis_tdata with the next expected beat on every rising edge where the DUT's "
            "m_axis_tvalid = '1' and m_axis_tready = '1'; never on the cycle of the outputs row.")
    issues: list[dict] = []
    gate = _csv_gated_compare(lines)
    if gate:
        issues.append({"code": "streaming_cycle_compare",
                       "detail": f"'{gate}' compares m_axis_tdata on the cycle the CSV row says, but the "
                                 f"pipeline answers several cycles later, so every beat mismatches. {rule}"})
    if not _HANDSHAKE_RE.search(chr(10).join(lines)):
        issues.append({"code": "streaming_no_handshake_compare",
                       "detail": f"No compare guarded by the DUT's handshake (m_axis_tvalid = '1' and "
                                 f"m_axis_tready = '1') was found. {rule}"})
    return issues


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

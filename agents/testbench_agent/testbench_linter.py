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

    for m in _SELF_CMP_RE.finditer(text):
        if m.group(1).lower() == m.group(2).lower():
            issues.append({"code": "self_comparison",
                           "detail": f"'if {m.group(1)} /= {m.group(2)}' compares a variable with itself; read the "
                                     f"outputs-row cycle into a second variable (v_cycle_out) and compare the two."})
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

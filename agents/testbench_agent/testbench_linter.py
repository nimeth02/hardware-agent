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

    return issues


def _compile(spec: dict, rtl: dict, testbench: dict) -> tuple[str | None, list[dict]]:
    """Write DUT and testbench through the file-server MCP, then compile the
    DUT (VHDL-93) and the testbench (VHDL-2008) into the same work library
    with the Vivado MCP server's run_xvhdl. Returns (compiler, issues);
    compiler is None when Vivado is unavailable (static checks only)."""
    dut = rtl.get("entity_name") or spec.get("entity_name") or "design"
    tb_name = testbench.get("entity_name") or f"{dut}_tb"
    sources = [(f"{dut}.vhd", "93", "DUT", rtl.get("code") or ""),
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

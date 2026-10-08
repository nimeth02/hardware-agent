# agents/rtl_agent/rtl_linter.py — lint check for generated VHDL RTL.
#
# Static checks always run (deterministic, no tools needed): entity
# present, name matches the spec, architecture present, every spec port
# referenced, no placeholders. Then, when Vivado's xvhdl is reachable, the
# code is compiled for real syntax errors.

from __future__ import annotations

import re
import tempfile

from agents.lint_utils import compiler_errors
from agents.mcp_utils import mcp_run_xvhdl, mcp_write_files

_ENTITY_RE = re.compile(r"\bentity\s+(\w+)\s+is", re.IGNORECASE)


def _static_checks(spec: dict, rtl: dict) -> list[dict]:
    issues: list[dict] = []

    def issue(code: str, detail: str) -> None:
        issues.append({"code": code, "detail": detail})

    code = rtl.get("code") or ""
    expected = spec.get("entity_name")

    if not code.strip():
        issue("empty_code", "No RTL code was returned.")
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
    elif expected and m.group(1) != expected:
        issue("name_mismatch", f"Entity is '{m.group(1)}' but spec entity_name is '{expected}'.")
    if not re.search(r"\barchitecture\b", code, re.IGNORECASE):
        issue("no_architecture", "No architecture body found.")

    for p in spec.get("ports") or []:
        name = p.get("name") if isinstance(p, dict) else None
        if name and not re.search(rf"\b{re.escape(name)}\b", code):
            issue("missing_port", f"Spec port '{name}' does not appear in the code.")

    return issues


def _compile(code: str, entity: str | None) -> tuple[str | None, list[dict]]:
    """Write the design through the file-server MCP and compile it with the
    Vivado MCP server's run_xvhdl. Returns (compiler, issues); compiler is
    None when Vivado is unavailable (static checks only)."""
    name = f"{entity or 'design'}.vhd"
    with tempfile.TemporaryDirectory() as tmp:
        mcp_write_files(tmp, {name: code})
        result = mcp_run_xvhdl(tmp, [(name, "93")])[0]
    if result.get("error_class") == "environment":
        return None, []
    output = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    return "xvhdl", compiler_errors(output, code, 0 if result.get("status") == "ok" else 1)


def lint_rtl(spec: dict, rtl: dict) -> dict:
    """Returns {"compiler": "xvhdl" | None, "issues": [{code, detail}]}.
    compiler is None when only the static checks could run (Vivado or the
    MCP servers unavailable)."""
    issues = _static_checks(spec, rtl)
    compiler = None
    if rtl.get("code"):
        try:
            compiler, compile_issues = _compile(rtl["code"], spec.get("entity_name") or rtl.get("entity_name"))
            issues += compile_issues
        except Exception as e:  # MCP server could not start / write failed
            issues.append({"code": "compiler_failed", "detail": f"Compiler could not run: {e}"})
    return {"compiler": compiler, "issues": issues}

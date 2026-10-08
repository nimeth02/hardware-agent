# agents/lint_utils.py — turn raw xvhdl output into stable, actionable
# lint issues (shared by the RTL and testbench linters).

from __future__ import annotations

import re

# "[C:/Users/.../tmpab12cd/counter4_tb.vhd:30]" -> "[counter4_tb.vhd:30]"
_TEMP_PATH_RE = re.compile(r"\[(?:[A-Za-z]:)?[^\[\]]*?[\\/]([^\\/\[\]]+:\d+)\]")
_LINE_REF_RE = re.compile(r"\[[^\[\]]*?:(\d+)\]")


def compiler_errors(output: str, source: str, returncode: int, label: str | None = None) -> list[dict]:
    """ERROR lines from a compiler run as {"code": "compile_error", "detail"}.

    The temp-directory path is stripped so identical errors produce identical
    details (the agents' stall detection fingerprints them), and the quoted
    source line is appended so the fixing LLM sees exactly what to change.
    """
    lines = source.splitlines()
    errors: list[str] = []
    for raw in output.splitlines():
        text = raw.strip()
        if not text.startswith("ERROR"):
            continue
        text = _TEMP_PATH_RE.sub(r"[\1]", text)
        m = _LINE_REF_RE.search(text)
        if m and 0 < int(m.group(1)) <= len(lines):
            n = int(m.group(1))
            text += f"  >> line {n}: {lines[n - 1].strip()}"
        errors.append(text)
    if returncode != 0 and not errors:
        errors.append(f"Compiler exited with code {returncode}.")
    prefix = f"{label}: " if label else ""
    return [{"code": "compile_error", "detail": prefix + e} for e in errors]

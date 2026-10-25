# agents/lint_utils.py — turn raw xvhdl output into stable, actionable
# lint issues (shared by the RTL and testbench linters).

from __future__ import annotations

import re

# "[C:/Users/.../tmpab12cd/counter4_tb.vhd:30]" -> "[counter4_tb.vhd:30]"
_TEMP_PATH_RE = re.compile(r"\[(?:[A-Za-z]:)?[^\[\]]*?[\\/]([^\\/\[\]]+:\d+)\]")
_LINE_REF_RE = re.compile(r"\[[^\[\]]*?:(\d+)\]")


_QUOTED_RE = re.compile(r"'(\w+)'")
# One bad declaration or one unbalanced line makes xvhdl report a chain of
# errors about the same symbols and the statements around them. Those kinds
# are never the root cause; the first error of the region is.
_FOLLOW_ON_RE = re.compile(
    r"is not a variable|is illegal in an expression|is not declared|is not a signal"
    r"|syntax error near '(?:begin|end|if|then|else|;|\)|,)'", re.IGNORECASE)
CASCADE_WINDOW = 40          # lines after a kept error within which follow-ons are hidden


def collapse_follow_on_errors(errors: list[tuple[int | None, str]]) -> tuple[list[str], int]:
    """Keep the root compiler errors, hide their follow-ons. `errors` are
    (line number or None, text) in compiler order. An error is a follow-on
    when it comes within CASCADE_WINDOW lines of the previous error (so a
    dense chain counts as one region) and either has a follow-on wording or
    names a symbol quoted in a kept error. Returns (kept texts, number
    hidden). Identical texts are kept once."""
    kept: list[str] = []
    seen: set[str] = set()
    symbols: set[str] = set()
    hidden = 0
    previous: int | None = None
    for line, text in errors:
        if text in seen:
            hidden += 1
            continue
        near = line is not None and previous is not None and 0 <= line - previous <= CASCADE_WINDOW
        previous = line if line is not None else previous
        names = set(_QUOTED_RE.findall(text))
        if near and (_FOLLOW_ON_RE.search(text) or names & symbols):
            hidden += 1
            continue
        kept.append(text)
        seen.add(text)
        symbols |= names
    return kept, hidden


def compiler_errors(output: str, source: str, returncode: int, label: str | None = None) -> list[dict]:
    """ERROR lines from a compiler run as {"code": "compile_error", "detail"}.

    The temp-directory path is stripped so identical errors produce identical
    details (the agents' stall detection fingerprints them), the quoted source
    line is appended so the fixing LLM sees exactly what to change, and
    follow-on errors are hidden so it fixes the root causes first.
    """
    lines = source.splitlines()
    errors: list[tuple[int | None, str]] = []
    for raw in output.splitlines():
        text = raw.strip()
        if not text.startswith("ERROR"):
            continue
        text = _TEMP_PATH_RE.sub(r"[\1]", text)
        m = _LINE_REF_RE.search(text)
        n = int(m.group(1)) if m else None
        if n is not None and 0 < n <= len(lines):
            text += f"  >> line {n}: {lines[n - 1].strip()}"
        errors.append((n, text))
    kept, hidden = collapse_follow_on_errors(errors)
    if hidden:
        kept.append(f"{hidden} follow-on error(s) hidden: they come from the error(s) above "
                    f"(a bad declaration or an unbalanced statement breaks everything after it). "
                    f"Fix those first; the code is compiled again afterwards.")
    if returncode != 0 and not kept:
        kept.append(f"Compiler exited with code {returncode}.")
    prefix = f"{label}: " if label else ""
    return [{"code": "compile_error", "detail": prefix + e} for e in kept]

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
from agents.rtl_agent.rtl_smoke_check import smoke_issues
from agents.vhdl_version import VHDL_93, resolve_vhdl_version, xvhdl_std

_ENTITY_RE = re.compile(r"\bentity\s+(\w+)\s+is", re.IGNORECASE)

# Constructs the rulebooks forbid, caught before the compiler so the model
# gets a plain "forbidden" message instead of a cryptic VRFC error.
_FORBIDDEN_EVERYWHERE = [
    (re.compile(r"\buse\s+ieee\.std_logic_(arith|unsigned|signed)\b", re.IGNORECASE),
     "deprecated Synopsys library ieee.std_logic_{0}; use ieee.numeric_std"),
    # xvhdl accepts a comparison against an (others => ...) aggregate; xelab
    # refuses it ("OTHERS is illegal aggregate choice for unconstrained
    # target"), long after lint. Only assignments may use the aggregate.
    (re.compile(r"(?:(?<![<:>/])=|/=)\s*\(\s*others\s*=>", re.IGNORECASE),   # not <= or := assignments
     "comparing with (others => ...) is refused by xelab (no width); compare an unsigned with 0 "
     "or a vector with a sized literal such as \"00000000\""),
]
_FORBIDDEN_IN_93 = [
    (re.compile(r"\bprocess\s*\(\s*all\s*\)", re.IGNORECASE),
     "process(all) is VHDL-2008 only; list the signals explicitly"),
    (re.compile(r"\bstd_ulogic_vector\b", re.IGNORECASE),
     "std_ulogic_vector is VHDL-2008 only; use std_logic_vector"),
]


def _forbidden_constructs(code: str, version: str) -> list[dict]:
    rules = list(_FORBIDDEN_EVERYWHERE) + (list(_FORBIDDEN_IN_93) if version == VHDL_93 else [])
    issues = []
    for n, line in enumerate(code.splitlines(), 1):
        stripped = line.split("--", 1)[0]          # ignore comments
        for pattern, message in rules:
            m = pattern.search(stripped)
            if m:
                issues.append({"code": "forbidden_construct",
                               "detail": f"line {n}: {message.format(*m.groups())} >> {line.strip()}"})
    return issues


# "signal|variable NAME[, NAME] : unsigned|signed|std_logic_vector(H downto L)"
_DECL_RE = re.compile(
    r"\b(?:signal|variable)\s+([\w\s,]+?)\s*:\s*(?:unsigned|signed|std_logic_vector)"
    r"\s*\(\s*(\d+)\s+downto\s+(\d+)\s*\)", re.IGNORECASE)
# "target <= expr;" or "target := expr;" (may span lines; comments already removed)
_ASSIGN_RE = re.compile(r"(?:^|;)\s*(\w+)\s*(?:<=|:=)([^;]+)(?=;)", re.IGNORECASE)
_CALL_RE = re.compile(r"(\w+)\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_SLICE_RE = re.compile(r"(\w+)\s*\(\s*(\d+)\s+(?:downto|to)\s+(\d+)\s*\)$", re.IGNORECASE)


def _split_top(expr: str, op: str) -> list[str]:
    """Split on `op` outside any brackets."""
    parts, depth, cur = [], 0, ""
    for ch in expr:
        depth += (ch == "(") - (ch == ")")
        if ch == op and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return parts + [cur]


def _expr_width(expr: str, widths: dict[str, int]) -> int | None:
    """Bit width of a simple VHDL expression, or None when it can't be worked out."""
    expr = expr.strip()
    if not expr:
        return None
    pieces = _split_top(expr, "&")
    if len(pieces) > 1:
        sizes = [_expr_width(p, widths) for p in pieces]
        return None if None in sizes else sum(sizes)
    if re.fullmatch(r"'.'", expr):
        return 1
    if re.fullmatch(r'"[^"]*"', expr):
        return len(expr) - 2
    if expr.lower() in widths:
        return widths[expr.lower()]
    m = _SLICE_RE.match(expr)
    if m:
        return abs(int(m.group(2)) - int(m.group(3))) + 1
    if expr.startswith("(") and expr.endswith(")") and len(_split_top(expr[1:-1], "&")) >= 1:
        inner = expr[1:-1]
        if inner.count("(") == inner.count(")"):
            return _expr_width(inner, widths)
    m = _CALL_RE.match(expr)
    if m:
        func, args = m.group(1).lower(), _split_top(m.group(2), ",")
        if func in ("resize", "to_signed", "to_unsigned") and len(args) == 2 and args[1].strip().isdigit():
            return int(args[1])
        if func in ("signed", "unsigned", "std_logic_vector") and len(args) == 1:
            return _expr_width(args[0], widths)
    return None


def _multiplier_width_issues(code: str) -> list[dict]:
    """numeric_std gives a product as wide as BOTH inputs added together;
    xvhdl compiles a mismatched target, but the simulation then stops with
    'array size mismatch'. Checks `target <= A * B;` where the width of A and
    B can be worked out (names, resize, signed/unsigned, '&', slices)."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    widths: dict[str, int] = {}
    for m in _DECL_RE.finditer(stripped):
        for name in m.group(1).split(","):
            widths[name.strip().lower()] = int(m.group(2)) - int(m.group(3)) + 1
    issues = []
    for m in _ASSIGN_RE.finditer(stripped):
        target, expr = m.group(1).lower(), m.group(2)
        factors = _split_top(expr, "*")
        if len(factors) != 2 or target not in widths:
            continue
        wa, wb = _expr_width(factors[0], widths), _expr_width(factors[1], widths)
        if wa is None or wb is None or widths[target] == wa + wb:
            continue
        n = stripped.count("\n", 0, m.start(1)) + 1
        issues.append({"code": "multiplier_width",
                       "detail": f"line {n}: {factors[0].strip()} ({wa} bits) * {factors[1].strip()} ({wb} bits) "
                                 f"is {wa + wb} bits wide, but '{target}' is {widths[target]} bits; "
                                 f"make '{target}' {wa + wb} bits, then resize() it afterwards. Do NOT widen "
                                 f"the factors to match the target: resize() around a factor makes the product "
                                 f"wider again (the result is always the sum of the factor widths). Multiply "
                                 f"the factors at their own natural widths and resize only the product"})
    return issues


_PROCESS_RE = re.compile(r"\bprocess\b.*?\bend\s+process\b\s*\w*\s*;", re.IGNORECASE | re.DOTALL)
# a whole-signal assignment "name <=" at the start of a statement (not "if a <= b")
_TARGET_RE = re.compile(r"(?:^|;|\bthen\b|\belse\b|\bbegin\b|=>)\s*(\w+)\s*<=", re.IGNORECASE | re.MULTILINE)


def _multiple_driver_issues(code: str) -> list[dict]:
    """A signal written from two places (two processes, or a process and a
    plain `x <= ...;` line) has two drivers; unsigned/signed signals then stop
    the simulation. Only whole-signal writes are compared."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    drivers: dict[str, list[str]] = {}
    for n, m in enumerate(_PROCESS_RE.finditer(stripped), 1):
        for name in {t.lower() for t in _TARGET_RE.findall(m.group(0))}:
            drivers.setdefault(name, []).append(f"process #{n}")
    outside = _PROCESS_RE.sub(lambda m: "\n" * m.group(0).count("\n"), stripped)
    for m in _TARGET_RE.finditer(outside):
        line = outside.count("\n", 0, m.start(1)) + 1
        drivers.setdefault(m.group(1).lower(), []).append(f"line {line}")
    return [{"code": "multiple_drivers",
             "detail": f"'{name}' is written in {len(where)} places ({', '.join(where)}); "
                       f"write each signal from exactly one process or one assignment"}
            for name, where in drivers.items() if len(where) > 1]


# "label: process (...)" up to its "begin": the declarative region where only
# variable/constant/... declarations may appear.
_PROCESS_DECLS_RE = re.compile(r"\bprocess\b(?:\s*\([^)]*\))?(.*?)\bbegin\b", re.IGNORECASE | re.DOTALL)
_DECL_KEYWORDS = ("variable", "constant", "alias", "type", "subtype", "file", "attribute", "function",
                  "procedure", "impure", "pure", "use", "shared")
_BARE_DECL_RE = re.compile(r"^\s*(\w+(?:\s*,\s*\w+)*)\s*:\s*(?!=)(\w+)", re.IGNORECASE)


def _process_declaration_issues(code: str) -> list[dict]:
    """Inside a process, `temp : signed(47 downto 0);` without the `variable`
    keyword is a syntax error that xvhdl follows with a dozen 'is not a
    variable' / 'illegal in an expression' errors. Caught here with the fix."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    issues = []
    for m in _PROCESS_DECLS_RE.finditer(stripped):
        start_line = stripped.count("\n", 0, m.start(1)) + 1
        for offset, line in enumerate(m.group(1).splitlines()):
            d = _BARE_DECL_RE.match(line)
            if d and d.group(1).split(",")[0].strip().lower() not in _DECL_KEYWORDS:
                n = start_line + offset
                issues.append({"code": "missing_variable_keyword",
                               "detail": f"line {n}: '{d.group(1).strip()}' is declared inside a process without the "
                                         f"`variable` keyword; write `variable {d.group(1).strip()} : ...;` "
                                         f">> {line.strip()}"})
    return issues


_STRING_OR_CHAR_RE = re.compile(r"\"[^\"\n]*\"|'.'")


def _unbalanced_parentheses_issues(code: str) -> list[dict]:
    """A statement whose '(' and ')' do not match (common in long resize /
    shift_left arithmetic chains) is a syntax error xvhdl reports as a vague
    'syntax error near ;'. A statement ends at a ';' OUTSIDE parentheses, so
    a port list `port ( a : in ...; b : out ... );` is one statement. Reported
    once per bad statement with the counts."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    # count on a copy with string/character literals blanked (same length), quote from the original
    blanked = _STRING_OR_CHAR_RE.sub(lambda m: " " * len(m.group(0)), stripped)
    issues = []

    def report(start: int) -> None:
        segment = blanked[start:]
        end = segment.find(";")
        segment = segment if end < 0 else segment[:end]
        lead = len(segment) - len(segment.lstrip())
        n = stripped.count("\n", 0, start + lead) + 1
        original = stripped[start + lead:start + len(segment)].strip()
        first = original.splitlines()[0].strip() if original else ""
        issues.append({"code": "unbalanced_parentheses",
                       "detail": f"line {n}: statement has {segment.count('(')} '(' but {segment.count(')')} ')'; "
                                 f"split it into one arithmetic step per line with named intermediates >> {first}"})

    depth, start = 0, 0
    for i, ch in enumerate(blanked):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:                      # a ')' with no '(' before it in this statement
                report(start)
                depth = 0
        elif ch == ";" and depth == 0:         # statement complete and balanced
            start = i + 1
    if depth > 0:                              # a '(' never closed: the statement that opened it is the culprit
        report(start)
    return issues


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


def _compile(code: str, entity: str | None, version: str) -> tuple[str | None, list[dict]]:
    """Write the design through the file-server MCP and compile it with the
    Vivado MCP server's run_xvhdl in the spec's VHDL edition. Returns
    (compiler, issues); compiler is None when Vivado is unavailable (static
    checks only)."""
    name = f"{entity or 'design'}.vhd"
    with tempfile.TemporaryDirectory() as tmp:
        mcp_write_files(tmp, {name: code})
        result = mcp_run_xvhdl(tmp, [(name, xvhdl_std(version))])[0]
    if result.get("error_class") == "environment":
        return None, []
    output = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    return "xvhdl", compiler_errors(output, code, 0 if result.get("status") == "ok" else 1)


def lint_rtl(spec: dict, rtl: dict) -> dict:
    """Returns {"compiler": "xvhdl" | None, "issues": [{code, detail}]}.
    compiler is None when only the static checks could run (Vivado or the
    MCP servers unavailable)."""
    version = resolve_vhdl_version(spec)
    issues = _static_checks(spec, rtl)
    compiler = None
    if rtl.get("code"):
        issues += _forbidden_constructs(rtl["code"], version)
        issues += _process_declaration_issues(rtl["code"])
        issues += _unbalanced_parentheses_issues(rtl["code"])
        issues += _multiplier_width_issues(rtl["code"])
        issues += _multiple_driver_issues(rtl["code"])
        try:
            compiler, compile_issues = _compile(rtl["code"], spec.get("entity_name") or rtl.get("entity_name"),
                                                version)
            issues += compile_issues
        except Exception as e:  # MCP server could not start / write failed
            issues.append({"code": "compiler_failed", "detail": f"Compiler could not run: {e}"})
        if compiler and not issues:     # only a design that is otherwise clean gets the slower dry run
            issues += smoke_issues(spec, rtl["code"])
    return {"compiler": compiler, "issues": issues}

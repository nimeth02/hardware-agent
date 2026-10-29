# agents/rtl_agent/rtl_widths.py — bit widths of simple VHDL expressions.
#
# Shared by the linter (static width checks) and the dry run (naming the
# line behind an "Array sizes do not match" crash). numeric_std rules:
#   a * b        -> width(a) + width(b)     (a * 4: integer factor counts as width(a), so 2*width(a))
#   a + b, a - b -> max(width(a), width(b)) (an integer operand: the vector's width)
#   a & b        -> width(a) + width(b)
#   resize(a, N), to_signed(v, N), to_unsigned(v, N) -> N
#   shift_left/right(a, n), abs a, not a, signed(a), unsigned(a), std_logic_vector(a) -> width(a)
# Anything else (function calls, aggregates, records) is "unknown" (None) and
# never reported.

from __future__ import annotations

import re
from typing import Iterator

# "signal|variable NAME[, NAME] : unsigned|signed|std_logic_vector(H downto L)"
DECL_RE = re.compile(
    r"\b(?:signal|variable)\s+([\w\s,]+?)\s*:\s*(?:unsigned|signed|std_logic_vector)"
    r"\s*\(\s*(\d+)\s+downto\s+(\d+)\s*\)", re.IGNORECASE)
# "target <= expr;" / "target := expr;" at the start of a statement: after ';',
# 'then', 'else', 'begin', 'loop' or a line start. Indexed targets (x(3) <= ...)
# are left out: their width is not the declared one.
ASSIGN_RE = re.compile(
    r"(?:^|;|\bthen\b|\belse\b|\bbegin\b|\bloop\b)\s*(\w+)\s*(<=|:=)\s*([^;]+)(?=;)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE)
_CALL_RE = re.compile(r"(\w+)\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_SLICE_RE = re.compile(r"(\w+)\s*\(\s*(\d+)\s+(?:downto|to)\s+(\d+)\s*\)$", re.IGNORECASE)
_INT_RE = re.compile(r"^\d+$")
_STRING_OR_CHAR_RE = re.compile(r"\"[^\"\n]*\"|'.'")
_KEEP_WIDTH = ("shift_left", "shift_right", "rotate_left", "rotate_right", "abs",
               "signed", "unsigned", "std_logic_vector", "std_ulogic_vector")


def strip_comments(code: str) -> str:
    """The code with every `-- ...` comment removed, line structure kept."""
    return "\n".join(line.split("--", 1)[0] for line in code.splitlines())


def declared_widths(code: str) -> dict[str, int]:
    """{lower-case name: width} for every signal/variable declared with a
    `(H downto L)` range of a numeric_std or std_logic_vector type."""
    widths: dict[str, int] = {}
    for m in DECL_RE.finditer(strip_comments(code)):
        for name in m.group(1).split(","):
            widths[name.strip().lower()] = int(m.group(2)) - int(m.group(3)) + 1
    return widths


def split_top(expr: str, op: str) -> list[str]:
    """Split on `op` (one character, or a word for `and`/`or`) outside any
    brackets. Character and string literals are not looked inside."""
    blanked = _STRING_OR_CHAR_RE.sub(lambda m: " " * len(m.group(0)), expr)
    parts, depth, start = [], 0, 0
    i = 0
    while i < len(blanked):
        ch = blanked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and blanked.startswith(op, i):
            parts.append(expr[start:i])
            i += len(op)
            start = i
            continue
        i += 1
    return parts + [expr[start:]]


def _is_int(expr: str) -> bool:
    return bool(_INT_RE.match(expr.strip()))


def expr_width(expr: str, widths: dict[str, int]) -> int | None:
    """Bit width of a simple VHDL expression, or None when it can't be worked out."""
    expr = expr.strip()
    if not expr or re.search(r"\bwhen\b|=>", expr):
        return None
    # a + b - c: the widest known operand (an integer literal has the other's width)
    terms = additive_terms(expr)
    if len(terms) > 1:
        sizes = [None if _is_int(t) else expr_width(t, widths) for t in terms]
        known = [s for s in sizes if s is not None]
        if not known or any(s is None and not _is_int(t) for s, t in zip(sizes, terms)):
            return None
        return max(known)
    pieces = split_top(expr, "&")
    if len(pieces) > 1:
        sizes = [expr_width(p, widths) for p in pieces]
        return None if None in sizes else sum(sizes)
    factors = split_top(expr, "*")
    if len(factors) == 2:
        a, b = factors
        wa, wb = expr_width(a, widths), expr_width(b, widths)
        if wa is not None and _is_int(b):
            return 2 * wa                               # numeric_std: vector * integer doubles the width
        if wb is not None and _is_int(a):
            return 2 * wb
        return None if wa is None or wb is None else wa + wb
    if len(factors) > 2:
        return None
    if re.fullmatch(r"'.'", expr):
        return 1
    if re.fullmatch(r'"[^"]*"', expr):
        return len(expr) - 2
    if expr.lower() in widths:
        return widths[expr.lower()]
    m = re.match(r"^(?:(?:abs|not)\b|-)\s*(.+)$", expr, re.IGNORECASE | re.DOTALL)    # unary: same width
    if m:
        return expr_width(m.group(1), widths)
    m = _SLICE_RE.match(expr)
    if m:
        return abs(int(m.group(2)) - int(m.group(3))) + 1
    if expr.startswith("(") and expr.endswith(")"):
        inner = expr[1:-1]
        if inner.count("(") == inner.count(")"):
            return expr_width(inner, widths)
    m = _CALL_RE.match(expr)
    if m:
        func, args = m.group(1).lower(), split_top(m.group(2), ",")
        if func in ("resize", "to_signed", "to_unsigned") and len(args) == 2 and _is_int(args[1]):
            return int(args[1])
        if func in _KEEP_WIDTH and args:
            return expr_width(args[0], widths)
    return None


def additive_terms(expr: str) -> list[str]:
    """Top-level operands of + and -, a leading unary minus left on its term."""
    terms: list[str] = []
    for plus_part in split_top(expr, "+"):
        minus_parts = split_top(plus_part, "-")
        head = minus_parts[0]
        rest = minus_parts[1:]
        if not head.strip():                            # "-x ..." : unary minus on the first term
            if rest:
                head, rest = "-" + rest[0], rest[1:]
        terms.append(head)
        terms.extend(rest)
    return [t for t in terms if t.strip()]


def assignments(code: str) -> Iterator[tuple[int, str, str, str]]:
    """(line number, target, '<=' | ':=', expression) for every whole-signal
    or whole-variable assignment in the code (comments removed)."""
    stripped = strip_comments(code)
    for m in ASSIGN_RE.finditer(stripped):
        yield stripped.count("\n", 0, m.start(1)) + 1, m.group(1), m.group(2), m.group(3)


def source_line(code: str, n: int) -> str:
    src = code.splitlines()
    return src[n - 1].strip() if 0 < n <= len(src) else ""

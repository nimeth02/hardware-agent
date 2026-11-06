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

import ast
import re
from typing import Iterator

# "signal|variable|constant NAME[, NAME] : unsigned|signed|std_logic_vector(" — the
# range inside the brackets may be numbers or constants: (23 downto 0), (V_BITS-1 downto 0)
_VECTOR_DECL_RE = re.compile(
    r"\b(signal|variable|constant)\s+([\w\s,]+?)\s*:\s*(unsigned|signed|std_logic_vector|std_ulogic_vector)\s*\(",
    re.IGNORECASE)
_RANGE_SPLIT_RE = re.compile(r"\s+(downto|to)\s+", re.IGNORECASE)
# "constant E_BITS : integer := 24;" and a generic "ACC_BITS : natural := 2*E_BITS"
_INT_DECL_RE = re.compile(r"\b(\w+)\s*:\s*(?:integer|natural|positive)\s*:=\s*([^;)]+)", re.IGNORECASE)
# "target <= expr;" / "target := expr;" at the start of a statement: after ';',
# 'then', 'else', 'begin', 'loop' or a line start. Indexed targets (x(3) <= ...)
# are left out: their width is not the declared one.
ASSIGN_RE = re.compile(
    r"(?:^|;|\bthen\b|\belse\b|\bbegin\b|\bloop\b)\s*(\w+)\s*(<=|:=)\s*([^;]+)(?=;)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE)
_CALL_RE = re.compile(r"(\w+)\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_SLICE_RE = re.compile(r"(\w+)\s*\(\s*([^()]+?)\s+(?:downto|to)\s+([^()]+?)\s*\)$", re.IGNORECASE)
_INT_RE = re.compile(r"^\d+$")
_STRING_OR_CHAR_RE = re.compile(r"\"[^\"\n]*\"|'.'")
_KEEP_WIDTH = ("shift_left", "shift_right", "rotate_left", "rotate_right", "abs",
               "signed", "unsigned", "std_logic_vector", "std_ulogic_vector")


def strip_comments(code: str) -> str:
    """The code with every `-- ...` comment removed, line structure kept."""
    return "\n".join(line.split("--", 1)[0] for line in code.splitlines())


def int_value(text: str, constants: dict[str, int] | None) -> int | None:
    """The integer a static expression works out to (`16`, `V_BITS-1`,
    `2*E_BITS`, `2**ADDR_W`) using the known constants, else None."""
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except (SyntaxError, ValueError):
        return None

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            value = (constants or {}).get(node.id.lower())
            if value is None:
                raise ValueError(node.id)
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = ev(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if isinstance(node.op, (ast.Div, ast.FloorDiv)) and b:
                return a // b
            if isinstance(node.op, ast.Pow) and 0 <= b <= 64:
                return a ** b
        raise ValueError(ast.dump(node))

    try:
        return ev(tree)
    except (ValueError, TypeError, RecursionError):
        return None


def int_constants(stripped: str) -> dict[str, int]:
    """{lower-case name: value} for every integer constant or generic whose
    value can be worked out (`constant E_BITS : integer := 24;`, `generic
    (ACC_BITS : natural := 2*E_BITS)`); two passes, so a constant may use one
    declared after it. timing_recovery (2026-11-03 11:10): every width in
    the file was `(V_BITS-1 downto 0)`, so no width check could run and a
    16-bit register given a 24-bit sum crashed six dry runs in a row with
    only the process name."""
    found = {m.group(1).lower(): m.group(2) for m in _INT_DECL_RE.finditer(stripped)}
    values: dict[str, int] = {}
    for _ in range(2):
        for name, text in found.items():
            if name not in values:
                value = int_value(text, values)
                if value is not None:
                    values[name] = value
    return values


def vector_declarations(stripped: str, constants: dict[str, int]) -> Iterator[tuple[int, list[str], int, str]]:
    """(offset, lower-case names, width, type text) for every signal /
    variable / constant declared as a sized vector whose range works out."""
    for m in _VECTOR_DECL_RE.finditer(stripped):
        depth, i = 1, m.end()
        while i < len(stripped) and depth:
            depth += {"(": 1, ")": -1}.get(stripped[i], 0)
            i += 1
        if depth:
            return
        inner = stripped[m.end():i - 1]
        parts = _RANGE_SPLIT_RE.split(inner, maxsplit=1)
        if len(parts) != 3:
            continue
        hi, lo = int_value(parts[0], constants), int_value(parts[2], constants)
        if hi is None or lo is None:
            continue
        names = [n.strip().lower() for n in m.group(2).split(",")]
        yield m.start(), names, abs(hi - lo) + 1, f"{m.group(3)}({' '.join(inner.split())})"


def declared_widths(code: str) -> dict[str, int]:
    """{lower-case name: width} for every signal/variable/constant declared
    with a `(H downto L)` range of a numeric_std or std_logic_vector type,
    the whole file in one map (a name declared twice keeps the LAST width).
    Width checks use scoped_widths() instead; this is kept for callers that
    only need "is this name a sized vector at all"."""
    stripped = strip_comments(code)
    widths: dict[str, int] = {}
    for _, names, width, _ in vector_declarations(stripped, int_constants(stripped)):
        for name in names:
            widths[name] = width
    return widths


# A process, function or procedure body: the variables declared inside it
# exist only there. The function branch ends at `end function;`,
# `end function name;`, `end name;` or a bare `end;` (never at `end if;`).
_SCOPE_RE = re.compile(
    r"\bprocess\b.*?\bend\s+process\b"
    r"|\b(?:function|procedure)\s+(\w+)\b.*?\bend\s+(?:(?:function|procedure)\b\s*)?(?:\1\b\s*)?;",
    re.IGNORECASE | re.DOTALL)


class Widths(dict):
    """{lower-case name: width} for one line, carrying the file's integer
    constants (`.constants`, for `resize(x, V_BITS)`) and each name's
    declared type text (`.types`, e.g. "signed(V_BITS-1 downto 0)") so a
    message can quote the exact declaration to copy."""

    def __init__(self, *args, constants: dict[str, int] | None = None, types: dict[str, str] | None = None, **kw):
        super().__init__(*args, **kw)
        self.constants = constants or {}
        self.types = types or {}


class WidthScopes:
    """Declared widths with the scope they hold in. `at(line)` is the map in
    force on that line: the file-level signals, overridden by the variables
    of the process/function the line sits in.

    One map for the whole file is wrong as soon as two processes reuse a
    variable name at different widths. timing_recovery (2026-11-02 17:52):
    `prod_v` was 22 bits in p_s3 and 31 bits in p_s4, both correct; the
    file-wide map said 31 everywhere, so p_s3's two product lines were
    reported as "22-bit product into the 31-bit prod_v" and the model was
    told to declare exactly what it had already declared, for nine rounds."""

    def __init__(self, code: str):
        stripped = strip_comments(code)
        self.constants = int_constants(stripped)
        self.file_level: dict[str, int] = {}
        self.file_types: dict[str, str] = {}
        # (first line, last line, widths, types) of every process / function / procedure
        self.scopes: list[tuple[int, int, dict[str, int], dict[str, str]]] = [
            (stripped.count("\n", 0, m.start()) + 1, stripped.count("\n", 0, m.end()) + 1, {}, {})
            for m in _SCOPE_RE.finditer(stripped)]
        for offset, names, width, typ in vector_declarations(stripped, self.constants):
            line = stripped.count("\n", 0, offset) + 1
            scope = self._innermost(line)
            widths, types = (scope[2], scope[3]) if scope else (self.file_level, self.file_types)
            for name in names:
                widths[name] = width
                types[name] = typ

    def _innermost(self, line: int) -> tuple[int, int, dict[str, int], dict[str, str]] | None:
        inside = [s for s in self.scopes if s[0] <= line <= s[1]]
        return min(inside, key=lambda s: s[1] - s[0]) if inside else None

    def at(self, line: int) -> Widths:
        widths = Widths(self.file_level, constants=self.constants, types=dict(self.file_types))
        scope = self._innermost(line)
        if scope:
            widths.update(scope[2])
            widths.types.update(scope[3])
        return widths


def scoped_widths(code: str) -> WidthScopes:
    return WidthScopes(code)


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
    """Bit width of a simple VHDL expression, or None when it can't be worked
    out. `widths` from WidthScopes.at() carries the integer constants, so
    `resize(x, V_BITS)` and `x(V_BITS-1 downto 0)` are read too."""
    constants = getattr(widths, "constants", None) or {}

    def is_int(t: str) -> bool:                         # a literal or an integer constant name
        return _is_int(t) or (t.strip().lower() in constants and t.strip().lower() not in widths)

    expr = expr.strip()
    if not expr or re.search(r"\bwhen\b|=>", expr):
        return None
    # a + b - c: the widest known operand (an integer literal has the other's width)
    terms = additive_terms(expr)
    if len(terms) > 1:
        sizes = [None if is_int(t) else expr_width(t, widths) for t in terms]
        known = [s for s in sizes if s is not None]
        if not known or any(s is None and not is_int(t) for s, t in zip(sizes, terms)):
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
        if wa is not None and is_int(b):
            return 2 * wa                               # numeric_std: vector * integer doubles the width
        if wb is not None and is_int(a):
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
        hi, lo = int_value(m.group(2), constants), int_value(m.group(3), constants)
        return None if hi is None or lo is None else abs(hi - lo) + 1
    if expr.startswith("(") and expr.endswith(")"):
        inner = expr[1:-1]
        if inner.count("(") == inner.count(")"):
            return expr_width(inner, widths)
    m = _CALL_RE.match(expr)
    if m:
        func, args = m.group(1).lower(), split_top(m.group(2), ",")
        if func in ("resize", "to_signed", "to_unsigned") and len(args) == 2:
            return int_value(args[1], constants)
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

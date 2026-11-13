# agents/rtl_agent/lint/rtl_equation_check.py — the spec's sample formulas, worked
# out again from the RTL lines.
#
# The spec gives some values as a weighted sum of samples:
#   v1 = -0.5*x[m+2] + 1.5*x[m+1] - 0.5*x[m] - 0.5*x[m-1]
# For every such formula the RTL lines that compute the value of that name
# (`v1`, `v1i_v`, `v1_q_s3`) are worked out the same way: each line is read
# as "how much of each input does the result hold", following variables and
# computed registers back to the stored samples:
#   v1 := resize(xp2, 15) - resize(xp1, 15) - resize(xm, 15) - resize(xm1, 15);
#   v1 := shift_right(v1, 1);         ->   0.5*xp2 - 0.5*xp1 - 0.5*xm - 0.5*xm1
# The two lists of weights must be the same, in any order and up to one
# common factor 2**k (a design may keep 2*v1 and shift later). Here the spec
# has -0.5, -0.5, -0.5, 1.5 and the code -0.5, -0.5, -0.5, 0.5: reported.
#
# No simulation and no AI call: plain arithmetic on the text. A line that
# uses anything else than + - resize shift_left shift_right and a multiply
# by a whole number is not read and never reported. Which sample each weight
# sits on is NOT compared (the code's names x0..x3 do not say which x[m+k]
# they are); only the weights are.
#
# Reviewed timing_recovery RTL of 2026-11-10: the v1 line above went through
# every check and four simulations; the output was wrong on every beat.

from __future__ import annotations

import ast
import re
from fractions import Fraction

from agents.rtl_agent.lint.rtl_widths import assignments, int_value, scoped_widths, source_line, split_top

Linear = dict[str, Fraction]            # input name -> weight

_NAME_RE = re.compile(r"[A-Za-z_]\w*")
_SAMPLE_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\[\s*([^\[\]]+?)\s*\]")          # x[m+2]
_EQUATION_START_RE = re.compile(r"(?<![\w\]=<>!/:])([A-Za-z_]\w*)\s*=(?!=)")
_TRAILING_WORDS_RE = re.compile(r"[\s,;.:]+$")
_SAME_VALUE_CALLS = ("resize", "signed", "unsigned")
MIN_SAMPLES = 2                         # `v0 = x[m]` is a copy, not a formula


# -- the spec side ---------------------------------------------------------
def _linear_from_ast(node, names: dict[str, str]) -> Linear | Fraction | None:
    """A Python-syntax arithmetic expression as weights of its names, a plain
    number, or None when it is not a weighted sum."""
    if isinstance(node, ast.Expression):
        return _linear_from_ast(node.body, names)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Fraction(str(node.value))
    if isinstance(node, ast.Name):
        return {names[node.id]: Fraction(1)} if node.id in names else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _linear_from_ast(node.operand, names)
        return value if value is None or isinstance(node.op, ast.UAdd) else _scaled(value, Fraction(-1))
    if isinstance(node, ast.BinOp):
        a, b = _linear_from_ast(node.left, names), _linear_from_ast(node.right, names)
        if a is None or b is None:
            return None
        if isinstance(node.op, (ast.Add, ast.Sub)):
            return _added(a, b if isinstance(node.op, ast.Add) else _scaled(b, Fraction(-1)))
        if isinstance(node.op, ast.Mult):
            if isinstance(a, Fraction):
                return _scaled(b, a)
            return _scaled(a, b) if isinstance(b, Fraction) else None
        if isinstance(node.op, ast.Div) and isinstance(b, Fraction) and b:
            return _scaled(a, 1 / b)
    return None


def _scaled(value: Linear | Fraction, by: Fraction) -> Linear | Fraction:
    return value * by if isinstance(value, Fraction) else {k: v * by for k, v in value.items()}


def _added(a: Linear | Fraction, b: Linear | Fraction) -> Linear | Fraction | None:
    if isinstance(a, Fraction) and isinstance(b, Fraction):
        return a + b
    if isinstance(a, Fraction) or isinstance(b, Fraction):
        return None                                     # a sample plus a bare number: not a weighted sum
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, Fraction(0)) + v
    return out


def spec_formulas(lines: list[str]) -> dict[str, tuple[str, Linear]]:
    """{lower-case name: (formula text, weights by sample)} for every
    `name = weighted sum of two or more samples` in the spec's lines. The
    first formula of a name is kept."""
    found: dict[str, tuple[str, Linear]] = {}
    for line in lines:
        line = line.replace("−", "-").replace("–", "-").replace("×", "*").replace("·", "*")
        starts = list(_EQUATION_START_RE.finditer(line))
        for n, m in enumerate(starts):
            end = starts[n + 1].start() if n + 1 < len(starts) else len(line)
            rhs = _TRAILING_WORDS_RE.sub("", line[m.end():end])
            names: dict[str, str] = {}

            def token(s: re.Match) -> str:
                sample = f"{s.group(1)}[{''.join(s.group(2).split())}]"
                return names.setdefault(sample, f"S{len(names)}_")

            python = _SAMPLE_RE.sub(token, rhs)
            if len(names) < MIN_SAMPLES or not re.fullmatch(r"[\dS_\s.+\-*/()]+", python):
                continue
            try:
                value = _linear_from_ast(ast.parse(python.strip(), mode="eval"), {v: k for k, v in names.items()})
            except (SyntaxError, ValueError, ZeroDivisionError):
                continue
            if isinstance(value, dict):
                value = {k: v for k, v in value.items() if v}
                if len(value) >= MIN_SAMPLES:
                    found.setdefault(m.group(1).lower(), (f"{m.group(1)} = {' '.join(rhs.split())}", value))
    return found


# -- the RTL side ----------------------------------------------------------
def _unwrap(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        inner, depth = expr[1:-1], 0
        for ch in inner:
            depth += ch == "("
            depth -= ch == ")"
            if depth < 0:
                return expr
        expr = inner.strip()
    return expr


def _signed_terms(expr: str) -> list[tuple[int, str]]:
    """Top-level operands of + and - with their sign: `a - b + c` -> [(1, a), (-1, b), (1, c)]."""
    terms: list[tuple[int, str]] = []
    for plus_part in split_top(expr, "+"):
        pieces = split_top(plus_part, "-")
        if pieces[0].strip():
            terms.append((1, pieces[0]))
        terms.extend((-1, p) for p in pieces[1:])
    return [(sign, t) for sign, t in terms if t.strip()]


class _Reader:
    def __init__(self, code: str, unread_as_input: bool = False):
        # unread_as_input: a value built with a multiply of two signals (`e <= ymid * d`)
        # cannot be read as a weighted sum; True keeps it as an input of its own name
        self.unread_as_input = unread_as_input
        self.scopes = scoped_widths(code)
        self.constants = self.scopes.constants
        self.statements = list(assignments(code))
        writes: dict[str, list[tuple[int, str]]] = {}
        for line, target, op, expr in self.statements:
            if op == "<=" and not re.fullmatch(r"\(\s*others\s*=>\s*'.'\s*\)", expr.strip(), re.IGNORECASE):
                writes.setdefault(target.lower(), []).append((line, expr))
        # a register with ONE computed write is followed back; a copy (`x1 <= x0;`) is a stored
        # sample and stays an input, as does anything written in two places
        self.computed = {name: ws[0] for name, ws in writes.items()
                         if len(ws) == 1 and not _NAME_RE.fullmatch(_unwrap(ws[0][1]))}
        self.variables: dict[tuple, Linear | None] = {}
        self.line = 0

    def _scope(self, line: int) -> tuple | None:
        scope = self.scopes._innermost(line)
        return scope[:2] if scope else None

    def linear(self, expr: str, seen: frozenset = frozenset()) -> Linear | None:
        expr = _unwrap(expr)
        terms = _signed_terms(expr)
        if len(terms) > 1 or (terms and terms[0][0] < 0):
            total: Linear = {}
            for sign, term in terms:
                part = self.linear(term, seen)
                if part is None:
                    return None
                for k, v in part.items():
                    total[k] = total.get(k, Fraction(0)) + sign * v
            return total
        factors = split_top(expr, "*")
        if len(factors) == 2:
            for number, other in (factors, factors[::-1]):
                by = int_value(number, self.constants)
                if by is not None:
                    part = self.linear(other, seen)
                    return None if part is None else {k: v * by for k, v in part.items()}
            return None
        if len(factors) > 2:
            return None
        if _NAME_RE.fullmatch(expr):
            name = expr.lower()
            key = (self._scope(self.line), name)
            if key in self.variables:
                held = self.variables[key]
                return {name: Fraction(1)} if held is None and self.unread_as_input else held
            if name in self.computed and name not in seen:
                line, source = self.computed[name]
                here, self.line = self.line, line
                try:
                    held = self.linear(source, seen | {name})
                finally:
                    self.line = here
                return {name: Fraction(1)} if held is None and self.unread_as_input else held
            return {name: Fraction(1)} if name in self.scopes.at(self.line) else None
        m = re.fullmatch(r"(\w+)\s*\((.*)\)", expr, re.DOTALL)
        if not m:
            return None
        func, args = m.group(1).lower(), split_top(m.group(2), ",")
        if func in _SAME_VALUE_CALLS and len(args) in (1, 2):
            return self.linear(args[0], seen)
        if func in ("shift_right", "shift_left") and len(args) == 2:
            part, by = self.linear(args[0], seen), int_value(args[1], self.constants)
            if part is None or by is None or not 0 <= by <= 64:
                return None
            factor = Fraction(2 ** by) if func == "shift_left" else Fraction(1, 2 ** by)
            return {k: v * factor for k, v in part.items()}
        return None

    def values(self):
        """(line, target, weights) for every assignment that is a weighted
        sum of two or more inputs, in file order."""
        self.variables = {}
        for line, target, op, expr in self.statements:
            self.line = line
            value = self.linear(expr)
            if value is not None:
                value = {k: v for k, v in value.items() if v}
            if op == ":=":
                self.variables[(self._scope(line), target.lower())] = value
            if value and len(value) >= MIN_SAMPLES:
                yield line, target, value


def _same_up_to_power_of_two(spec: list[Fraction], code: list[Fraction]) -> bool:
    ratios = {c / s for s, c in zip(sorted(spec), sorted(code))}
    if len(ratios) != 1:
        return False
    ratio = ratios.pop()
    return ratio > 0 and (ratio.numerator == 1 or ratio.denominator == 1) \
        and (ratio.numerator * ratio.denominator) & (ratio.numerator * ratio.denominator - 1) == 0


def _text(value: Fraction) -> str:
    return str(float(value)).removesuffix(".0") if value.denominator > 1 else str(value.numerator)


def _weighted(weights: Linear) -> str:
    out = ""
    for name, w in weights.items():
        out += f" {'-' if w < 0 else '+'} {'' if abs(w) == 1 else _text(abs(w)) + '*'}{name}"
    return out.strip().removeprefix("+ ")


def equation_issues(spec_lines: list[str], code: str) -> list[dict]:
    """[{code: "equation_coefficients_differ", detail}] for every RTL value
    named after a spec formula whose weights are not the formula's."""
    formulas = spec_formulas(spec_lines)
    if not formulas:
        return []
    issues, reported = [], set()
    for line, target, weights in _Reader(code).values():
        name = target.lower()
        # v1, v1i_v, v1_q_s3, v1i_s3: the formula's name, an I/Q letter, a stage / variable ending
        spec_name = next((f for f in formulas
                          if re.fullmatch(rf"{re.escape(f)}_?[iq]?(_(s\d+|v|r|w|d\d*|reg|next))*", name)), None)
        inputs = frozenset(weights)                     # v1i_v and the v1i_s3 it is stored in: one message
        if not spec_name or (spec_name, inputs) in reported:
            continue
        text, wanted = formulas[spec_name]
        if len(weights) != len(wanted) or _same_up_to_power_of_two(list(wanted.values()), list(weights.values())):
            continue
        reported.add((spec_name, inputs))
        have = ", ".join(_text(w) for w in sorted(weights.values()))
        want = ", ".join(_text(w) for w in sorted(wanted.values()))
        issues.append({
            "code": "equation_coefficients_differ",
            "detail": f"line {line}: `{target}` does not compute the spec's formula. Worked out from this line "
                      f"and the lines that feed it, `{target}` = {_weighted(weights)}; its weights are "
                      f"{have}. The spec says `{text}`; its weights are {want}. They must be the same list "
                      f"(a common factor 2 is fine when it is shifted back later). Write one term per sample "
                      f"of the formula with its own sign and weight: 0.5*x is `shift_right(x, 1)`, 1.5*x is "
                      f"`x + shift_right(x, 1)`; when you keep twice the value, 2*x is `shift_left(x, 1)` and "
                      f"3*x is `shift_left(x, 1) + x` (shift_left(x, 1) alone is 2*x, never 3*x or 1.5*x) "
                      f">> {source_line(code, line)}"})
    return issues


# -- the spec's product formulas -------------------------------------------
# `y = (v2*mu + v1)*mu + v0` multiplied out is v2*mu*mu + v1*mu + v0. The RTL
# value that is built from the same names (v2i_s3, mu_s3, v1i_s3, v0i_s4) is
# multiplied out the same way, following registers and variables back until a
# name of the formula is reached. Two things are compared:
#   - the list of products. `p1 <= v2*mu; p2 <= v1*mu; sum <= p1 + p2 + v0` is
#     v2*mu + v1*mu + v0: mu multiplies v2 once, the formula needs twice
#     (timing_recovery 2026-11-11 13:09, 275 mismatches, no check fired);
#   - a value kept at 2**k times its spec formula (`v2` without its 0.5) that
#     no later shift divides back: its product is 2**k too large next to the
#     other products. The scale of the multiplied name itself (mu's fraction
#     bits) is not needed: with it the products' scales must rise in equal
#     steps, and they are checked for exactly that.
Terms = dict[tuple[str, ...], Fraction]     # names multiplied together (sorted) -> weight
_LEAF_ENDINGS = r"_?[iq]?(_(s\d+|v|r|w|d\d*|reg|next))*"


def _terms_added(a: Terms, b: Terms, sign: int = 1) -> Terms:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, Fraction(0)) + sign * v
    return {k: v for k, v in out.items() if v}


def _terms_times(a: Terms, b: Terms) -> Terms:
    out: Terms = {}
    for ka, va in a.items():
        for kb, vb in b.items():
            key = tuple(sorted(ka + kb))
            out[key] = out.get(key, Fraction(0)) + va * vb
    return {k: v for k, v in out.items() if v}


def _terms_from_ast(node) -> Terms | None:
    if isinstance(node, ast.Expression):
        return _terms_from_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return {(): Fraction(str(node.value))}
    if isinstance(node, ast.Name):
        return {(node.id.lower(),): Fraction(1)}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _terms_from_ast(node.operand)
        return value if value is None or isinstance(node.op, ast.UAdd) else _terms_added({}, value, -1)
    if isinstance(node, ast.BinOp):
        a, b = _terms_from_ast(node.left), _terms_from_ast(node.right)
        if a is None or b is None:
            return None
        if isinstance(node.op, (ast.Add, ast.Sub)):
            return _terms_added(a, b, 1 if isinstance(node.op, ast.Add) else -1)
        if isinstance(node.op, ast.Mult):
            return _terms_times(a, b)
        if isinstance(node.op, ast.Div) and set(b) == {()}:
            return {k: v / b[()] for k, v in a.items()}
        if isinstance(node.op, ast.Pow) and set(b) == {()} and b[()] in (1, 2, 3, 4):
            out = a
            for _ in range(int(b[()]) - 1):
                out = _terms_times(out, a)
            return out
    return None


def spec_products(lines: list[str]) -> dict[str, tuple[str, Terms]]:
    """{lower-case name: (formula text, multiplied-out products)} for every
    `name = expression of plain names` in the spec's lines in which two names
    are multiplied together. The first formula of a name is kept."""
    found: dict[str, tuple[str, Terms]] = {}
    for line in lines:
        line = line.replace("−", "-").replace("–", "-").replace("×", "*").replace("·", "*")
        starts = list(_EQUATION_START_RE.finditer(line))
        for n, m in enumerate(starts):
            end = starts[n + 1].start() if n + 1 < len(starts) else len(line)
            rhs = _TRAILING_WORDS_RE.sub("", line[m.end():end])
            python = rhs.replace("^", "**")
            if not re.fullmatch(r"[\w\s.+\-*/()]+", python):
                continue
            try:
                value = _terms_from_ast(ast.parse(python.strip(), mode="eval"))
            except (SyntaxError, ValueError, ZeroDivisionError):
                continue
            if value and any(len(mono) >= 2 for mono in value):
                found.setdefault(m.group(1).lower(), (f"{m.group(1)} = {' '.join(rhs.split())}", value))
    return found


def _log2(value: Fraction) -> int | None:
    """k when value is exactly 2**k, else None."""
    if value <= 0:
        return None
    for whole, sign in ((value, 1), (1 / value, -1)):
        if whole.denominator == 1 and whole.numerator & (whole.numerator - 1) == 0:
            return sign * (whole.numerator.bit_length() - 1)
    return None


def _spec_name_of(name: str, spec_names) -> str | None:
    """v1, v1i_v, v1_q_s3, v1i_s3 -> v1: the formula's name, an I/Q letter, a
    stage / variable ending."""
    return next((f for f in sorted(spec_names, key=len, reverse=True)
                 if re.fullmatch(rf"{re.escape(f)}{_LEAF_ENDINGS}", name.lower())), None)


def _leaf_scales(formulas: dict[str, tuple[str, Linear]], code: str) -> dict[str, tuple[Fraction | None, int]]:
    """{RTL name: (how many times the spec's value it holds, line)} for every
    RTL value named after a weighted-sum formula. The factor is None when the
    weights are not the formula's times one 2**k (reported by
    equation_coefficients_differ). A name written twice keeps the last."""
    out: dict[str, tuple[Fraction | None, int]] = {}
    if not formulas:
        return out
    for line, target, weights in _Reader(code).values():
        spec_name = _spec_name_of(target, formulas)
        if not spec_name:
            continue
        wanted, ratio = formulas[spec_name][1], None
        if len(weights) == len(wanted):
            ratios = {c / s for s, c in zip(sorted(wanted.values()), sorted(weights.values()))}
            if len(ratios) == 1 and _log2(next(iter(ratios))) is not None:
                ratio = ratios.pop()
        out[target.lower()] = (ratio, line)
    return out


class _Products:
    """Reads an RTL expression as multiplied-out products of one formula's names."""

    def __init__(self, code: str, names: set[str], scales: dict[str, tuple[Fraction | None, int]]):
        self.scopes = scoped_widths(code)
        self.constants = self.scopes.constants
        self.statements = list(assignments(code))
        writes: dict[str, list[tuple[int, str]]] = {}
        for line, target, op, expr in self.statements:
            if op == "<=" and not re.fullmatch(r"\(\s*others\s*=>\s*'.'\s*\)", expr.strip(), re.IGNORECASE):
                writes.setdefault(target.lower(), []).append((line, expr))
        self.single = {name: ws[0] for name, ws in writes.items() if len(ws) == 1}
        self.names, self.scales = names, scales
        self.variables: dict[tuple, tuple[Terms, frozenset] | None] = {}
        self.line = 0

    def _scope(self, line: int) -> tuple | None:
        scope = self.scopes._innermost(line)
        return scope[:2] if scope else None

    def read(self, expr: str, seen: frozenset = frozenset()) -> tuple[Terms, frozenset] | None:
        """(products, the RTL names they were read from), or None when the
        expression uses anything that is not followed."""
        expr = _unwrap(expr)
        terms = _signed_terms(expr)
        if len(terms) > 1 or (terms and terms[0][0] < 0):
            total, leaves = {}, frozenset()
            for sign, term in terms:
                part = self.read(term, seen)
                if part is None:
                    return None
                total, leaves = _terms_added(total, part[0], sign), leaves | part[1]
            return total, leaves
        factors = split_top(expr, "*")
        if len(factors) > 1:
            total, leaves = {(): Fraction(1)}, frozenset()
            for factor in factors:
                part = self.read(factor, seen)
                if part is None:
                    return None
                total, leaves = _terms_times(total, part[0]), leaves | part[1]
            return total, leaves
        if _NAME_RE.fullmatch(expr):
            name = expr.lower()
            spec_name = _spec_name_of(name, self.names)
            if spec_name:                               # a name of the formula: stop here
                return {(spec_name,): self.scales.get(name, (None, 0))[0] or Fraction(1)}, frozenset({name})
            key = (self._scope(self.line), name)
            if key in self.variables:
                return self.variables[key]
            if name in self.single and name not in seen:
                line, source = self.single[name]
                here, self.line = self.line, line
                try:
                    return self.read(source, seen | {name})
                finally:
                    self.line = here
        number = int_value(expr, self.constants)
        if number is not None:
            return ({(): Fraction(number)} if number else {}), frozenset()
        m = re.fullmatch(r"(\w+)\s*\((.*)\)", expr, re.DOTALL)
        if not m:
            return None
        func, args = m.group(1).lower(), split_top(m.group(2), ",")
        if func in _SAME_VALUE_CALLS + ("std_logic_vector",) and len(args) in (1, 2):
            return self.read(args[0], seen)
        part, factor = None, None
        if func in ("shift_right", "shift_left") and len(args) == 2:
            part, by = self.read(args[0], seen), int_value(args[1], self.constants)
            if by is not None and 0 <= by <= 128:
                factor = Fraction(2 ** by) if func == "shift_left" else Fraction(1, 2 ** by)
        piece = re.fullmatch(r"(.+?)\s+downto\s+(.+)", m.group(2).strip(), re.IGNORECASE | re.DOTALL)
        if piece and func in self.scopes.at(self.line):    # p(30 downto 8): the low 8 bits dropped
            part, low = self.read(func, seen), int_value(piece.group(2), self.constants)
            if low is not None and 0 <= low <= 128:
                factor = Fraction(1, 2 ** low)
        if part is None or factor is None:
            return None
        return {k: v * factor for k, v in part[0].items()}, part[1]

    def values(self):
        """(line, target, products, RTL names read) for every assignment that
        can be read, in file order."""
        self.variables = {}
        for line, target, op, expr in self.statements:
            self.line = line
            value = self.read(expr)
            if op == ":=":
                self.variables[(self._scope(line), target.lower())] = value
            if value and value[0]:
                yield line, target, value[0], value[1]


def _product_text(mono: tuple[str, ...]) -> str:
    return "*".join(sorted(mono, reverse=True)) or "a plain number"


def _on_one_scale(wanted: Terms, terms: Terms, stepped: str) -> bool | None:
    """True when the RTL products are the formula's on one common scale: the
    product with `stepped` k times is 2**(a + k*b) of the formula's for one a
    and one b. None when it cannot be told (fewer than three steps, or a
    factor that is not a power of two)."""
    points = []
    for mono, weight in wanted.items():
        k = _log2(terms[mono] / weight)
        if k is None:
            return None
        points.append((mono.count(stepped), k))
    if len({d for d, _ in points}) < 3:
        return None
    (d0, e0), (d1, e1) = min(points), max(points)
    return all((d - d0) * (e1 - e0) == (d1 - d0) * (e - e0) for d, e in points)


def polynomial_issues(spec_lines: list[str], code: str) -> list[dict]:
    """[{code: "equation_terms_differ" | "equation_value_not_scaled_back", detail}]
    for every RTL value built from all the names of a spec product formula
    that is not that formula."""
    products = spec_products(spec_lines)
    if not products:
        return []
    scales = _leaf_scales(spec_formulas(spec_lines), code)
    issues = []
    for text, wanted in products.values():
        names = {n for mono in wanted for n in mono}
        in_several = [n for n in names if sum(n in mono for mono in wanted) >= 2]
        checked: set[frozenset] = set()
        for line, target, terms, leaves in _Products(code, names, scales).values():
            if {n for mono in terms for n in mono} != names or leaves in checked:
                continue
            checked.add(leaves)                         # ysym <= resize(sum): the same value, one message
            if set(terms) != set(wanted):
                missing = [_product_text(m) for m in wanted if m not in terms]
                extra = [_product_text(m) for m in terms if m not in wanted]
                issues.append({
                    "code": "equation_terms_differ",
                    "detail": f"line {line}: `{target}` does not compute the spec's formula `{text}`. Worked out "
                              f"from this line and the lines that feed it, `{target}` is the sum of these "
                              f"products: {', '.join(_product_text(m) for m in terms)}. The formula multiplied "
                              f"out is the sum of: {', '.join(_product_text(m) for m in wanted)}."
                              + (f" Missing: {', '.join(missing)}." if missing else "")
                              + (f" Not in the formula: {', '.join(extra)}." if extra else "")
                              + " Build the formula bracket by bracket, one step per bracket: compute the inner "
                                "bracket first (multiply, shift the product back, add), keep that result, then "
                                "multiply THAT RESULT by the next factor and add the last term. Two products "
                                "that are each multiplied once and then added are not the formula "
                                f">> {source_line(code, line)}"})
                continue
            if len(in_several) != 1 or any(scales.get(leaf, (1, 0))[0] is None for leaf in leaves):
                continue
            if _on_one_scale(wanted, terms, in_several[0]) is not False:
                continue
            kept = {leaf: scales[leaf] for leaf in leaves if leaf in scales and scales[leaf][0] != 1}
            by_name = {_spec_name_of(leaf, names): ratio for leaf, (ratio, _) in kept.items()}
            if not kept or len(by_name) != len(kept):
                continue
            undone = dict(terms)
            for name, ratio in by_name.items():
                undone = {mono: w / ratio ** mono.count(name) for mono, w in undone.items()}
            if _on_one_scale(wanted, undone, in_several[0]) is not True:
                continue                                # another cause: left to the fraction-bit check
            for leaf, (ratio, leaf_line) in kept.items():
                name, k = _spec_name_of(leaf, names), abs(_log2(ratio))
                fix = f"`{'shift_right' if ratio > 1 else 'shift_left'}(..., {k})` on the value"
                issues.append({
                    "code": "equation_value_not_scaled_back",
                    "detail": f"line {leaf_line}: `{leaf}` holds {_text(ratio)} times the spec's `{name}` (the "
                              f"weights of its sum are {_text(ratio)} times the formula's) and no later line "
                              f"undoes that: in `{target}` (line {line}, spec formula `{text}`) every product "
                              f"with `{name}` is {_text(ratio)} times too {'large' if ratio > 1 else 'small'} "
                              f"next to the other terms. Use {fix} where `{leaf}` is computed, or shift its "
                              f"product by {k} more bit{'s' if k > 1 else ''} >> {source_line(code, leaf_line)}"})
    return issues


# -- the spec's two loop gains ---------------------------------------------
# `acc = acc + K2 * e; v = K1 * e + acc` with `K1 = 2^-7, K2 = 2^-15`: the
# error is added to a running total with one gain and to the result with
# another. What 1.0 is in the RTL's numbers is the design's own choice, so
# neither gain can be checked alone; how many times larger K1 is than K2 can.
# The RTL lines are read as weights of the error:
#   acc_next := acc_r + shift_right(e_w, 7);      -- running total: e / 128
#   v_r      <= shift_left(e_w, 1) + acc_next;    -- result: 2*e + e/128
# 2 is 256 times 1/128, and 2^-7 is 256 times 2^-15: right. A result that
# reads the total before this update (2*e only) is the same loop one update
# later and passes too. `shift_right(e_w, 7)` in both lines is 1 time:
# reported (the loop then never settles and the count of output beats is
# wrong).
_NUMBER_IS_RE = re.compile(r"(?<![\w\]])([A-Za-z_]\w*)\s*=\s*(\(?[-+]?\d[\d.^*/()+\-]*)")


def _number_from_ast(node) -> Fraction | None:
    if isinstance(node, ast.Expression):
        return _number_from_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Fraction(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _number_from_ast(node.operand)
        return value if value is None or isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        a, b = _number_from_ast(node.left), _number_from_ast(node.right)
        if a is None or b is None:
            return None
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        if isinstance(node.op, ast.Div) and b:
            return a / b
        if isinstance(node.op, ast.Pow) and a and b.denominator == 1 and abs(b) <= 64:
            return a ** int(b)
    return None


def spec_numbers(lines: list[str]) -> dict[str, Fraction]:
    """{lower-case name: value} for every `NAME = number` in the spec's lines
    (`K1=2^-7`, `N = 4`, `alpha = 1/2`). The first value of a name is kept."""
    found: dict[str, Fraction] = {}
    for line in lines:
        line = line.replace("−", "-").replace("–", "-").replace("×", "*").replace("·", "*")
        for m in _NUMBER_IS_RE.finditer(line):
            text = m.group(2).rstrip(".+-*/(").replace("^", "**")
            try:
                value = _number_from_ast(ast.parse(text, mode="eval"))
            except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
                continue
            if value is not None:
                found.setdefault(m.group(1).lower(), value)
    return found


def spec_loop_gains(lines: list[str]) -> list[dict]:
    """[{total, result, error, gain_total, gain_result, wanted, text}] for
    every pair `T = T + G2 * E` / `R = G1 * E + T` in the spec whose G1 and G2
    have a value in the spec. wanted is G1 / G2."""
    # "v = K1 * e + acc where K1 = 2^-7", "..., and y_prev = y_sym": the formula ends at the word
    cut = [re.sub(r"\b(where|and)\b", ";", line, flags=re.IGNORECASE) for line in lines]
    numbers, products = spec_numbers(lines), spec_products(cut)
    out = []
    for total, (total_text, terms) in products.items():
        pairs = [mono for mono in terms if len(mono) == 2]
        if set(terms) != {(total,), *pairs} or len(pairs) != 1 or terms[(total,)] != 1 or terms[pairs[0]] != 1:
            continue
        gains = [n for n in pairs[0] if numbers.get(n)]
        if len(gains) != 1 or pairs[0][0] == pairs[0][1]:
            continue
        error = next(n for n in pairs[0] if n != gains[0])
        for result, (result_text, other) in products.items():
            mine = [mono for mono in other if len(mono) == 2 and error in mono]
            if result == total or set(other) != {(total,), *mine} or len(mine) != 1 \
                    or other[(total,)] != 1 or other[mine[0]] != 1:
                continue
            gain = next((n for n in mine[0] if n != error and numbers.get(n)), None)
            if gain and gain != gains[0]:
                out.append({"total": total, "result": result, "error": error, "gain_total": gains[0],
                            "gain_result": gain, "wanted": numbers[gain] / numbers[gains[0]],
                            "text": f"{total_text}; {result_text}"})
    return out


def _named_for(name: str, spec_name: str) -> bool:
    """acc, acc_r, acc_next, acc_s7 are all the spec's `acc`."""
    return re.fullmatch(rf"{re.escape(spec_name)}(_\w+)?", name.lower()) is not None


def _power_text(value: Fraction) -> str:
    k = _log2(abs(value)) if value else None
    return _text(value) if k is None or abs(k) < 4 else f"{'-' if value < 0 else ''}2^{k}"


def _shift_text(name: str, weight: Fraction) -> str | None:
    k = _log2(weight)
    if k is None:
        return None
    return name if k == 0 else f"shift_left({name}, {k})" if k > 0 else f"shift_right({name}, {-k})"


def loop_gain_issues(spec_lines: list[str], code: str) -> list[dict]:
    """[{code: "loop_gain_ratio", detail}] when the RTL's two loop gains are
    not as many times apart as the spec's two constants."""
    loops = spec_loop_gains(spec_lines)
    if not loops:
        return []
    numbers = spec_numbers(spec_lines)
    reader = _Reader(code, unread_as_input=True)
    values: list[tuple[int, str, Linear]] = []
    # the running total read as a register is its stored value, not the line that writes it
    stored = frozenset(n for n in reader.computed if any(_named_for(n, loop["total"]) for loop in loops))
    for line, target, op, expr in reader.statements:
        reader.line = line
        value = reader.linear(expr, stored | {target.lower()} if op == "<=" else stored)
        if value is not None:
            value = {k: v for k, v in value.items() if v}
        if op == ":=":
            reader.variables[(reader._scope(line), target.lower())] = value
        if value and len(value) >= MIN_SAMPLES:
            values.append((line, target, value))

    def split(value: Linear, total: str) -> tuple[frozenset, Fraction] | None:
        """(the error's names, their one weight) when the value is the running
        total kept whole plus one weight of everything else."""
        kept = [k for k in value if _named_for(k, total)]
        rest = {k: v for k, v in value.items() if k not in kept}
        if len(kept) != 1 or value[kept[0]] != 1 or not rest or len(set(rest.values())) != 1:
            return None
        return frozenset(rest), next(iter(rest.values()))

    issues, reported = [], set()
    for loop in loops:
        totals = {}                                     # error names -> (line, target, weight); the last write
        for line, target, value in values:
            part = split(value, loop["total"])
            if part and _named_for(target, loop["total"]):
                totals[part[0]] = (line, target, part[1])
        for line, target, value in values:
            part = split(value, loop["total"])
            if not part or part[0] not in totals or not _named_for(target, loop["result"]) \
                    or _named_for(target, loop["total"]) or target.lower() in reported:
                continue
            total_line, total_target, small = totals[part[0]]
            have, wanted = part[1] / small, loop["wanted"]
            if have in (wanted, wanted + 1):            # + 1: the result reads the total after this update
                continue
            reported.add(target.lower())
            error = " + ".join(sorted(part[0]))
            g1, g2 = loop["gain_result"].upper(), loop["gain_total"].upper()
            term = _shift_text(error, small * wanted)
            fix = (f"Keep line {total_line} as it is and write the {g1} term of `{target}` as `{term}`"
                   if term and len(part[0]) == 1 else
                   f"Keep line {total_line} as it is and make the {g1} term of `{target}` {_power_text(wanted)} "
                   f"times the term added on line {total_line}")
            issues.append({
                "code": "loop_gain_ratio",
                "detail": f"line {line}: the two loop gains are not the spec's. `{target}` takes "
                          f"{_power_text(part[1])} times `{error}`, and the running total `{total_target}` "
                          f"(line {total_line}) takes {_power_text(small)} times it: the first is "
                          f"{_power_text(have)} times the second. The spec says `{loop['text']}` with "
                          f"{g1} = {_power_text(numbers[loop['gain_result']])} and "
                          f"{g2} = {_power_text(numbers[loop['gain_total']])}: {g1} is "
                          f"{_power_text(wanted)} times {g2}, so the first must be {_power_text(wanted)} times "
                          f"the second ({_power_text(wanted + 1)} when `{target}` reads the total after this "
                          f"update). With the wrong gain the loop never settles and the number of output beats "
                          f"is wrong. {fix} >> {source_line(code, line)}"})
    return issues

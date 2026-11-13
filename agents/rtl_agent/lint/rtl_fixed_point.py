# agents/rtl_agent/lint/rtl_fixed_point.py — where the binary point sits.
#
# Every value in the RTL is followed from where it is made to where it is
# added to another value, counting its fraction bits on the way:
#   a name that belongs to a `<NAME>_F` constant (MU_F -> mu, mu_s3, mu_v)  -> that many
#   an input port of the entity (the samples)                              -> 0, "sample" scale
#   a * b                              -> fraction bits of a + fraction bits of b
#   shift_right(a, n) / shift_left(a, n) -> n fewer / n more
#   resize, signed, unsigned, abs, -, sat..()/round..() helpers -> unchanged
#   a + b, a - b                       -> both must have the same count
# Anything else (literals, slices, other function calls, `when`) is unknown
# and never reported.
#
# Reported: an add or subtract whose two sides differ by a WHOLE fraction
# (at least the smallest `_F` constant of the file) AND were multiplied by
# different amounts of named fraction. A smaller difference is a gain
# written as a shift (`shift_right(v2, 1)` for 0.5 * v2); two sides that
# differ only by shifts are two gains (`K1*e + acc` with K1 = 2**-7 and
# K2 = 2**-15). Both are left alone. Values of a different kind are never
# compared: `K1*e + acc` adds a
# product of two samples to a loop value, and how many bits that shift needs
# depends on the gain, which this file cannot know.
#
# Reviewed timing_recovery RTL (2026-11-10): `p1 := v2 * mu` (mu: 1.0 = 2**16)
# then `(resize(p1, P2_W) + resize(v1, P2_W)) * mu` and
# `resize(p2, SUM_W) + resize(xm, SUM_W)`, with one `shift_right(sum, 15)` at
# the end. product_added_unscaled only sees a product register added
# directly to a narrower plain signal; this follows the count through
# variables, stage registers, shifts and function parameters.

from __future__ import annotations

import re

from agents.rtl_agent.lint.rtl_widths import (
    additive_terms, assignments, int_value, scoped_widths, source_line, split_top, strip_comments,
)

# (how many samples were multiplied into the value,
#  fraction bits that came from named `_F` fractions,
#  fraction bits in total: the named ones plus every shift)
Format = tuple[int, int, int]

_FRACTION_CONSTANT_RE = re.compile(r"^(\w+?)_(?:f|frac|fbits|frac_bits)$")
_INPUT_PORT_RE = re.compile(r"\b(\w+)\s*:\s*in\s+(?:std_logic_vector|std_ulogic_vector|signed|unsigned)\b",
                            re.IGNORECASE)
_NAME_RE = re.compile(r"[A-Za-z_]\w*")
_CALL_RE = re.compile(r"(\w+)\s*\((.*)\)", re.IGNORECASE | re.DOTALL)
_SLICE_OR_INDEX_RE = re.compile(r"(\w+)\s*\([^()]*\)")
_SAME_SCALE_CALLS = ("resize", "signed", "unsigned", "std_logic_vector", "std_ulogic_vector")
# helpers of the design that give back their argument, limited or rounded: sat12(x), clip(x), round_s(x)
_SAME_SCALE_HELPER_RE = re.compile(r"^(sat|clip|clamp|limit|round|trunc)", re.IGNORECASE)
_CONFLICT = "conflict"
MAX_PASSES = 40                 # one pass per pipeline row is enough; 40 rows is far beyond any design here


def fraction_constants(constants: dict[str, int]) -> dict[str, int]:
    """{value name: fraction bits} from the file's `<NAME>_F` constants:
    `constant MU_F : integer := 16` -> {"mu": 16}."""
    found = {}
    for name, value in constants.items():
        m = _FRACTION_CONSTANT_RE.match(name)
        if m and 0 < value <= 64:
            found[m.group(1)] = value
    return found


def _unwrap(expr: str) -> str:
    """`((x))` -> `x`."""
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        inner, depth = expr[1:-1], 0
        for ch in inner:
            depth += ch == "("
            depth -= ch == ")"
            if depth < 0:
                return expr                              # `(a) + (b)`: not one bracket pair
        expr = inner.strip()
    return expr


class _Tracker:
    def __init__(self, code: str):
        self.code = code
        self.scopes = scoped_widths(code)
        self.constants = self.scopes.constants
        self.seeds = fraction_constants(self.constants)
        stripped = strip_comments(code)
        entity = re.search(r"\bentity\b.*?\bend\b", stripped, re.IGNORECASE | re.DOTALL)
        self.inputs = {m.group(1).lower() for m in _INPUT_PORT_RE.finditer(entity.group(0))} if entity else set()
        self.statements = list(assignments(code))
        self.signals: dict[str, Format | str] = {}
        self.variables: dict[tuple, Format | None] = {}
        self.parameters: dict[tuple, Format | str] = {}
        self.issues: list[dict] = []
        self.reporting = False
        self.line = 0

    # -- names ---------------------------------------------------------
    def _seed(self, name: str) -> Format | None:
        if name in self.constants and name not in self.scopes.at(self.line):
            return None                                         # MU_F, MU_W, MU_ONE: integers, not values
        for prefix, bits in self.seeds.items():
            # mu, mu_s3, mu_v, mu_i_s4, eta_next; not mu_sq_s4 (a value built from mu has its own scale)
            if re.fullmatch(rf"{re.escape(prefix)}(_[iq])?(_(s\d+|r|v|w|d\d*|reg|next|prev))*", name):
                return 0, bits, bits
        return None

    def _scope(self, line: int) -> tuple | None:
        scope = self.scopes._innermost(line)
        return scope[:2] if scope else None

    def _name(self, name: str) -> Format | None:
        seeded = self._seed(name)
        if seeded:
            return seeded
        scope = self._scope(self.line)
        if (scope, name) in self.variables:
            return self.variables[(scope, name)]
        known = self.parameters.get((scope, name), self.signals.get(name))
        if isinstance(known, tuple):
            return known
        return (1, 0, 0) if name in self.inputs else None

    # -- expressions ---------------------------------------------------
    def format_of(self, expr: str) -> Format | None:
        expr = _unwrap(expr)
        if not expr or re.search(r"\bwhen\b|=>|&", expr):
            return None
        terms = additive_terms(expr)
        if len(terms) > 1:
            return self._sum(expr, terms)
        factors = split_top(expr, "*")
        if len(factors) == 2:
            a, b = (self._factor(f) for f in factors)
            if a == "integer":
                return b if isinstance(b, tuple) else None      # x * 3: a gain, the scale stays
            if b == "integer":
                return a if isinstance(a, tuple) else None
            return (a[0] + b[0], a[1] + b[1], a[2] + b[2]) if a and b else None
        if len(factors) > 2:
            return None
        m = re.match(r"^(?:abs\b|-)\s*(.+)$", expr, re.IGNORECASE | re.DOTALL)
        if m:
            return self.format_of(m.group(1))
        if _NAME_RE.fullmatch(expr):
            return self._name(expr.lower())
        m = _SLICE_OR_INDEX_RE.fullmatch(expr)
        if m and m.group(1).lower() in self.inputs:
            return 1, 0, 0                                      # a field of the input word: one sample
        m = _CALL_RE.fullmatch(expr)
        if not m:
            return None
        func, args = m.group(1).lower(), split_top(m.group(2), ",")
        if func in _SAME_SCALE_CALLS or (len(args) == 1 and _SAME_SCALE_HELPER_RE.match(func)
                                         and func in self.scopes.functions):
            return self.format_of(args[0])
        if func in ("shift_right", "shift_left") and len(args) == 2:
            inner, by = self.format_of(args[0]), int_value(args[1], self.constants)
            if inner is None or by is None:
                return None
            return inner[0], inner[1], inner[2] + (by if func == "shift_left" else -by)
        for arg in args:                                        # an unknown call: still check the sums inside it
            self.format_of(arg)
        return None

    def _factor(self, expr: str) -> Format | str | None:
        text = _unwrap(expr).lower()
        if re.fullmatch(r"\d+", text) or (text in self.constants and not self._seed(text)):
            return "integer"
        return self.format_of(expr)

    def _sum(self, expr: str, terms: list[str]) -> Format | None:
        known = [(t, f) for t in terms for f in [self.format_of(t)] if f is not None]
        if not known:
            return None
        if len({f[0] for _, f in known}) > 1:
            return None                                         # different kinds of value: the gain decides, not us
        widest = max(known, key=lambda tf: tf[1][2])
        if self.reporting:
            for term, fmt in known:
                # a whole fraction apart, and not multiplied by the same named fractions
                if fmt[1] != widest[1][1] and widest[1][2] - fmt[2] >= min(self.seeds.values()):
                    self._report(widest[0], term, widest[1], fmt)
                    break
        return widest[1]

    def _amount(self, bits: int) -> str:
        """16 -> `MU_F`, 32 -> `2*MU_F`, anything else the number."""
        for name, value in sorted(self.seeds.items()):
            if bits % value == 0 and bits // value in (1, 2, 3):
                return f"{name.upper()}_F" if bits == value else f"{bits // value}*{name.upper()}_F"
        return str(bits)

    def _report(self, big: str, small: str, big_fmt: Format, small_fmt: Format) -> None:
        if any(i["detail"].startswith(f"line {self.line}:") for i in self.issues):
            return                                              # one message per line
        big, small = " ".join(_unwrap(big).split()), " ".join(_unwrap(small).split())
        gap, named = big_fmt[2] - small_fmt[2], big_fmt[1] - small_fmt[1]
        constants = ", ".join(f"{name.upper()}_F = {bits}" for name, bits in sorted(self.seeds.items()))
        if named > 0:
            by = self._amount(named)
            fix = (f"Bring both sides to the same scale BEFORE the add: `shift_right({big}, {by})`, or shift "
                   f"`{small}` left by {by} and shift the sum back once.")
        else:
            fix = (f"`{small}` was shifted back further than the fractions it was multiplied by; shift each "
                   f"product back by exactly the fraction bits of its multiplier, once.")
        self.issues.append({
            "code": "fraction_bits_misaligned",
            "detail": f"line {self.line}: this add mixes two scales. `{big}` has {gap} more fraction bits than "
                      f"`{small}` ({big_fmt[2]} against {small_fmt[2]}, counted from the multiplies and shifts "
                      f"that made them; fraction constants of the file: {constants}), so `{big}` counts "
                      f"2**{gap} times too much in the sum and `{small}` is lost in its lowest bits. {fix} "
                      f"After every multiply by a fraction the product has that fraction's bits on top of its "
                      f"own: y = shift_right((shift_right(v2*mu, MU_F) + v1) * mu, MU_F) + v0 "
                      f">> {source_line(self.code, self.line)}"})

    # -- the file ------------------------------------------------------
    def _read_parameters(self) -> bool:
        """A function parameter has the scale of the values the calls pass,
        when every call passes the same one."""
        stripped = strip_comments(self.code)
        changed = False
        by_function: dict[str, list[dict]] = {}
        for p in self.scopes.parameters:
            by_function.setdefault(p["function"], []).append(p)
        for function, params in by_function.items():
            first, last = params[0]["span"]
            for cm in re.finditer(rf"\b{re.escape(function)}\s*\(", stripped, re.IGNORECASE):
                line = stripped.count("\n", 0, cm.start()) + 1
                if first <= line <= last:
                    continue
                depth, end = 1, cm.end()
                while end < len(stripped) and depth:
                    depth += {"(": 1, ")": -1}.get(stripped[end], 0)
                    end += 1
                args = split_top(stripped[cm.end():end - 1], ",")
                self.line, reporting = line, self.reporting
                self.reporting = False                          # the call's own line is checked as a statement
                for p, arg in zip(params, args):
                    changed |= self._merge(self.parameters, (p["span"], p["name"]), self.format_of(arg))
                self.reporting = reporting
        return changed

    @staticmethod
    def _merge(store: dict, key, fmt: Format | None) -> bool:
        if fmt is None or store.get(key) in (fmt, _CONFLICT):
            return False
        store[key] = fmt if key not in store else _CONFLICT
        return True

    def _pass(self) -> bool:
        changed = self._read_parameters()
        self.variables = {}
        for line, target, op, expr in self.statements:
            self.line = line
            fmt = self.format_of(expr)
            name = target.lower()
            if op == ":=":
                self.variables[(self._scope(line), name)] = fmt
            elif not self._seed(name):
                changed |= self._merge(self.signals, name, fmt)
        return changed

    def run(self) -> list[dict]:
        if not self.seeds:
            return []                                           # no `_F` constant: nothing says where 1.0 is
        for _ in range(MAX_PASSES):
            if not self._pass():
                break
        self.reporting = True
        self._pass()
        return self.issues


def fixed_point_issues(code: str) -> list[dict]:
    """[{code: "fraction_bits_misaligned", detail}] for every add or subtract
    whose sides differ by a whole fraction; [] when the file names no
    `<NAME>_F` fraction constant."""
    return _Tracker(code).run()

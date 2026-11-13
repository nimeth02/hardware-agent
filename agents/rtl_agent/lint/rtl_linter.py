# agents/rtl_agent/lint/rtl_linter.py — lint check for generated VHDL RTL.
#
# Static checks always run (deterministic, no tools needed): entity
# present, name matches the spec, architecture present, every spec port
# referenced, no placeholders (TODO/FIXME/... and comments that say the code
# is not written yet), no output valid tied to '0'. Then, when Vivado's xvhdl
# is reachable, the code is compiled for real syntax errors.

from __future__ import annotations

import json
import re
import tempfile

from agents.lint_utils import compiler_errors
from agents.mcp_utils import mcp_run_xelab, mcp_run_xvhdl, mcp_write_files
from agents.rtl_agent.copy_guard import copied_answer
from agents.rtl_agent.lint.rtl_equation_check import equation_issues, loop_gain_issues, polynomial_issues
from agents.rtl_agent.lint.rtl_fixed_point import fixed_point_issues
from agents.rtl_agent.lint.rtl_smoke_check import smoke_issues
from agents.rtl_agent.lint.rtl_widths import (
    additive_terms, assignments, expr_width, int_value, scoped_widths, source_line, split_top, strip_comments,
)
from agents.vhdl_version import VHDL_93, resolve_vhdl_version, xvhdl_std

_ENTITY_RE = re.compile(r"\bentity\s+(\w+)\s+is", re.IGNORECASE)

# Comments that say "this part is not written yet": the model's own planning
# leaked into the file. timing_recovery run 09:31 (2026-11-02): the NCO
# process body was only "-- Wait, I need to declare variables", "-- I will
# generate the full code in the final answer", "-- Placeholder for the rest
# of the code"; TODO/FIXME/... never matched, the output valid was tied to
# '0', and the same file came back eight times. Only comment text is checked.
_STUB_COMMENT_RE = re.compile(
    r"placeholder|final answer|rest of the (code|design|logic|process)|"
    r"in the (next|final) (step|answer|version|message|response)|"
    r"(to be|will be|still) (implemented|written|filled in|completed)|"
    r"not (yet )?(implemented|written|done)|\bfor now\b|"
    r"\bI (will|'ll) (generate|write|rewrite|replace|fix|restart|redo|add|provide|include)\b|"
    r"\blet's (rewrite|restart|redo|replace|fix|start over)\b",
    re.IGNORECASE)

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
    # timing_recovery 2026-11-02: `x sra 7` cost a compile round ("found '0'
    # definitions of operator sra"); on signed/unsigned these are 2008-only.
    (re.compile(r"\b(sra|srl)\b", re.IGNORECASE),
     "{0} on signed/unsigned is VHDL-2008 only; use shift_right(x, n) from numeric_std"),
    (re.compile(r"\b(sla|sll)\b", re.IGNORECASE),
     "{0} on signed/unsigned is VHDL-2008 only; use shift_left(x, n) from numeric_std"),
    (re.compile(r"\b(rol|ror)\b", re.IGNORECASE),
     "{0} on signed/unsigned is VHDL-2008 only; use rotate_left(x, n) / rotate_right(x, n) from numeric_std"),
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
_CALL_RE = re.compile(r"(\w+)\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_INT_RE = re.compile(r"^\d+$")


def _declaration_of(code: str, name: str) -> tuple[str, str] | None:
    """('signal' | 'variable', 'signed' | 'unsigned' | 'std_logic_vector') of
    a declared name, or None when it is not declared that way."""
    m = re.search(rf"\b(signal|variable)\s+(?:\w+\s*,\s*)*{re.escape(name)}\s*(?:,\s*\w+\s*)*:\s*"
                  rf"(signed|unsigned|std_logic_vector)\b", strip_comments(code), re.IGNORECASE)
    return (m.group(1).lower(), m.group(2).lower()) if m else None


_PLAIN_NAME_RE = re.compile(r"^\w+$")
# "(31 downto 0)": a range typed as numbers, no width name in it
_TYPED_RANGE_RE = re.compile(r"\(\s*\d+\s+downto\s+\d+\s*\)\s*$")
_RANGE_TEXT_RE = re.compile(r"\w+\s*\(\s*(.+?)\s+downto\s+(.+?)\s*\)\s*$", re.IGNORECASE | re.DOTALL)


def _width_name(code: str, name: str, widths) -> tuple[str, str | None]:
    """How to write the width of a declared vector as a NAME, plus the
    declaration step that makes it so (None when nothing is needed):
      mu_s4  : signed(MU_W-1 downto 0)   -> ("MU_W", None)
      v2i_s4 : signed(13 downto 0)       -> ("V2I_W", "declare `constant V2I_W : integer := 14;` and change
                                              `signal v2i_s4 : signed(13 downto 0)` to `signal v2i_s4 : signed(V2I_W-1 downto 0)`")
    The constant drops a trailing stage suffix (`_s4`) so every stage copy of
    the same value shares one constant; if that name is already a different
    constant, the full name is used."""
    typ = widths.types.get(name.lower(), "")
    m = _RANGE_TEXT_RE.match(typ)
    hi, lo = (m.group(1).strip(), m.group(2).strip()) if m else ("", "0")
    if not m or not (_INT_RE.match(hi) and _INT_RE.match(lo)):
        if lo == "0":
            m1 = re.fullmatch(r"(.+?)\s*-\s*1", hi)
            return (m1.group(1).strip() if m1 else f"({hi}+1)"), None
        return f"({hi}-({lo})+1)", None
    width = widths[name.lower()]
    constant = re.sub(r"_s\d+$", "", name, flags=re.IGNORECASE).upper() + "_W"
    if widths.constants.get(constant.lower(), width) != width or constant.lower() in widths:
        constant = name.upper() + "_W"
    decl = _declaration_of(code, name)
    keyword = decl[0] if decl else "signal"
    kind = typ.split("(", 1)[0]
    return constant, (f"declare `constant {constant} : integer := {width};` and change `{keyword} {name} : {typ}` to "
                      f"`{keyword} {name} : {kind}({constant}-1 downto 0)`")


def _natural_factor(factor: str, widths: dict[str, int]) -> tuple[str, int | None]:
    """A multiplier factor with its resize() wrapper taken off, and the width
    of what is left: `resize(signed('0' & mu_q8), 22)` -> ("signed('0' & mu_q8)", 9).
    The width may be a number or written from constants (`I_W+V2_W-1`):
    timing_recovery 2026-11-11 wrote `p0 := resize(xm, I_W+V2_W-1) * c0;`
    into a p0 of the right size, the wrapper was not taken off, and three
    rounds were told to make p0 43 bits "with no resize() around any factor"."""
    factor = factor.strip()
    m = _CALL_RE.match(factor)
    if m and m.group(1).lower() == "resize":
        args = split_top(m.group(2), ",")
        sized = len(args) == 2 and int_value(args[1], getattr(widths, "constants", None)) is not None
        inner = args[0].strip() if sized else ""
        if inner and inner.count("(") == inner.count(")"):
            return inner, expr_width(inner, widths)
    return factor, expr_width(factor, widths)


def _multiplier_width_issues(code: str) -> list[dict]:
    """numeric_std gives a product as wide as BOTH inputs added together;
    xvhdl compiles a mismatched target, but the simulation then stops with
    'array size mismatch'. Checks `target <= A * B;` where the width of A and
    B can be worked out (names, resize, signed/unsigned, '&', slices), in
    every statement position (after ';', 'then', 'else', 'begin').

    The fix is spelled out at the factors' NATURAL widths, resize() wrappers
    taken off, as one exact declaration plus one exact line. timing_recovery
    (2026-10-30) was told "make p1 44 bits" for `resize(v2, 22) *
    resize(.., 22)`, dropped the resize() as the same message asked, and was
    then told "make p1 23 bits": two rounds for one line.

    Widths are looked up in the scope of the line (see WidthScopes): a
    process-local `prod_v` is checked against ITS declaration, not against a
    same-named variable of another process. The message quotes the line's
    own operator: a variable is written with `:=`, and "write `prod_v <= ...`"
    would turn a correct variable write into a compile error.

    For the plain shape `p <= a * b` (two declared names, no wrapper) the
    product must also be DECLARED from the factors' width names
    (`signed(A_W+B_W-1 downto 0)`), as the rulebooks ask: a typed-in
    `signed(31 downto 0)` is reported even when it is right today. Every
    run of 2026-11-05 drafted `p2i_s5 : signed(29 downto 0)` for a 14 x 18
    product (mu counted at 16 bits) and `p1 : signed(25 downto 0)` for
    13 x 14, fixed them with new typed numbers, and the same slip was back
    the next run and after every widening of a factor. With names the
    product follows its inputs and the fix text no longer teaches numbers."""
    scopes = scoped_widths(code)
    issues = []
    for n, target, op, expr in assignments(code):
        key = target.lower()
        widths = scopes.at(n)
        factors = split_top(expr, "*")
        if len(factors) != 2 or key not in widths or len(additive_terms(expr)) != 1:
            continue                        # a pure product only; "a * b - c" is the general width check's job
        wa, wb = expr_width(factors[0], widths), expr_width(factors[1], widths)
        left = widths[key]
        if wa is None or wb is None:
            continue
        fa, fb = factors[0].strip(), factors[1].strip()
        plain_names = _PLAIN_NAME_RE.match(fa) and _PLAIN_NAME_RE.match(fb) and fa.lower() in widths and fb.lower() in widths
        decl = _declaration_of(code, target)
        line = f"`{target} {op} {' '.join(expr.split())};`"
        if plain_names and decl:
            # the rulebook shape: the product declared from the two width names
            (name_a, hint_a), (name_b, hint_b) = _width_name(code, fa, widths), _width_name(code, fb, widths)
            keyword, kind = decl
            steps = [h for h in (hint_a, hint_b) if h]
            steps.append(f"declare `{keyword} {target} : {kind}({name_a}+{name_b}-1 downto 0);` "
                         f"({wa} + {wb} = {wa + wb} bits) and keep {line}")
            fix = "Write the product width from the factors' width names, in ONE step: " + "; then ".join(steps) + \
                  ". Never type the product width as a number."
            if left != wa + wb:
                head = (f"line {n}: {line} puts a {wa + wb}-bit product into the {left}-bit '{target}' ({fa} is {wa} "
                        f"bits, {fb} is {wb} bits; a product is as wide as both factors added, and the simulation "
                        f"stops with 'Array sizes do not match'). ")
                issues.append({"code": "multiplier_width", "detail": head + fix, "_resized": (key, wa + wb, n)})
            elif _TYPED_RANGE_RE.search(widths.types.get(key, "")):
                head = (f"line {n}: {line} stores the product in '{target}', declared with a typed-in width "
                        f"`{widths.types[key]}`. The number is right today ({wa} + {wb} = {wa + wb} bits), but a "
                        f"typed number goes stale the next time a factor is widened, and the simulation then stops "
                        f"with 'Array sizes do not match'. ")
                issues.append({"code": "multiplier_width", "detail": head + fix})
            continue
        if left == wa + wb:
            continue
        (plain_a, na), (plain_b, nb) = _natural_factor(fa, widths), _natural_factor(fb, widths)
        head = (f"line {n}: {line} puts a {wa + wb}-bit product into the "
                f"{left}-bit '{target}' ({fa} is {wa} bits, {fb} is {wb} bits; a product is as wide as both factors "
                f"added, and the simulation stops with 'Array sizes do not match'). ")
        resized = wa + wb                   # the width this message asks `target` to be declared at
        if na is not None and nb is not None and decl:
            keyword, kind = decl
            natural = resized = na + nb
            plain = (f"`{target} {op} {plain_a} * {plain_b};` ({na} + {nb} = {natural} bits: each factor at its own "
                     f"width, no resize() around any factor)")
            if natural == left:
                resized = None
                fix = (f"'{target}' is already {natural} bits: write {plain}. A resize() around a factor only makes "
                       f"the product wider; it never makes it fit.")
            elif all(_PLAIN_NAME_RE.match(f) and f.lower() in widths for f in (plain_a, plain_b)):
                # two declared names once the wrappers are off: the width is written from their width names
                (name_a, hint_a), (name_b, hint_b) = _width_name(code, plain_a, widths), _width_name(code, plain_b, widths)
                steps = [h for h in (hint_a, hint_b) if h]
                steps.append(f"declare `{keyword} {target} : {kind}({name_a}+{name_b}-1 downto 0);` and write {plain}")
                fix = ("Fix it in ONE step, the width written from the factors' width names: " + "; then ".join(steps) +
                       f". Where another width is needed later, `resize({target}, N)` on its own line.")
            else:
                fix = (f"Fix it in ONE step, exactly like this: declare `{keyword} {target} : {kind}({natural - 1} "
                       f"downto 0);` and write {plain}. Where a narrower value is needed later, `resize({target}, N)` "
                       f"on its own line.")
        else:
            fix = (f"Make '{target}' {wa + wb} bits, then resize() it afterwards on its own line. Do NOT widen the "
                   f"factors to match the target: resize() around a factor makes the product wider again.")
        issues.append({"code": "multiplier_width", "detail": head + fix,
                       **({"_resized": (key, resized, n)} if resized else {})})
    return issues


# "* to_signed(4, 16)", "* to_unsigned(8192, 25)", "* 4", "* 8192"
_POW2_FACTOR_RE = re.compile(r"\*\s*(?:(?:to_signed|to_unsigned)\s*\(\s*(\d+)\s*,\s*\d+\s*\)|(\d+)\b)", re.IGNORECASE)


def _power_of_two_multiply_issues(code: str) -> list[dict]:
    """`x * to_signed(4, 16)` or `x * 4` is a shift written as a multiply. It
    is slow in hardware and, worse, the product is twice as wide as x, so a
    16-bit target crashes the dry run with 'left array has 16 elements, right
    array has 32 elements' (llr_demap16, 2026-10-29 16:xx: inside calc_mmsb,
    three attempts could not find it). The recipe says shift; this enforces it."""
    scopes = scoped_widths(code)
    issues = []
    for n, target, _, expr in assignments(code):
        if target.lower() not in scopes.at(n):
            continue                        # an integer/natural (loop counter, generic maths) is fine
        for m in _POW2_FACTOR_RE.finditer(expr):
            value = int(m.group(1) or m.group(2))
            if value < 2 or value & (value - 1):
                continue                                        # not a power of two
            shift = value.bit_length() - 1
            issues.append({"code": "power_of_two_multiply",
                           "detail": f"line {n}: '{target}' multiplies by {value}, a power of two. Write it as a "
                                     f"shift on a value already widened to the target width: "
                                     f"`shift_left(resize(x, {target}'length), {shift})` — the multiply makes the "
                                     f"result {'twice as wide as x' if m.group(2) else 'as wide as both factors added'} "
                                     f"and crashes the dry run with 'Array sizes do not match' "
                                     f">> {source_line(code, n)}"})
            break
    return issues


def _lines_of(issues: list[dict]) -> set[int]:
    return {int(m.group(1)) for i in issues for m in [re.match(r"line (\d+):", i["detail"])] if m}


def _width_issues(code: str) -> list[dict]:
    """All width checks, each bad line explained once: the power-of-two
    message (it carries the fix) beats the multiplier message, which beats
    the general one. A product the multiplier message asks to be declared
    at a new width is counted at that new width on the other lines."""
    pow2 = _power_of_two_multiply_issues(code)
    mult = [i for i in _multiplier_width_issues(code) if _lines_of([i]) - _lines_of(pow2)]
    resized = [r for i in mult if (r := i.pop("_resized", None))]
    return pow2 + mult + _assignment_width_issues(code, _lines_of(pow2 + mult), resized)


def _multiplies_by(expr: str, name: str) -> bool:
    """True when `expr` is a two-factor product and `name` is read in one factor."""
    factors = split_top(expr, "*")
    return len(factors) == 2 and len(additive_terms(expr)) == 1 and \
        any(re.search(rf"\b{re.escape(name)}\b", f, re.IGNORECASE) for f in factors)


def _unconstrained_parameter_issues(code: str) -> list[dict]:
    """A function input written without a width (`mu : signed`) that the
    function multiplies. Such an input is as wide as whatever the call passes,
    so no reader of the file (the model included) can size `v2 * mu` from
    the function alone; a wrong product width then compiles and shows up only
    as a dry-run crash that names the process, not the line. timing_recovery
    (2026-11-10 14:43): `p1 := v2 * mu` was 13 + 18 bits into a 30-bit p1,
    and four fix rounds over two attempts returned the same file. The fix is
    the header: the width the calls pass, as its declared name (`mu :
    signed(MU_W-1 downto 0)`). Inputs that are only clamped or resized
    (`sat12(x : signed)`) are left alone: the recipe's helper shape."""
    scopes = scoped_widths(code)
    issues = []
    for p in scopes.parameters:
        if p["declared"]:
            continue
        first, last = p["span"]
        uses = [(n, expr) for n, _, _, expr in assignments(code)
                if first <= n <= last and _multiplies_by(expr, p["name"])]
        if not uses:
            continue
        n, _ = uses[0]
        head = (f"line {p['line']}: function `{p['function']}` takes `{p['name']} : {p['kind']}` with no width, and "
                f"line {n} multiplies by it (`{source_line(code, n)}`). An input with no width is as wide as whatever "
                f"each call passes, so the product's width cannot be read from the function, and a wrong product "
                f"width is found only by a dry-run crash that names the process, not the line. ")
        if p["width"] is not None:
            passed = sorted({arg for _, arg, _ in p["calls"]})
            fix = (f"Every call passes {', '.join(f'`{a}`' for a in passed[:3])} ({p['width']} bits): write "
                   f"`{p['name']} : {p['type']}` in the function header and keep the calls as they are.")
        else:
            seen = "; ".join(f"line {line} passes `{arg}`" for line, arg, _ in p["calls"][:3]) or "no call found"
            fix = (f"Give it the width of the value the call passes ({seen}): write "
                   f"`{p['name']} : {p['kind']}(<WIDTH_NAME>-1 downto 0)` in the function header, with the "
                   f"constant that sizes that value, and make every call pass a value of that width.")
        issues.append({"code": "unconstrained_function_parameter", "detail": head + fix})
    return issues


# `to_signed(65536, 16)`, `to_unsigned(ONE, E_BITS)`: a number given with a width (both may be constants)
_SIZED_LITERAL_RE = re.compile(r"\b(to_signed|to_unsigned)\s*\(\s*([^(),]+?)\s*,\s*([^(),]+?)\s*\)", re.IGNORECASE)
# the condition of an if / elsif / while
_CONDITION_RE = re.compile(r"\b(?:if|elsif|while)\b(.*?)\b(?:then|loop)\b", re.IGNORECASE | re.DOTALL)
# `mu_raw > 65535`, `cnt <= 300`, `x >= 2**16`, `y = LIMIT` inside such a condition
_COMPARE_LITERAL_RE = re.compile(r"\b(\w+)\s*(>=|<=|/=|>|<|=)\s*(-?\s*\w+(?:\s*\*\*\s*\d+)?)(?![\w(])", re.IGNORECASE)


def _int_range(width: int, signed: bool) -> tuple[int, int]:
    return (-(1 << (width - 1)), (1 << (width - 1)) - 1) if signed else (0, (1 << width) - 1)


def _bits_for(value: int, signed: bool) -> int:
    """The smallest width whose range holds `value`."""
    width = 1
    while width < 128:
        lo, hi = _int_range(width, signed)
        if lo <= value <= hi:
            return width
        width += 1
    return width


def _literal_width_issues(code: str) -> list[dict]:
    """A number that does not fit the width it is given.

    `to_signed(65536, 16)`: numeric_std keeps only the low 16 bits and
    merely warns "vector truncated", so the value becomes 0 and the design
    runs on with it. timing_recovery (2026-11-03 12:30): `eta_s8 <= eta_n +
    to_signed(65536, 16)` was meant to add 1.0 to the NCO phase; it added 0,
    the phase never wrapped back, the strobe fired 233 times instead of
    ~199 (then 28), and four attempts blamed the testbench. In the same
    file `if mu_raw > 65535` on a 16-bit signed could never be true: the
    library compares such a number correctly, so the branch is simply dead
    and nothing warns. Widths and numbers may be written as constants or
    generics; the width is looked up in the scope of the line."""
    scopes = scoped_widths(code)
    constants = scopes.constants
    stripped = _STRING_OR_CHAR_RE.sub(lambda m: " " * len(m.group(0)), strip_comments(code))
    statements = list(assignments(code))
    issues: list[dict] = []
    seen: set[int] = set()

    def line_of(pos: int) -> int:
        return stripped.count("\n", 0, pos) + 1

    def target_of(n: int, call: str) -> str | None:
        """The target of the assignment statement that holds `call` on or before line n."""
        wanted = re.sub(r"\s+", "", call).lower()
        for sn, target, _, expr in reversed(statements):
            if sn <= n and wanted in re.sub(r"\s+", "", expr).lower():
                return target
        return None

    for m in _SIZED_LITERAL_RE.finditer(stripped):
        func = m.group(1).lower()
        signed = func == "to_signed"
        value, width = int_value(m.group(2), constants), int_value(m.group(3), constants)
        if value is None or width is None or width < 1:
            continue
        lo, hi = _int_range(width, signed)
        n = line_of(m.start())
        if lo <= value <= hi or n in seen:
            continue
        seen.add(n)
        wrapped = value % (1 << width)
        if signed and wrapped >= 1 << (width - 1):
            wrapped -= 1 << width
        kind = "signed" if signed else "unsigned"
        need = _bits_for(value, signed)
        call = f"{func}({m.group(2).strip()}, {m.group(3).strip()})"
        target = target_of(n, m.group(0))
        typ = scopes.at(n).types.get(target.lower()) if target else None
        into = (f"`{target}`" + (f" (`{typ}`)" if typ else "")) if target else "the register this value goes into"
        scale_bits = width - 2 if signed else width - 1
        issues.append({"code": "literal_width",
                       "detail": f"line {n}: `{call}`: {value} does not fit in {width} {kind} bits (range {lo}..{hi}). "
                                 f"numeric_std keeps only the low {width} bits, so the value silently becomes {wrapped} "
                                 f"(xsim prints 'vector truncated' and carries on); {value} needs at least {need} {kind} "
                                 f"bits. Either widen {into} and every value it is added to or compared with, to "
                                 f"`{kind}({need - 1} downto 0)`, or keep {width} bits and put the whole loop on a "
                                 f"smaller scale (e.g. 1.0 = 2**{scale_bits} = {1 << scale_bits}) so that this number and "
                                 f"every value it meets fit. Do not just change the number. >> {source_line(code, n)}"})

    for c in _CONDITION_RE.finditer(stripped):
        for cm in _COMPARE_LITERAL_RE.finditer(c.group(1)):
            name, op, lit = cm.group(1), cm.group(2), " ".join(cm.group(3).split())
            n = line_of(c.start(1) + cm.start())
            widths = scopes.at(n)
            width = widths.get(name.lower())
            typ = (widths.types.get(name.lower()) or "").lower()
            if width is None or not typ.startswith(("signed", "unsigned")):
                continue
            value = int_value(lit, constants)
            if value is None:
                continue
            signed = typ.startswith("signed")
            lo, hi = _int_range(width, signed)
            if lo <= value <= hi or n in seen:
                continue
            seen.add(n)
            above = value > hi
            never = op in ((">", ">=", "=") if above else ("<", "<=", "="))
            kind = "signed" if signed else "unsigned"
            issues.append({"code": "literal_width",
                           "detail": f"line {n}: `{name} {op} {lit}` is {'never' if never else 'always'} true: `{name}` "
                                     f"is {width} {kind} bits (range {lo}..{hi}) and {value} lies outside it, so this "
                                     f"branch is {'dead' if never else 'taken every time'} and nothing warns. The number "
                                     f"is on the wrong scale for this width, or `{name}` is too narrow ({value} needs "
                                     f"{_bits_for(value, signed)} {kind} bits): fix the scale or the width, do not just "
                                     f"change the number. >> {source_line(code, n)}"})
    return issues


_CONVERSION_RE = re.compile(r"^(std_logic_vector|signed|unsigned)\s*\((.+)\)\s*$", re.IGNORECASE | re.DOTALL)


def _widened(expr: str, width: int) -> str:
    """`expr` rewritten to `width` bits. resize() works on signed/unsigned,
    so it goes INSIDE a std_logic_vector() conversion and around anything
    else: `std_logic_vector(y_sat)` -> `std_logic_vector(resize(y_sat, 24))`,
    `unsigned(x)` -> `resize(unsigned(x), 24)`."""
    expr = expr.strip()
    m = _CONVERSION_RE.match(expr)
    if m and m.group(1).lower() == "std_logic_vector" and m.group(2).count("(") == m.group(2).count(")"):
        return f"{m.group(1)}(resize({m.group(2).strip()}, {width}))"
    return f"resize({expr}, {width})"


def _assignment_width_issues(code: str, skip_lines: set[int],
                             resized: list[tuple[str, int, int]] | None = None) -> list[dict]:
    """Left and right side of `target <= expr` must have the same width; the
    compiler accepts a mismatch and the dry run then crashes ('Array sizes do
    not match, left array has L elements, right array has R elements') with
    a location that names only the process, not the line. Reported here, with
    the line, whenever both widths can be worked out. `skip_lines` are lines
    another width check already explained.

    A narrower right-hand side gets the exact widened line; a wider one gets
    the two-step advice. One message for both (timing_recovery, 2026-10-30)
    told `y_sym_i <= std_logic_vector(y_sat)` (12 into 24 bits) to "compute
    at the natural width, then resize as a separate step", which fits only
    the wide case, and the line survived two rounds.

    `resized` is (name, new width, line) for every product the multiplier
    check asks, this same round, to be declared at a new width. Lines that
    read such a name are counted at the NEW width. timing_recovery
    2026-11-11: `v2m1_2` (16 bits) was to become 32, and on the same round
    `c0 := V2_ONE + V2_QUARTER * v2m1_2` was told "a variable of exactly 32
    bits" (16 + 16); the model did both, the product was then 16 + 32 = 48
    bits, and the next round reported the helper it had just been told to
    add."""
    scopes = scoped_widths(code)
    all_assignments = list(assignments(code))

    def widths_at(line: int):
        """The widths on `line`, plus the names (and their new widths) other messages redeclare."""
        widths, here, changed = scopes.at(line), scopes._innermost(line), {}
        for name, width, at in resized or []:
            home = scopes._innermost(at)
            local = bool(home and name in home[2])              # declared inside that process / function
            if (here is home) if local else not (here and name in here[2]):
                if widths.get(name) != width:
                    widths[name], changed[name] = width, at
        return widths, changed

    def stored_into(name: str, line: int) -> tuple[str, int, int] | None:
        """The first later plain `X <= name;` / `X := name;` with X's width known: (X, width, line)."""
        for n, target, _, expr in all_assignments:
            if n > line and expr.strip().lower() == name.lower():
                width = scopes.at(n).get(target.lower())
                if width is not None:
                    return target, width, n
        return None

    def computed_at(name: str, line: int) -> tuple[int, str, str] | None:
        """The last `name := ...` / `name <= ...` before `line`: (line, op, expr)."""
        hits = [(n, op, expr) for n, target, op, expr in all_assignments if n < line and target.lower() == name.lower()]
        return hits[-1] if hits else None

    issues = []
    for n, target, op, expr in all_assignments:
        widths, changed = widths_at(n)
        if n in skip_lines or target.lower() not in widths:
            continue
        right = expr_width(expr, widths)
        left = widths[target.lower()]
        if right is None or right == left:
            continue
        shown = " ".join(expr.split())
        read = [(name, at) for name, at in changed.items() if re.search(rf"\b{re.escape(name)}\b", expr, re.IGNORECASE)]
        counted = ("" if not read else " (counted with " + ", ".join(
            f"`{name}` at {widths[name]} bits, as the message for line {at} asks" for name, at in read) + ")")
        if right < left:
            fix = (f"The right-hand side (`{shown}`) is NARROWER than the target. Widen it on this very line, "
                   f"exactly like this: `{target} {op} {_widened(shown, left)};` (resize() takes a signed/unsigned "
                   f"value; convert a std_logic_vector first)")
        else:
            # the width to resize to is the FINAL register's when `target` is only a helper on the way
            # there (timing_recovery 2026-11-03 11:17: "resize to 17" for v_next, then v_s12 is 16 bits)
            want, note = left, ""
            final = stored_into(target, n)
            if final and final[1] != left:
                want = final[1]
                typ = scopes.at(final[2]).types.get(final[0].lower()) or f"{want} bits"
                note = (f" — {want}, not {left}, because `{target}` is stored in `{final[0]}` ({want} bits, "
                        f"line {final[2]}): declare `{target}` as `{typ}` like `{final[0]}`")
            if re.fullmatch(r"\w+", shown):
                src = computed_at(shown, n)
                where = f" (line {src[0]}: `{shown} {src[1]} {' '.join(src[2].split())}` -> resize to {want} there)" if src else ""
                typ = widths.types.get(target.lower()) or f"{want} bits"
                fix = (f"The right-hand side `{shown}` is a plain {right}-bit value stored in the {left}-bit `{target}`: "
                       f"write `{target} {op} resize({shown}, {want});` on this line{note}. (If `{shown}` exists only "
                       f"to feed `{target}`, declare it as `{typ}` instead and resize where it is computed{where}.)")
            else:
                fix = (f"The right-hand side (`{shown}`) is WIDER than the target (a product is as wide as both factors "
                       f"added; a sum, a shift or a type conversion is as wide as its widest operand). Do not squeeze it "
                       f"on this line: put `{shown}` into a signal or variable of exactly {right} bits, then "
                       f"`{target} {op} resize(<that>, {want});` on its own line (or take the slice of it you need){note}")
        issues.append({"code": "assignment_width",
                       "detail": f"line {n}: '{target}' is {left} bits but the right-hand side is {right} bits{counted}; the "
                                 f"simulation stops with 'Array sizes do not match'. {fix} >> {source_line(code, n)}"})
    return issues


_PROCESS_RE = re.compile(r"\bprocess\b.*?\bend\s+process\b\s*\w*\s*;", re.IGNORECASE | re.DOTALL)
# a whole-signal assignment "name <=" at the start of a statement (not "if a <= b")
_TARGET_RE = re.compile(r"(?:^|;|\bthen\b|\belse\b|\bbegin\b|=>)\s*(\w+)\s*<=", re.IGNORECASE | re.MULTILINE)


# "p_main : " right before "process": the label of the process
_LABEL_BEFORE_RE = re.compile(r"(\w+)\s*:\s*(?:postponed\s+)?$", re.IGNORECASE)


def _multiple_driver_issues(code: str) -> list[dict]:
    """A signal written from two places (two processes, or a process and a
    plain `x <= ...;` line) has two drivers; unsigned/signed signals then stop
    the simulation. Only whole-signal writes are compared. The message names
    the processes (by label) and the lines, and points at a lone write, in
    practice a reset line in the wrong process: timing_recovery (2026-10-30)
    reset `strobe` and `mu_q8` in p_main while p_strobe computed them, and
    "process #1, process #2" was all the model got for six rounds."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    writers: dict[str, list[tuple[str, list[int]]]] = {}
    for n, m in enumerate(_PROCESS_RE.finditer(stripped), 1):
        label = _LABEL_BEFORE_RE.search(stripped[:m.start()])
        where = f"process {label.group(1)}" if label else f"process #{n}"
        lines: dict[str, set[int]] = {}
        for t in _TARGET_RE.finditer(m.group(0)):
            lines.setdefault(t.group(1).lower(), set()).add(stripped.count("\n", 0, m.start() + t.start(1)) + 1)
        for name, at in lines.items():
            writers.setdefault(name, []).append((where, sorted(at)))
    outside = _PROCESS_RE.sub(lambda m: "\n" * m.group(0).count("\n"), stripped)
    for m in _TARGET_RE.finditer(outside):
        line = outside.count("\n", 0, m.start(1)) + 1
        writers.setdefault(m.group(1).lower(), []).append(("a concurrent assignment", [line]))
    issues = []
    for name, places in writers.items():
        if len(places) < 2:
            continue
        listed = "; ".join(f"{where} (line{'s' if len(at) > 1 else ''} {', '.join(map(str, at))})"
                           for where, at in places)
        detail = (f"'{name}' is written in {len(places)} places: {listed}. A signal may have exactly ONE writer: "
                  f"keep every write of '{name}', including its reset, in the one process that computes it and "
                  f"delete the others.")
        lone = [(w, at) for w, at in places if len(at) == 1 and w.startswith("process")]
        busy = [(w, at) for w, at in places if len(at) > 1]
        if len(lone) == 1 and busy:
            detail += (f" {lone[0][0]} writes it only once (line {lone[0][1][0]}), most likely its reset line: "
                       f"delete that line there and reset '{name}' in {busy[0][0]} instead.")
        issues.append({"code": "multiple_drivers", "detail": detail})
    return issues


# "label: process (...)" up to its "begin": the declarative region where only
# variable/constant/... declarations may appear.
_PROCESS_DECLS_RE = re.compile(r"\bprocess\b(?:\s*\([^)]*\))?(.*?)\bbegin\b", re.IGNORECASE | re.DOTALL)
_DECL_KEYWORDS = ("variable", "constant", "alias", "type", "subtype", "file", "attribute", "function",
                  "procedure", "impure", "pure", "use", "shared")
_BARE_DECL_RE = re.compile(r"^\s*(\w+(?:\s*,\s*\w+)*)\s*:\s*(?!=)(\w+)", re.IGNORECASE)


_END_PROCESS_RE = re.compile(r"\bend\s+process\b", re.IGNORECASE)
_BODY_DECL_RE = re.compile(r"^[ \t]*(variable|signal|constant)\s+(\w+)", re.IGNORECASE | re.MULTILINE)


def _process_declaration_issues(code: str) -> list[dict]:
    """Inside a process, `temp : signed(47 downto 0);` without the `variable`
    keyword is a syntax error that xvhdl follows with a dozen 'is not a
    variable' / 'illegal in an expression' errors. Caught here with the fix.

    `end process` contains the word "process" too; it is blanked first so the
    text up to the NEXT process's `begin` is not read as a declarative
    region. Before that, the label of every second process (and of any
    generate or instance) was reported as a missing `variable`:
    timing_recovery (2026-10-30), `p_strobe : process(aclk)`, seven rounds,
    and the model deleted an `end if;` trying to obey."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    stripped = _END_PROCESS_RE.sub(lambda m: " " * len(m.group(0)), stripped)      # same length: lines unchanged
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


def _declaration_after_begin_issues(code: str) -> list[dict]:
    """A `variable`/`constant` line after the process's `begin`, or a `signal`
    line anywhere inside a process, is a syntax error xvhdl reports as
    "syntax error near 'variable'" and then "is illegal in an expression" for
    every use (timing_recovery retry, 2026-10-30: 13 follow-on errors from
    two misplaced lines). Reported with where the line belongs. A process
    whose declarative region holds a function/procedure (its own `begin`) is
    left to the compiler."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    issues = []
    for m in _PROCESS_RE.finditer(stripped):
        block = m.group(0)
        head = _PROCESS_DECLS_RE.match(block)
        if not head or re.search(r"\b(function|procedure)\b", head.group(1), re.IGNORECASE):
            continue
        for d in _BODY_DECL_RE.finditer(block, head.end()):
            n = stripped.count("\n", 0, m.start() + d.start()) + 1
            kw, name = d.group(1).lower(), d.group(2)
            belongs = ("in the architecture, above its `begin`; a signal can never be declared inside a process "
                       "(make it a `variable` here if only this process uses it)" if kw == "signal"
                       else "between `process(...)` and its `begin`")
            issues.append({"code": "declaration_after_begin",
                           "detail": f"line {n}: `{kw} {name}` is declared AFTER the process's `begin`; xvhdl reports "
                                     f"'syntax error near {kw}' and then 'is illegal in an expression' for every use. "
                                     f"Declarations go {belongs}. Move this one line there; the statements stay "
                                     f"where they are >> {source_line(code, n)}"})
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


# `if ... then` / `case ... is` / `... loop` and their `end if` / `end case` /
# `end loop`. `elsif` is one word, so \bif\b never matches inside it; `end if`
# is tried first so its `if` is not counted as an opener.
_BLOCK_TOKEN_RE = re.compile(r"\bend\s+(if|case|loop)\b|\b(if|case|loop)\b", re.IGNORECASE)


def _pair_by_indent(opens: list[tuple[int, int]], ends: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
    """(lines of opens left without an end, lines of ends left without an open).
    Each `end X` first takes the nearest earlier unmatched `X` at the same
    indentation; the ends still alone then take the nearest earlier unmatched
    `X` at any indentation. What remains is the mistake."""
    free = list(opens)
    alone = []
    for e_line, e_indent in ends:
        same = [o for o in free if o[0] < e_line and o[1] == e_indent]
        if same:
            free.remove(same[-1])
        else:
            alone.append((e_line, e_indent))
    extra = []
    for e_line, e_indent in alone:
        earlier = [o for o in free if o[0] < e_line]
        if earlier:
            free.remove(earlier[-1])
        else:
            extra.append(e_line)
    return [o[0] for o in free], extra


def _block_balance_issues(code: str) -> list[dict]:
    """Inside a process, an `if`/`case`/`loop` without its `end if`/`end case`/
    `end loop`, or one `end if;` too many, is a syntax error xvhdl reports only
    at `end process` as 'syntax error near process' and 'mismatch on label'.
    The model then hunts for a label problem and changes nothing
    (timing_recovery 15:11, 2026-11-02: one `end if;` missing after an
    if/elsif in p_nco, the identical file returned nine times). Counted per
    process; the block to fix is named by indentation (see _pair_by_indent)."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    blanked = _STRING_OR_CHAR_RE.sub(lambda m: " " * len(m.group(0)), stripped)   # `report "end if"` is prose
    lines = code.splitlines()
    issues = []
    for p in _PROCESS_RE.finditer(blanked):
        body = p.group(0)
        head = _PROCESS_DECLS_RE.match(body)              # a function declared in the process has its own if/end if
        first = head.end() if head else 0
        opens: dict[str, list] = {"if": [], "case": [], "loop": []}
        ends: dict[str, list] = {"if": [], "case": [], "loop": []}
        for m in _BLOCK_TOKEN_RE.finditer(body, first):
            pos = p.start() + m.start()
            line_start = blanked.rfind("\n", 0, pos) + 1
            text = blanked[line_start:blanked.find("\n", pos) if blanked.find("\n", pos) != -1 else len(blanked)]
            entry = (blanked.count("\n", 0, pos) + 1, len(text) - len(text.lstrip()))
            (ends if m.group(1) else opens)[(m.group(1) or m.group(2)).lower()].append(entry)
        proc_line = blanked.count("\n", 0, p.start()) + 1
        label = re.search(r"(\w+)\s*:\s*$", blanked[blanked.rfind("\n", 0, p.start()) + 1:p.start()])
        where = f"process {label.group(1) + ' ' if label else ''}(line {proc_line})"
        for kind in ("if", "case", "loop"):
            if len(opens[kind]) == len(ends[kind]):
                continue
            missing, extra = _pair_by_indent(opens[kind], ends[kind])
            if missing:
                n = missing[0]
                detail = (f"{where} opens {len(opens[kind])} `{kind}` but closes {len(ends[kind])} with `end {kind}`: "
                          f"{len(missing)} `end {kind};` missing. Most likely the `{kind}` at line {n} is never closed; "
                          f"add `end {kind};` after its last branch >> {lines[n - 1].strip() if n <= len(lines) else ''}. "
                          f"xvhdl reports this only as 'syntax error near process' / 'mismatch on label' at `end process`; "
                          f"the label is fine.")
            else:
                detail = (f"{where} closes {len(ends[kind])} `{kind}` with `end {kind}` but opens only {len(opens[kind])}: "
                          f"{len(extra)} `end {kind};` too many, at line {', line '.join(str(n) for n in extra)}. "
                          f"Remove that line; do not delete an `{kind}` to compensate.")
            issues.append({"code": "unclosed_block", "detail": detail})
    return issues


_INDEX_TXT = r"\((?:[^()]|\([^()]*\))*\)"                  # (7 downto 0), (to_integer(k))
# "x_s3 <= ... y_s1 ...;" — a register of stage K written from a register of
# stage J. Signal names follow the streaming recipe: <field>_s<stage>. The
# right-hand side stops BEFORE the `;` (a second statement on the same line is
# then seen too) and may not hold `then` (a two-line `if a and\n x_s1 <= x_s3
# then` is a comparison, not a write); a label, a record field and a one-level
# index on the target are allowed; `=>` starts a case alternative's statement.
_STAGE_ASSIGN_RE = re.compile(
    r"(?:^|;|\bthen\b|\belse\b|\bbegin\b|\bloop\b|=>)\s*(?:\w+\s*:(?!=)\s*)?(\w+?)_s(\d+)(?:\.\w+)?\s*"
    rf"(?:{_INDEX_TXT})?\s*<=\s*((?:(?!\bthen\b)[^;])+)(?=;)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE)      # MULTILINE: a statement on its own line starts at ^
_STAGE_REF_RE = re.compile(r"\b(\w+?)_s(\d+)\b", re.IGNORECASE)


def _pipeline_stage_skip_issues(code: str) -> list[dict]:
    """Stage K may read only stage K-1 registers. A value that is read from
    two stages back (i_mmsb_s3 <= calc_mmsb(i_s1) while i_s2 exists) arrives
    one clock EARLIER than the values that went through every stage, so it
    is combined with the previous row's data: in the llr_demap16 runs of
    2026-10-29 the metrics skipped stage 2 while ns did not, every beat was
    multiplied by the gap row's ns = 0, and m_axis_tdata was 0 for all 50
    beats (three attempts, byte-identical RTL each time). Caught here, the
    RTL agent gets the exact line instead of a simulation verdict."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    issues: list[dict] = []
    for m in _STAGE_ASSIGN_RE.finditer(stripped):
        target, k, expr = m.group(1), int(m.group(2)), m.group(3)
        line = stripped.count("\n", 0, m.start(1)) + 1
        skipped = sorted({(name, int(j)) for name, j in _STAGE_REF_RE.findall(expr) if k - int(j) >= 2})
        for name, j in skipped:
            issues.append({"code": "pipeline_stage_skip",
                           "detail": f"line {line}: {target}_s{k} is computed from {name}_s{j}, skipping stage(s) "
                                     f"{j + 1}..{k - 1}. Stage {k} may read only _s{k - 1} registers: a value that "
                                     f"skips a stage arrives one clock earlier than the values that do not, so it "
                                     f"is combined with the PREVIOUS row's data (e.g. a metric multiplied by the "
                                     f"gap row's ns = 0 gives output 0). Read {name}_s{k - 1} instead, and carry "
                                     f"{name} through the skipped stage(s) ({name}_s{j + 1} <= {name}_s{j}; ...)."})
    return issues


def _stage_map_issues(rtl: dict) -> list[dict]:
    """skills/RTL/pipeline_stage_map.md makes the model write a stage map
    (one line per stage, which registers it holds) as its first note before
    the code, because a stage that reads two stages back is caught far more
    easily in that table than in 150 lines of VHDL. A pipelined design (a
    _s2 register exists) returned without the map skipped that step, so it
    is sent back for one more round rather than trusted."""
    code = rtl.get("code") or ""
    if not re.search(r"(?<![\w.])\w+_s2\s*(?:\([^)]*\))?\s*<=", code, re.IGNORECASE):
        return []                                       # not a staged pipeline
    notes = rtl.get("notes") or []
    text = "\n".join(n for n in notes if isinstance(n, str))
    if re.search(r"stage\s*map", text, re.IGNORECASE):
        return []
    return [{"code": "stage_map_missing",
             "detail": "The design has pipeline stages (_s1, _s2, ...) but \"notes\" has no \"stage map\" entry. "
                       "Write the stage map first (one line per stage listing its registers, each line reading "
                       "only the line above; see pipeline_stage_map.md), then make the code follow it."}]


# one declaration statement with its names and type: "signal a_s1, c_s1 : signed(11 downto 0);"
_NAMED_DECL_RE = re.compile(r"\b(signal|variable|constant|alias)\s+([\w\s,]+?)\s*:\s*([^;]*);", re.IGNORECASE)
_OTHER_DECL_RE = re.compile(r"\b(?:function|procedure|type|subtype|component|package)\s+(\w+)", re.IGNORECASE)
# the entity's ports and generics, a component's ports, a subprogram's parameters: "name[, name] :"
_PARAM_LIST_RE = re.compile(r"\bentity\s+\w+\s+is\b.*?\bend\b|\bcomponent\s+\w+\b.*?\bend\s+component\b"
                            r"|\b(?:function|procedure)\s+\w+\s*\(((?:[^()]|\([^()]*\))*)\)", re.IGNORECASE | re.DOTALL)
_PARAM_NAME_RE = re.compile(r"(\w+(?:\s*,\s*\w+)*)\s*:\s*(?!=)")
# labels: "p_s1 : process", "g_s1 : for ...", "u_s1 : my_comp port map", "end process p_s1"
_LABEL_RE = re.compile(r"\b(\w+)\s*:\s*(?:postponed\s+)?(?:process|for|if|while|block|entity|component|configuration"
                       r"|\w+\s+(?:generic|port)\s+map)\b|\bend\s+(?:process|generate|block|loop)\s+(\w+)", re.IGNORECASE)
# a write at the start of a statement: "name <=", "name(i) <=", "name.field <=", "name :="
_WRITE_RE = re.compile(r"(?:^|;|=>|\bthen\b|\belse\b|\bbegin\b|\bloop\b|\bgenerate\b|\bselect\b)\s*(\w+)\s*"
                       r"(?:\((?:[^()]|\([^()]*\))*\)|\.\w+)*\s*(?:<=|:=)", re.IGNORECASE | re.MULTILINE)
_INSTANCE_OUTPUT_RE = re.compile(r"=>\s*(\w+)\s*[,)]")               # "q => x_s1," an instance drives it
_STAGE_NAME_RE = re.compile(r"(?<![\w.])(\w+?)_s(\d+)\b(?!\s*=>)", re.IGNORECASE)   # not a record field, not a formal


def _stage_register_declaration_issues(code: str) -> list[dict]:
    """Every pipeline register `<name>_s<K>` the code uses must be declared,
    and one that is read must be written somewhere. timing_recovery run 16:02
    (2026-11-02): `mu_s2 <= mu_s1;` and six more carry lines read stage-1
    registers that were never declared or written (loop values the last stage
    computes and feeds back to stage 1); xvhdl listed all seven, the follow-on
    filter showed one, and two fix rounds returned the same file. Reported
    here with the exact declaration (typed like the nearest `<name>_s<J>`
    sibling) and the exact line to write it on. A register that is read but
    never written compiles fine and stays undefined in the dry run, so it is
    reported too."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    stripped = _STRING_OR_CHAR_RE.sub(lambda m: " " * len(m.group(0)), stripped)
    stages = [int(k) for _, k in _STAGE_REF_RE.findall(stripped)]
    if not stages:
        return []
    k_max = max(stages)

    declared: dict[str, tuple[str, str, int]] = {}            # name -> (keyword, type, line)
    for m in _NAMED_DECL_RE.finditer(stripped):
        line = stripped.count("\n", 0, m.start()) + 1
        for name in m.group(2).split(","):
            declared[name.strip().lower()] = (m.group(1).lower(), m.group(3).strip(), line)
    other = {m.group(1).lower() for m in _OTHER_DECL_RE.finditer(stripped)}
    for block in _PARAM_LIST_RE.finditer(stripped):
        for m in _PARAM_NAME_RE.finditer(block.group(1) or block.group(0)):
            other |= {n.strip().lower() for n in m.group(1).split(",")}
    labels = {(m.group(1) or m.group(2)).lower() for m in _LABEL_RE.finditer(stripped)}
    writes = {(m.group(1).lower(), m.start(1)) for m in _WRITE_RE.finditer(stripped)}
    driven = {m.group(1).lower() for m in _INSTANCE_OUTPUT_RE.finditer(stripped)}

    # a name's own declaration is not a use: blanked at equal length so positions still match `writes`
    scan = _NAMED_DECL_RE.sub(lambda m: " " * len(m.group(0)), stripped)
    uses: dict[str, list[tuple[int, bool]]] = {}              # name -> [(line, is a write)], source order
    for m in _STAGE_NAME_RE.finditer(scan):
        name = m.group(0).lower()
        if name in labels or name in other:
            continue
        line = stripped.count("\n", 0, m.start()) + 1
        uses.setdefault(name, []).append((line, (name, m.start()) in writes or name in driven))

    def where_to_write(name: str, base: str, k: int) -> str:
        if k > 1:
            return f"write it in the stage-{k} process (`p_s{k}`) from the row above: `{name} <= {base}_s{k - 1};`"
        if k_max > 1 and f"{base}_s{k_max}" in declared:
            # a ring (`mu_s1 <= mu_s11`) would run k_max separate loops: the rule is one loop process
            return (f"do not copy it back from the last stage (`{name} <= {base}_s{k_max};` would run {k_max} "
                    f"separate loops). If `{base}` is a loop value, compute it in the loop process `p_loop` and "
                    f"read that instead of `{name}`: eta, acc, v and y_prev as `eta_r`, `acc_r`, `v_r`, `yprev_i_r` "
                    f"(no stage number; eta updated every input sample, the others on each strobe), strobe and mu "
                    f"as `strobe_s2` / `mu_s2`, carried down the rows. "
                    f"If it is data, write it in the stage-1 process (`p_s1`) from the entity input "
                    f"(see pipeline_stage_map.md, LOOP VALUES)")
        return f"write it in the stage-1 process (`p_s1`) from the entity input: `{name} <= <input>;`"

    issues = []
    for name, occ in uses.items():
        base, k = _STAGE_REF_RE.fullmatch(name).groups()
        k = int(k)
        written = any(w for _, w in occ)
        reads = [line for line, w in occ if not w]
        if name in declared:
            if declared[name][0] in ("signal", "variable") and reads and not written:
                issues.append({"code": "unwritten_stage_register",
                               "detail": f"line {reads[0]}: `{name}` is read but never written anywhere, so it keeps "
                                         f"its start value ('U' or the declared default) for the whole dry run and "
                                         f"the compiler does not complain. Either {where_to_write(name, base, k)}, "
                                         f"or delete it and the lines that read it >> {source_line(code, reads[0])}"})
            continue
        siblings = sorted((abs(int(m.group(2)) - k), n) for n in declared
                          for m in [_STAGE_REF_RE.fullmatch(n)] if m and m.group(1).lower() == base)
        if siblings:
            sib = siblings[0][1]
            kw, typ, line = declared[sib]
            add = f"add `{kw} {name} : {typ};` next to `{sib}` (line {line})"
        else:
            add = (f"add `signal {name} : <the type of the value it carries>;` in the architecture declarations, "
                   f"above its `begin`")
        first = occ[0][0]
        detail = (f"line {first}: `{name}` is used but never declared (the compiler says \"'{name}' is not "
                  f"declared\" and every later use fails too): {add}")
        if not written:
            detail += f", and {where_to_write(name, base, k)}"
        issues.append({"code": "undeclared_stage_register", "detail": f"{detail} >> {source_line(code, first)}"})
    return issues


_VALID_PIPE_DECL_RE = re.compile(
    r"\bvalid_pipe\s*:\s*std_logic_vector\s*\(\s*(\d+)\s+downto\s+(\d+)\s*\)", re.IGNORECASE)
# "m_axis_tvalid_i <= valid_pipe(6);" — the bit the output valid flag is taken from
# (a strobe-gated flag, "<= valid_pipe(6) and strobe_s7;", counts the same bit)
_VALID_OUT_RE = re.compile(r"(?:^|;|\bbegin\b)\s*(\w*valid\w*)\s*<=\s*valid_pipe\s*\(\s*(\d+)\s*\)\s*(?:and\b[^;]*)?;",
                           re.IGNORECASE)
_VALID_STAGE_RE = re.compile(r"\bvalid_s(\d+)\s*<=", re.IGNORECASE)
# "m_axis_tvalid <= valid_s4;" or "m_axis_tvalid <= pipe_s4.valid;" — valid taken from a stage register
_VALID_OUT_STAGE_RE = re.compile(r"(?:^|;|\bbegin\b)\s*(\w*valid\w*)\s*<=\s*((\w+_s(\d+))(?:\.\w+)?)\s*(?:and\b[^;]*)?;",
                                 re.IGNORECASE)
# any clocked "name <= expr;" inside a process (name not a stage register, not the valid chain)
_CLOCKED_ASSIGN_RE = re.compile(r"(?:^|;|\bthen\b|\belse\b|\bbegin\b|\bloop\b)\s*(\w+)\s*(?:\([^)]*\))?\s*<=\s*([^;]+)(?=;)",
                                re.IGNORECASE | re.DOTALL | re.MULTILINE)      # (?=;) keeps the ';' for the next statement
# "m_axis_tdata : out std_logic_vector(23 downto 0)" — an output port of the entity
_OUT_PORT_RE = re.compile(r"\b(\w+)\s*:\s*out\s+\w", re.IGNORECASE)
# "advance <= m_axis_tready or (not valid_pipe(6));" — the pipeline-enable line that names the last valid bit
_ADVANCE_RE = re.compile(r"(?:^|;|\bbegin\b)\s*(advance\s*<=[^;]*?valid_pipe\s*\(\s*\d+\s*\)[^;]*);", re.IGNORECASE | re.MULTILINE)
# words in an output expression that are not a signal to follow
_NOT_A_SIGNAL = {"std_logic_vector", "signed", "unsigned", "resize", "to_signed", "to_unsigned", "others", "abs",
                 "shift_right", "shift_left", "not", "and", "or", "xor", "when", "else", "downto", "to"}


def _assignments_to(name: str, text: str) -> list[tuple[int, str]]:
    """Every statement that writes `name` (whole or a slice): (offset of the name, right-hand side)."""
    pat = re.compile(rf"(?:^|;|\bthen\b|\belse\b|\bbegin\b|\bloop\b)\s*({re.escape(name)})\s*(?:\([^)]*\))?\s*<=\s*([^;]+);",
                     re.IGNORECASE | re.DOTALL | re.MULTILINE)
    return [(m.start(1), m.group(2)) for m in pat.finditer(text)]


def _output_data_stage(stripped: str, clocked_spans: list[tuple[int, int]]) -> tuple[int, list[str]] | None:
    """Which stage the data output really reads, and the plain registers
    between that stage and the port: (K, [register names]). The output port
    is the entity's `out` port named tdata (else data/dout); its assignment
    is followed one hop, `m_axis_tdata <= m_axis_tdata_reg;` and then
    `m_axis_tdata_reg <= ... y_s7 ...;`, a hop inside a clocked process being
    a register. timing_recovery 17:58 (2026-11-02): the output read _s7 and
    _s8.._s11 carried loop state back to _s1, so "11 stages" was the wrong
    number and the model's fix (11 valid bits) made the flag 3 clocks late.
    None when there is no such port or no stage register on its path."""
    def is_register(pos: int) -> bool:
        return any(a <= pos < b for a, b in clocked_spans)

    def stage_of(expr: str) -> int | None:
        stages = [int(k) for _, k in _STAGE_REF_RE.findall(expr)]
        return max(stages) if stages else None

    ports = [m.group(1) for m in _OUT_PORT_RE.finditer(stripped)]
    data_ports = [p for p in ports if "tdata" in p.lower()] or [p for p in ports if re.search(r"data|dout", p, re.I)]
    for port in data_ports:
        for pos, expr in _assignments_to(port, stripped):
            k = stage_of(expr)
            if k is not None:
                return k, ([port] if is_register(pos) else [])
            for name in dict.fromkeys(re.findall(r"\b[A-Za-z_]\w*", expr)):      # follow one plain register / wire
                if name.lower() in _NOT_A_SIGNAL:
                    continue
                for pos2, expr2 in _assignments_to(name, stripped):
                    k = stage_of(expr2)
                    if k is not None:
                        regs = ([name] if is_register(pos2) else []) + ([port] if is_register(pos) else [])
                        return k, regs
    return None


def _valid_data_length_issues(code: str) -> list[dict]:
    """The valid flag and the data must pass through the SAME number of
    registers. Counted here: data depth = stage registers _s1.._sK up to the
    stage the data output reads (see _output_data_stage; stages after it are
    the old loop ring and do not count) plus the output register(s) on that path;
    without a recognisable output port, the highest stage plus any register
    written from it inside a clocked process. Valid depth = the valid_pipe
    bit the output valid flag is taken from (+1), else the declared width,
    else the number of valid_sK registers. llr_demap16 run 12:18 (2026-10-29):
    seven data registers, six valid bits, so m_axis_tvalid rose one clock
    before m_axis_tdata_reg was written and every beat was read as 0. The
    finding names the exact lines to change (declaration, output flag,
    advance) so the model does not have to guess."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    # only registers count: `x_sK <=` inside a process with a clock edge (a
    # concurrent `i_s0 <= signed(...)` outside any process is a wire)
    clocked_spans = [(p.start(), p.end()) for p in _PROCESS_RE.finditer(stripped)
                     if re.search(r"\brising_edge\s*\(", p.group(0))]
    clocked = [stripped[a:b] for a, b in clocked_spans]
    stages = sorted({int(m.group(2)) for body in clocked for m in _STAGE_ASSIGN_RE.finditer(body)})
    if not stages:
        return []
    k_min, k_max = stages[0], stages[-1]

    registered_valid = False
    for body in clocked:
        for m in _CLOCKED_ASSIGN_RE.finditer(body):
            if re.fullmatch(r"\w*(tvalid|valid_out|out_valid)\w*", m.group(1), re.IGNORECASE):
                registered_valid = True             # the output valid flag is itself a register

    out_data = _output_data_stage(stripped, clocked_spans)
    if out_data and k_min <= out_data[0] <= k_max:
        k_out, regs = out_data
        extra = regs[-1] if regs else None
        data_depth = k_out - k_min + 1 + len(regs)
        data_path = f"_s{k_min}.._s{k_out}" + "".join(f", then `{r}`" for r in regs)
        if k_out < k_max:
            data_path += (f"; _s{k_out + 1}.._s{k_max} come AFTER the output row and are not counted: nothing may "
                          f"come after the output row, those rows are the old loop ring; move their loop values into "
                          f"`p_loop` as `<name>_r` and delete the rows (pipeline_stage_map.md, LOOP VALUES; linter: "
                          f"loop_state_ring)")
    else:
        k_out = k_max
        data_depth = k_max - k_min + 1
        data_path = f"_s{k_min}.._s{k_max}"
        # a register after the last stage: written inside a clocked process from _s<k_max> names
        last_stage = re.compile(rf"\b\w+_s{k_max}\b", re.IGNORECASE)
        extra = None
        for body in clocked:
            for m in _CLOCKED_ASSIGN_RE.finditer(body):
                target = m.group(1)
                if re.search(r"_s\d+$", target, re.IGNORECASE) or "valid" in target.lower():
                    continue
                if last_stage.search(m.group(2)) and not extra:
                    extra = target
        if extra:
            data_depth += 1
            data_path += f", then `{extra}`"

    valid_depth, valid_path = None, ""

    def output_valid(pattern: re.Pattern) -> re.Match | None:
        """First match whose target is the output flag, not a chain register
        (`valid_s2 <= valid_s1;` is a shift step, not the output). A stage
        number anywhere in the name marks a chain register, oddly named or
        not: timing_recovery 15:11 (2026-11-02) read `valid_s3_f <= valid_s2;`
        as the output flag and reported an eight-deep chain as two."""
        return next((m for m in pattern.finditer(stripped)
                     if not re.search(r"_s\d+(_|$)", m.group(1), re.IGNORECASE)), None)

    out = output_valid(_VALID_OUT_RE)
    out_stage = output_valid(_VALID_OUT_STAGE_RE)
    decl = _VALID_PIPE_DECL_RE.search(stripped)
    if out:
        valid_depth = int(out.group(2)) + 1
        valid_path = f"valid_pipe(0)..valid_pipe({out.group(2)}), {out.group(1)} <= valid_pipe({out.group(2)})"
    elif out_stage:                                  # m_axis_tvalid <= valid_s4 / pipe_s4.valid
        valid_depth = int(out_stage.group(4)) - k_min + 1
        valid_path = f"stage registers up to _s{out_stage.group(4)}, {out_stage.group(1)} <= {out_stage.group(2)}"
    elif decl:
        valid_depth = int(decl.group(1)) - int(decl.group(2)) + 1
        valid_path = f"valid_pipe({decl.group(2)} .. {decl.group(1)})"
    else:
        valid_stages = sorted({int(k) for k in _VALID_STAGE_RE.findall(stripped)})
        if valid_stages:
            valid_depth = valid_stages[-1] - min(k_min, valid_stages[0]) + 1
            valid_path = f"valid_s{valid_stages[0]}..valid_s{valid_stages[-1]}"
    if valid_depth is None:
        return []                                   # no recognisable valid chain: nothing to count against
    if extra and registered_valid:
        valid_depth += 1                            # the output valid register is the extra stage's valid bit
        valid_path += " + the registered output valid flag"
    if valid_depth == data_depth:
        return []
    gap = abs(valid_depth - data_depth)
    early = f"{gap} clock{'s' if gap > 1 else ''} {'BEFORE' if valid_depth < data_depth else 'AFTER'}"
    last = data_depth - 1                            # the bit the output valid flag must come from
    if extra and valid_depth == data_depth - 1:
        fix = (f"either delete `{extra}` and drive the output straight from the stage-{k_out} registers "
               f"(`m_axis_tdata <= std_logic_vector(... _s{k_out} ...)` as a plain assignment outside any "
               f"process), or give `{extra}` its own valid bit: declare valid_pipe({last} downto 0), "
               f"write `valid_pipe({last}) <= valid_pipe({last - 1});` in the valid-chain process "
               f"(p_valid, the only writer of valid_pipe), "
               f"and use valid_pipe({last}) for m_axis_tvalid and for `advance`")
    else:
        fix = (f"make the valid chain exactly {data_depth} bits long (one bit per data register up to the "
               f"output, the output valid flag taken from valid_pipe({last})), or remove the data register(s) "
               f"nothing needs; update the stage map so every row up to the output ends with its own valid bit")

    # the exact lines, so the model edits rather than guesses
    def line_of(pos: int) -> int:
        return stripped.count("\n", 0, pos) + 1

    exact = []
    if decl and int(decl.group(1)) != last:
        exact.append(f"line {line_of(decl.start())}: `valid_pipe : std_logic_vector({decl.group(1)} downto "
                     f"{decl.group(2)})` -> `({last} downto 0)`")
    if out and int(out.group(2)) != last:
        exact.append(f"line {line_of(out.start(1))}: `{out.group(1)} <= valid_pipe({out.group(2)})` -> "
                     f"`valid_pipe({last})`")
    adv = _ADVANCE_RE.search(stripped)
    if adv and not re.search(rf"valid_pipe\s*\(\s*{last}\s*\)", adv.group(1)):
        exact.append(f"line {line_of(adv.start(1))}: `{adv.group(1).strip()}` -> use valid_pipe({last})")
    if decl and valid_depth < data_depth:
        exact.append("in p_valid add " + " ".join(f"`valid_pipe({n}) <= valid_pipe({n - 1});`"
                                                  for n in range(valid_depth, data_depth)))
    elif decl:
        exact.append(f"in p_valid delete the shifts above valid_pipe({last})")
    lines = (" Exact lines: " + "; ".join(exact) + ".") if exact else ""
    return [{"code": "valid_data_length_mismatch",
             "detail": f"the data passes through {data_depth} registers ({data_path}) but the valid flag through "
                       f"{valid_depth} ({valid_path}). The valid flag therefore reaches the output {early} "
                       f"the data: the testbench reads the output register's old/reset value (\"expected X got 0\") "
                       f"or the next beat's value. Fix: {fix}.{lines}"}]


# "abs_i_s2 <= unsigned(-resize(i_s1, 13));" — a unary minus at the start of an operand
_UNARY_MINUS_RE = re.compile(r"(?:^\s*|\(\s*)-\s*\w")
_ABS_TARGET_RE = re.compile(r"abs|mag", re.IGNORECASE)


def _abs_without_sign_check_issues(code: str) -> list[dict]:
    """`abs_x <= unsigned(-x)` with no test of the sign is not an absolute
    value: it negates positive inputs too. The right form is the numeric_std
    operator (`abs resize(x, 13)`) or an if/else on the sign. llr_demap16 run
    13:23 (2026-10-29): both magnitudes were written this way, every m_lsb
    came out wrong, and the recipe skill had shown that very line."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    issues = []
    for m in _CLOCKED_ASSIGN_RE.finditer(stripped):
        target, expr = m.group(1), m.group(2)
        if not _ABS_TARGET_RE.search(target) or not _UNARY_MINUS_RE.search(expr):
            continue
        if re.search(r"\bwhen\b|\babs\b", expr, re.IGNORECASE):
            continue                                        # "x when x < 0 else y" / abs() already
        before = stripped[:m.start(1)].rstrip()
        if re.search(r"\b(then|else)$", before, re.IGNORECASE):
            continue                                        # one branch of an if on the sign
        n = stripped.count("\n", 0, m.start(1)) + 1
        operand = re.search(r"-\s*(?:resize\s*\(\s*)?(\w+)", expr)
        src = operand.group(1) if operand else "x"
        issues.append({"code": "abs_without_sign_check",
                       "detail": f"line {n}: '{target}' is computed as a plain negation with no test of the sign, "
                                 f"so positive inputs become negative (-x is not |x|). Write "
                                 f"`{target} <= unsigned(abs(resize({src}, {target}'length)));` "
                                 f"(numeric_std's abs; widen FIRST so -2048 fits), or an "
                                 f"if/else on the sign bit with the negation only in the negative branch "
                                 f">> {stripped.splitlines()[n - 1].strip()}"})
    return issues


def _writes_of(stripped: str, name: str) -> list[tuple[int, str]]:
    """[(line, expression)] for every whole-signal write `name <= expr;`
    (a `then`/`else`/`begin`/`;`/`=>` must precede it, so `if x <= 3` is not
    a write)."""
    pat = re.compile(rf"(?:^|;|\bthen\b|\belse\b|\bbegin\b|\bloop\b|=>)\s*{re.escape(name)}\s*<=\s*([^;]+);",
                     re.IGNORECASE | re.MULTILINE)
    return [(stripped.count("\n", 0, m.start(1)) + 1, m.group(1).strip()) for m in pat.finditer(stripped)]


def _constant_output_valid_issues(spec: dict, code: str) -> list[dict]:
    """An output valid flag whose every write is the constant '0' can never
    rise, so the dry run would only report smoke_output_never_valid 6-8 s
    later with a canned hint. Followed through one signal at a time
    (`m_axis_tvalid <= m_axis_tvalid_i;` then `m_axis_tvalid_i <= '0';`). A
    reset write of '0' next to a real write passes. timing_recovery run 09:31
    (2026-11-02): the output logic was never written, `m_axis_tvalid_i <= '0'`
    was the whole of it, and the same file came back eight times."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    lines = stripped.splitlines()
    issues = []
    for p in spec.get("ports") or []:
        port = p.get("name") if isinstance(p, dict) else None
        if not port or "valid" not in port.lower() or str(p.get("direction", "")).lower().startswith("in"):
            continue
        name, seen = port, [port.lower()]
        while True:
            writes = _writes_of(stripped, name)
            if len(writes) == 1 and re.fullmatch(r"\w+", writes[0][1]) and writes[0][1].lower() not in seen:
                name = writes[0][1]                         # `port <= inner;` -> look at inner instead
                seen.append(name.lower())
                continue
            break
        if not writes or any(expr != "'0'" for _, expr in writes):
            continue
        n = writes[0][0]
        via = f" (through '{name}')" if name != port else ""
        issues.append({"code": "output_valid_stuck_at_zero",
                       "detail": f"line {n}: output valid '{port}' is only ever assigned the constant '0'{via}, "
                                 f"so no output beat can ever appear and the dry run would report "
                                 f"smoke_output_never_valid. This part of the design is unfinished: write the "
                                 f"logic that raises {port} (for a pipeline, from the valid register of the last "
                                 f"stage) >> {lines[n - 1].strip()}"})
    return issues


# "if advance = '1' then" / "elsif advance = '1' and valid_pipe(0) = '1' then" — the word a clocked write is gated by
_ENABLE_WORD_RE = re.compile(r"\b(?:if|elsif|and|or)\s+(\w+)\s*=\s*'1'", re.IGNORECASE)


def _self_blocking_advance_issues(spec: dict, code: str) -> list[dict]:
    """A pipeline enable (`advance`, or any word the stage processes are gated
    by, `elsif advance = '1' then`) computed from the pipeline's own valid
    flag, `advance <= not valid_pipe(9);` or `not m_axis_tvalid_i`, with no
    downstream ready term. The recipe's handshake line is `advance <=
    m_axis_tready or not valid_pipe(last);` and the ready term is what
    empties the last bit. Without it nothing ever clears valid_pipe(last)
    once it is '1' (the chain only moves while advance is '1'), so at that
    clock advance drops to '0' and every register, the valid chain and the
    output flag keep their values for the rest of the run. timing_recovery
    18:52 (2026-11-06): the spec has no m_axis_tready port, the rewrite
    dropped the ready term and kept `not valid_pipe(9)`; from cycle 14 (reset
    plus ten stages) m_axis_tvalid stayed '1' with one constant value on
    every cycle, 801 mismatches, the same picture for three attempts. The
    fix text names `advance <= '1';`: `advance <= s_axis_tvalid;` gave the
    right beat rate there but never empties the pipe after the last input
    (equalizer 2026-11-12 16:57, 88 extra beats; the dry run reports that
    one as smoke_more_outputs_than_inputs)."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    lines = stripped.splitlines()
    ports = [p for p in (spec.get("ports") or []) if isinstance(p, dict) and p.get("name")]
    inputs = [p["name"] for p in ports if str(p.get("direction", "")).lower().startswith("in")]
    ready_inputs = [n for n in inputs if "ready" in n.lower()]
    valid_inputs = [n for n in inputs if "valid" in n.lower()]
    input_valid = {n.lower() for n in valid_inputs}

    enables = {"advance"} | {m.group(1) for m in _ENABLE_WORD_RE.finditer(stripped)}
    enables = {e for e in enables if not re.search(r"valid|rst|reset|strobe|ready|clk|clock", e, re.IGNORECASE)}
    issues = []
    for name in sorted(enables):
        for n, expr in _writes_of(stripped, name):
            words = re.findall(r"\b[A-Za-z_]\w*", expr)
            if any("ready" in w.lower() for w in words):
                continue                                # `m_axis_tready or not valid_pipe(k)`: the ready term empties the pipe
            own = next((w for w in words if "valid" in w.lower() and w.lower() not in input_valid), None)
            if own is None:
                continue                                # `advance <= s_axis_tvalid;` or `'1'`: never waits for itself
            bit = re.search(rf"\b{re.escape(own)}\s*\(\s*\d+\s*\)", expr)
            own_ref = bit.group(0) if bit else own
            if ready_inputs:
                fix = (f"Add the ready term the recipe shows: `{name} <= {ready_inputs[0]} or not {own_ref};` "
                       f"(the output is free, or the pipeline is empty).")
            else:
                first = f"valid_pipe(0) <= {valid_inputs[0]};" if valid_inputs else "the first valid bit's line"
                fix = (f"The spec has no downstream ready port, so the pipeline must never wait: write "
                       f"`{name} <= '1';` (every register advances on every clock; keep `{first}` so a clock "
                       f"with no input sends a '0' down the valid chain and the output valid drops) and do not read "
                       f"valid_pipe or the output valid flag in it. A history chain or loop value is still "
                       f"written only under the valid bit of the row above it.")
            issues.append({"code": "self_blocking_advance",
                           "detail": f"line {n}: the pipeline enable `{name}` is computed from the pipeline's own valid "
                                     f"flag {own_ref} with no downstream ready term. Nothing can clear {own_ref} once "
                                     f"it is '1' (the valid chain only moves while `{name}` is '1'), so at that clock "
                                     f"`{name}` drops to '0' and every register, the valid chain and the output flag "
                                     f"keep their values for the rest of the run: m_axis_tvalid stuck at '1' with one "
                                     f"constant value on every cycle (timing_recovery 2026-11-06 18:52, 801 "
                                     f"mismatches). {fix} >> {lines[n - 1].strip()}"})
    return issues


def _spec_lines(spec: dict) -> list[str]:
    """The spec's prose, one entry per line: behavior first (the rules), then
    constraints, assumptions and the description."""
    out = []
    for key in ("behavior", "constraints", "assumptions", "description"):
        value = spec.get(key)
        for item in (value if isinstance(value, list) else [value]):
            if item:
                out.append(item if isinstance(item, str) else json.dumps(item))
    return out


def _spec_line_about(spec: dict, name: str) -> str | None:
    """The spec line that names `name` as a word (`y_prev_i` also tries
    `y_prev`): the one giving its formula (`name = ...`) when there is one,
    else the first mention."""
    for word in dict.fromkeys([name, re.sub(r"_[iq]$", "", name, flags=re.IGNORECASE)]):
        lines = [l for l in _spec_lines(spec) if re.search(rf"\b{re.escape(word)}\b", l, re.IGNORECASE)]
        formula = [l for l in lines if re.search(rf"\b{re.escape(word)}\s*=[^=]", l, re.IGNORECASE)]
        if lines:
            return (formula or lines)[0]
    return None


# "strobe_s1 <= strobe_s11;" — a loop value re-entering row 1 from a later row
# (the `;` is left for the next statement on the same line)
_LOOP_RING_RE = re.compile(r"(?:^|;|\bthen\b|\belse\b|\bbegin\b|=>)\s*(\w+?)_s1\s*<=\s*\1_s(\d+)\s*(?=;)",
                           re.IGNORECASE | re.MULTILINE)
# pure type changes: `resize(mu_s10, 8)` is still a copy of mu_s10
_CONVERSIONS = {"signed", "unsigned", "std_logic_vector", "resize", "to_signed", "to_unsigned", "to_integer",
                "std_logic", "others", "downto", "to"}
_OPERATOR_RE = re.compile(r"[-+*/<>&]|\b(?:and|or|xor|nand|nor|not|mod|rem|when|sll|srl|sla|sra|rol|ror|abs)\b",
                          re.IGNORECASE)


def _is_computed(expr: str, own: re.Pattern) -> bool:
    """True when a write's right-hand side is more than a copy of the same
    value (`x_s2 <= x_s1`, `resize(x_s10, 8)`) or a constant (`(others =>
    '0')`, `to_unsigned(0, 8)`): another signal, a variable or a function is
    read, or the value itself is combined with an operator (`eta_s2 - W`)."""
    names = re.findall(r"\b[A-Za-z_]\w*", expr)
    if any(n.lower() not in _CONVERSIONS and not own.match(n) for n in names):
        return True
    return any(own.match(n) for n in names) and bool(_OPERATOR_RE.search(expr.replace("=>", " ")))


# condition words that do not make a write data-dependent: clock, reset, advance, valid, enable, ready
_CONTROL_WORD_RE = re.compile(r"^(?:rising_edge|falling_edge|\w*clk\w*|\w*clock\w*|\w*rst\w*|\w*reset\w*|advance"
                              r"|\w*valid\w*|\w*enable\w*|\w*_en|en|ce|\w*ready\w*)$", re.IGNORECASE)
_IF_RE = re.compile(r"\b(?:if|elsif)\b(.*?)\bthen\b", re.IGNORECASE | re.DOTALL)
# words of the language, not signals (timing_recovery v73: `advance = '1' and valid_pipe(10) = '1'`
# was read as a data condition because of the `and`)
_VHDL_WORDS = {"and", "or", "not", "xor", "nand", "nor", "xnor", "mod", "rem", "abs", "true", "false", "null",
               "is", "then", "else", "elsif", "when", "to", "downto", "rising_edge", "falling_edge"}


def _under_data_condition(stripped: str, pos: int) -> bool:
    """True when the statement at `pos` sits under an `if`/`elsif` (inside its
    process) whose condition reads a data value: `if eta_s2 < 0 then
    strobe_s3 <= '1'` computes strobe although the right-hand side is a
    constant. A reset or advance guard (`if aresetn = '0'`, `elsif advance =
    '1'`) is not a data condition."""
    start = next((p.start() for p in _PROCESS_RE.finditer(stripped) if p.start() <= pos < p.end()), None)
    if start is None:
        return False
    conds = list(_IF_RE.finditer(stripped, start, pos))
    if not conds:
        return False
    words = re.findall(r"\b[A-Za-z_]\w*", conds[-1].group(1))
    return any(w.lower() not in _CONVERSIONS and w.lower() not in _VHDL_WORDS and not _CONTROL_WORD_RE.match(w)
               for w in words)


def _loop_value_never_computed_issues(spec: dict, code: str) -> list[dict]:
    """A loop value that only goes round the ring (`strobe_s1 <= strobe_s11`,
    `strobe_s2 <= strobe_s1`, ..., `strobe_s11 <= strobe_s10`, plus resets)
    and is never written from anything else keeps its reset value for the
    whole run. timing_recovery 18:25 (2026-11-02): strobe and mu were copied
    round eleven rows and never computed, the notes claimed "strobe is
    generated when eta_next < 0", and the dry run gave 998 "got 0" beats.
    Reported with the spec line the value should implement. A ring in any
    other shape (re-entering at row 2, a copy through resize, another name
    on the way) is _loop_state_ring_issues' job."""
    stripped = _code_only(code)
    issues = []
    for ring in _LOOP_RING_RE.finditer(stripped):
        base, k_back = ring.group(1), int(ring.group(2))
        if k_back < 2:
            continue
        writes = re.compile(rf"(?:^|;|\bthen\b|\belse\b|\bbegin\b|\bloop\b|=>)\s*({re.escape(base)}_s(\d+))\s*"
                            rf"(?:{_INDEX_TXT})?\s*<=\s*([^;]+)(?=;)", re.IGNORECASE | re.DOTALL | re.MULTILINE)
        own = re.compile(rf"^{re.escape(base)}_s\d+$", re.IGNORECASE)
        if any(_is_computed(w.group(3), own) or _under_data_condition(stripped, w.start(1))
               for w in writes.finditer(stripped)):
            continue
        line = stripped.count("\n", 0, ring.start(1)) + 1
        last = next((w for w in writes.finditer(stripped) if int(w.group(2)) == k_back), None)
        where = f"p_s{k_back}" + (f" (line {stripped.count(chr(10), 0, last.start(1)) + 1})" if last else "")
        rule = _spec_line_about(spec, base)
        says = (f"The spec says: \"{rule}\" Write that" if rule
                else "The spec does not name it, so either compute it or delete the whole ring. If it is needed, write it")
        issues.append({"code": "loop_value_never_computed",
                       "detail": f"line {line}: `{base}` only goes round the loop (`{base}_s1 <= {base}_s{k_back}`, then "
                                 f"`{base}_s2 <= {base}_s1` ... `{base}_s{k_back} <= {base}_s{k_back - 1}`) and no line ever "
                                 f"computes it, so it keeps its reset value for the whole run: every output shows it as 0 "
                                 f"and anything gated by it never happens. Delete the ring (it starts in p_s1, the last "
                                 f"copy is in {where}): a tracking loop's values live in ONE process, `p_loop`, which "
                                 f"updates eta every input sample and acc, v, y_prev once per strobe "
                                 f"(pipeline_stage_map.md, LOOP VALUES). {says} in "
                                 f"`p_loop`: strobe and mu as `strobe_s2` / `mu_s2` (`if eta_next < 0 then strobe_s2 "
                                 f"<= '1'; ... else strobe_s2 <= '0'; end if;`), carried down the rows to the output; "
                                 f"eta, acc, v and y_prev as `eta_r`, `acc_r`, `v_r`, `yprev_i_r`. Copying is not "
                                 f"computing; a note that says this is done while the code only copies is wrong."})
    return issues


# ── loop-state ring: a value that goes round the pipeline rows ──────────────────
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*(?:\.\w+)*")
_BRANCH_RE = re.compile(r"\b(if|elsif|else|end\s+if|case|end\s+case)\b", re.IGNORECASE)
_ROW_SUFFIX_RE = re.compile(r"_s(\d+)$", re.IGNORECASE)
# any signal or variable write, wherever it stands: after a label, with a record
# field or a one-level index on the target, as a case alternative (`=>`); the
# right-hand side stops BEFORE the `;` (a second statement on the same line is
# seen too) and may not hold `then` (a two-line `if a and\n x_s1 <= x_s3 then`
# is a comparison, not a write)
_ANY_WRITE_RE = re.compile(
    r"(?:^|;|\bthen\b|\belse\b|\bbegin\b|\bloop\b|=>)\s*(?:\w+\s*:(?!=)\s*)?([A-Za-z]\w*(?:\.\w+)*)\s*"
    rf"(?:{_INDEX_TXT})?\s*(<=|:=)\s*((?:(?!\bthen\b)[^;])+)(?=;)", re.IGNORECASE | re.DOTALL | re.MULTILINE)


def _code_only(code: str) -> str:
    """The code with `/* */` and `--` comments and every string or character
    literal blanked; line numbers unchanged."""
    code = _BLOCK_COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), code)
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    return _STRING_OR_CHAR_RE.sub(lambda m: " " * len(m.group(0)), stripped)


def _node(name: str) -> str:
    """`Pipe_S1.eta` -> `pipe_s1`: a record is one register."""
    return name.lower().split(".")[0]


def _row_of(node: str) -> int | None:
    m = _ROW_SUFFIX_RE.search(node)
    return int(m.group(1)) if m else None


def _branch_events(text: str) -> list[tuple[int, str, str]]:
    """(position, push | add | else | pop, condition) for every if / elsif /
    else / case and its end inside one process, in source order. An `else`
    counts only as a statement of its own (after a `;`), not the `else` of
    a `when ... else` expression."""
    events = []
    for m in _BRANCH_RE.finditer(text):
        word = m.group(1).lower()
        if word in ("if", "elsif"):
            then = re.compile(r"\bthen\b", re.IGNORECASE).search(text, m.end())
            events.append((m.start(), "push" if word == "if" else "add", text[m.end():then.start()] if then else ""))
        elif word == "case":
            is_ = re.compile(r"\bis\b", re.IGNORECASE).search(text, m.end())
            events.append((m.start(), "push", text[m.end():is_.start()] if is_ else ""))
        elif word == "else":
            if text[:m.start()].rstrip().endswith(";"):
                events.append((m.start(), "else", ""))
        else:
            events.append((m.start(), "pop", ""))
    return events


def _alternatives(events: list[tuple[int, str, str]], pos: int) -> list[str]:
    """The condition of the alternative that holds the statement at `pos`
    (process-relative), one per open if / case level, outermost first; ''
    for an else branch."""
    stack: list[str] = []
    for epos, kind, cond in events:
        if epos > pos:
            break
        if kind == "push":
            stack.append(cond)
        elif kind in ("add", "else") and stack:
            stack[-1] = cond
        elif kind == "pop" and stack:
            stack.pop()
    return stack


def _names_in(text: str) -> set[str]:
    """The registers, wires, variables and constants a piece of code names
    (conversions and language words left out)."""
    return {_node(w) for w in _IDENT_RE.findall(text)
            if w.lower() not in _CONVERSIONS and w.lower() not in _VHDL_WORDS}


def _clocked_statements(stripped: str) -> list[tuple[str, int, int, str, str, str, list[str]]]:
    """Every `<=` / `:=` statement of every clocked process: (process label,
    process index, line, target node, op, expression, conditions of the
    alternatives that hold it)."""
    out = []
    for i, p in enumerate(_PROCESS_RE.finditer(stripped)):
        body = p.group(0)
        if not re.search(r"\brising_edge\s*\(", body):
            continue
        label_m = re.search(r"(\w+)\s*:\s*(?:postponed\s+)?$", stripped[:p.start()])
        label = label_m.group(1) if label_m else f"the process at line {stripped.count(chr(10), 0, p.start()) + 1}"
        events = _branch_events(body)
        for m in _ANY_WRITE_RE.finditer(body):
            out.append((label, i, stripped.count("\n", 0, p.start() + m.start(1)) + 1, _node(m.group(1)),
                        m.group(2), m.group(3), _alternatives(events, m.start(1))))
    return out


def _variable_resolver(stmts):
    """resolved(process index, names) -> the same names with every variable
    replaced by what its `:=` lines in that process read, transitively."""
    var_reads: dict[tuple[int, str], set[str]] = {}
    for _, i, _, target, op, expr, _ in stmts:
        if op == ":=":
            var_reads.setdefault((i, target), set()).update(_names_in(expr))

    def resolved(i: int, names: set[str], seen: frozenset = frozenset()) -> set[str]:
        out: set[str] = set()
        for n in names:
            if (i, n) in var_reads and n not in seen:
                out |= resolved(i, var_reads[(i, n)], seen | {n})
            else:
                out.add(n)
        return out
    return resolved


def _strongly_connected(nodes: set[str], successors) -> list[list[str]]:
    """Tarjan's components, iterative (a design is small, but a long chain
    must not hit the recursion limit)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comps: list[list[str]] = []
    for root in sorted(nodes):
        if root in index:
            continue
        index[root] = low[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(sorted(successors(root))))]
        while work:
            v, it = work[-1]
            pushed = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = len(index)
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(successors(w)))))
                    pushed = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if pushed:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                comps.append(comp)
    return comps


def _loop_state_ring_issues(code: str) -> list[dict]:
    """A loop value that goes round the pipeline rows: a cycle of writes that
    passes through two or more stage rows (`eta_s1 <= eta_s11`, `eta_s2 <=
    eta_s1`, ..., `eta_s11 <= eta_next_v` with `eta_next_v := eta_s10 - w`).
    With a new sample every tick such a ring holds K different copies of the
    value at once and each sample continues the copy from K samples ago: K
    separate loops run side by side, none of them the spec's loop.
    timing_recovery 2026-11-03, 14:51 (11 rows) and 18:41 (16 rows): eta, v,
    acc and y_prev went round every row, the beat rate was one per 16 clocks
    instead of one per 4, and a copy that once took an undefined start value
    never recovered; no checker saw it.

    The value is followed through the signals themselves, not through one
    name: every write's reads (its expression, the variables it names resolved
    through their `:=` lines in the same process) are edges into the written
    register, a wire outside the processes is a node too, and a ring is a
    strongly connected component across two or more rows. The loop's own
    one-tick memory, a register with no stage number written from its own old
    value and something else (`eta_r <= eta_next`, `eta_next := eta_r - W`),
    is left out of the graph: a cycle through it is the loop the rules teach;
    a plain hold (`x <= x`) does not count. A history chain (`x1_s2 <=
    x0_s2`) and a delay line numbered the wrong way round stay inside one
    row or never come back, so they are no ring. One finding per ring,
    naming every value it carries; a ring that nothing computes is reported
    too (as `loop_value_never_computed`) unless it has the plain
    `<b>_s1 <= <b>_sK;` shape that _loop_value_never_computed_issues
    reports with the spec line."""
    stripped = _code_only(code)
    processes = [(p.start(), p.end()) for p in _PROCESS_RE.finditer(stripped)]
    events = [_branch_events(stripped[a:b]) for a, b in processes]

    def names_in(text: str) -> set[str]:
        return {_node(w) for w in _IDENT_RE.findall(text)
                if w.lower() not in _CONVERSIONS and w.lower() not in _VHDL_WORDS}

    writes = [(m.start(1), stripped.count("\n", 0, m.start(1)) + 1, _node(m.group(1)), m.group(2), m.group(3))
              for m in _ANY_WRITE_RE.finditer(stripped)]
    if not any(_row_of(t) is not None for _, _, t, _, _ in writes):
        return []

    def proc_of(pos: int) -> int | None:
        return next((i for i, (a, b) in enumerate(processes) if a <= pos < b), None)

    var_reads: dict[tuple[int | None, str], set[str]] = {}
    for pos, _, target, op, expr in writes:
        if op == ":=":
            var_reads.setdefault((proc_of(pos), target), set()).update(names_in(expr))

    def resolved(proc: int | None, names: set[str], seen: frozenset = frozenset()) -> set[str]:
        """Variables replaced by what their `:=` lines read, transitively."""
        out: set[str] = set()
        for n in names:
            if (proc, n) in var_reads and n not in seen:
                out |= resolved(proc, var_reads[(proc, n)], seen | {n})
            else:
                out.add(n)
        return out

    def guards(pos: int, proc: int | None) -> set[str]:
        """Data words of the if / elsif / case conditions around a statement
        (the reset, advance, valid and enable words are control, not data)."""
        if proc is None:
            return set()
        start, stack = processes[proc][0], []
        for epos, kind, cond in events[proc]:
            if start + epos > pos:
                break
            if kind == "push":
                stack.append([cond])
            elif kind == "add" and stack:
                stack[-1].append(cond)
            elif kind == "pop" and stack:
                stack.pop()
        return resolved(proc, {w for conds in stack for c in conds for w in names_in(c)
                               if not _CONTROL_WORD_RE.match(w)})

    edges: dict[str, set[str]] = {}
    reg_writes: dict[str, list[tuple[int, set[str], set[str], str, bool]]] = {}
    for pos, line, target, op, expr in writes:
        if op != "<=":
            continue
        proc = proc_of(pos)
        reads = resolved(proc, names_in(expr))
        for r in reads:
            edges.setdefault(r, set()).add(target)
        reg_writes.setdefault(target, []).append((line, reads, guards(pos, proc), expr, proc is not None))

    memory = {n for n, ws in reg_writes.items() if _row_of(n) is None
              and any(in_proc and n in reads and reads - {n} for _, reads, _, _, in_proc in ws)}
    nodes = (set(edges) | {t for ts in edges.values() for t in ts}) - memory
    plain_rings = {m.group(1).lower() for m in _LOOP_RING_RE.finditer(stripped) if int(m.group(2)) >= 2}
    source = code.splitlines()

    issues = []
    for comp in _strongly_connected(nodes, lambda v: edges.get(v, set()) - memory):
        members = set(comp)
        rows = sorted({r for r in map(_row_of, comp) if r is not None})
        if len(rows) < 2:
            continue
        backs: list[tuple[int, str, int]] = []           # (line, register, from row): the copies back
        for n in comp:
            row = _row_of(n)
            if row is None:
                continue
            for line, reads, _, _, _ in reg_writes.get(n, []):
                inside = [r for r in reads if r in members]
                higher = [_row_of(r) for r in inside if _row_of(r) is not None and _row_of(r) > row]
                if higher or any(_row_of(r) is None for r in inside):
                    backs.append((line, n, max(higher) if higher else rows[-1]))
        if not backs:
            continue
        backs.sort()
        line, target, from_row = backs[0]
        first_line = {}
        for l, n, _ in backs:
            first_line.setdefault(_ROW_SUFFIX_RE.sub("", n), l)
        bases = list(first_line)
        base, row, k = bases[0], _row_of(target), rows[-1] - rows[0] + 1
        stmt = source[line - 1].strip() if 0 < line <= len(source) else f"{target} <= ..."
        carries = (("; the same ring carries " + ", ".join(f"`{b}` (line {first_line[b]})" for b in bases[1:]))
                   if len(bases) > 1 else "")
        computed = any(reads - members or g or (_OPERATOR_RE.search(expr.replace("=>", " ")) and reads & members)
                       for n in comp for _, reads, g, expr, _ in reg_writes.get(n, []))
        if not computed:
            if any(b in plain_rings for b in bases):
                continue                                     # _loop_value_never_computed_issues reports it
            issues.append({"code": "loop_value_never_computed",
                           "detail": f"line {line}: `{stmt}` sends `{base}` round the rows{carries} and no line ever "
                                     f"computes it: it only goes from row {rows[0]} to row {rows[-1]} and back, so it "
                                     f"keeps its reset value for the whole run; every output shows it as 0 and anything "
                                     f"gated by it never happens. Delete the ring: a tracking loop's values live in ONE "
                                     f"process, `p_loop`, which updates eta every input sample and acc, v, y_prev once "
                                     f"per strobe (pipeline_stage_map.md, LOOP VALUES). Compute it there: strobe and mu "
                                     f"as `strobe_s2` / `mu_s2` (`if eta_next < 0 then strobe_s2 <= '1'; ... else "
                                     f"strobe_s2 <= '0'; end if;`), carried down the rows to the output; eta, acc, v and "
                                     f"y_prev as `eta_r`, `acc_r`, `v_r`, `yprev_i_r`. Copying is not computing. "
                                     f">> {stmt}"})
            continue
        carried = [b for b in bases if b == "mu" or b.startswith("mu_") or "strobe" in b]
        state = [b for b in bases if b not in carried]
        if state:
            fix = (f"Delete the copies back into row {row} and every `<name>_s<K>` register of "
                   + ", ".join(f"`{b}`" for b in state) + ": a loop value lives in ONE register with no stage number ("
                   + ", ".join(f"`{b}_r`" for b in state) + "), written only by the one loop process `p_loop`, which "
                   "resets it; `eta_r` is updated in every tick that brings a new sample, from its own old value "
                   "(`eta_next := eta_r - W; ... eta_r <= eta_next;`), and acc, v and y_prev are updated in `p_loop` "
                   "from the row where their new value is ready, when that row's valid bit and its strobe are '1'. "
                   "The rows read the `_r` registers directly where they need them.")
        else:
            fix = f"Delete the copies back into row {row}."
        if carried:
            fix += (" " + " and ".join(f"`{b}`" for b in carried) + (" are" if len(carried) > 1 else " is")
                    + " decided in `p_loop` in the same update as eta and written as `strobe_s2` / `mu_s2`, then "
                    "carried down the rows like data to the output row (`strobe_s3 <= strobe_s2; ...`), never "
                    "copied back.")
        issues.append({"code": "loop_state_ring",
                       "detail": f"line {line}: `{stmt}` copies `{base}` from row {from_row} back into row {row}"
                                 f"{carries}. With a new sample every tick that ring holds {k} different copies of "
                                 f"`{base}` at once, each sample continues the value from {k} samples ago, and the "
                                 f"design runs {k} separate loops side by side, none of them the spec's loop "
                                 f"(timing_recovery 2026-11-03: one beat per 16 clocks instead of one per 4, and a "
                                 f"copy that once holds 'X' never recovers). {fix} (pipeline_stage_map.md, LOOP "
                                 f"VALUES) >> {stmt}"})
    return sorted(issues, key=lambda i: int(re.match(r"line (\d+)", i["detail"]).group(1)))


# ── loop registers wait for the row they read; a history chain is reset ─────────
_VALID_BIT_RE = re.compile(r"\bvalid_pipe\s*\(\s*(\d+)\s*\)|\bvalid_s(\d+)\b", re.IGNORECASE)
_RESET_WORD_RE = re.compile(r"\b\w*(?:rst|reset)\w*\b", re.IGNORECASE)


def _under_reset(alts: list[str]) -> bool:
    return any(_RESET_WORD_RE.search(c) for c in alts)


def _cyclic_nodes(stmts, reads_of) -> set[str]:
    """Registers whose value depends on their own past, directly (`acc <=
    acc + x`) or through others (v_r -> eta_r -> strobe -> ... -> e -> v_r);
    `reads_of` holds, per statement, what it reads and what its guards read."""
    edges: dict[str, set[str]] = {}
    for (_, _, _, target, op, _, _), (reads, gw) in zip(stmts, reads_of):
        if op == "<=":
            for r in reads | gw:
                edges.setdefault(r, set()).add(target)
    nodes = set(edges) | {t for ts in edges.values() for t in ts}
    cyclic: set[str] = set()
    for comp in _strongly_connected(nodes, lambda v: edges.get(v, set())):
        if len(comp) > 1 or comp[0] in edges.get(comp[0], set()):
            cyclic.update(comp)
    return cyclic


def _state_read_before_valid_issues(code: str) -> list[dict]:
    """A loop register loaded from a pipeline row before that row holds a
    valid sample: no valid gate at all, or a valid_pipe bit that belongs to
    an earlier row. At that clock the row register still has its undefined
    start value, the loop register takes 'U', and a loop value that once
    holds 'X' never recovers: the strobe compare is false for ever and no
    simulator warning says so. timing_recovery 2026-11-03 18:41: p_s16
    updated eta/v/acc from row 15 under no valid bit; 13 of 16 loop copies
    went 'X' in the first 16 clocks, one beat came per 16 clocks instead of
    per 4. A loop register is one with a reset that some process reads
    back and whose value depends on its own past: directly (`acc <= acc +
    x`) or through other registers and the conditions that gate them (v_r
    -> eta_r -> strobe -> ... -> e -> v_r). A plain data register, even one
    written under a compare, is recomputed every tick and overwritten
    before anyone cares. The bit that travels with row K's
    sample is valid_pipe(K-1) (row 1 <-> valid_pipe(0),
    pipeline_stage_map.md); a later bit is reported too: by then the row
    holds a younger sample. Reads of the register's own row (a history
    chain's shift) are a one-tick delay, not a wait; any other spelling of
    a valid gate (`pipe_s1.valid`, `s_axis_tvalid`) counts as a wait that
    is not checked by number. A design without a valid chain is left alone
    (the dry run judges it). The history chain's extra depth is not counted
    here: the chain is reset (_history_chain_reset_issues), so its taps
    are defined zeros, as in the golden model."""
    stripped = _code_only(code)
    if not re.search(r"\bvalid_pipe\b|\bvalid_s\d+\b", stripped, re.IGNORECASE):
        return []
    stmts = _clocked_statements(stripped)
    resolved = _variable_resolver(stmts)

    def guard_words(i: int, alts: list[str]) -> set[str]:
        return resolved(i, {w for c in alts for w in _names_in(c) if not _CONTROL_WORD_RE.match(w)})

    reads_of = [(resolved(i, _names_in(expr)), guard_words(i, alts)) for _, i, _, _, _, expr, alts in stmts]
    read_back = set().union(*(r | g for r, g in reads_of)) if reads_of else set()
    state = ({t for _, _, _, t, op, _, alts in stmts if op == "<=" and _under_reset(alts)} & read_back
             & _cyclic_nodes(stmts, reads_of))
    source = code.splitlines()

    hits: dict[tuple[str, int], list] = {}
    for (label, i, line, target, op, expr, alts), (reads, gw) in zip(stmts, reads_of):
        if op != "<=" or target not in state or _under_reset(alts):
            continue
        rows: dict[int, list[str]] = {}
        for n in sorted(reads | gw):
            r = _row_of(n)
            if r is not None and r != _row_of(target) and "valid" not in n:
                rows.setdefault(r, []).append(n)
        if not rows:
            continue
        bits = [int(a) if a else int(b) - 1 for c in alts for a, b in _VALID_BIT_RE.findall(c)]
        j = max(bits) if bits else None
        deepest, shallowest = max(rows), min(rows)
        if j is None:
            if any(re.search(r"valid", c, re.IGNORECASE) for c in alts):
                continue                                 # gated by a valid in another spelling: not counted
            kind = "early"
        elif j < deepest - 1:
            kind = "early"
        elif j > shallowest - 1:
            kind = "late"
        else:
            continue
        hits.setdefault((label, i), []).append((line, target, rows, j, kind))

    issues = []
    for (label, i), found in sorted(hits.items(), key=lambda kv: kv[1][0][0]):
        found.sort()
        line, target, rows, j, kind = found[0]
        stmt = source[line - 1].strip() if 0 < line <= len(source) else f"{target} <= ..."
        others = sorted({t for _, t, _, _, _ in found[1:] if t != target})
        also = (" The same holds for " + ", ".join(f"`{t}`" for t in others) + " in this process:"
                if others else " Every other loop register this process loads from a row:")
        k = max(rows) if kind == "early" else min(rows)
        names = ", ".join(f"`{n}`" for n in rows[k])
        bit = f"valid_pipe({k - 1})" if k >= 1 else "the input valid (s_axis_tvalid)"
        mixed = (f" This update also reads rows {min(rows)} and {max(rows)} together, which hold different "
                 f"samples: read every value from one row (carry the older one down)." if len(rows) > 1 else "")
        if kind == "early":
            gate = ("without any valid gate" if j is None else
                    f"under valid_pipe({j}) only, the bit of row {j + 1}")
            issues.append({"code": "state_read_before_valid",
                           "detail": f"line {line}: `{stmt}` in `{label}` loads the loop register `{target}` from row "
                                     f"{k} ({names}) {gate}: at that clock row {k} has not received a sample yet, so "
                                     f"the register takes the undefined start value ('U') of what it reads, and a "
                                     f"loop value that once holds 'X' never recovers (timing_recovery 2026-11-03 "
                                     f"18:41: 13 of 16 loop copies were 'X' and one beat came per 16 clocks instead "
                                     f"of per 4; the simulator prints no warning for it). Make the update wait for "
                                     f"the sample: `elsif advance = '1' then if {bit} = '1' then "
                                     f"{target} <= ...; end if;` (add `and strobe_s{k} = '1'` when the spec updates "
                                     f"this value on a strobe).{also} wait for valid_pipe(K-1), the bit that travels "
                                     f"with row K's sample (`yprev_i_r <= ysym_i_s7` under valid_pipe(6), `v_r` "
                                     f"from `e_s10` under valid_pipe(9); pipeline_stage_map.md, LOOP VALUES)."
                                     f"{mixed} >> {stmt}"})
        else:
            issues.append({"code": "state_read_after_valid",
                           "detail": f"line {line}: `{stmt}` in `{label}` loads the loop register `{target}` from row "
                                     f"{k} ({names}) under valid_pipe({j}), the bit of row {j + 1}: by then row {k} "
                                     f"already holds a sample {j + 1 - k} tick(s) younger than the one that bit "
                                     f"announces, so the register stores the wrong sample's value. The bit that "
                                     f"travels with row {k}'s sample is {bit}: write `if "
                                     f"{bit} = '1' then {target} <= ...; end if;` (add `and "
                                     f"strobe_s{k} = '1'` when the spec updates this value on a strobe).{also} use "
                                     f"the bit of its own row the same way (pipeline_stage_map.md, LOOP VALUES)."
                                     f"{mixed} >> {stmt}"})
    return issues


def _history_chain_reset_issues(code: str) -> list[dict]:
    """A history chain (same-row copies, `x1_s2 <= x0_s2; x2_s2 <= x1_s2`)
    whose registers have neither a reset nor an initial value. Its older
    taps are 'U' until as many valid samples have gone in as the chain is
    long, whatever is computed from them is 'X', and a loop register that
    takes that value is 'X' for ever, although the loop row waited for its
    row's valid bit as it should (timing_recovery 2026-11-03 14:51: p_s11
    waited for valid_pipe(9), x0_s2..x3_s2 were still undefined, 11 beats
    then none). The golden model starts from zero history, so the chain is
    reset to zero (pipeline_stage_map.md, OLDER SAMPLES); the head of the
    chain (`x0_s2 <= i_s1`) counts as a member, because the first shift
    copies it before it was ever loaded."""
    stripped = _code_only(code)
    stmts = _clocked_statements(stripped)
    families: dict[int, set[str]] = {}
    first_line: dict[int, int] = {}
    for _, _, line, target, op, expr, _ in stmts:
        k = _row_of(target)
        if op != "<=" or k is None:
            continue
        src = _names_in(expr)
        if len(src) == 1 and not _OPERATOR_RE.search(expr.replace("=>", " ")):
            s = next(iter(src))
            if s != target and _row_of(s) == k:
                families.setdefault(k, set()).update({target, s})
                first_line.setdefault(k, line)
    if not families:
        return []
    defined = {t for _, _, _, t, op, _, alts in stmts if op == "<=" and _under_reset(alts)}
    for m in _NAMED_DECL_RE.finditer(stripped):
        if ":=" in m.group(3):
            defined |= {n.strip().lower() for n in m.group(2).split(",")}
    source = code.splitlines()
    issues = []
    for k in sorted(families):
        members = sorted(families[k])
        missing = [m for m in members if m not in defined]
        if not missing:
            continue
        line = first_line[k]
        stmt = source[line - 1].strip() if 0 < line <= len(source) else ""
        head = members[0]
        issues.append({"code": "history_chain_not_reset",
                       "detail": f"line {line}: the history chain in row {k} ({', '.join(f'`{m}`' for m in members)}) "
                                 f"has no reset: {', '.join(f'`{m}`' for m in missing)} start undefined, so until "
                                 f"{len(members)} valid samples have gone in the older taps are 'U', whatever is "
                                 f"computed from them is 'X', and a loop register that takes that value (`v_r`, "
                                 f"`acc_r`) is 'X' for ever, even though the loop row waited for its valid bit "
                                 f"(timing_recovery 2026-11-03 14:51: the loop row waited for valid_pipe(9) as it "
                                 f"should, x0_s2..x3_s2 were still undefined: 11 beats, then none; the simulator "
                                 f"prints no warning for it). A history chain is state, like the loop registers: "
                                 f"reset every register of it in its own process (`if rst = '1' then {head} <= "
                                 f"(others => '0'); ... elsif advance = '1' and valid_pipe({k - 1}) = '1' then "
                                 f"{head} <= ...; ... end if;`), or give each one an initial value in its "
                                 f"declaration (`:= (others => '0')`); the golden model starts from zero history "
                                 f"too (pipeline_stage_map.md, OLDER SAMPLES). >> {stmt}"})
    return issues


# the input samples a spec reads around sample n: `x[m-3]`, `x[n+2]`, `x[m]`; and the base point `m = n - 2`
_SPEC_SAMPLE_INDEX_RE = re.compile(r"\bx\s*\[\s*([mn])\s*(?:([+-])\s*(\d+))?\s*\]", re.IGNORECASE)
_SPEC_BASE_POINT_RE = re.compile(r"\bm\s*=\s*n\s*([+-])\s*(\d+)", re.IGNORECASE)
_FIRST_NUMBER_RE = re.compile(r"\d+")


def _spec_history_window(spec: dict) -> tuple[int, int, str] | None:
    """(oldest, newest, how the spec spells it): offsets from sample n of the
    input samples the spec reads. `x[m-3]` with `m = n - 2` is -5. None when
    the spec names no `x[...]` sample, or uses m without saying what m is."""
    text = "\n".join(_spec_lines(spec))
    base = _SPEC_BASE_POINT_RE.search(text)
    m_off = int(base.group(2)) * (1 if base.group(1) == "+" else -1) if base else None
    offsets, words = [], []
    for hit in _SPEC_SAMPLE_INDEX_RE.finditer(text):
        var, sign, k = hit.group(1).lower(), hit.group(2), int(hit.group(3) or 0)
        off = (k if sign == "+" else -k) if sign else 0
        if var == "m":
            if m_off is None:
                return None
            off += m_off
        offsets.append(off)
        words.append(re.sub(r"\s+", "", hit.group(0)))
    if not offsets:
        return None
    oldest, newest = min(offsets), max(offsets)
    rel = lambda o: "x[n]" if o == 0 else f"x[n{o:+d}]"
    spelled = f"{words[offsets.index(oldest)]}..{words[offsets.index(newest)]}"
    if base:
        spelled += f" with m = n {base.group(1)} {base.group(2)}, that is {rel(oldest)}..{rel(newest)}"
    return oldest, newest, spelled


def _history_depth_issues(spec: dict, code: str) -> list[dict]:
    """A numbered history chain (`x0_s2 <= i_s1; x1_s2 <= x0_s2; ...`) with
    fewer registers than the window of input samples the spec reads.
    timing_recovery (2026-11-10, the reviewed RTL): x0..x3 per channel, four
    taps, while `y_mid = farrow(x[m-3], x[m-2], x[m-1], x[m], mu)` with
    m = n-2 reaches back to x[n-5]: six samples. The midpoint window read a
    sample the chain never stored, on every beat, and pipeline_stage_map.md
    showed four taps too. The register in the row above (`i_s1`) does not
    count: reading it from the Farrow row is the pipeline_stage_skip bug.
    Copies whose names are not one stem plus a number (`yprev_s2 <=
    ysym_s2`) and designs without a chain are left to the dry run."""
    window = _spec_history_window(spec)
    if window is None:
        return []
    oldest, newest, spelled = window
    needed = newest - oldest + 1
    if needed < 2:
        return []
    stripped = _code_only(code)
    edges: list[tuple[str, str, int]] = []           # (target, same-row source, line)
    for _, _, line, target, op, expr, _ in _clocked_statements(stripped):
        k = _row_of(target)
        if op != "<=" or k is None:
            continue
        src = _names_in(expr)
        if len(src) != 1 or _OPERATOR_RE.search(expr.replace("=>", " ")):
            continue
        s = next(iter(src))
        if s != target and _row_of(s) == k:
            edges.append((target, s, line))
    if not edges:
        return []
    parent: dict[str, str] = {}

    def root(a: str) -> str:
        while parent.setdefault(a, a) != a:
            a = parent[a]
        return a

    for t, s, _ in edges:
        parent[root(t)] = root(s)
    chains: dict[str, set[str]] = {}
    for t, s, _ in edges:
        chains.setdefault(root(t), set()).update({t, s})
    stem = lambda n: _FIRST_NUMBER_RE.sub("#", n, count=1)
    natural = lambda n: [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", n)]
    short: dict[int, list[tuple[list[str], int]]] = {}
    for members in chains.values():
        names = sorted(members, key=natural)
        if len(names) < 2 or len(names) >= needed or len({stem(n) for n in names}) != 1:
            continue
        line = min(l for t, _, l in edges if t in members)
        short.setdefault(_row_of(names[0]), []).append((names, line))
    source = code.splitlines()
    issues = []
    for k, found in sorted(short.items()):
        found.sort(key=lambda f: f[1])
        names, line = found[0]
        stmt = source[line - 1].strip() if 0 < line <= len(source) else ""
        chains_text = ", ".join(f"`{f[0][0]}`..`{f[0][-1]}`" for f in found)
        last = names[-1]
        m = _FIRST_NUMBER_RE.search(last)
        new = [f"{last[:m.start()]}{int(m.group(0)) + i}{last[m.end():]}" for i in range(1, needed - len(names) + 1)] if m else []
        add = " ".join(f"`{n} <= {p};`" for p, n in zip([last] + new, new))
        issues.append({"code": "history_too_short",
                       "detail": f"line {line}: the history chain in row {k} ({chains_text}) holds {len(names)} samples, "
                                 f"but the spec reads {spelled}: {needed} input samples side by side. The oldest window "
                                 f"reads a sample this chain never stored, so that part of the result is wrong on every "
                                 f"beat. Add {needed - len(names)} more register(s) to EACH chain, shifted the same way "
                                 f"({add}), declared and reset with the chain, and read the older window from the new "
                                 f"taps (oldest first: the midpoint of a timing-error detector uses x[m-3]..x[m], the "
                                 f"symbol uses x[m-1]..x[m+2]). Do not reach for the row above instead "
                                 f"(pipeline_stage_map.md, OLDER SAMPLES). >> {stmt}"})
    return issues


_STROBE_WORD_RE = re.compile(r"\bstrobe\b", re.IGNORECASE)
# the bare form of the output flag: "m_axis_tvalid_i <= valid_pipe(10);" / "<= valid_s4;" with nothing else
_BARE_VALID_OUT_RE = re.compile(r"(?:^|;|\bbegin\b)\s*(\w*valid\w*)\s*<=\s*(valid_pipe\s*\(\s*\d+\s*\)|valid_s\d+(?:\.\w+)?)\s*;",
                                re.IGNORECASE | re.MULTILINE)


def _output_valid_without_strobe_issues(spec: dict, code: str) -> list[dict]:
    """When the spec says the output comes on a strobe (one beat per symbol,
    about 1 in N clocks), the output valid flag must be the last valid bit
    AND the strobe bit of the same row. The bare last bit raises a beat for
    every input sample; the testbench expects only the strobe beats and sees
    about N times too many (timing_recovery 18:32, 2026-11-02: 198 beats
    expected, ~785 delivered, 998 mismatches, every beat "got 0")."""
    about = [l for l in _spec_lines(spec)
             if _STROBE_WORD_RE.search(l) and re.search(r"\boutput\b|\bper\b|\brate\b|\bon strobe\b", l, re.IGNORECASE)]
    if not about:
        return []
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    processes = [(p.start(), p.end(), p.group(0)) for p in _PROCESS_RE.finditer(stripped)]
    clocked_spans = [(a, b) for a, b, body in processes if re.search(r"\brising_edge\s*\(", body)]
    for m in _BARE_VALID_OUT_RE.finditer(stripped):
        target, bit = m.group(1), m.group(2)
        if re.search(r"_s\d+(_|$)", target, re.IGNORECASE):
            continue                                    # a chain register, not the output flag
        pos = m.start(1)
        enclosing = next((body for a, b, body in processes if a <= pos < b), None)
        if enclosing and re.search(r"\b(if|elsif|when)\b[^;]*\bstrobe", enclosing, re.IGNORECASE):
            continue                                    # gated by an if on the strobe instead
        out = _output_data_stage(stripped, clocked_spans)
        k_out = out[0] if out else None
        if k_out is not None and re.search(rf"\bstrobe_s{k_out}\b", stripped, re.IGNORECASE):
            strobe = f"strobe_s{k_out}"
        else:
            found = re.search(r"\b\w*strobe\w*\b", stripped, re.IGNORECASE)
            strobe = found.group(0) if found else (f"strobe_s{k_out}" if k_out else "<the strobe bit of the output row>")
        line = stripped.count("\n", 0, pos) + 1
        return [{"code": "output_valid_without_strobe",
                 "detail": f"line {line}: `{target} <= {bit};` raises an output beat for EVERY input sample, but the "
                           f"spec says: \"{about[0]}\" (one beat per strobe). The testbench expects only the strobe beats, "
                           f"so it sees several times too many beats and every compare is off (\"expected X got 0\", "
                           f"\"unexpected output beat\"). Write `{target} <= {bit} and {strobe};` (the strobe bit "
                           f"carried in the SAME row the output data comes from) and keep m_axis_tdata / m_axis_tuser "
                           f"from that row. The strobe itself is computed in the loop process `p_loop` "
                           f"(`if eta_next < 0 then strobe_s2 <= '1'`) and carried down the rows to the output "
                           f"row, never copied round a ring (pipeline_stage_map.md, LOOP VALUES)."}]
    return []


# Two different registers given exactly the same computed expression.
# timing_recovery 2026-11-09 14:18 (run 35b45c09): ymid_i_s7 and ysym_i_s7
# were both `sat12(resize(p2i_s6, ..) + resize(p1i_s6, ..) + resize(v0i_s6, ..))`.
# The mid-point of the Gardner error was the symbol itself, the error came
# out 0 on every strobe, and mu never left its reset value ("expected 249
# got 0" on every beat).
_NAME_RE = re.compile(r"[A-Za-z_]\w*")


def _signals_in(expr: str, widths) -> set[str]:
    """The declared signals / variables an expression reads (constants,
    functions and keywords are not in the width map)."""
    return {n.lower() for n in _NAME_RE.findall(expr) if n.lower() in widths}


_CAST_CALL_RE = re.compile(r"(?:resize|signed|unsigned|std_logic_vector|std_ulogic_vector)\s*\((.*)\)",
                           re.IGNORECASE | re.DOTALL)


def _is_plain_copy(expr: str, widths) -> bool:
    """`x`, `x(11 downto 0)`, `resize(x, W)`, `std_logic_vector(x)`: one
    signal moved or re-typed, nothing computed. `sat12(x)` (a function of
    the design) is a computation."""
    core = expr.strip()
    while True:
        m = _CAST_CALL_RE.fullmatch(core)
        if not m:
            break
        core = split_top(m.group(1), ",")[0].strip()
    m = re.fullmatch(r"([A-Za-z_]\w*)(?:\s*\([^()]*\))?", core)
    return bool(m) and m.group(1).lower() in widths


def _duplicate_expression_issues(code: str) -> list[dict]:
    """`a <= f(x, y, z); b <= f(x, y, z);` with a and b different signals and
    the expression computed (two or more signals, or a function of one such
    as `sat12(t2i_s6)`): the two registers hold the same value on every
    clock, so one of the two computations the design needs was never written.
    Plain copies (`a_s5 <= a_s4`, `resize(x, W)`, a slice) are not reported;
    neither are reset aggregates (`(valid => '0', ...)`) or a register
    written in more than one place (an if/else that swaps two expressions
    between two registers).

    2026-11-09 18:59 (run a3133ede): `ysym_i_s7 <= sat12(t2i_s6); ymid_i_s7 <=
    sat12(t2i_s6);` slipped past the first version of this check, which only
    looked at expressions reading two or more signals."""
    scopes = scoped_widths(code)
    # a process variable (`diff := a - b; d0 <= abs(diff); diff := c - d; d1 <= abs(diff);`) changes between
    # two lines with the same text, so an expression that reads one proves nothing
    variables = {target.lower() for _, target, op, _ in assignments(code) if op == ":="}
    computed = [(line, target, expr) for line, target, op, expr in assignments(code)
                if op == "<=" and "=>" not in expr and not ({n.lower() for n in _NAME_RE.findall(expr)} & variables)]
    written: dict[str, int] = {}
    for _, target, _ in computed:
        written[target.lower()] = written.get(target.lower(), 0) + 1
    first: dict[str, tuple[int, str]] = {}
    issues, reported = [], set()
    for line, target, expr in computed:
        if written[target.lower()] != 1:
            continue
        key = re.sub(r"\s+", "", expr).lower()
        widths = scopes.at(line)
        signals = _signals_in(expr, widths)
        if not signals or (len(signals) < 2 and _is_plain_copy(expr, widths)):
            continue
        seen = first.setdefault(key, (line, target))
        if seen[1].lower() == target.lower() or target.lower() in reported:
            continue
        reported.add(target.lower())
        issues.append({"code": "duplicate_expression",
                       "detail": f"line {line}: `{target}` is given exactly the same expression as `{seen[1]}` (line "
                                 f"{seen[0]}), so the two registers hold the same value on every clock and one of the "
                                 f"two computations the spec asks for is missing. If `{target}` is a different value "
                                 f"(a mid-point interpolated from the OLDER samples x[m-3]..x[m] while the symbol uses "
                                 f"x[m-1]..x[m+2]; a second filter output), give it its own expression built from its "
                                 f"own inputs, with its own product registers in the rows above. If the two really are "
                                 f"the same value, delete `{target}` and read `{seen[1]}` in both places "
                                 f">> {source_line(code, line)}"})
    return issues


# A product added to a plain value without scaling it back.
# timing_recovery 2026-11-09 14:18 (run 35b45c09): `p2i_s6 <= s2i_s5 * mu_s5`
# (mu is a fraction: 1.0 = 2**MU_F) and then
# `sat12(resize(p2i_s6, W) + resize(p1i_s6, W) + resize(v0i_s6, W))`: the two
# products were 2**32 times too big next to the 12-bit v0, the output sat at
# +2047/-2048 whenever mu was not 0, and equalled the raw input sample when
# it was.
MIN_FACTOR_BITS = 8           # both factors at least this wide: a data value times a fraction, not `x * 3`


def _strip_resize(term: str) -> str:
    """`resize(resize(x, A), B)`, `(x)`, `signed(x)` -> `x`; anything else unchanged."""
    term = term.strip()
    while True:
        m = re.fullmatch(r"(?:resize|signed|unsigned)\s*\((.*)\)", term, re.IGNORECASE | re.DOTALL)
        if m:
            args = split_top(m.group(1), ",")
            if len(args) in (1, 2):
                term = args[0].strip()
                continue
        if term.startswith("(") and term.endswith(")") and len(split_top(term[1:-1], ",")) == 1 \
                and term[1:-1].count("(") == term[1:-1].count(")") and not _unbalanced_prefix(term[1:-1]):
            term = term[1:-1].strip()
            continue
        return term


def _unbalanced_prefix(text: str) -> bool:
    """True when a `)` closes before its `(` opens: `a) + (b` is not one bracketed expression."""
    depth = 0
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if depth < 0:
            return True
    return False


def _bracketed_parts(expr: str) -> list[str]:
    """The expression itself and the inside of every bracket pair in it."""
    parts, stack = [expr], []
    for i, ch in enumerate(expr):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            parts.append(expr[stack.pop() + 1:i])
    return parts


def _unscaled_product_sum_issues(code: str) -> list[dict]:
    """A register that only ever holds `a * b` (both factors signals of
    MIN_FACTOR_BITS or more) added, as it is or inside resize(), to a plain
    signal that is at least MIN_FACTOR_BITS narrower and is not itself built
    from products. The product carries the fraction bits of its multiplier
    and must be shifted back (or the plain value shifted up) before the add.
    Sums of products with products, `shift_right(p, F) + v` and
    `p + shift_left(v, F)` are not reported. Variables count like signals:
    the reviewed timing_recovery RTL (2026-11-10) had `p1 := v2 * mu` and
    `(resize(p1, P2_W) + resize(v1, P2_W)) * mu` inside a `farrow` function,
    and a check that only read `<=` lines let it through."""
    scopes = scoped_widths(code)
    writes: dict[str, list[tuple[int, str, str]]] = {}
    for line, target, op, expr in assignments(code):
        if op in ("<=", ":=") and not re.fullmatch(r"\(\s*others\s*=>\s*'.'\s*\)", expr.strip(), re.IGNORECASE):
            writes.setdefault(target.lower(), []).append((line, expr, op))

    def factors_of(expr: str, line: int) -> tuple[str, str] | None:
        parts = split_top(_strip_resize(expr), "*")
        if len(parts) != 2:
            return None
        widths = scopes.at(line)
        sizes = [expr_width(p, widths) for p in parts]
        if None in sizes or min(sizes) < MIN_FACTOR_BITS or not all(_signals_in(p, widths) for p in parts):
            return None
        return parts[0].strip(), parts[1].strip()

    products = {name: ws[0] for name, ws in writes.items() if all(factors_of(e, l) for l, e, _ in ws)}
    # product-scale: a product, or a register that only ever holds sums / copies of product-scale registers
    scaled = set(products)
    changed = True
    while changed:
        changed = False
        for name, ws in writes.items():
            if name in scaled:
                continue
            terms = [_strip_resize(t).lower() for _, e, _ in ws for t in additive_terms(_strip_resize(e))]
            if terms and all(t in scaled for t in terms):
                scaled.add(name)
                changed = True

    issues, reported = [], set()
    for line, target, op, expr in assignments(code):
        widths = scopes.at(line)
        for part in _bracketed_parts(expr):
            terms = [_strip_resize(t) for t in additive_terms(part)]
            if len(terms) < 2:
                continue
            prods = [t.lower() for t in terms if t.lower() in products]
            plains = [t for t in terms if _NAME_RE.fullmatch(t) and t.lower() in widths and t.lower() not in scaled]
            for p in prods:
                for z in plains:
                    wp, wz = widths.get(p), widths.get(z.lower())
                    if wp is None or wz is None or wz > wp - MIN_FACTOR_BITS or (p, z.lower()) in reported:
                        continue
                    reported.add((p, z.lower()))
                    p_line, p_expr, p_op = products[p]
                    a, b = factors_of(p_expr, p_line)
                    issues.append({"code": "product_added_unscaled",
                                   "detail": f"line {line}: `{p}` is a product (line {p_line}: `{p} {p_op} "
                                             f"{' '.join(p_expr.split())}`, {wp} bits) and is added to the plain "
                                             f"{wz}-bit `{z}` without being scaled back. A value times a fraction "
                                             f"carries the fraction's bits: when 1.0 in `{b}` is 2**F, the product is "
                                             f"2**F times too big next to `{z}`, and the sum saturates on every "
                                             f"sample where the fraction is not 0. Shift the product back before the "
                                             f"add: `shift_right({p}, F)` with F = the number of fraction bits of "
                                             f"`{b}` (use the named constant), or shift `{z}` up by F and shift the "
                                             f"sum back once. Do this after EVERY multiply by a fraction: "
                                             f"y = shift_right((shift_right(v2*mu, F) + v1) * mu, F) + v0 "
                                             f">> {source_line(code, line)}"})
    return issues


# "{I[11:0], Q[11:0]}" in a spec line: several values packed in one port, the first in the top bits
_PACKED_FIELD = r"[A-Za-z_]\w*(?:\s*\[\s*\d+\s*:\s*\d+\s*\])?"
_PACKED_RE = re.compile(rf"\{{\s*({_PACKED_FIELD}(?:\s*,\s*{_PACKED_FIELD})+)\s*\}}")
_PACKED_FIELD_RE = re.compile(r"([A-Za-z_]\w*)(?:\s*\[\s*(\d+)\s*:\s*(\d+)\s*\])?")


def _packed_ports(spec: dict) -> dict[str, tuple[str, list[tuple[str, int, int]]]]:
    """{port: (the spec's text, [(value name, high bit, low bit)])} for every
    port a spec line describes as `{A[h:l], B[h:l]}` whose widths add up to
    the port's width."""
    ports = {str(p["name"]).lower(): p for p in spec.get("ports") or [] if isinstance(p, dict) and p.get("name")}
    out: dict[str, tuple[str, list[tuple[str, int, int]]]] = {}
    for line in _spec_lines(spec):
        m = _PACKED_RE.search(line)
        named = [n for n in ports if re.search(rf"\b{re.escape(n)}\b", line, re.IGNORECASE)]
        if not m or len(named) != 1 or named[0] in out:
            continue
        try:
            total = int(str(ports[named[0]].get("width")))
        except ValueError:
            continue
        items = [_PACKED_FIELD_RE.fullmatch(item.strip()) for item in m.group(1).split(",")]
        names = [i.group(1).lower() for i in items]
        widths = [int(i.group(2)) - int(i.group(3)) + 1 if i.group(2) else 0 for i in items]
        if not any(widths) and total % len(items) == 0:
            widths = [total // len(items)] * len(items)     # {I, Q}: equal shares
        if min(widths) < 1 or sum(widths) != total or len(set(names)) != len(names):
            continue
        fields, high = [], total - 1
        for name, width in zip(names, widths):
            fields.append((name, high, high - width + 1))
            high -= width
        out[named[0]] = (m.group(0), fields)
    return out


def _packed_value_of(name: str, values: list[str], known: set[str]) -> str | None:
    """Which packed value an RTL name is for: `i_s1`, `ysym_q_s10` carry the
    value's name as a word; `xi_s1` counts when its twin `xq_s1` exists too.
    None unless exactly one value fits."""
    tokens, hits = name.lower().split("_"), set()
    for value in values:
        for n, token in enumerate(tokens):
            if token == value:
                hits.add(value)
            for other in values:
                twins = [other + token[len(value):]] if token.startswith(value) else []
                twins += [token[:-len(value)] + other] if token.endswith(value) else []
                if other != value and any("_".join(tokens[:n] + [t] + tokens[n + 1:]) in known for t in twins):
                    hits.add(value)
    return hits.pop() if len(hits) == 1 else None


def _packed_field_order_issues(spec: dict, code: str) -> list[dict]:
    """A port the spec packs as {I[11:0], Q[11:0]} has I in the top bits.
    Reported: a register named for one value that takes exactly the bits of
    another (`i_s1 <= signed(s_axis_tdata(11 downto 0))`), and an output
    joined in another order (`m_axis_tdata <= q & i`). timing_recovery
    (2026-11-11 13:09): I and Q were read swapped; no check and no compiler
    can see it, and every compared value was wrong."""
    packed = _packed_ports(spec)
    if not packed:
        return []
    stripped = strip_comments(code)
    constants = scoped_widths(code).constants
    statements = list(assignments(code))
    known = {target.lower() for _, target, _, _ in statements}
    issues = []
    for port, (text, fields) in packed.items():
        values = [f for f, _, _ in fields]
        layout = ", ".join(f"{f.upper()} is bits {h} downto {l}" for f, h, l in fields)
        bits_of = {(h, l): f for f, h, l in fields}
        spot = {f: f"{port}({h} downto {l})" for f, h, l in fields}

        def value_in(expr: str) -> str | None:
            found = {_packed_value_of(n, values, known) for n in re.findall(r"[A-Za-z_]\w*", expr)} - {None}
            return found.pop() if len(found) == 1 else None

        def wrong_bits(line: int, name: str, high: str, low: str, value: str | None, verb: str) -> None:
            taken = bits_of.get((int_value(high, constants), int_value(low, constants)))
            if value and taken and taken != value:
                issues.append({
                    "code": "packed_field_order",
                    "detail": f"line {line}: `{name}` {verb} bits {high.strip()} downto {low.strip()} of `{port}`, "
                              f"and those bits are {taken.upper()}, not {value.upper()}. The spec packs the port as "
                              f"{text}: the first name is in the top bits, so {layout}. Use `{spot[value]}` for "
                              f"{value.upper()} >> {source_line(code, line)}"})

        piece = rf"\b{re.escape(port)}\s*\(\s*([^()]+?)\s+downto\s+([^()]+?)\s*\)"
        for line, target, _, expr in statements:
            if target.lower() == port:                  # the whole output written as a & b
                parts = split_top(expr, "&")
                while len(parts) == 1:
                    inner = re.fullmatch(r"\s*\w+\s*\((.*)\)\s*", parts[0], re.DOTALL)
                    if not inner:
                        break
                    parts = split_top(inner.group(1), "&")
                got = [value_in(p) for p in parts]
                if len(parts) == len(fields) and None not in got and len(set(got)) == len(got) and got != values:
                    issues.append({
                        "code": "packed_field_order",
                        "detail": f"line {line}: `{port}` is joined as {{{', '.join(g.upper() for g in got)}}}, "
                                  f"but the spec packs it as {text}: the first name is in the top bits, so "
                                  f"{layout}. In `a & b` the left operand is the top bits: put the "
                                  f"{values[0].upper()} value first >> {source_line(code, line)}"})
                continue
            reads = re.findall(piece, expr, re.IGNORECASE)
            if len(reads) == 1:
                wrong_bits(line, target, reads[0][0], reads[0][1], _packed_value_of(target, values, known), "takes")
        for m in re.finditer(piece + r"\s*<=\s*([^;]+);", stripped, re.IGNORECASE):
            line = stripped.count("\n", 0, m.start()) + 1
            wrong_bits(line, m.group(3).strip(), m.group(1), m.group(2), value_in(m.group(3)), "is written to")
    return issues


def _static_checks(spec: dict, rtl: dict) -> list[dict]:
    issues: list[dict] = []

    def issue(code: str, detail: str) -> None:
        issues.append({"code": code, "detail": detail})

    code = rtl.get("code") or ""
    expected = spec.get("entity_name")

    if not code.strip():
        # parse_rtl_response leaves the code empty, and says why in notes, when
        # the reply was cut off or was not JSON; pass that reason on to the model
        issue("empty_code", " ".join(["No RTL code was returned.", *(rtl.get("notes") or [])]))
        return issues

    if "```" in code:
        issue("markdown_fence", "Code contains markdown fences — strip them before compiling.")
    stubs: list[tuple[int, str]] = []
    for n, line in enumerate(code.splitlines(), 1):
        # "..." only counts as a stub when it is the whole line; an ellipsis
        # inside a comment or report string is normal prose.
        if re.search(r"\bTODO\b|\bFIXME\b", line) or re.fullmatch(r"\s*(--)?\s*\.\.\.\s*", line):
            issue("placeholder", f"Placeholder left in the code at line {n}: {line.strip()}")
        elif "--" in line and _STUB_COMMENT_RE.search(line.split("--", 1)[1]):
            stubs.append((n, line.strip()))
    for k, (n, text) in enumerate(stubs[:5]):               # a flood of such notes is cut to five lines
        more = f" (and {len(stubs) - 5} more like this)" if k == 4 and len(stubs) > 5 else ""
        issue("placeholder", f"line {n}: this comment says the code is not written yet; the design is "
                             f"unfinished. Write that logic now instead of the note >> {text}{more}")

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
    built: dict = {}
    # xelab leaves files of its own in the folder; a leftover must not fail the lint
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mcp_write_files(tmp, {name: code})
        result = mcp_run_xvhdl(tmp, [(name, xvhdl_std(version))])[0]
        top = _ENTITY_RE.search(code)
        if result.get("status") == "ok" and top:
            try:
                built = mcp_run_xelab(tmp, top.group(1), name, xvhdl_std(version))
            except Exception:           # xelab could not be started: the xvhdl result still stands
                built = {}
    if result.get("error_class") == "environment":
        return None, []
    output = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    issues = compiler_errors(output, code, 0 if result.get("status") == "ok" else 1)
    if built.get("status") == "error" and built.get("error_class") != "environment":
        issues += _elaboration_errors((built.get("stdout") or "") + "\n" + (built.get("stderr") or ""), code)
    return "xvhdl", issues


_ELEMENTS_RE = re.compile(r"expression has (\d+) elements?\s*;\s*expected (\d+)", re.IGNORECASE)


def _elaboration_errors(output: str, code: str) -> list[dict]:
    """xelab's ERROR lines as `elaboration_error` issues. xvhdl only reads the
    grammar; xelab works out every width, so a start value of the wrong width
    on a declaration line passes xvhdl and stops here. timing_recovery
    2026-11-11, attempt 3: `constant V2_HALF : signed(V2_W-1 downto 0) :=
    to_signed(1, V2_W-1);` (15 bits into 16) was first seen by the real
    simulation, whose analyst called it a missing comma; the plain meaning of
    that message is added here."""
    # "Static elaboration of top level ... failed" is xelab's closing summary, not a cause
    causes = "\n".join(ln for ln in output.splitlines() if "Static elaboration" not in ln)
    issues = compiler_errors(causes, code, 0, label="xelab (the build step after xvhdl)")
    for i in issues:
        i["code"] = "elaboration_error"
        m = _ELEMENTS_RE.search(i["detail"])
        if m:
            i["detail"] += (f"  [a {m.group(1)}-bit value is given to a {m.group(2)}-bit name on this line: a width "
                            f"mistake, not a missing comma. Make the value {m.group(2)} bits wide (the width "
                            f"argument of to_signed/to_unsigned/resize must be the declared width). xelab stops at "
                            f"the first such line: check the declarations after it for the same mistake]")
    return issues


_UNDECLARED_ERROR_RE = re.compile(r"'(\w+)' is not declared", re.IGNORECASE)
_HIDDEN_NOTE_RE = re.compile(r"\d+ follow-on error\(s\) hidden")


def _without_duplicate_undeclared(static: list[dict], compile_issues: list[dict]) -> list[dict]:
    """The compiler's "'mu_s1' is not declared" lines for names that
    undeclared_stage_register already explained (with the line to add) are
    dropped, so the fix prompt carries each missing name once. When only the
    "N follow-on error(s) hidden" note would be left, it goes too."""
    explained = {m.group(1).lower() for i in static if i["code"] == "undeclared_stage_register"
                 for m in [re.match(r"line \d+: `(\w+)`", i["detail"])] if m}
    if not explained:
        return compile_issues
    kept = [i for i in compile_issues
            if not ((m := _UNDECLARED_ERROR_RE.search(i["detail"])) and m.group(1).lower() in explained)]
    return [] if all(_HIDDEN_NOTE_RE.search(i["detail"]) for i in kept) else kept


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
        issues += _declaration_after_begin_issues(rtl["code"])
        issues += _unbalanced_parentheses_issues(rtl["code"])
        issues += _block_balance_issues(rtl["code"])
        issues += _width_issues(rtl["code"])
        issues += _unconstrained_parameter_issues(rtl["code"])
        issues += _literal_width_issues(rtl["code"])
        issues += _multiple_driver_issues(rtl["code"])
        issues += _pipeline_stage_skip_issues(rtl["code"])
        issues += _stage_register_declaration_issues(rtl["code"])
        issues += _valid_data_length_issues(rtl["code"])
        issues += _abs_without_sign_check_issues(rtl["code"])
        issues += _stage_map_issues(rtl)
        issues += _constant_output_valid_issues(spec, rtl["code"])
        issues += _self_blocking_advance_issues(spec, rtl["code"])
        issues += _loop_value_never_computed_issues(spec, rtl["code"])
        issues += _loop_state_ring_issues(rtl["code"])
        issues += _state_read_before_valid_issues(rtl["code"])
        issues += _history_chain_reset_issues(rtl["code"])
        issues += _history_depth_issues(spec, rtl["code"])
        issues += _output_valid_without_strobe_issues(spec, rtl["code"])
        issues += _duplicate_expression_issues(rtl["code"])
        unscaled = _unscaled_product_sum_issues(rtl["code"])
        issues += unscaled
        # the same add explained once: product_added_unscaled names the product's own line
        issues += [i for i in fixed_point_issues(rtl["code"]) if _lines_of([i]) - _lines_of(unscaled)]
        issues += equation_issues(_spec_lines(spec), rtl["code"])
        issues += polynomial_issues(_spec_lines(spec), rtl["code"])
        issues += loop_gain_issues(_spec_lines(spec), rtl["code"])
        issues += _packed_field_order_issues(spec, rtl["code"])
        try:
            compiler, compile_issues = _compile(rtl["code"], spec.get("entity_name") or rtl.get("entity_name"),
                                                version)
            issues += _without_duplicate_undeclared(issues, compile_issues)
        except Exception as e:  # MCP server could not start / write failed
            issues.append({"code": "compiler_failed", "detail": f"Compiler could not run: {e}"})
        if compiler and not issues:     # only a design that is otherwise clean gets the slower dry run
            issues += smoke_issues(spec, rtl["code"])
    return {"compiler": compiler, "issues": issues}


def unchanged_on_retry_issue(rtl: dict | None, previous_rtl: dict | None,
                             attempt_history: list[dict] | None) -> dict | None:
    """A lint issue when a retry delivered the very file the orchestrator
    handed back to be revised (`previous_rtl`), or None. Compared after
    trimming blank ends only; `attempt_history` is the orchestrator's note on
    the earlier calls, whose downstream failures and suggested fix are quoted.

    The orchestrator only calls again because that file failed downstream
    (a simulation blamed it). Returning it unchanged cannot fix anything,
    but it passes every other check and costs a full simulation to find out
    (llr_demap16, 2026-10-29: three byte-identical answers, three dry runs,
    run ended on the step cap). Treated as a lint issue, the normal fix loop
    asks for a real change and the stall guard ends the call in NEEDS_REVIEW
    when none comes. Called by the agent's lint step, not by lint_rtl, because
    only the agent holds the previous file. The generator re-asks once before
    this (copy_guard); a copy seen here survived that re-ask."""
    if not copied_answer(rtl, previous_rtl):
        return None
    blamed = [n for a in attempt_history or [] for n in a.get("downstream") or []]
    failures = [f for n in blamed for f in (n.get("failures") or [])][:5]
    fixes = [n.get("suggested_fix") for n in blamed if n.get("suggested_fix")]
    return {"code": "unchanged_on_retry",
            "detail": "The code returned is identical to previous_code, which a simulation already rejected; "
                      "returning it again fixes nothing. Change the lines that cause the reported failures"
                      + (f" ({'; '.join(failures)})" if failures else "")
                      + (f". Suggested fix: {fixes[-1]}" if fixes else ".")}

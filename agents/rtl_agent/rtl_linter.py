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
from agents.rtl_agent.rtl_widths import (
    additive_terms, assignments, declared_widths, expr_width, source_line, split_top,
)
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
def _multiplier_width_issues(code: str) -> list[dict]:
    """numeric_std gives a product as wide as BOTH inputs added together;
    xvhdl compiles a mismatched target, but the simulation then stops with
    'array size mismatch'. Checks `target <= A * B;` where the width of A and
    B can be worked out (names, resize, signed/unsigned, '&', slices), in
    every statement position (after ';', 'then', 'else', 'begin')."""
    widths = declared_widths(code)
    issues = []
    for n, target, _, expr in assignments(code):
        target = target.lower()
        factors = split_top(expr, "*")
        if len(factors) != 2 or target not in widths or len(additive_terms(expr)) != 1:
            continue                        # a pure product only; "a * b - c" is the general width check's job
        wa, wb = expr_width(factors[0], widths), expr_width(factors[1], widths)
        if wa is None or wb is None or widths[target] == wa + wb:
            continue
        issues.append({"code": "multiplier_width",
                       "detail": f"line {n}: {factors[0].strip()} ({wa} bits) * {factors[1].strip()} ({wb} bits) "
                                 f"is {wa + wb} bits wide, but '{target}' is {widths[target]} bits; "
                                 f"make '{target}' {wa + wb} bits, then resize() it afterwards. Do NOT widen "
                                 f"the factors to match the target: resize() around a factor makes the product "
                                 f"wider again (the result is always the sum of the factor widths). Multiply "
                                 f"the factors at their own natural widths and resize only the product"})
    return issues


# "* to_signed(4, 16)", "* to_unsigned(8192, 25)", "* 4", "* 8192"
_POW2_FACTOR_RE = re.compile(r"\*\s*(?:(?:to_signed|to_unsigned)\s*\(\s*(\d+)\s*,\s*\d+\s*\)|(\d+)\b)", re.IGNORECASE)


def _power_of_two_multiply_issues(code: str) -> list[dict]:
    """`x * to_signed(4, 16)` or `x * 4` is a shift written as a multiply. It
    is slow in hardware and, worse, the product is twice as wide as x, so a
    16-bit target crashes the dry run with 'left array has 16 elements, right
    array has 32 elements' (llr_demap16, 2026-10-29 16:xx: inside calc_mmsb,
    three attempts could not find it). The recipe says shift; this enforces it."""
    widths = declared_widths(code)
    issues = []
    for n, target, _, expr in assignments(code):
        if target.lower() not in widths:
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
    the general one."""
    pow2 = _power_of_two_multiply_issues(code)
    mult = [i for i in _multiplier_width_issues(code) if _lines_of([i]) - _lines_of(pow2)]
    return pow2 + mult + _assignment_width_issues(code, _lines_of(pow2 + mult))


def _assignment_width_issues(code: str, skip_lines: set[int]) -> list[dict]:
    """Left and right side of `target <= expr` must have the same width; the
    compiler accepts a mismatch and the dry run then crashes ('Array sizes do
    not match, left array has L elements, right array has R elements') with
    a location that names only the process, not the line. Reported here, with
    the line, whenever both widths can be worked out. `skip_lines` are lines
    another width check already explained."""
    widths = declared_widths(code)
    issues = []
    for n, target, op, expr in assignments(code):
        if n in skip_lines or target.lower() not in widths:
            continue
        right = expr_width(expr, widths)
        left = widths[target.lower()]
        if right is None or right == left:
            continue
        issues.append({"code": "assignment_width",
                       "detail": f"line {n}: '{target}' is {left} bits but the right-hand side is {right} bits "
                                 f"(a product is as wide as both factors added; a sum is as wide as its widest "
                                 f"operand). Make the widths equal: compute at the natural width into a signal or "
                                 f"variable of that width, then `resize(..., {left})` as a separate step "
                                 f">> {source_line(code, n)}"})
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


# "x_s3 <= ... y_s1 ...;" — a register of stage K written from a register of
# stage J. Signal names follow the streaming recipe: <field>_s<stage>.
_STAGE_ASSIGN_RE = re.compile(
    r"(?:^|;|\bthen\b|\belse\b|\bbegin\b|\bloop\b)\s*(\w+?)_s(\d+)\s*(?:\([^)]*\))?\s*<=\s*([^;]+);",
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


_VALID_PIPE_DECL_RE = re.compile(
    r"\bvalid_pipe\s*:\s*std_logic_vector\s*\(\s*(\d+)\s+downto\s+(\d+)\s*\)", re.IGNORECASE)
# "m_axis_tvalid_i <= valid_pipe(6);" — the bit the output valid flag is taken from
_VALID_OUT_RE = re.compile(r"(?:^|;|\bbegin\b)\s*(\w*valid\w*)\s*<=\s*valid_pipe\s*\(\s*(\d+)\s*\)\s*;",
                           re.IGNORECASE)
_VALID_STAGE_RE = re.compile(r"\bvalid_s(\d+)\s*<=", re.IGNORECASE)
# "m_axis_tvalid <= valid_s4;" or "m_axis_tvalid <= pipe_s4.valid;" — valid taken from a stage register
_VALID_OUT_STAGE_RE = re.compile(r"(?:^|;|\bbegin\b)\s*(\w*valid\w*)\s*<=\s*((\w+_s(\d+))(?:\.\w+)?)\s*;",
                                 re.IGNORECASE)
# any clocked "name <= expr;" inside a process (name not a stage register, not the valid chain)
_CLOCKED_ASSIGN_RE = re.compile(r"(?:^|;|\bthen\b|\belse\b|\bbegin\b|\bloop\b)\s*(\w+)\s*(?:\([^)]*\))?\s*<=\s*([^;]+)(?=;)",
                                re.IGNORECASE | re.DOTALL | re.MULTILINE)      # (?=;) keeps the ';' for the next statement


def _valid_data_length_issues(code: str) -> list[dict]:
    """The valid flag and the data must pass through the SAME number of
    registers. Counted here: data depth = stage registers _s1.._sK plus any
    register written from the last stage inside a clocked process (an output
    register such as m_axis_tdata_reg); valid depth = the valid_pipe bit the
    output valid flag is taken from (+1), else the declared width, else the
    number of valid_sK registers. llr_demap16 run 12:18 (2026-10-29): seven
    data registers, six valid bits, so m_axis_tvalid rose one clock before
    m_axis_tdata_reg was written and every beat was read as 0."""
    stripped = "\n".join(line.split("--", 1)[0] for line in code.splitlines())
    # only registers count: `x_sK <=` inside a process with a clock edge (a
    # concurrent `i_s0 <= signed(...)` outside any process is a wire)
    clocked = [p.group(0) for p in _PROCESS_RE.finditer(stripped) if re.search(r"\brising_edge\s*\(", p.group(0))]
    stages = sorted({int(m.group(2)) for body in clocked for m in _STAGE_ASSIGN_RE.finditer(body)})
    if not stages:
        return []
    k_min, k_max = stages[0], stages[-1]
    data_depth = k_max - k_min + 1
    data_path = f"_s{k_min}.._s{k_max}"

    # a register after the last stage: written inside a clocked process from _s<k_max> names
    last_stage = re.compile(rf"\b\w+_s{k_max}\b", re.IGNORECASE)
    extra, registered_valid = None, False
    for body in clocked:
        for m in _CLOCKED_ASSIGN_RE.finditer(body):
            target = m.group(1)
            if re.fullmatch(r"\w*(tvalid|valid_out|out_valid)\w*", target, re.IGNORECASE):
                registered_valid = True             # the output valid flag is itself a register
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
        (`valid_s2 <= valid_s1;` is a shift step, not the output)."""
        return next((m for m in pattern.finditer(stripped) if not re.search(r"_s\d+$", m.group(1), re.IGNORECASE)),
                    None)

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
    early = "BEFORE" if valid_depth < data_depth else "AFTER"
    if extra and valid_depth == data_depth - 1:
        fix = (f"either delete `{extra}` and drive the output straight from the stage-{k_max} registers "
               f"(`m_axis_tdata <= std_logic_vector(... _s{k_max} ...)` as a plain assignment outside the "
               f"process), or give `{extra}` its own valid bit: declare valid_pipe({data_depth - 1} downto 0), "
               f"write `valid_pipe({data_depth - 1}) <= valid_pipe({data_depth - 2});` next to `{extra} <= ...;`, "
               f"and use valid_pipe({data_depth - 1}) for m_axis_tvalid and for `advance`")
    else:
        fix = (f"make the valid chain exactly {data_depth} bits long (one bit per data register, the output "
               f"valid flag taken from the last bit), or remove the data register(s) nothing needs; update "
               f"the stage map so every row ends with its own valid bit")
    return [{"code": "valid_data_length_mismatch",
             "detail": f"the data passes through {data_depth} registers ({data_path}) but the valid flag through "
                       f"{valid_depth} ({valid_path}). The valid flag therefore reaches the output one clock {early} "
                       f"the data: the testbench reads the output register's old/reset value (\"expected X got 0\") "
                       f"or the next beat's value. Fix: {fix}."}]


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
        issues += _width_issues(rtl["code"])
        issues += _multiple_driver_issues(rtl["code"])
        issues += _pipeline_stage_skip_issues(rtl["code"])
        issues += _valid_data_length_issues(rtl["code"])
        issues += _abs_without_sign_check_issues(rtl["code"])
        issues += _stage_map_issues(rtl)
        try:
            compiler, compile_issues = _compile(rtl["code"], spec.get("entity_name") or rtl.get("entity_name"),
                                                version)
            issues += compile_issues
        except Exception as e:  # MCP server could not start / write failed
            issues.append({"code": "compiler_failed", "detail": f"Compiler could not run: {e}"})
        if compiler and not issues:     # only a design that is otherwise clean gets the slower dry run
            issues += smoke_issues(spec, rtl["code"])
    return {"compiler": compiler, "issues": issues}

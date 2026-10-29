# RTL Linter Review — request for FPGA-engineer feedback

**Project:** Hardware Agent (AI-generated VHDL → lint → simulate → implement)
**Component under review:** `agents/rtl_agent/rtl_linter.py` (+ `agents/lint_utils.py`, `agents/rtl_agent/rtl_smoke_check.py`)
**Date:** 2026-10-22
**What we need from you:** for each check below, tell us whether it is
**Correct** (catches a real problem, no false alarms), **Enhanced** (goes
beyond what xvhdl/xelab would tell us — keep it), **Over-specified** (rejects
legal or reasonable VHDL / Vivado would not care), or **Missing** (something an
RTL reviewer would check that we do not). A reply form is at the end.

---

## 1. Context: where the linter sits

An LLM writes VHDL RTL from a JSON spec (entity name, ports with direction and
width, clock, reset, behaviour, VHDL edition 93 or 2008). The linter runs after
every generation. Any issue it reports is sent back to the LLM as a fix
instruction, so a **false positive costs a regeneration** and a **false
negative costs a failed simulation/implementation run** later.

Order of checks (all static checks run on every call; the compiler and dry run
only when Vivado is reachable):

| # | Check | Method | Issue code |
|---|-------|--------|------------|
| 1 | Empty code / markdown fences / placeholders | text scan | `empty_code`, `markdown_fence`, `placeholder` |
| 2 | Entity present, name matches spec, architecture present | regex | `no_entity`, `name_mismatch`, `no_architecture` |
| 3 | Every spec port name appears in the code | regex (whole word) | `missing_port` |
| 4 | Forbidden constructs (deprecated libs, `= (others => …)`, 2008-only features in 93 mode) | regex per line | `forbidden_construct` |
| 5 | Declaration inside a process without `variable` keyword | regex on process declarative region | `missing_variable_keyword` |
| 6 | Unbalanced parentheses per statement | character scan, strings/char literals blanked | `unbalanced_parentheses` |
| 7 | `target <= A * B;` where target width ≠ width(A)+width(B) | regex width inference | `multiplier_width` |
| 8 | Same signal assigned from two processes / concurrent statements | regex | `multiple_drivers` |
| 9 | Real compile with `xvhdl` (`--2008` when spec says 2008); follow-on errors collapsed | Vivado | `compile_error` |
| 10 | Smoke dry run: auto-generated testbench, random inputs, 400 cycles, looks for a crash | Vivado xsim | `smoke_sim_crash` |
| 11 | A stage register `x_sK` reading a `_s(K-2)` or older register | regex on `_sK` names | `pipeline_stage_skip` |
| 12 | Pipelined design returned without a "stage map" note | notes scan | `stage_map_missing` |
| 13 | Data registers (stages + any output register) ≠ valid-chain bits | regex count of `_sK` / `valid_pipe` | `valid_data_length_mismatch` |
| 14 | `abs_x <= unsigned(-x)` with no sign test (`-x` is not \|x\|) | regex + preceding keyword | `abs_without_sign_check` |
| 16 | `x * 4` / `x * to_signed(4, N)` on a vector target (a shift written as a multiply; doubles the width) | regex on assignments, every statement position | `power_of_two_multiply` |
| 17 | Any `target <= expr` whose computable widths differ (`+`/`-` = widest operand, `*` = sum, `&` = sum, resize/shift/abs) | `rtl_widths.py` width arithmetic | `assignment_width` |
| 15 | Smoke dry run, output streams (`*tvalid`/`*tdata`/`*tready`): data 0 on every accepted beat, data moved while stalled, valid never rose | Vivado xsim, `SMOKE_STATS` line | `smoke_output_stuck_at_zero`, `smoke_stream_data_changed_while_stalled`, `smoke_output_never_valid` |

The design *rules* the LLM is asked to follow (registered outputs, one
arithmetic step per line, `_i` shadow signals for outputs, numeric_std only,
etc.) live in the prompt/skill files, **not** in the linter. The linter only
enforces the subset listed above.

---

## 2. Check-by-check detail

### 2.1 `placeholder`, `markdown_fence`, `empty_code`
- Flags any line containing the word `TODO` or `FIXME`, or a line that is only `...` (optionally after `--`).
- Flags ``` anywhere in the code.
- **Known behaviour:** a comment such as `-- no TODO here` is flagged. Harmless for generated code; tell us if you disagree.

### 2.2 `no_entity`, `name_mismatch`, `no_architecture`
- First `entity <name> is` must exist and `<name>` must equal the spec's `entity_name`.
- **Verified:** the comparison is **case-sensitive** (`Counter4` vs `counter4` is flagged). VHDL identifiers are case-insensitive. The spec is the authority on the name so this costs little, but strictly it is over-specified.
- Only the *first* entity in the file is checked.

### 2.3 `missing_port`
- Each spec port name must appear as a whole word anywhere in the code.
- **Known gaps:** a port name that appears only in a comment passes; direction, width and type are **not** checked here (the compile step and the smoke testbench catch mismatches later, less directly).
- Question: should the linter parse the entity port list and compare name / mode / width against the spec?

### 2.4 `forbidden_construct`
Rules applied in every edition:
- `use ieee.std_logic_arith / std_logic_unsigned / std_logic_signed` → rejected (numeric_std only).
- `x = (others => …)` or `x /= (others => …)` → rejected. Reason recorded in code: xvhdl compiles it, **xelab** refuses with "OTHERS is illegal aggregate choice for unconstrained target", so it would only fail at simulation. Assignments `x <= (others => '0')` are allowed.

Rules applied only when the spec says VHDL-93:
- `process(all)` → rejected (2008 only). **Correct.**
- `std_ulogic_vector` → rejected with the message "VHDL-2008 only". **We believe this is wrong:** `std_ulogic_vector` is defined in `std_logic_1164` since VHDL-87/93; only the 2008 subtype relationship with `std_logic_vector` is new. Please confirm; if so we will drop the rule or keep it as a style preference with a corrected message.

### 2.5 `missing_variable_keyword`
- Looks at the text between `process(...)` and its `begin`; any line of the form `name : type` whose first word is not `variable/constant/alias/type/subtype/file/attribute/function/procedure/impure/pure/use/shared` is flagged.
- Motivation: the LLM repeatedly wrote `temp_i : signed(47 downto 0);` inside a process, which produces a cascade of ~10 xvhdl errors.
- **BUG found while preparing this report (verified by running the function):** the regex also matches the word `process` in `end process;`, and then treats everything up to the *next* `begin` as a declarative region. Any **labelled concurrent statement** after the first process is therefore flagged as a missing `variable`:
  - `u1 : entity work.sub port map (...)` → flagged
  - `g1: for i in 0 to 3 generate` → flagged
  - `p2: process(clk)` (a second labelled process) → flagged
  This affects every multi-process design with labels. We will fix it (anchor on `process` not preceded by `end`); listed here so you know the current reports may contain it.

### 2.6 `unbalanced_parentheses`
- Blanks string and character literals, strips comments, then walks the text: a `)` with no open `(` in the current statement, or a `(` still open when the text ends, is reported once with the counts and the first line of the statement. A statement ends at `;` **outside** parentheses, so a port list is one statement.
- Motivation: xvhdl reports these only as "syntax error near ';'" on a later line.
- Possible blind spot: a `'` used as an attribute tick directly followed by a single character and another `'` (e.g. in `x'('a')`-style qualified expressions) could be mis-blanked. We have not seen it in practice.

### 2.7 `multiplier_width`
- Collects widths from `signal|variable NAME : unsigned|signed|std_logic_vector(H downto L)` with numeric bounds. For each `target <= A * B;` / `:=` with exactly one `*` at top level, infers width(A) and width(B) from: names, slices `x(7 downto 0)`, `resize(x, N)`, `to_signed/to_unsigned(v, N)`, `signed(...)`/`unsigned(...)`/`std_logic_vector(...)` wrappers, `'x'` and `"..."` literals, `&` concatenation.
- Reports when `width(target) ≠ width(A) + width(B)`; silent whenever any width is unknown (generic bounds, integer operands, constants, `a * b + c`, `resize(a*b, N)`).
- Motivation: numeric_std `*` returns `L'length + R'length` bits; a narrower target compiles but stops xsim with "Array sizes do not match".
- Question: is "target must be exactly L+R" the right rule, or should an explicit wider target (e.g. one guard bit) be accepted? Today a *wider* target is also flagged.

### 2.8 `multiple_drivers`
- Collects whole-signal targets (`name <=` at the start of a statement, after `then/else/begin/;/=>`) per `process ... end process` block and per concurrent statement outside processes; a name with more than one writer is reported.
- Element/slice writes (`x(i) <=`, `x(3 downto 0) <=`) are **ignored** (neither counted nor compared).
- **Verified false positives:**
  - Two architectures in one file that each declare a signal with the same name (e.g. `cnt` in `sub` and in `top`) → flagged, although they are different objects. Generated RTL is normally one entity per file, so this is rare, but it is wrong.
  - The same signal assigned in two **mutually exclusive `if … generate`** branches → flagged.
- Known false negative: two processes writing different slices of the same vector is a legal pattern in VHDL but writing the same slice from two places is not; both go unnoticed.
- Question: keep as is (cheap, catches the common LLM error) or restrict to one architecture at a time?

### 2.9 `compile_error` (xvhdl)
- The design is written to a temp dir and compiled with `xvhdl` (`--2008` when the spec says 2008). `ERROR` lines are kept; the temp path is stripped; the offending source line is appended.
- **Follow-on collapsing:** an error within 40 lines of the previous kept error is hidden when its text matches `is not a variable | is illegal in an expression | is not declared | is not a signal | syntax error near 'begin|end|if|then|else|;|)|,'` **or** it quotes a symbol already named in a kept error. A line "N follow-on error(s) hidden" is added. Identical texts are kept once.
- Risk: an independent `is not declared` within 40 lines of another error is hidden for that round; it reappears on the next compile after the first fix. Please say if the window or the wording list looks wrong.

### 2.10 `smoke_sim_crash` (dry run)
Runs only when all other checks are clean and Vivado is reachable.
- Builds a VHDL-2008 testbench from the spec: 10 ns clock (regardless of `frequency_mhz`), reset asserted 5 cycles (polarity from spec, default active-low), every other input driven with a pseudo-random LCG sequence for 400 cycles, outputs left open, no checking.
- Clock and reset are taken from `spec.clock.name` / `spec.reset.name` even when absent from `ports`.
- Compiles the DUT, runs xsim for 5 µs. Reports an issue **only** if the run ends in an error whose output contains `ERROR | FATAL | Failure | mismatch | do not match` (xsim's own `## …` echo lines excluded). The first 4 such lines are quoted.
- Motivation: width mismatches, out-of-range conversions and similar runtime faults that compile cleanly.
- **Over-specification risks:**
  - Random inputs ignore protocols (AXI-Stream handshakes, enables). A design with an internal `assert … severity failure` on illegal input sequences would be rejected for correct behaviour.
  - Port widths come from the spec, not the RTL; a generic-width RTL whose defaults differ from the spec would fail at elaboration and be reported as a crash.
  - The fixed 10 ns clock is fine for functional dry runs but says nothing about timing.

---

## 3. What the linter does NOT check (candidates for "Missing")
- Port **mode/width/type** vs spec (only the name).
- Sensitivity-list completeness for combinational processes (xvhdl does not error; Vivado synthesis warns).
- Latches (incomplete if/case in combinational processes).
- Registered outputs rule, reset coverage of every register, one-arithmetic-step-per-line rule — all prompt-only today.
- Reading an `out` port in VHDL-93 (left to xvhdl, which reports it clearly).
- Any synthesis/timing concern (handled later by Vivado implementation, not by lint).

---

## 4. Reply form

| # | Check | Correct / Enhanced / Over-specified | Comment |
|---|-------|-------------------------------------|---------|
| 2.1 | placeholder / fences | | |
| 2.2 | entity name (case-sensitive) | | |
| 2.3 | missing_port (name only) | | |
| 2.4a | deprecated Synopsys libraries | | |
| 2.4b | `= (others => …)` rejected | | |
| 2.4c | `process(all)` in 93 mode | | |
| 2.4d | `std_ulogic_vector` in 93 mode | | |
| 2.5 | missing `variable` keyword (after bug fix) | | |
| 2.6 | unbalanced parentheses | | |
| 2.7 | multiplier width must equal L+R | | |
| 2.8 | multiple drivers (whole-signal only) | | |
| 2.9 | xvhdl + follow-on collapsing (40-line window) | | |
| 2.10 | smoke dry run with random inputs | | |
| 3 | anything missing you would add | | |

Please return this file with the table filled in, or annotate inline.

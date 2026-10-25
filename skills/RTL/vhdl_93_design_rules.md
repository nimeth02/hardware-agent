# VHDL-93 design rules: forbidden constructs, required patterns, port style, complete template

Load this before writing ANY design file, together with
vhdl_language_rules.md (the full legal-VHDL reference taken from the IEEE
LRM: names, operators, aggregates, case, slices, ports, error table).
xvhdl's default standard is
VHDL-93 (no flag passed). Testbenches go in tb/ and use --2008; this
skill is for src/ files only.

═══════════════════════════════════════════════════════════════════
FORBIDDEN IN DESIGN FILES (VHDL-2008-only features)
═══════════════════════════════════════════════════════════════════

- `process(all)` — simplified sensitivity list. Use explicit lists:
  `process(clk, rst)`.
- Conditional / selected signal assignment INSIDE a process:
  `sig <= val1 when cond else val2;` or `with s select ...` written between
  a process's `begin` and `end process`. Use if/else or case there. (At the
  architecture level, outside any process, both forms are legal VHDL-93:
  LRM 9.5.1 and 9.5.2.)
- `if enable then` on a std_logic: write `if enable = '1' then` (the
  condition must be a boolean).
- Project style: use `std_logic_vector`, not `std_ulogic_vector`.
- Force / release statements.
- External names (`<< … >>`).
- Generic types or generic packages.

═══════════════════════════════════════════════════════════════════
MORE THINGS THAT DO NOT EXIST IN VHDL-93 (checked against the LRM grammar)
═══════════════════════════════════════════════════════════════════

Generated code often borrows these from VHDL-2008 or Verilog. The 93
grammar (LRM 7.2, 13.8, 9.7) has none of them:

- Block comments `/* ... */`. Only `-- ...` to the end of the line.
- Reduction operators on a vector: `or v`, `and v`, `xor v` with one
  operand. Write a loop, or compare: `v /= "0000"` instead of `or v`.
- Matching operators `?=`, `?/=`, `??`, and `case?` / `select?`.
- `maximum(a, b)`, `minimum(a, b)` (use an `if`), `to_string`, `to_hstring`,
  `to_ostring`, `rising_edge` on anything but std_logic, `std_logic'image`
  in design files.
- `else generate`, `elsif generate`, `case ... generate`. Use two
  `if ... generate` blocks with opposite conditions.
- Sized or signed bit strings: `12X"FF"`, `SX"F"`, `UB"1"`, `D"15"`.
  Write a plain `X"..."`, `B"..."` or `O"..."` string.
- Relational operators other than `= /= < <= > >=`; logical operators
  other than `and or nand nor xor xnor not`; `sll srl sla sra rol ror` work
  only on bit / boolean vectors, so on `signed` / `unsigned` call
  `shift_left`, `shift_right`, `rotate_left`, `rotate_right`.
- Reading an `out` port, and a bare std_logic as an `if` condition.
- `block` comments, `context` clauses, `package` generics, `force`/`release`,
  external names, `protected` types in design files.
- A port or signal of an unconstrained type without a range, except
  entity ports.

When unsure whether a construct is VHDL-93, write the plain older form:
an `if` statement, a loop, a named signal per step.

═══════════════════════════════════════════════════════════════════
ALSO FORBIDDEN — DEPRECATED IEEE LIBRARIES
═══════════════════════════════════════════════════════════════════

These are not VHDL standard — they're old Synopsys packages that break
arithmetic semantics. NEVER use them in design files:

- `ieee.std_logic_arith`
- `ieee.std_logic_unsigned`
- `ieee.std_logic_signed`

Use `ieee.numeric_std` for all arithmetic.

═══════════════════════════════════════════════════════════════════
REQUIRED LIBRARY HEADER (always exactly these three lines)
═══════════════════════════════════════════════════════════════════

  library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

═══════════════════════════════════════════════════════════════════
REQUIRED PATTERNS
═══════════════════════════════════════════════════════════════════

- All ports use `std_logic` or `std_logic_vector`.
- Use `unsigned` / `signed` from numeric_std for arithmetic inside the
  architecture, then convert back to std_logic_vector at the port boundary.
- Type conversion:
    unsigned(slv)                   slv → unsigned
    std_logic_vector(uns)           unsigned → slv
    to_unsigned(int, width)         integer → unsigned of given width
    to_integer(unsigned(slv))       slv → integer
- Synchronous process (synchronous reset, preferred for fast clocks):
    process(clk)
    begin
      if rising_edge(clk) then
        if rst = '1' then
          -- reset values
        elsif enable = '1' then
          -- clocked logic
        end if;
      end if;
    end process;
- Asynchronous reset (only if your spec explicitly demands it):
    process(clk, rst)
    begin
      if rst = '1' then
        -- reset values
      elsif rising_edge(clk) then
        -- clocked logic
      end if;
    end process;
- Constants and zeroing: `(others => '0')` for an all-zeros vector, but
  ONLY on the right of an assignment (`x <= (others => '0');`). Never
  compare against it: `if x = (others => '0')` compiles with xvhdl but
  xelab stops with "OTHERS is illegal aggregate choice for unconstrained
  target". Compare an unsigned with a number (`if count = 0 then`), or a
  std_logic_vector with a sized literal (`if v = "00000000" then`, or
  `unsigned(v) = 0`).
- Multiplication width: `unsigned * unsigned` (and `signed * signed`) is
  as wide as BOTH inputs added together (26 bits * 8 bits = 34 bits). The
  target must have exactly that width. xvhdl compiles a narrower or wider
  target, but the simulation stops with "array size mismatch". Declare the
  product at full width, then trim it afterwards:
    signal prod : unsigned(33 downto 0);          -- 26 + 8
    prod  <= a * b;                               -- a: 26 bits, b: 8 bits
    small <= resize(prod, 20);                    -- trim as a separate step
  The same holds for `+`/`-` after `resize`: size both sides explicitly.
  Never mix widths inside `a * (b & "000000")`; count the bits of every
  operand first.
- One writer per signal: every signal (and every output port's `_i`
  copy) is assigned in exactly ONE process or ONE concurrent `x <= ...;`
  line. Writing it from two places gives two drivers ('X' values on
  std_logic, an error on other types). If two processes both need to set a value, compute it in one
  process, or use two separate signals and combine them in a third place.
  A process must not list in its sensitivity list a signal that only it
  writes; clocked processes list just `clk` (and `rst` if asynchronous).
- Concatenation: `&` operator.
- Comparisons: `=` and `/=` on std_logic_vector are fine.
- Signal assignment uses `<=`. Variable assignment uses `:=`. Never mix.
- Inside a process, between `process (...)` and `begin`, EVERY declaration
  starts with `variable` (or `constant`):
    variable temp_i : signed(47 downto 0);
  A bare `temp_i : signed(47 downto 0);` is a syntax error, and every later
  use of temp_i then fails too ("is not a variable", "illegal in an
  expression"). Signals are declared in the architecture, never in a process.
- One arithmetic step per line. Put each operation into a named signal or
  variable of a declared width and chain them:
    t1 := resize(signed('0' & x_p1), 16) - resize(signed('0' & x_p2), 16);
    t2 := t1 + resize(signed('0' & x_m1), 16);
  Never nest several resize()/shift_left()/signed() calls in one statement:
  such chains are where parentheses go unbalanced ("syntax error near ;")
  and where widths stop matching.
- Every signal must be declared before use; every port must have a mode
  and a type.
- Never read an `out` port inside the architecture (xvhdl: "cannot read
  from 'out' object"). Do NOT change the port to `buffer` or `inout`.
  Instead declare `signal <name>_i : <same type>;`, use `<name>_i`
  everywhere inside (including handshake outputs such as tready/tvalid),
  and add exactly one `<name> <= <name>_i;` in the architecture body.

═══════════════════════════════════════════════════════════════════
PORT DECLARATION STYLE
═══════════════════════════════════════════════════════════════════

  entity my_design is
    port (
      clk      : in  std_logic;
      rst      : in  std_logic;
      enable   : in  std_logic;
      data_in  : in  std_logic_vector(7 downto 0);
      data_out : out std_logic_vector(7 downto 0);
      valid    : out std_logic
    );
  end entity my_design;

═══════════════════════════════════════════════════════════════════
COMPLETE DESIGN FILE TEMPLATE
═══════════════════════════════════════════════════════════════════

  library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

  entity NAME is
    port (
      clk : in  std_logic;
      rst : in  std_logic;
      -- inputs
      -- outputs
    );
  end entity NAME;

  architecture rtl of NAME is
    -- signal declarations
  begin
    -- processes
  end architecture rtl;

═══════════════════════════════════════════════════════════════════
ASCII-ONLY STRING RULE
═══════════════════════════════════════════════════════════════════

xvhdl rejects bytes ≥ 0x80 in source files. Do NOT use:
- em-dashes (—), en-dashes (–) → use ASCII hyphen `-`
- curly quotes (" " ' ') → use straight `"` and `'`
- typographic ellipsis (…) → use three dots `...`

If you copy text from a document into a comment or report string,
re-type it as ASCII.

═══════════════════════════════════════════════════════════════════
EVERY OUTPUT IS REGISTERED
═══════════════════════════════════════════════════════════════════

To meet the spec's clock (spec.clock.frequency_mhz), every output port must come out of a flip-flop,
not combinational logic. Pattern:

  signal data_out_reg : std_logic_vector(7 downto 0);
  ...
  process(clk)
  begin
    if rising_edge(clk) then
      data_out_reg <= <combinational expression>;
    end if;
  end process;
  data_out <= data_out_reg;

The bare `data_out <= ...` assignment at the end is fine — it's just
naming. The flop is the `data_out_reg <= ...` inside the process.

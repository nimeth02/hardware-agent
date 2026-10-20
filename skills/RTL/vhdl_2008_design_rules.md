# VHDL-2008 design rules: allowed 2008 features, required patterns, port style, complete template

Load this before writing ANY design file whose spec says
"vhdl_version": "2008". The design is compiled with `xvhdl --2008`, so
the VHDL-2008 features below are allowed. Testbenches go in tb/ and have
their own 2008 rules; this skill is for src/ files only. If the spec's
constraints mention Verilog or SystemVerilog, ignore them: this system
writes VHDL and the vhdl_version field is the only authority.

═══════════════════════════════════════════════════════════════════
VHDL-2008 FEATURES ALLOWED IN DESIGN FILES (use them plainly)
═══════════════════════════════════════════════════════════════════

- `process(all)` for purely combinational processes (every signal read is
  in the sensitivity list automatically). Clocked processes still use
  `process(clk)`.
- Conditional signal assignment in the architecture body:
  `sig <= val1 when cond else val2;`
- Conditions used directly as booleans: `if enable then` where `enable`
  is std_logic (the `?? ` conversion is implicit in if/when).
- `std_ulogic_vector` is allowed but not needed; prefer std_logic_vector
  on ports for tool compatibility.
- Unconstrained element arrays (`type word_array is array (natural range <>)
  of std_logic_vector`) and `'subtype`.
- Generic types / generic packages are legal but AVOID them: Vivado
  synthesis support is incomplete.

Still FORBIDDEN (not synthesizable or unsupported by Vivado synthesis):
- Force / release statements.
- External names (`<< ... >>`).
- Shared variables, protected types, file I/O, `wait for` in design files.

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
- Synchronous process (synchronous reset, preferred for 500 MHz designs):
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
- Constants and zeroing: `(others => '0')` for an all-zeros vector.
- Concatenation: `&` operator.
- Comparisons: `=` and `/=` on std_logic_vector are fine.
- Signal assignment uses `<=`. Variable assignment uses `:=`. Never mix.
- Every signal must be declared before use; every port must have a mode
  and a type.
- VHDL-2008 allows reading an `out` port inside the architecture, but
  keep the `_i` internal-signal pattern anyway so the same code also
  compiles under VHDL-93: declare `signal <name>_i : <same type>;`, use
  `<name>_i` everywhere inside (including handshake outputs such as
  tready/tvalid), and add exactly one `<name> <= <name>_i;`.

═══════════════════════════════════════════════════════════════════
COMMON COMPILE ERRORS AND HOW TO AVOID THEM
═══════════════════════════════════════════════════════════════════

- Variables inside a process MUST be declared with the `variable` keyword,
  between `process(...)` and `begin`:
    process(clk)
      variable abs_i : unsigned(11 downto 0);
    begin
  Writing `abs_i : unsigned(11 downto 0);` without `variable` is a syntax
  error that cascades into dozens of follow-up errors.
- Never zero a record with `(others => (others => '0'))` when its fields
  have different types (xvhdl: "type mismatch between 'others' choices").
  Either assign every field by name:
    stage <= (valid => '0', i => (others => '0'), ns => (others => '0'));
  or declare a named constant of the record type with every field listed
  and assign that constant. Prefer separate signals over records when a
  pipeline stage mixes std_logic, signed and unsigned.
- Multiplying signed by unsigned is not defined: convert the unsigned
  operand first, e.g. `a * signed('0' & b)` where `a : signed`, and give
  the product a signal/variable of exactly `a'length + b'length + 1` bits
  (or `resize` it). Do not nest conversions of unresolved width such as
  `signed('0' & x(3 downto 0))` inside an expression the compiler cannot
  type; put the converted value in a named variable first.
- `-x` on a `signed` is fine; on an `unsigned` it is not. Negate as signed,
  then convert: `unsigned(-x)` where `x : signed`.

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

For 500 MHz timing closure, every output port must come out of a flip-flop,
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

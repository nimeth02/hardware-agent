# VHDL-93 design rules: forbidden constructs, required patterns, port style, complete template

Load this before writing ANY design file. xvhdl's default standard is
VHDL-93 (no flag passed). Testbenches go in tb/ and use --2008; this
skill is for src/ files only.

═══════════════════════════════════════════════════════════════════
FORBIDDEN IN DESIGN FILES (VHDL-2008-only features)
═══════════════════════════════════════════════════════════════════

- `process(all)` — simplified sensitivity list. Use explicit lists:
  `process(clk, rst)`.
- Conditional signal assignment in architecture:
  `sig <= val1 when cond else val2;` — use a process with if/else instead.
- Unresolved types: `std_ulogic_vector`. Use `std_logic_vector`.
- Force / release statements.
- External names (`<< … >>`).
- Generic types or generic packages.

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

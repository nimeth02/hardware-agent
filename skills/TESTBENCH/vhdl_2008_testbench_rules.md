# VHDL-2008 testbench rules: structure, DUT hookup, test-vector replay, run control

Load this before writing ANY testbench. Testbenches are compiled with
`xvhdl --2008` and simulated with xsim by the simulation agent; the rules
below are what that flow depends on.

═══════════════════════════════════════════════════════════════════
NAMING AND STRUCTURE
═══════════════════════════════════════════════════════════════════

- Entity name is exactly `<dut_entity>_tb`, with NO ports.
- Library header:
    library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;
- One local signal per DUT port, same name and width as the port.
- Instantiate the DUT with the instance label `dut` and direct entity
  instantiation (no component declaration):
    dut: entity work.<dut_entity>
      port map (clk => clk, rst => rst, ...);
  The simulator logs waveforms from `/*/dut/*`; any other label loses them.

═══════════════════════════════════════════════════════════════════
CLOCK AND RESET
═══════════════════════════════════════════════════════════════════

- Clock from the spec frequency (default 100 MHz if unspecified), stopped
  by a `done` flag so the simulation can end. Declare the period as a
  `time` constant and divide THAT; never divide a number into a time
  literal (`10/2 ns`, `period_ns/2 ns` and `1 sec / 100e6` do not compile):
    constant clk_period : time := 10 ns;      -- 100 MHz
    signal done : boolean := false;
    clk <= not clk after clk_period / 2 when not done else '0';
- Reset is driven from the vectors (their reset column); do not add a
  separate reset sequence.
- Drive inputs, then `wait until rising_edge(clk); wait for 1 ns;` before
  reading outputs, so inputs are stable at the edge and outputs settled.

═══════════════════════════════════════════════════════════════════
TEST-VECTOR REPLAY (every testbench works this way)
═══════════════════════════════════════════════════════════════════

The golden vectors are two files written by the test-vector agent in a
fixed layout you derive from the spec — the files themselves are not part
of your input:
    test_vectors_in.csv   header: cycle, <every port with direction
                          "input", in spec order>   (stimulus)
    test_vectors_out.csv  header: cycle, <every port with direction
                          "output", in spec order>  (expected AFTER that
                          cycle's rising edge)
    rows: one per clock cycle in both files, same cycle numbers, integers
          only (0/1 for 1-bit signals, unsigned for buses)
The simulator stages both files in its working directory, so open them by
their bare names.

- The CSVs are the ONLY reference: never invent stimulus, expected values
  or extra checkpoints — the vectors already cover reset, normal operation
  and the boundary cases.
- Keep an error counter variable in the stimulus process. Every mismatch
  is reported with `severity error` (never `failure` — the run must
  continue to the summary) and increments the counter.
- Add `use std.textio.all;` to the library header.
- Per cycle: read one row from EACH file; if their "cycle" values differ,
  report it (severity error, count it) and stop replaying. Read the
  columns in exactly the header order above. Do not drive "cycle"; the
  clock port is a column in the inputs file — read it and ignore it (the
  clock process generates the clock). Drive every other input column;
  compare every output column.
- Every `variable` (line buffer, parsed values, the error counter) MUST
  be declared inside the stimulus process, between `process` and its
  `begin`. A `variable` in the architecture does not compile (xvhdl:
  "variable outside of subprogram or process must be 'shared'"). Only
  signals and the `file` declaration belong in the architecture.
- Pattern, for input ports clk, rst, en and output port count:
    architecture sim of counter_tb is
      signal clk   : std_logic := '0';
      signal rst, en : std_logic := '0';
      signal count : std_logic_vector(3 downto 0);
      signal done  : boolean := false;
      constant clk_period : time := 10 ns;
      file in_file  : text;                       -- the files, not variables, live here
      file out_file : text;
    begin
      dut: entity work.counter port map (clk => clk, rst => rst, en => en, count => count);
      clk <= not clk after clk_period / 2 when not done else '0';

      stimulus : process
        variable li, lo  : line;                  -- ALL variables inside the process
        variable fin, fout : file_open_status;
        variable v_cycle, v_cycle_out, v_clk, v_rst, v_en, v_count : integer;
        variable errors  : integer := 0;
        procedure commas_to_blanks(l : inout line) is  -- textio reads blank-separated integers
        begin
          for i in l.all'range loop
            if l.all(i) = ',' or l.all(i) = CR then
              l.all(i) := ' ';
            end if;
          end loop;
        end procedure;
      begin
        file_open(fin,  in_file,  "test_vectors_in.csv",  read_mode);
        file_open(fout, out_file, "test_vectors_out.csv", read_mode);
        if fin /= open_ok or fout /= open_ok then
          report "cannot open test_vectors_in.csv / test_vectors_out.csv" severity error;
          errors := errors + 1;
        else
          readline(in_file, li);                  -- header rows
          readline(out_file, lo);
          while not endfile(in_file) and not endfile(out_file) loop
            readline(in_file, li);  commas_to_blanks(li);
            readline(out_file, lo); commas_to_blanks(lo);
            read(li, v_cycle); read(li, v_clk); read(li, v_rst); read(li, v_en);   -- inputs file order
            read(lo, v_cycle_out); read(lo, v_count);                             -- outputs file order
            if v_cycle /= v_cycle_out then
              report "cycle mismatch between the two vector files: " & integer'image(v_cycle)
                & " vs " & integer'image(v_cycle_out) severity error;
              errors := errors + 1;
              exit;
            end if;
            if v_rst = 1 then rst <= '1'; else rst <= '0'; end if;   -- 1-bit input (v_clk is read and ignored)
            if v_en = 1 then en <= '1'; else en <= '0'; end if;      -- if/else, NEVER "<= x when c else y" here
            -- bus input: data <= std_logic_vector(to_unsigned(v_data, data'length));
            wait until rising_edge(clk);
            wait for 1 ns;
            if to_integer(unsigned(count)) /= v_count then      -- bus output
              report "cycle " & integer'image(v_cycle) & ": count expected "
                & integer'image(v_count) & " got "
                & integer'image(to_integer(unsigned(count))) severity error;
              errors := errors + 1;
            end if;
            -- 1-bit output: if (flag = '1') /= (v_flag = 1) then ... end if;
          end loop;
          file_close(in_file);
          file_close(out_file);
        end if;
        if errors = 0 then
          report "SIMULATION PASSED" severity note;
        else
          report "SIMULATION FAILED" severity note;
        end if;
        done <= true;
        wait;
      end process;
    end architecture;
- num_vectors clock cycles must fit the simulator's default run (see RUN
  CONTROL); finish with the result marker and `done <= true; wait;` as
  usual.
- Read EVERY column of each row, in header order, into its own variable:
  the inputs row has 1 + (number of input ports) values and the outputs
  row 1 + (number of output ports). Skipping a column (for example an
  output you do not check, such as `s_axis_tready`) shifts every later
  value into the wrong variable. The cycle numbers go into two different
  variables (v_cycle, v_cycle_out); comparing a variable with itself
  checks nothing.

═══════════════════════════════════════════════════════════════════
WIDE PORTS (32 bits or more)
═══════════════════════════════════════════════════════════════════

textio's `read(l, integer)` stops at 2**31-1 and the run dies with
"TEXTIO function READ: integer value is out of range". So a port of 32
bits or more MUST NOT go through an `integer` variable. Declare its
variable as a vector of the port's width and read it with this procedure,
declared inside the stimulus process next to commas_to_blanks (it reads
the decimal digits one character at a time, so any width works):

    variable v_s_axis_tdata : std_logic_vector(31 downto 0);   -- NOT integer
    procedure read_wide(l : inout line; v : out std_logic_vector) is
      variable c   : character;
      variable ok  : boolean;
      variable acc : unsigned(v'length - 1 downto 0) := (others => '0');
    begin
      loop                                   -- skip the blanks before the number
        read(l, c, ok);
        exit when not ok or c /= ' ';
      end loop;
      while ok and c >= '0' and c <= '9' loop
        acc := resize(acc * 10, acc'length) + to_unsigned(character'pos(c) - character'pos('0'), acc'length);
        read(l, c, ok);
      end loop;
      v := std_logic_vector(acc);
    end procedure;

    read(li, v_cycle); read(li, v_aclk); read_wide(li, v_s_axis_tdata); ...   -- in header order
    s_axis_tdata <= v_s_axis_tdata;                                            -- drive a wide input
    if m_axis_wide /= v_m_axis_wide then                                       -- compare a wide output
      report "cycle " & integer'image(v_cycle) & ": m_axis_wide expected hi="
        & integer'image(to_integer(unsigned(v_m_axis_wide(31 downto 16)))) & " lo="
        & integer'image(to_integer(unsigned(v_m_axis_wide(15 downto 0)))) & " got hi="
        & integer'image(to_integer(unsigned(m_axis_wide(31 downto 16)))) & " lo="
        & integer'image(to_integer(unsigned(m_axis_wide(15 downto 0)))) severity error;
      errors := errors + 1;
    end if;

Ports of 31 bits or fewer keep the plain `integer` read shown in the
pattern above.

═══════════════════════════════════════════════════════════════════
SIMULATOR LIMITS (xsim 2020.1)
═══════════════════════════════════════════════════════════════════

- Never use `to_string`, `to_hstring` or `to_ostring` on vectors: xvhdl
  accepts them but xelab 2020.1 stops with "The Vhdl 2008 to_string
  Operator is not supported yet for simulation". Print values as
  integers instead:
    integer'image(to_integer(unsigned(count)))
  and a single std_logic with std_logic'image(sig).
- Never write `sig <= a when cond else b;` INSIDE a process: xvhdl accepts
  it but xelab 2020.1 stops with "The Vhdl 2008 Sequential Conditional
  Signal Assignment is not supported yet for simulation". Inside a process
  use if/else:
    if v_rst = 1 then rst <= '1'; else rst <= '0'; end if;
  The `when ... else` form is fine only as a concurrent statement in the
  architecture body (the clock line above is one).

═══════════════════════════════════════════════════════════════════
RESULT MARKERS (the simulator greps for these verbatim)
═══════════════════════════════════════════════════════════════════

At the end of the stimulus process print exactly ONE of:
    report "SIMULATION PASSED" severity note;
    report "SIMULATION FAILED" severity note;
Never print both, never use any other wording, never attach them to
`severity failure`.

═══════════════════════════════════════════════════════════════════
RUN CONTROL
═══════════════════════════════════════════════════════════════════

- No unbounded loops. Finish with `done <= true; wait;`.
- Total simulated time must fit the simulator's default run of 10 us
  (at 100 MHz that is 1000 clock cycles) — keep stimulus well inside it.

═══════════════════════════════════════════════════════════════════
ASCII-ONLY RULE
═══════════════════════════════════════════════════════════════════

xvhdl rejects bytes >= 0x80. Use ASCII hyphens and straight quotes only.

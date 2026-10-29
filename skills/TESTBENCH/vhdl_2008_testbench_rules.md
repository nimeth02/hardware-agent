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

- SIGNED PORTS ARE STORED AS THEIR UNSIGNED BIT PATTERN. A port the spec
  marks "signed" is written to the CSV as 0 .. 2**N-1 (an 8-bit -127 is
  stored as 129, -1 as 255). Therefore ALWAYS compare a bus output with
  `to_integer(unsigned(port))` and drive a bus input with
  `std_logic_vector(to_unsigned(v, port'length))`, even for signed ports.
  NEVER use `signed(...)` / `to_signed(...)` on a CSV value: comparing
  `to_integer(signed(b3))` (= -127) with the CSV's 129 reports a mismatch
  for every negative value although the RTL is correct.

- Which file a port is in comes from its "direction" in the spec, NOT from
  its name. On an AXI-Stream: `s_axis_tready` is an OUTPUT of the design
  (outputs file), `m_axis_tready` is an INPUT (inputs file). List the
  spec's ports once, keep only direction "input" for the inputs file and
  only "output" for the outputs file, in the spec's order, and read in
  exactly that order. One port in the wrong file shifts every later column.
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
            -- 1-bit output (std_logic), complete form:
            if (flag = '1') /= (v_flag = 1) then
              report "cycle " & integer'image(v_cycle) & ": flag expected "
                & integer'image(v_flag) & " got " & std_logic'image(flag) severity error;
              errors := errors + 1;
            end if;
            --   NEVER std_logic'val(v_flag): position 0 is 'U' and 1 is 'X',
            --   so every expected value is wrong ("expected 'U' got '0'").
            --   NEVER unsigned(flag) / to_integer(unsigned(flag)) on a 1-bit
            --   port: xvhdl "cannot convert type 'std_logic' to type
            --   'unsigned'". NEVER "& flag &" in a report: a bare std_logic in
            --   a string is "ambiguous &". Print it with std_logic'image(flag).
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
STREAMING DESIGNS (spec has s_axis_tvalid/s_axis_tready/m_axis_tvalid/m_axis_tready)
═══════════════════════════════════════════════════════════════════

A pipelined stream design answers several cycles AFTER the input beat
(the spec only bounds the latency, e.g. "<= 8 cycles"), and it may stall
when m_axis_tready = '0'. The vectors cannot know the exact delay, so for
these designs compare in BEAT ORDER, not cycle by cycle:

- Still read both files row by row, every column, as above. A row of the
  outputs file with m_axis_tvalid = 1 is one EXPECTED BEAT; collect them in
  order (the k-th such row is the k-th expected output beat). Ignore the
  `cycle` alignment of the outputs rows and ignore the s_axis_tready and
  m_axis_tvalid columns as cycle-exact values.
- Drive each inputs row for one cycle, except: a row with s_axis_tvalid = 1
  is held (do not read the next row) until the rising edge at which the
  DUT's s_axis_tready = '1', so the beat is really accepted.
- AN IDLE ROW (s_axis_tvalid = 0) LASTS EXACTLY ONE EDGE. There is no beat
  to accept, so s_axis_tready is irrelevant: leave the hold loop at once.
  The exit line of the hold loop MUST therefore be
      if cur_s_axis_tvalid = 0 or dut_s_axis_tready = '1' then exit; end if;
  A loop that exits only on `cur_s_axis_tvalid = 1 and dut_s_axis_tready = '1'`
  waits 64 edges on EVERY idle row (reset rows, gaps, the rows after the
  last beat) and reports "cycle N: s_axis_tready never rose (deadlock)"
  for each of them although the design is fine (llr_demap16, 2026-10-29:
  cycles 0-1 and 50-59 were all idle rows). The linter rejects that loop.
- Decide "accepted" from a copy taken AT the edge, like the output side:
  `dut_s_axis_tready := s_axis_tready;` right after
  `wait until rising_edge(aclk);`. The beat was taken at this edge only if
  `dut_s_axis_tready = '1'`; otherwise re-drive the SAME row for the next
  edge. THE WAIT MUST HAVE A LIMIT: count the edges spent on one row and
  after 64 report "cycle N: s_axis_tready never rose (deadlock)" severity
  error, count it, and move on to the next row. A wait without this limit
  never ends when the DUT cannot accept (its output is blocked while
  m_axis_tready = '0'): the result marker is never printed and the whole
  run is reported as "no_marker" instead of a clear error.
- On EVERY rising edge where m_axis_tvalid = '1' and m_axis_tready = '1',
  compare m_axis_tdata with the next expected beat and advance to the
  following one. A beat with nothing left to expect is an error
  ("unexpected output beat").
- SAMPLE THE HANDSHAKE AT THE EDGE, NOT AFTER IT. The DUT consumes a beat
  at the rising edge where tvalid and tready were both '1' JUST BEFORE
  that edge. So copy m_axis_tvalid, m_axis_tready and m_axis_tdata into
  the variables dut_m_axis_tvalid, dut_m_axis_tready, dut_m_axis_tdata
  (keep these names; the checker accepts them) immediately after
  `wait until rising_edge(aclk);` and BEFORE `wait for 1 ns;` (at that
  moment the signals still hold their before-the-edge values), then
  compare using the copies. Checking the
  live signals 1 ns after the edge pairs the NEW tvalid/tdata with the
  tready you are about to change: a beat whose tvalid first appears while
  tready is '0' is then consumed unseen, and a correct RTL with 7 or 8
  cycles of latency reports "missing output beats".
- After the last inputs row, keep clocking with s_axis_tvalid = '0' and
  m_axis_tready = '1' for 16 more cycles so the pipeline drains; then any
  expected beat not yet seen is an error ("missing output beat k").
- While m_axis_tready = '0' and m_axis_tvalid = '1', m_axis_tdata must not
  change between edges; report it if it does.
- Report mismatches as "beat k (input cycle N): m_axis_tdata expected ...
  got ..." where N is the cycle of the inputs row that carried that beat.

Implementation sketch (inside the single stimulus process; the expected
beats are read first into an array, then the inputs are replayed):

    type beat_array is array (0 to 1023) of std_logic_vector(23 downto 0);
    variable exp_beats : beat_array;  variable n_exp, k_exp : integer := 0;
    variable dut_m_axis_tvalid, dut_m_axis_tready : std_logic;          -- before-the-edge copies
    variable dut_m_axis_tdata : std_logic_vector(23 downto 0);
    -- pass 1: read ALL outputs rows, keep rows with m_axis_tvalid = 1
    while not endfile(out_file) loop
      readline(out_file, lo); commas_to_blanks(lo);
      read(lo, v_cycle_out); read(lo, v_s_axis_tready); read(lo, v_m_axis_tvalid); read_wide(lo, v_m_axis_tdata);
      if v_m_axis_tvalid = 1 then exp_beats(n_exp) := v_m_axis_tdata; n_exp := n_exp + 1; end if;
    end loop;
    -- pass 2: replay inputs; check every accepted output beat against exp_beats(k_exp)
    ...
      wait until rising_edge(aclk);
      dut_m_axis_tvalid := m_axis_tvalid; dut_m_axis_tready := m_axis_tready; dut_m_axis_tdata := m_axis_tdata;
      wait for 1 ns;
      if dut_m_axis_tvalid = '1' and dut_m_axis_tready = '1' then   -- the beat consumed at this edge
        if k_exp >= n_exp then report "unexpected output beat" severity error; errors := errors + 1;
        elsif dut_m_axis_tdata /= exp_beats(k_exp) then report "beat " & integer'image(k_exp) & ...; errors := errors + 1;
        end if;
        k_exp := k_exp + 1;
      end if;
      -- now drive the next inputs row (s_axis_*, m_axis_tready) for the coming edge
    ...
    if k_exp < n_exp then report "missing output beats" severity error; errors := errors + 1; end if;

Designs WITHOUT a stream handshake (counters, plain registered logic) keep
the cycle-exact comparison of the pattern above.

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

Copy the procedure EXACTLY. Never replace `v'length` with a number such as
`31 downto 0`: the one procedure serves every port it is called for (a
24-bit output and a 32-bit input alike), and a fixed width stops the dry
run at time 0 with "Array sizes do not match, left array has 24 elements,
right array has 32 elements". Declare every v_<port> and dut_<port>
variable with exactly that port's width from the spec (the linter checks
both: read_wide_fixed_width, vector_variable_width).

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

═══════════════════════════════════════════════════════════════════
CHECK YOURSELF BEFORE ANSWERING (these are the mistakes that repeat)
═══════════════════════════════════════════════════════════════════

[ ] No `<= ... when ... else ...` inside any process; use if/else there.
    Search your code for the word `when` and check each one.
[ ] One `read(...)` per column of each CSV row, in header order: the
    inputs row has 1 + (input ports) values, the outputs row 1 + (output
    ports). Count them against the header lines in the spec, including
    columns you do not use (clock, outputs you do not compare).
[ ] Ports of 32 bits or more use read_wide, never an integer variable.
[ ] Exactly one "SIMULATION PASSED" or "SIMULATION FAILED" report.
[ ] No to_string / to_hstring on vectors.

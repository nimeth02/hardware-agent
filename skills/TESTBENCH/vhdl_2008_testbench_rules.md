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
  increments the counter; only the FIRST 20 are printed, with `severity
  error` (never `failure` — the run must continue to the summary):
      if errors < 20 then report "cycle ..." severity error; end if;
      errors := errors + 1;
  Before the result marker print the total once:
      report "mismatches: " & integer'image(errors) severity note;
  A testbench that prints a report on every one of 1200 cycles writes a
  log the tool has to cut at 200 KB: the first mismatches and the analyst's
  evidence disappear in the cut (on 2026-10-30, timing_recovery, the end
  with the marker was lost too and the run was judged "no marker" and
  rerun for nothing).
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
- Pattern, for input ports clk, rst, en and output port count (a design
  WITHOUT a stream handshake; for s_axis_tvalid + m_axis_tvalid designs copy
  the COMPLETE STREAMING TESTBENCH under STREAMING DESIGNS instead):
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
              if errors < 20 then                                -- print the first 20 only, count them all
                report "cycle " & integer'image(v_cycle) & ": count expected "
                  & integer'image(v_count) & " got "
                  & integer'image(to_integer(unsigned(count))) severity error;
              end if;
              errors := errors + 1;
            end if;
            -- 1-bit output (std_logic), complete form:
            if (flag = '1') /= (v_flag = 1) then
              if errors < 20 then
                report "cycle " & integer'image(v_cycle) & ": flag expected "
                  & integer'image(v_flag) & " got " & std_logic'image(flag) severity error;
              end if;
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
        report "mismatches: " & integer'image(errors) severity note;   -- the total, once
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
STREAMING DESIGNS (spec has s_axis_tvalid AND m_axis_tvalid; the tready
wires s_axis_tready / m_axis_tready may be missing)
═══════════════════════════════════════════════════════════════════

A pipelined stream design answers several cycles AFTER the input beat
(the spec only bounds the latency, e.g. "<= 8 cycles", or lets the RTL
declare it: "a fixed integer latency L must be declared"), and it may
stall when m_axis_tready = '0'. The vectors cannot know the exact delay,
so for these designs compare in BEAT ORDER, not cycle by cycle. This
applies as soon as the spec has s_axis_tvalid and m_axis_tvalid, also
when it has NO m_axis_tready ("downstream is always ready") and NO
s_axis_tready port.

- Still read both files row by row, every column, as above. A row of the
  outputs file with m_axis_tvalid = 1 is one EXPECTED BEAT; collect them in
  order (the k-th such row is the k-th expected output beat). Ignore the
  `cycle` alignment of the outputs rows and ignore the s_axis_tready and
  m_axis_tvalid columns as cycle-exact values.
- NEVER COMPARE THE FLAGS CYCLE BY CYCLE. The CSV's m_axis_tvalid column
  says WHICH rows hold expected beats, not WHEN the design raises its
  valid: the design's latency and its strobe pattern (one output per
  symbol in a timing-recovery loop) are its own and the golden model does
  not know them; what s_axis_tready shows during reset is its own too.
  All of these are rejected (linter: streaming_flag_cycle_compare):
      if (m_axis_tvalid = '1') /= (v_m_axis_tvalid = 1) then ...
      if dut_m_axis_tvalid = '0' and v_m_axis_tvalid = 1 then report "... missing m_axis_tvalid"
      if dut_m_axis_tvalid = '1' and v_m_axis_tvalid = 0 then report "... unexpected m_axis_tvalid"
      if (s_axis_tready = '1') /= (v_s_axis_tready = 1) then ...
  Read v_s_axis_tready and v_m_axis_tvalid only to find the expected beats.
  (timing_recovery, 2026-10-30: "missing m_axis_tvalid" on every expected
  row and "unexpected m_axis_tvalid" on every real beat, the beats
  themselves never compared, and the RTL's strobe timing was blamed.)
- EVERY PAYLOAD COLUMN IS PART OF THE BEAT. The outputs file may carry
  more than m_axis_tdata (m_axis_tuser, m_axis_tlast, ...): keep one
  expected array per such column (exp_tdata, exp_tuser), fill them from
  the same rows (m_axis_tvalid = 1), and compare each of them on the beat.
  On rows with m_axis_tvalid = 0 those columns hold 0: never compare them.
- NO m_axis_tready PORT ("downstream always ready"): do not declare, drive
  or read m_axis_tready anywhere (it does not compile; linter:
  no_m_axis_tready_port). A beat is consumed on EVERY rising edge where the
  DUT's m_axis_tvalid = '1', so the compare condition is
      if dut_m_axis_tvalid = '1' then
  Everything else (expected-beat arrays, the bounded hold loop on
  s_axis_tready, the drain) stays the same; during the drain only
  s_axis_tvalid <= '0' is driven.
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
- On EVERY rising edge where m_axis_tvalid = '1' and m_axis_tready = '1'
  (without an m_axis_tready port: where m_axis_tvalid = '1'), compare
  m_axis_tdata (and every other payload column) with the next expected
  beat and advance to the following one. A beat with nothing left to
  expect is an error ("unexpected output beat").
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
  m_axis_tready = '1' (only if that port exists) for 16 more cycles so the
  pipeline drains; then any expected beat not yet seen is an error
  ("missing output beats: expected N got K" — one line, not one per beat).
- The report cap applies here too: print the first 20 mismatching beats,
  count them all, print the total before the marker.
- While m_axis_tready = '0' and m_axis_tvalid = '1', m_axis_tdata must not
  change between edges; report it if it does.
- Report mismatches as "beat k (input cycle N): m_axis_tdata expected ...
  got ..." where N is the cycle of the inputs row that carried that beat.

CLOSED-LOOP DESIGNS: COMPARE WITH A TOLERANCE, NEVER BIT FOR BIT.
A closed-loop design feeds its own output back into the next step: a
timing-recovery loop (TED, NCO, loop filter, Farrow / interpolator, mu),
a PLL / DLL, an AGC, a CFO or phase tracker, an adaptive equaliser (the
spec's behavior names an error term e, an accumulator / integrator, a gain
K1 / K2 / alpha, or a state updated from the output). The golden model
computes such a loop in exact double arithmetic; the RTL rounds every
product to a fixed width. The first symbol differs by one or two steps,
that difference feeds the error, and every later strobe time and mu drift
from the model: a bit-exact compare of such a design can NEVER pass, no
matter how the RTL is written (timing_recovery, 2026-11-05: beat 1
expected I=-46 Q=-47, got I=-47 Q=-46; 207 then 181 mismatches over three
attempts; the RTL was blamed every time). For these designs:
- Compare each FIELD of a payload word separately, with a tolerance, not
  the packed word: one step of the I field moves the packed word by 4096.
  Decode the fields at the spec's [hi:lo] positions as signed values, then
      if abs(got_i - exp_i) > TOL_SYM or abs(got_q - exp_q) > TOL_SYM then
  with `constant TOL_SYM : integer := 2;` (2 steps of the output's
  fixed-point unit; use the spec's tolerance when it gives one). The
  fractional offset (m_axis_tuser / mu) gets its own constant, e.g.
  `constant TOL_MU : integer := 8;` out of 256, because mu is a loop state
  and drifts more than the symbol.
- Report with the same words as before ("beat k (input cycle N):
  m_axis_tdata expected ... got ...") so the analyst's reader still finds
  them; add " (tolerance TOL)" at the end of the message.
- The beat count may differ by a few: the RTL's loop delay changes when
  the strobes fall. Report "missing output beats" / "unexpected output
  beat" only when the count differs by more than 2 % of the expected beats
  (at least 2 beats).
- Everything else (beat order, arrays, next_edge, the drain) is unchanged.

COMPLETE STREAMING TESTBENCH. Copy this file and change only the entity
name, the ports, their widths and the CSV columns to match the spec. It is
written for a design `stream_core` with the ports aclk, aresetn,
s_axis_tvalid, s_axis_tready (output), s_axis_tdata (24 bits),
m_axis_tvalid, m_axis_tdata (24 bits) and m_axis_tuser (8 bits), and NO
m_axis_tready port. All checking happens in ONE procedure, next_edge: it
waits for one rising edge, copies the DUT's outputs at that edge, and
compares the beat consumed at that edge with the next expected beat. Every
edge of the run (the hold loop and the drain included) goes through
next_edge, so no beat is missed. Checked on 2026-11-03: with the
m_axis_tready changes below it prints SIMULATION PASSED on llr_demap16.

    library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;
    use std.textio.all;

    entity stream_core_tb is
    end entity stream_core_tb;

    architecture sim of stream_core_tb is
      signal aclk          : std_logic := '0';
      signal aresetn       : std_logic := '0';
      signal s_axis_tvalid : std_logic := '0';
      signal s_axis_tready : std_logic;
      signal s_axis_tdata  : std_logic_vector(23 downto 0) := (others => '0');
      signal m_axis_tvalid : std_logic;
      signal m_axis_tdata  : std_logic_vector(23 downto 0);
      signal m_axis_tuser  : std_logic_vector(7 downto 0);
      signal done          : boolean := false;
      constant clk_period  : time := 10 ns;                 -- 100 MHz
      file in_file  : text;
      file out_file : text;
    begin
      dut: entity work.stream_core
        port map (aclk          => aclk,
                  aresetn       => aresetn,
                  s_axis_tvalid => s_axis_tvalid,
                  s_axis_tready => s_axis_tready,
                  s_axis_tdata  => s_axis_tdata,
                  m_axis_tvalid => m_axis_tvalid,
                  m_axis_tdata  => m_axis_tdata,
                  m_axis_tuser  => m_axis_tuser);

      aclk <= not aclk after clk_period / 2 when not done else '0';

      stimulus : process
        variable li, lo     : line;
        variable fin, fout  : file_open_status;
        -- one variable per CSV column: inputs file (cycle, aclk, aresetn, s_axis_tvalid, s_axis_tdata)
        variable v_cycle, v_aclk, v_aresetn, v_s_axis_tvalid : integer;
        variable v_s_axis_tdata : std_logic_vector(23 downto 0);
        -- outputs file (cycle, s_axis_tready, m_axis_tvalid, m_axis_tdata, m_axis_tuser)
        variable v_cycle_out, v_s_axis_tready, v_m_axis_tvalid : integer;
        variable v_m_axis_tdata : std_logic_vector(23 downto 0);
        variable v_m_axis_tuser : std_logic_vector(7 downto 0);
        -- the expected beats: one array per payload column, filled in pass 1
        type tdata_array is array (0 to 1023) of std_logic_vector(23 downto 0);
        type tuser_array is array (0 to 1023) of std_logic_vector(7 downto 0);
        variable exp_tdata : tdata_array;
        variable exp_tuser : tuser_array;
        variable n_exp, k_exp : integer := 0;
        -- the DUT's outputs copied AT the edge (their before-the-edge values)
        variable dut_s_axis_tready, dut_m_axis_tvalid : std_logic;
        variable dut_m_axis_tdata : std_logic_vector(23 downto 0);
        variable dut_m_axis_tuser : std_logic_vector(7 downto 0);
        variable hold_count : integer := 0;
        variable errors     : integer := 0;

        procedure commas_to_blanks(l : inout line) is
        begin
          for i in l.all'range loop
            if l.all(i) = ',' or l.all(i) = CR then
              l.all(i) := ' ';
            end if;
          end loop;
        end procedure;

        procedure read_wide(l : inout line; v : out std_logic_vector) is
          variable c   : character;
          variable ok  : boolean;
          variable acc : unsigned(v'length - 1 downto 0) := (others => '0');
        begin
          loop
            read(l, c, ok);
            exit when not ok or c /= ' ';
          end loop;
          while ok and c >= '0' and c <= '9' loop
            acc := resize(acc * 10, acc'length) + to_unsigned(character'pos(c) - character'pos('0'), acc'length);
            read(l, c, ok);
          end loop;
          v := std_logic_vector(acc);
        end procedure;

        -- ONE rising edge: copy the DUT's outputs at the edge, then check the beat
        -- consumed at this edge (if any) against the next expected beat.
        -- Every edge of the run goes through here, so no beat is missed.
        procedure next_edge is
        begin
          wait until rising_edge(aclk);
          dut_s_axis_tready := s_axis_tready;
          dut_m_axis_tvalid := m_axis_tvalid;
          dut_m_axis_tdata  := m_axis_tdata;
          dut_m_axis_tuser  := m_axis_tuser;
          wait for 1 ns;
          if dut_m_axis_tvalid = '1' then                -- no m_axis_tready port: every such edge is a beat
            if k_exp >= n_exp then
              if errors < 20 then
                report "cycle " & integer'image(v_cycle) & ": unexpected output beat" severity error;
              end if;
              errors := errors + 1;
            else
              if dut_m_axis_tdata /= exp_tdata(k_exp) then
                if errors < 20 then
                  report "beat " & integer'image(k_exp) & " (input cycle " & integer'image(v_cycle)
                    & "): m_axis_tdata expected " & integer'image(to_integer(unsigned(exp_tdata(k_exp))))
                    & " got " & integer'image(to_integer(unsigned(dut_m_axis_tdata))) severity error;
                end if;
                errors := errors + 1;
              end if;
              if dut_m_axis_tuser /= exp_tuser(k_exp) then
                if errors < 20 then
                  report "beat " & integer'image(k_exp) & " (input cycle " & integer'image(v_cycle)
                    & "): m_axis_tuser expected " & integer'image(to_integer(unsigned(exp_tuser(k_exp))))
                    & " got " & integer'image(to_integer(unsigned(dut_m_axis_tuser))) severity error;
                end if;
                errors := errors + 1;
              end if;
            end if;
            k_exp := k_exp + 1;
          end if;
        end procedure;

      begin
        file_open(fin,  in_file,  "test_vectors_in.csv",  read_mode);
        file_open(fout, out_file, "test_vectors_out.csv", read_mode);
        if fin /= open_ok or fout /= open_ok then
          report "cannot open test_vectors_in.csv / test_vectors_out.csv" severity error;
          errors := errors + 1;
        else
          -- pass 1: read EVERY outputs row, every column; the rows with
          -- m_axis_tvalid = 1 are the expected beats, in order
          readline(out_file, lo);                        -- header
          while not endfile(out_file) loop
            readline(out_file, lo); commas_to_blanks(lo);
            read(lo, v_cycle_out);
            read(lo, v_s_axis_tready);
            read(lo, v_m_axis_tvalid);
            read_wide(lo, v_m_axis_tdata);
            read_wide(lo, v_m_axis_tuser);
            if v_m_axis_tvalid = 1 and n_exp < 1024 then
              exp_tdata(n_exp) := v_m_axis_tdata;
              exp_tuser(n_exp) := v_m_axis_tuser;
              n_exp := n_exp + 1;
            end if;
          end loop;
          file_close(out_file);

          -- pass 2: replay the inputs rows; a beat row is held until the DUT
          -- accepts it, an idle row lasts exactly one edge
          readline(in_file, li);                         -- header
          while not endfile(in_file) loop
            readline(in_file, li); commas_to_blanks(li);
            read(li, v_cycle);
            read(li, v_aclk);                            -- read and ignored: the clock process makes the clock
            read(li, v_aresetn);
            read(li, v_s_axis_tvalid);
            read_wide(li, v_s_axis_tdata);
            if v_aresetn = 1 then aresetn <= '1'; else aresetn <= '0'; end if;
            if v_s_axis_tvalid = 1 then s_axis_tvalid <= '1'; else s_axis_tvalid <= '0'; end if;
            s_axis_tdata <= v_s_axis_tdata;
            hold_count := 0;
            loop
              next_edge;
              if v_s_axis_tvalid = 0 or dut_s_axis_tready = '1' then exit; end if;
              hold_count := hold_count + 1;
              if hold_count >= 64 then
                if errors < 20 then
                  report "cycle " & integer'image(v_cycle) & ": s_axis_tready never rose (deadlock)" severity error;
                end if;
                errors := errors + 1;
                exit;
              end if;
            end loop;
          end loop;
          file_close(in_file);

          -- drain: 16 more edges with no input beat, then count what never came
          s_axis_tvalid <= '0';
          for i in 1 to 16 loop
            next_edge;
          end loop;
          if k_exp < n_exp then
            report "missing output beats: expected " & integer'image(n_exp)
              & " got " & integer'image(k_exp) severity error;
            errors := errors + 1;
          end if;
        end if;

        report "mismatches: " & integer'image(errors) severity note;
        if errors = 0 then
          report "SIMULATION PASSED" severity note;
        else
          report "SIMULATION FAILED" severity note;
        end if;
        done <= true;
        wait;
      end process;
    end architecture sim;

WITH AN m_axis_tready PORT (an INPUT of the design, so a column of the
inputs file), change exactly these lines and nothing else:
  1. signals: add     signal m_axis_tready : std_logic := '0';
  2. port map: add    m_axis_tready => m_axis_tready,
  3. variables: add   variable v_m_axis_tready : integer;
                      variable dut_m_axis_tready : std_logic;
  4. inputs row: read its column at its place in the header (for
     aclk, aresetn, s_axis_tvalid, s_axis_tdata, m_axis_tready that is
     after s_axis_tdata):   read(li, v_m_axis_tready);
     and drive it with the other inputs:
                      if v_m_axis_tready = 1 then m_axis_tready <= '1'; else m_axis_tready <= '0'; end if;
  5. next_edge: copy it with the other wires and use both in the guard:
                      dut_m_axis_tready := m_axis_tready;
                      if dut_m_axis_tvalid = '1' and dut_m_axis_tready = '1' then
  6. drain: add       m_axis_tready <= '1';   next to   s_axis_tvalid <= '0';

WITHOUT AN s_axis_tready PORT: delete its signal, its port-map line, its
v_ and dut_ variables and its read from the outputs row (that row then has
one column fewer), and replace the whole hold loop (from `hold_count := 0;`
to its `end loop;`) with a single `next_edge;`: every inputs row then lasts
exactly one edge.

PAYLOAD COLUMNS: the example has two (m_axis_tdata, m_axis_tuser). For a
design without m_axis_tuser delete its signal, port-map line, variables,
array type, array, read, pass-1 store and next_edge compare. For an extra
one (m_axis_tlast, ...) add the same things with its own exp_ array.

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
[ ] Only the first 20 mismatches are printed (`if errors < 20 then report`);
    the total is printed once before the marker.
[ ] Spec has s_axis_tvalid and m_axis_tvalid: beats are compared in order;
    no compare of m_axis_tvalid or s_axis_tready against the CSV, no
    "missing/unexpected m_axis_tvalid" report; every payload column
    (m_axis_tdata, m_axis_tuser, ...) has its own expected array;
    m_axis_tready appears only if the spec has that port.
[ ] Spec describes a closed loop (TED / NCO / loop filter / PLL / AGC /
    error term fed back): payload fields are decoded and compared with
    TOL_SYM / TOL_MU, never `/=` on the packed word; the beat count may
    differ by 2 %.

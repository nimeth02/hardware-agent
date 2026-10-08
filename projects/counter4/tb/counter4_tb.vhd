-- projects/counter4/tb/counter4_tb.vhd -- example self-checking testbench
-- (VHDL-2008) driven by the golden test vectors in test_vectors.csv.
--
-- Each CSV row is one clock cycle: cycle,rst,en,count. The inputs of a row
-- are applied before that cycle's rising edge and "count" is the expected
-- output right after it. xsim runs with work_proj/ as its working
-- directory, so the simulation agent copies the CSV there before running.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;

entity counter4_tb is
end entity counter4_tb;

architecture sim of counter4_tb is
  constant clk_period : time := 10 ns;      -- 100 MHz

  signal clk   : std_logic := '0';
  signal rst   : std_logic := '0';
  signal en    : std_logic := '0';
  signal count : std_logic_vector(3 downto 0);
  signal done  : boolean := false;
begin
  dut: entity work.counter4
    port map (clk => clk, rst => rst, en => en, count => count);

  clk <= not clk after clk_period / 2 when not done else '0';

  stimulus: process
    file vectors      : text;
    variable l        : line;
    variable fstatus  : file_open_status;
    variable cycle_v  : integer;
    variable rst_v    : integer;
    variable en_v     : integer;
    variable exp_v    : integer;
    variable sep      : character;
    variable checks   : integer := 0;
    variable errors   : integer := 0;
  begin
    file_open(fstatus, vectors, "test_vectors.csv", read_mode);
    if fstatus /= open_ok then
      report "cannot open test_vectors.csv" severity error;
      report "SIMULATION FAILED" severity note;
      done <= true;
      wait;
    end if;

    readline(vectors, l);                    -- header: cycle,rst,en,count

    while not endfile(vectors) loop
      readline(vectors, l);
      if l'length = 0 then
        next;
      end if;
      read(l, cycle_v); read(l, sep);
      read(l, rst_v);   read(l, sep);
      read(l, en_v);    read(l, sep);
      read(l, exp_v);

      if rst_v = 1 then rst <= '1'; else rst <= '0'; end if;
      if en_v = 1  then en  <= '1'; else en  <= '0'; end if;

      wait until rising_edge(clk);
      wait for 1 ns;

      checks := checks + 1;
      if to_integer(unsigned(count)) /= exp_v then
        report "cycle " & integer'image(cycle_v) & ": count mismatch, expected "
          & integer'image(exp_v) & " got " & integer'image(to_integer(unsigned(count)))
          severity error;
        errors := errors + 1;
      end if;
    end loop;
    file_close(vectors);

    report integer'image(checks) & " vectors checked, " & integer'image(errors) & " mismatches" severity note;
    if errors = 0 then
      report "SIMULATION PASSED" severity note;
    else
      report "SIMULATION FAILED" severity note;
    end if;
    done <= true;
    wait;
  end process;
end architecture sim;

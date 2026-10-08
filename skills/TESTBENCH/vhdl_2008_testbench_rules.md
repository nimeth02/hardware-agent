# VHDL-2008 testbench rules: structure, DUT hookup, self-checking, run control

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
- Apply reset for at least two clock cycles with the polarity/type the
  spec states, then release it on a clock edge.
- Change inputs just after a rising edge (`wait until rising_edge(clk);
  wait for 1 ns;`) so they are stable before the next edge.

═══════════════════════════════════════════════════════════════════
SELF-CHECKING
═══════════════════════════════════════════════════════════════════

- Keep an error counter variable in the stimulus process.
- Every check is an `assert`/`if` that reports the mismatch with
  `severity error` (never `failure` — the run must continue to the summary)
  and increments the counter:
    if unsigned(count) /= to_unsigned(3, 4) then
      report "count mismatch: got " & integer'image(to_integer(unsigned(count)))
        severity error;
      errors := errors + 1;
    end if;
- Cover every item of the spec's "behavior" and "constraints": reset
  value, normal operation, enable low / hold, and the boundary cases the
  behaviour implies (wrap-around, full/empty, overflow).

═══════════════════════════════════════════════════════════════════
SIMULATOR LIMITS (xsim 2020.1)
═══════════════════════════════════════════════════════════════════

- Never use `to_string`, `to_hstring` or `to_ostring` on vectors: xvhdl
  accepts them but xelab 2020.1 stops with "The Vhdl 2008 to_string
  Operator is not supported yet for simulation". Print values as
  integers instead:
    integer'image(to_integer(unsigned(count)))
  and a single std_logic with std_logic'image(sig).

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

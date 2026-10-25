# Vivado RTL guidelines (from AMD UG949, "UltraFast Design Methodology")

Applies to every edition. These rules make the RTL fit the Xilinx chip
(xc7z020, Zynq-7000) so it meets the spec's clock when Vivado builds it.
Rules about declaration places, widths and `and`/`or` brackets are in
streaming_datapath_recipe.md; they are not repeated here.
Every chapter of UG949 is covered below; each rule says what the RTL must
do so that Vivado step succeeds.

═══════════════════════════════════════════════════════════════════
1. DESIGN HIERARCHY (UG949 ch.3, pp.41-45)
═══════════════════════════════════════════════════════════════════

- One entity per file; the top entity is named exactly as the spec says.
- Register the OUTPUTS of every module (and the inputs if the spec allows
  the latency). A path that stays inside one module is easy to make fast;
  a path that runs through two modules is not.
- Keep related logic in the same module: the multiply, its rounding and its
  saturation belong together, not spread over several entities.
- Keep the clock and reset inputs at the top entity and pass them DOWN into
  sub-modules; never create a clock inside a sub-module.
- Do not add synthesis attributes (KEEP_HIERARCHY, DONT_TOUCH, MARK_DEBUG,
  KEEP) unless the spec asks for them: they stop Vivado from optimizing.

═══════════════════════════════════════════════════════════════════
2. IP AND PRIMITIVES (UG949 ch.3, pp.45-49, 71-73)
═══════════════════════════════════════════════════════════════════

- INFER, do not instantiate: write `a * b`, `a + b`, arrays and `if` logic
  in plain VHDL and let Vivado pick the DSP, carry chain, RAM or LUT. Never
  instantiate Xilinx primitives (DSP48E1, RAMB36E1, BUFG, MMCME2_ADV, ...)
  or IP cores unless the spec names one.
- No `library unisim`, no `component` from Vivado IP, no `.xci` files.

═══════════════════════════════════════════════════════════════════
3. RESET (UG949 ch.3, pp.50-54)
═══════════════════════════════════════════════════════════════════

- Use a SYNCHRONOUS reset: test it inside `if rising_edge(clk) then`, never
  in the sensitivity list. A reset that acts on its own (asynchronous) stops
  Vivado from using the built-in multiplier (DSP) registers and costs extra
  logic and speed. Only use an asynchronous reset if the spec says so.
- Reset only what must be reset: the valid bits and the state. Do NOT reset
  data registers (products, sums, results); they are overwritten by the next
  valid input. Fewer resets = faster and smaller.
- Write the reset first, then the enable:
      if rst = '1' then ... elsif en = '1' then ... end if;
- Put reset-free data registers in their OWN clocked process, separate from
  the process that has the reset. Mixing them forces the reset onto the data.
- Never code a set AND a reset on the same register, and never use both the
  high and the low version of the same reset signal in different places.
- The spec's reset polarity and type win over these defaults.

═══════════════════════════════════════════════════════════════════
4. ENABLES AND CONTROL SETS (UG949 ch.3, pp.49-50, 55-60; ch.6, pp.264-266)
═══════════════════════════════════════════════════════════════════

- An `if` with no `else` inside a clocked process creates a clock enable.
  That is fine for a stalled pipeline (data held when m_axis_tready = '0').
  For values that are "don't care" when not valid, assign a constant in an
  `else` instead; it avoids a clock enable.
- Use ONE enable signal (`advance`) and ONE reset for the whole pipeline,
  not a different enable or reset per stage. Each different combination of
  clock + enable + reset ("control set") forces registers apart and slows
  the chip. Target: as few control sets as possible.
- Keep enable and reset polarity consistent across the design.
- Do not gate or divide the clock in RTL. Never write `clk2 <= clk and en`
  or a counter-made clock. Use a clock enable and keep one clock.

═══════════════════════════════════════════════════════════════════
5. MULTIPLY, ADD, SATURATE (UG949 ch.3, pp.61, 68-69, 76-79; ch.6, 237-239)
═══════════════════════════════════════════════════════════════════

- A multiply is done by the built-in DSP block. Give it 3 register stages
  for best speed: input register, the product register, output register.
  A multiply with no register around it is the usual reason a design is slow.
- Use `signed` values for arithmetic that goes into a multiplier; the DSP
  is signed inside. Mixed signed/unsigned loses precision or speed.
- An adder wider than a few bits is one carry chain: fast enough for one add
  per stage. Two chained adds in one stage are slow; put a register between.
- A 3-input add (a + b + c) is as cheap as a 2-input add and may stay in one
  stage; more than 3 values: add in a tree over several stages.
- Rounding and shifting: do them in a stage AFTER the product register.
- Saturation (compare + clamp) is a comparison plus a choice (mux): its own
  stage, after the sum is registered.
- A 4-way `case`/mux is one logic level, an 8-way is still one, a 16-way is
  one; a 32-way or bigger mux is two or more levels: split it over stages.
- Logic levels per stage: at 100 MHz up to ~12, at 125-250 MHz up to ~6,
  above 250 MHz 2-3. A LUT, a mux and a carry chain each count as a level.
  If a stage has more, add a register stage; latency is cheaper than a
  failed clock.
- Add pipeline registers on the CONTROL bits (valid, handshake) in step
  with the data, so data stays aligned. One bit per stage is cheap.

═══════════════════════════════════════════════════════════════════
6. MEMORIES, TABLES AND SHIFT REGISTERS (UG949 ch.3, pp.62-71, 79, 82-87)
═══════════════════════════════════════════════════════════════════

- A table up to 64 entries becomes LUT memory by itself; write it as a
  constant array and read it in a clocked process.
- A memory with 256 or more entries becomes block RAM. Write it as a signal
  array written and read inside ONE clocked process, with NO reset on the
  array and a registered output. Reset only the output register, and only
  with a synchronous reset.
- Put a register directly after every memory read before any arithmetic.
- Do not read the memory output into two different places (fan-out must be
  1 from the array to the output register) or the RAM's own output register
  cannot be used.
- A shift register / delay line (`d(i) <= d(i-1)`) without reset and
  without enable on its middle stages becomes small SRL logic; a reset on a
  middle stage breaks that. Put the reset only on the first or last stage.
- Do not use the memory's address register for anything else (no feedback
  from the address register into an adder that also feeds the RAM).

═══════════════════════════════════════════════════════════════════
7. STARTUP VALUES (UG949 ch.3, p.71)
═══════════════════════════════════════════════════════════════════

- Give every register an initial value in the declaration:
      signal valid_pipe : std_logic_vector(3 downto 0) := (others => '0');
  The chip loads it at power-up, so a reset is not needed just to start.
  The spec's reset still must clear the valid bits when it is asserted.

═══════════════════════════════════════════════════════════════════
8. FAN-OUT (UG949 ch.3, pp.73-75; ch.6, pp.267-270)
═══════════════════════════════════════════════════════════════════

- A signal that drives more than a few hundred inputs (typically a stall
  or enable that drives every stage) is slow. Keep the number of stages that
  use one enable small, or copy the enable register once per group of stages
  (name the copies `advance_a`, `advance_b`).
- Drive high fan-out signals from a plain register (FD), not from a LUT or
  a memory output, so Vivado can copy it.
- Do not add `max_fanout` attributes yourself; the tool copies registers
  when needed.

═══════════════════════════════════════════════════════════════════
9. CLOCKING (UG949 ch.3, pp.88-141)
═══════════════════════════════════════════════════════════════════

- Use the spec's clock port directly as the only clock. Vivado adds the
  clock buffer (BUFG) itself; never instantiate BUFG, BUFR, BUFH, MMCM or
  PLL in generated RTL.
- No logic on the clock: a LUT, `and`, `or`, `not` or mux in the clock path
  makes the clock skew unpredictable. `not clk` for a falling edge is only
  allowed if the spec asks for falling-edge behavior.
- One clock per design unless the spec lists more. A slower rate inside the
  design is made with a counter-driven clock ENABLE, never a derived clock.
- Never run a clock "down and back up" through sub-modules; clocks flow
  from the top entity down only.

═══════════════════════════════════════════════════════════════════
10. CLOCK DOMAIN CROSSING (UG949 ch.3, pp.141-145)
═══════════════════════════════════════════════════════════════════

Only when the spec has two or more clocks (otherwise skip this section):
- A single control bit crossing to another clock goes through TWO registers
  in a row in the receiving clock, with the attribute
  `attribute ASYNC_REG of sync_reg : signal is "TRUE";` on both.
- A bus (several bits) never crosses as plain registers: use a handshake
  (valid in one clock, acknowledge back) or a gray-coded counter, or a FIFO.
- A reset that crosses clocks is synchronized like a single bit.
- Do not replicate the two synchronizer registers for fan-out; add a third
  register after them if needed.

═══════════════════════════════════════════════════════════════════
11. CONSTRAINTS (UG949 ch.4, pp.146-187)
═══════════════════════════════════════════════════════════════════

The system writes the clock constraint (create_clock on the spec's clock
port) for you. The RTL must make that single constraint enough:
- Do not create internal clocks (see §9); each one would need its own
  constraint.
- Every input port is registered once before use, every output port comes
  from a register (also in the design rules). Then no input/output delay
  constraints are needed.
- Do not write constraints, attributes or `-- synthesis` pragmas into the
  RTL to loosen timing (no false paths, no multicycle paths). If a path is
  slow, pipeline it.
- No power or physical (LOC, PBLOCK) constraints in RTL.

═══════════════════════════════════════════════════════════════════
12. SYNTHESIS (UG949 ch.5, pp.188-201)
═══════════════════════════════════════════════════════════════════

- Write RTL that synthesis can optimize: no KEEP/DONT_TOUCH/MARK_DEBUG, no
  MAX_FANOUT, no RAM_STYLE/ROM_STYLE/USE_DSP/SHREG_EXTRACT attributes unless
  the spec asks. The defaults choose well.
- Let Vivado retime: keep the pipeline as a chain of registers between
  small blocks of logic, so registers can be moved to balance stages.
- Avoid single lonely CARRY cells: a 2-bit add or a `<` on 3 bits is better
  as plain logic; write it as a comparison, not an arithmetic expression,
  when the width is tiny.
- Avoid `if / elsif / elsif` chains where a `case` would do; a priority
  chain is slower than a parallel choice.
- Avoid very wide muxes built with MUXF7/MUXF8 in congested designs; keep
  muxes 16-way or smaller per stage.
- A design that is over 250 lines is usually repeating itself; use a
  function (see the recipe).

═══════════════════════════════════════════════════════════════════
13. IMPLEMENTATION AND TIMING CLOSURE (UG949 ch.5-6, pp.201-298)
═══════════════════════════════════════════════════════════════════

Vivado places and routes the design, then reports the worst negative slack
(WNS). The RTL controls these outcomes:
- WNS < 0 with high "logic delay": too many levels per stage (§5): add
  registers.
- WNS < 0 with high "net delay" or congestion: too much logic driven by one
  signal (§8), too many control sets (§4), or wide muxes (§12).
- Hold violations after routing: usually a clock made in logic (§9) or an
  unsynchronized crossing (§10).
- When the orchestrator gives you a timing failure as feedback, add a
  pipeline stage to the stated path and keep the outputs identical; do not
  loosen anything else.
- Utilization: the xc7z020 has 220 DSPs, 140 block RAMs, 53k LUTs. A design
  that needs more will not fit; count DSPs (one per multiply) before writing.

═══════════════════════════════════════════════════════════════════
14. POWER (UG949 ch.3, pp.81-87; ch.6, pp.299-305)
═══════════════════════════════════════════════════════════════════

- Hold data registers still when there is no valid input (use the pipeline
  enable); registers that only toggle on valid data use less power.
- Prefer `case` over `if`-chains for parallel choices (less glitching).
- For big memories, write them so only one block is active per read (deep
  and narrow arrays, read with the full address in one process).
- Do not add extra pipeline stages beyond what timing needs; every register
  costs power.

═══════════════════════════════════════════════════════════════════
15. DRC, CONFIGURATION AND DEBUG (UG949 ch.6, pp.305-315)
═══════════════════════════════════════════════════════════════════

- No latches: every signal assigned in a combinational process needs a value
  in every `if` / `case` branch (`when others =>`, final `else`).
- No combinational loops (a signal that depends on itself with no register).
- No multi-driven signals: one process per signal.
- No `wait for`, `after`, `report`, `assert` or file I/O in RTL: they are
  for test programs only and cannot be built into a chip.
- No `inout` or tri-state signals ('Z') unless the spec has an inout port.
- No debug cores, no MARK_DEBUG, no ILA/VIO instances in generated RTL.
- No division with `/` between two signals; use shifts (see the recipe).

═══════════════════════════════════════════════════════════════════
16. CHECK BEFORE ANSWERING
═══════════════════════════════════════════════════════════════════

[ ] one clock, straight from the port; no logic on the clock
[ ] reset is synchronous and covers only valid bits and state
[ ] one enable (`advance`) and one reset for the whole pipeline
[ ] every multiply result is registered; sum, round, saturate are separate stages
[ ] no stage chains more than one add after a multiply
[ ] module outputs are registered
[ ] every combinational `if`/`case` has a value in all branches
[ ] no primitives, IP, attributes, constraints or debug cores in the RTL
[ ] if two clocks: every crossing uses §10

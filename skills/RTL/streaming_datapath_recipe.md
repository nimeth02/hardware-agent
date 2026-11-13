# Streaming datapath recipe: how to lay out a fixed-latency arithmetic design

Applies to every edition (VHDL-93 and VHDL-2008). Load it with the design
rules. It exists because generated designs kept failing in the same ways:
very long files, declarations in the wrong place, products stored in
variables of the wrong width, and one signal written from several places.

═══════════════════════════════════════════════════════════════════
1. WHERE EACH THING IS DECLARED (compile errors "syntax error near
   'signal'", "'x' is not a variable", "'x' is not declared")
═══════════════════════════════════════════════════════════════════

- `signal` and `constant`: ONLY between `architecture ... is` and the
  architecture's `begin`. Never inside a process, never after `begin`.
- `variable`: ONLY between `process(...)` and that process's `begin`.
- Variables are assigned with `:=`, signals with `<=`. A name declared as
  a signal can never be assigned with `:=`, and vice versa.
- A sensitivity list holds plain signal names only (no expressions, no
  variables).
- `function`, `procedure`, `type` and `subtype`: ONLY between
  `architecture ... is` and the architecture's `begin`, BEFORE the signals
  that use them. A function written after `begin` gives "syntax error near
  'function'" and then every call fails with "'name' is not declared".
- Mixing `and` with `or` (or `xor`) in one expression needs brackets:
  `advance <= (s_axis_tvalid and m_axis_tready) or (not valid_pipe(3));`
  Without them VHDL gives "syntax error near 'or'".
- Record aggregates like `s1 <= (valid => '0', i => (others => '0'))` only
  work when the record `type` is declared (before `begin`) and the signal has
  that type. Simplest: do NOT use records for pipeline stages; use one plain
  signal per field (`valid_s1`, `i_s1`, `q_s1`, ...).

═══════════════════════════════════════════════════════════════════
2. KEEP THE FILE SMALL: functions, not copy-paste
═══════════════════════════════════════════════════════════════════

If the same arithmetic is needed for 4 or 8 values (I and Q, four
constellation points, four output bits), write it ONCE as a function in the
architecture's declarative part and call it, e.g.:

    function sat6(x : signed) return signed is   -- clamp to -32..31
    begin
      if x > 31 then return to_signed(31, 6);
      elsif x < -32 then return to_signed(-32, 6);
      else return resize(x, 6);
      end if;
    end function;

A function input that the function MULTIPLIES must carry its width in the
header, written with the width name of the value the calls pass:

    function farrow(xm1, xm, xp1, xp2 : signed(I_W-1 downto 0);
                    mu : signed(MU_W-1 downto 0)) return signed is

A plain `mu : signed` is as wide as whatever each call passes, so nobody
reading the function can size `v2 * mu`; the wrong product width compiles
and only crashes the dry run ("Array sizes do not match ... in process
p_s7", no line). Inputs that are only clamped or resized (sat6 above) may
stay unsized.

A design that needs more than about 250 lines is almost always repeating
itself; shorten it before adding more.

═══════════════════════════════════════════════════════════════════
3. ONE SHORT CLOCKED PROCESS PER PIPELINE STAGE
═══════════════════════════════════════════════════════════════════

Write ONE clocked process per row of the stage map (pipeline_stage_map.md),
labelled after the row (`p_s1`, `p_s2`, ...), plus ONE small process
`p_valid` for the valid chain. Keep every process under about 40 lines
(one screen). A 200-line process is where variables get declared after
`begin`, a stage reads two rows back and the valid chain is miscounted
(timing_recovery, 2026-11-02: 8 misplaced declarations in one process).
If a row needs more than 40 lines, move its arithmetic into a function
(section 2) or split it into two stages (section 3b).

    p_valid : process(clk)             -- has the reset (so do p_loop and a history
    begin                              -- chain, nothing else); the ONLY writer of valid_pipe
      if rising_edge(clk) then
        if rst = '1' then                -- use the spec's reset polarity
          valid_pipe <= (others => '0');
        elsif advance = '1' then
          valid_pipe(0) <= s_axis_tvalid;    -- one line per stage-map row
          valid_pipe(1) <= valid_pipe(0);
          ...
        end if;
      end if;
    end process p_valid;

    p_s1 : process(clk)                -- row s1: writes ONLY *_s1 names,
    begin                              -- reads only the entity inputs
      if rising_edge(clk) then
        if advance = '1' then          -- no reset on data registers
          a_s1  <= signed(s_axis_tdata(11 downto 0));
          c_s1  <= signed(s_axis_tdata(23 downto 12));
          ns_s1 <= ns;
        end if;
      end if;
    end process p_s1;

    p_s2 : process(clk)                -- row s2: reads *_s1 (and its own row in a history chain), writes *_s2
                                       -- (strobe_s2 / mu_s2 are the exception: p_loop writes them, not p_s2)
      variable sum_v : signed(12 downto 0);   -- variables go HERE, before begin
    begin
      if rising_edge(clk) then
        if advance = '1' then
          sum_v := resize(a_s1, 13) + resize(c_s1, 13);   -- 12 -> 13 bits
          b_s2  <= sum_v;                                  -- 13 bits
          ns_s2 <= ns_s1;                                  -- carried along
        end if;
      end if;
    end process p_s2;

- `p_s<K>` writes only registers named `_s<K>` and reads only `_s<K-1>`
  names (for K = 1, the entity inputs) plus, where needed, the loop values
  of `p_loop`. In a tracking loop `strobe_s2` and `mu_s2` are the one
  exception: `p_loop` writes them, `p_s2` does not. Every signal then has
  exactly one writer by construction, and a stage can only read the row
  above it.
- No stage process touches `valid_pipe`, and data registers have no reset:
  they are simply overwritten by the next valid beat
  (vivado_rtl_guidelines.md section 3). The exception is a tracking loop
  (timing recovery, phase or gain control; pipeline_stage_map.md, LOOP
  VALUES): ONE process `p_loop` holds the loop values (`eta_r`, `acc_r`,
  `v_r`, `yprev_i_r`: no stage number), resets them, updates eta every
  input sample, and also writes `strobe_s2` and `mu_s2`; the history chain
  it reads (`x0_i_s2` ..) is reset too. Never copy loop values round the
  rows back into row 1 (`eta_s1 <= eta_s11`): that runs 11 separate loops.
  Only `p_valid` writes `valid_pipe`; two processes writing it (one the
  reset, one a bit) give an 'X' valid flag that no checker reports.
- Label every process (`p_valid`, `p_s1`, ...): the linter names the process
  in its messages.

- Stage N reads ONLY stage N-1 registers. Never let stage 3 read `a_s1`
  directly (skipping `b_s2`): the data then arrives at the output one
  cycle BEFORE `valid_pipe(last)`, and when the valid flag finally rises
  the output register already holds the next item (seen as "expected X
  got 0" and "got X" one beat later). If a stage has nothing to compute,
  still copy the data through it (`b_s2 <= a_s1`).
- Latency = number of stages. Count them against the spec's limit, but
  never merge stages to save latency if that breaks the timing rules in
  section 3b.
- `valid_pipe` is a std_logic_vector, one bit per stage, shifted every
  advance inside `p_valid` and nowhere else.
- EVERY value a stage uses comes from the PREVIOUS stage's registers —
  including side inputs such as a noise/scale value `ns`, a mode or an
  index that enters with the beat. Carry them along: `ns_s1 <= ns;
  ns_s2 <= ns_s1; ...` and multiply in stage 4 with `ns_s3`, never with
  `ns_s1` or the raw input. Mixing a fresh side input with an older
  symbol's numbers gives wrong results whenever two consecutive beats
  differ, and zeros after a gap.
- The valid chain and the data chain MUST be the same length: the number
  of `valid_pipe` bits equals the number of registers a data value really
  passes through from input to `m_axis_tdata`. A register that nothing
  reads does not count — delete it. If valid is one stage longer than
  data, every output beat shows the NEXT beat's data and the first beat
  is lost; one stage shorter and the testbench sees stale data.
- AN OUTPUT REGISTER IS A STAGE. `m_axis_tdata_reg <= ... b3_s6 ...` inside
  a clocked process is a 7th register after stages 1-6 (row s7 of the map,
  its own process), so it needs a 7th valid bit (`valid_pipe(6) <=
  valid_pipe(5);` in `p_valid`) and
  `m_axis_tvalid_i <= valid_pipe(6)`. With only valid_pipe(5 downto 0) the
  valid flag rises one clock BEFORE the register is written and every beat
  is read as 0 (linter: `valid_data_length_mismatch`). Simplest and
  preferred: NO separate output register — join the last stage's registers
  with a plain assignment outside any process:
      m_axis_tdata <= std_logic_vector(b3_s6 & b2_s6 & b1_s6 & b0_s6);
  Then valid_pipe has exactly as many bits as there are stages.
- COUNT BEFORE YOU RETURN: list every register one input bit passes through
  on its way to `m_axis_tdata` (i_s1, abs_i_s2, ..., b0_s6[, m_axis_tdata_reg]).
  That count is the ONLY correct length of valid_pipe, and the output valid
  flag is its LAST bit. The dry run checks this: if m_axis_tvalid is high
  while m_axis_tdata is 0 on every beat, the design is sent back
  (`smoke_output_stuck_at_zero`).
- Handshake (stall-safe, no data loss):
    advance       <= m_tready or not valid_pipe(last);  -- output free or empty
    s_tready_i    <= advance;                           -- accept only when advancing
    m_tvalid_i    <= valid_pipe(last);
  An input is accepted when `s_tvalid = '1' and advance = '1'`; when
  `advance = '0'` every register holds its value. On a strobe design use
  the output flag itself, `advance <= m_tready or not m_tvalid_i;`
  (m_tvalid_i = valid_pipe(last) and strobe_s<last>): with the bare valid
  bit a non-strobe row would stall the whole pipeline while m_tready is
  low, and a sink that waits for tvalid before raising tready would never
  see a beat.
- NO DOWNSTREAM READY PORT (the spec says "No m_axis_tready port exists" /
  "downstream is always ready"): the pipeline must never wait, so it moves
  on EVERY clock:
      advance <= '1';
  `valid_pipe(0) <= s_axis_tvalid;` stays as it is: a clock with no input
  loads a '0', that '0' travels down the chain, and m_axis_tvalid drops
  when no result is left. Never `advance <= s_axis_tvalid;`: the chain then
  moves only while an input arrives, so after the last input the results
  still inside never come out and valid_pipe(last) stays '1' with the same
  data on every clock (equalizer 2026-11-12 16:57: 20 inputs, 108 output
  beats, the last 3 results missing; with `advance <= '1';` the same file
  gave 0 mismatches; dry run: `smoke_more_outputs_than_inputs`).
  Because the rows now move on clocks with no input too, every register
  that REMEMBERS across samples (a history chain, the loop values of
  `p_loop`, an accumulator) is written only under the valid bit of the row
  above it, `elsif advance = '1' and valid_pipe(0) = '1' then`
  (pipeline_stage_map.md). Plain stage registers need no such gate.
  Never `advance <= not valid_pipe(last);` or `not m_tvalid_i`: without the
  ready term nothing ever clears the last valid bit, so at the first clock
  it is '1' advance drops to '0' and every register, the valid chain and the
  output flag freeze for the rest of the run, m_axis_tvalid stuck at '1' with
  one constant value on every cycle (timing_recovery 2026-11-06 18:52, 801
  mismatches, three attempts in a row; linter: `self_blocking_advance`).
- OUTPUT ON STROBE (timing recovery, symbol sync, any spec that says
  "output ... on strobe" or "one output per N inputs"): the output valid
  flag is the last valid bit AND the strobe bit of the SAME row, never the
  bare valid bit:
      m_tvalid_i <= valid_pipe(9) and strobe_s10;   -- s10 = the output row, the last row
  The bare bit raises a beat for every input sample; the testbench expects
  one beat per strobe and sees about 4x too many (timing_recovery
  2026-11-02 18:32: 998 mismatches, every beat "got 0"; linter:
  `output_valid_without_strobe`). The strobe and mu are COMPUTED in
  `p_loop`, in the same update as eta (`if eta_next < 0 then strobe_s2 <=
  '1'; ...`), and carried down the rows to the output row like data
  (pipeline_stage_map.md, LOOP VALUES). Never send them round a ring
  (`strobe_s1 <= strobe_s11; strobe_s2 <= strobe_s1; ...`): copied only,
  they keep their reset value for ever (linter: `loop_value_never_computed`).
- Do not add extra "state machines" for plain pipelines.

═══════════════════════════════════════════════════════════════════
3b. TIMING: ONE SMALL STEP PER STAGE (spec.clock.frequency_mhz)
═══════════════════════════════════════════════════════════════════

The design is checked against the spec's clock (period = 1000 / MHz ns;
100 MHz = 10 ns). Between two registers there is only that much time, so
one stage may hold only ONE of these:

- one multiply (register its full-width product, nothing else),
- one add / subtract (or a short sum of 2-3 values),
- one shift + rounding constant,
- one saturation (compare + clamp),
- one choice between values (mux).

Rules:
- Register the product of EVERY multiply before using it again.
- Never put a multiply and an add, or an add and a saturation, in the same
  stage. A path of multiply -> add -> add -> saturate in one stage can take
  50 ns and fails a 10 ns clock.
- Saturation is always its own stage, after the sum is registered.
- Sum of more than 3 values: add them as a tree over several stages.
- If the spec's latency limit is too small for this, use the smallest
  number of stages that still follows these rules and say so in your notes.
  Timing correctness matters more than saving one stage.

═══════════════════════════════════════════════════════════════════
4. WRITE THE BIT WIDTH NEXT TO EVERY ARITHMETIC LINE
═══════════════════════════════════════════════════════════════════

Before writing a line, write its widths in a comment, then declare the
target with exactly that width.

- `a + b`, `a - b`: resize both to the target width first (the result has
  the width of the wider operand, so overflow wraps silently).
- `a * b` (signed*signed or unsigned*unsigned only; never mix): width is
  width(a) + width(b). 12 bits * 8 bits = 20 bits. A 32-bit by 32-bit
  product is 64 bits.
- THE WIDTHS OF A MULTIPLY ARE NAMES, NEVER TYPED-IN NUMBERS. Give every
  value that goes into a multiply a width constant, declare it (and every
  stage copy of it) with that name, and declare the product as the SUM of
  the names. Never add the two widths in your head and type the total.
  Declare the product at that full width, then trim it in a separate step:
      -- architecture, before begin:
      constant A_W : integer := 12;                    -- width of a
      constant B_W : integer := 8;                     -- width of b
      signal a_s3  : signed(A_W-1 downto 0);
      signal b_s3  : signed(B_W-1 downto 0);
      -- inside the stage-4 process:
      variable prod : signed(A_W+B_W-1 downto 0);      -- always A_W + B_W
      prod  := a_s3 * b_s3;
      small := resize(prod, 16);                       -- trim as a separate step
  To make a value wider later, change its constant (`A_W := 13`), never
  the declaration lines: every stage copy and every product then follow by
  themselves. A typed-in product width goes stale: timing_recovery
  2026-11-03 12:30 widened `v2i_s3` from 12 to 13 bits and kept
  `variable p2i : signed(27 downto 0)`, so 13 + 16 = 29 bits went into 28
  (linter: `multiplier_width`; the dry run stops with "Array sizes do not
  match"). The linter reads these names, so it still checks every product.
  Do NOT widen the inputs to the target width with resize() and then
  multiply: that makes the product twice as wide as the target.
- Shifts, powers of two and division (VHDL-93 + numeric_std):
    shift right (keeps sign for signed):  shift_right(x, 13)
    shift left:                           shift_left(x, 13)
  `sll`, `srl`, `sra` do NOT work on signed/unsigned; use the functions.
  Multiplying or dividing by a power of two (2, 4, ... 8192) is a shift,
  never a multiplier or divider. Never write `x * to_signed(8192, N)`,
  `x * 4`, `x / 8192` or `x sra 13`, and never use `/` between two signals.
  (The linter rejects a power-of-two multiply: `power_of_two_multiply`.
  Besides being slow, `x * to_signed(4, 16)` is 32 bits wide and crashes a
  16-bit target with "Array sizes do not match".) Inside a FUNCTION the
  same rules hold: `res := shift_left(resize(x, 16), 2);`, never
  `res := resize(x, 16) * to_signed(4, 16);`.
  "Round half up then divide by 8192" is THREE steps, each at a written
  width (the linter rejects the one-line version as `assignment_width`):
      signal sum : signed(24 downto 0);        -- same width as the product p
      signal sh  : signed(24 downto 0);        -- shift_right keeps 25 bits
      signal res : signed(15 downto 0);        -- the narrow target
      sum <= p + to_signed(4096, p'length);    -- 25 bits, constant sized like p
      sh  <= shift_right(sum, 13);             -- still 25 bits
      res <= resize(sh, 16);                   -- 25 -> 16 bits, separate step
  WRONG (25 bits into 16, `assignment_width`):
      res <= shift_right(p + to_signed(4096, 25), 13);
  If the maths seems to need a real division by a changing value, stop and
  re-read the spec: it is almost always a multiplication or a shift.
- `shift_right(x, n)` does NOT make x narrower: a 25-bit x stays 25 bits
  (the top bits are copies of the sign). Store it in a 25-bit signal or
  variable, then `resize(..., 16)` on its own line; never assign
  `shift_right(...)` of a wide value straight to a narrower signal.
- `shift_left(x, n)` does NOT make x wider: a 12-bit x stays 12 bits and the
  top bits are lost. To multiply by 4 or 8 into a wider target, widen FIRST,
  then shift:
      variable x16 : signed(15 downto 0);               -- target width
      x16     := resize(x_v, 16);                       -- 12 -> 16 bits
      m_msb_v := shift_left(x16, 2);                    -- 4*x, 16 bits
  WRONG (crashes with "left array has 16 elements, right array has 12"):
      m_msb_v := shift_left(x_v, 2);                    -- x_v is only 12 bits
  The same goes for `signed(abs_v)` of an unsigned value: it needs one extra
  top bit (`signed('0' & abs_v)`) before it is used as a signed number.
- THE LAST HELPER BEFORE A REGISTER HAS THE REGISTER'S WIDTH NAME. When a
  register is declared with a constant (`signal v_s12 : signed(V_BITS-1
  downto 0)`), the variable that feeds it is declared with the SAME name
  and resized to it, never to a typed-in number:
      variable v_next : signed(V_BITS-1 downto 0);
      v_next := resize(v_sum, V_BITS);
      v_s12  <= v_next;
  `v_next : signed(16 downto 0)` ("17 bits, final v") into the 16-bit
  `v_s12` cost two crash rounds (timing_recovery 2026-11-03 11:17). The
  linter reads the constants and reports every such line at once
  (`assignment_width`); fix them all in one round.
- Both sides of every `<=` / `:=` must have the SAME width, or the dry run
  stops with "Array sizes do not match, left array has 24 elements, right
  array has 33 elements" (the compiler does not catch it). Typical causes:
  * Joining pieces with `&`: the width is the SUM of the pieces. A 24-bit
    output built from four values must join four values of exactly 6 bits:
      m_axis_tdata <= std_logic_vector(b3 & b2 & b1 & b0);   -- 6+6+6+6 = 24
    Make each piece exactly 6 bits first (`resize(x, 6)` after saturating),
    never join a wider product or sum.
  * Assigning a product or sum straight to a narrower or wider signal:
    use `resize(value, target'length)` and say the width in a comment.
  * Slicing the input wrongly: `s_axis_tdata(31 downto 20)` is 12 bits.
  * Negating into a wider signal: `-x` is as wide as `x`, so a 12-bit `-x`
    does not fit a 13-bit magnitude signal. Widen first, then negate:
    `neg_i <= -resize(i_s, 13);`.
- ABSOLUTE VALUE: `-x` is NOT |x|. It flips positive inputs negative too,
  and the linter rejects it (`abs_without_sign_check`). Use numeric_std's
  `abs` on the WIDENED value, one line, no if needed:
      abs_i_s2 <= unsigned(abs(resize(i_s1, 13)));   -- 12 -> 13 bits, |-2048| = 2048 fits
  If you prefer an if/else, the negation goes ONLY in the negative branch:
      if i_s1 < 0 then abs_i_s2 <= unsigned(-resize(i_s1, 13));
      else             abs_i_s2 <= unsigned(resize(i_s1, 13));  end if;
  Write the width of every piece in a comment next to the line.
- Mixed signedness: convert explicitly, e.g. a signed value times an
  unsigned one: `a * signed('0' & b)` (b gains one sign bit: 8 -> 9 bits).
  So when the spec says "signed 16 x unsigned 8 -> signed 24", the VHDL
  product is 16 + 9 = 25 bits, NOT 24. Declare the product signal with the
  real width, written with the width names (the extra sign bit is the
  `+ 1`), and narrow it afterwards; never store the product in the spec's
  width directly:
      constant M_W  : integer := 16;                       -- width of m (signed)
      constant NS_W : integer := 8;                        -- width of ns (unsigned)
      signal p_full : signed(M_W+NS_W downto 0);           -- M_W + (NS_W + 1) = 25 bits
      p_full <= m * signed('0' & ns);                      -- natural widths
      p      <= resize(p_full, 24);                        -- the spec's 24 bits
  (`p <= m * signed('0' & ns);` into a 24-bit p is the "multiplier_width"
  lint error; the lint tells the exact widths.)
- Shifts and rounding: `shift_right(x, n)` keeps the width; add the
  rounding constant BEFORE the shift, using a constant sized like x.
- Saturation: compare against constants, then `resize` (see the function
  above); do not rely on wrap-around.
- Fractions: every value that is a fraction (1.0 = 2**F) gets a named
  constant `<NAME>_F` for its fraction bits (`constant MU_F : integer :=
  16;` for mu, mu_s3, mu_v). A product with that value has F extra
  fraction bits: shift it back with the constant, `shift_right(v2 * mu,
  MU_F)`, BEFORE it is added to anything that was not multiplied by the
  same fraction. One shift at the very end is wrong when a plain value was
  added in between: `y = shift_right((shift_right(v2*mu, MU_F) + v1) * mu,
  MU_F) + v0` (linter: `fraction_bits_misaligned`, `product_added_unscaled`).
- Weighted sums from the spec (`v1 = -0.5*x[m+2] + 1.5*x[m+1] - 0.5*x[m]
  - 0.5*x[m-1]`): one term per sample, each with the spec's own sign and
  weight. 0.5*x is `shift_right(x, 1)`, 1.5*x is `x + shift_right(x, 1)`.
  When you keep twice the value and shift once at the end, 2*x is
  `shift_left(x, 1)` and 3*x is `shift_left(x, 1) + x`: `shift_left(x, 1)`
  alone for the 1.5 term gives 1.0*x after the final shift, the wrong
  weight (32 of 41 timing_recovery files up to 2026-11-11). The linter
  works every such sum out again and compares the weights with the spec
  line (`equation_coefficients_differ`).

═══════════════════════════════════════════════════════════════════
5. CHECK YOURSELF BEFORE ANSWERING
═══════════════════════════════════════════════════════════════════

[ ] every signal declared before `begin`, every variable inside its process
[ ] every function/type is declared before `begin`; no records for pipeline stages
[ ] every expression mixing `and`/`or` has brackets
[ ] each signal assigned in exactly one process / one concurrent line
[ ] one clocked process per stage-map row (`p_s1`, `p_s2`, ...) plus `p_valid`
    (and `p_loop` for a tracking loop), none longer than about 40 lines; only
    `p_valid` writes `valid_pipe`; only `p_valid`, `p_loop` and a history
    chain have a reset
[ ] tracking loop: eta, acc, v and y_prev are `_r` registers written only by
    `p_loop`; eta changes every input sample; strobe_s2 and mu_s2 come from
    `p_loop` and are carried down to the output row; no `_s1` register is
    copied from a later row (`eta_s1 <= eta_s11` is the 11-loops bug)
[ ] every `*` target is exactly width(a) + width(b), declared with the width
    names (`signed(A_W+B_W-1 downto 0)`), never with a typed-in number; every
    value that goes into a multiply is declared with its width name too
[ ] left and right side of every assignment (and every `&` join) have equal widths
[ ] stage count ≤ the spec's latency limit (without breaking section 3b)
[ ] every multiply result is registered; no stage mixes multiply/add/saturate
[ ] every side input (ns, mode, index, ...) is delayed stage by stage and used
    from the SAME stage as the data it is combined with
[ ] the stage map (pipeline_stage_map.md) is the first note, and every
    `x_sK <= ...` line reads only `_s(K-1)` names; nothing reads `_s(K-2)`
[ ] valid_pipe length = number of data registers on the path to m_axis_tdata,
    COUNTING any output register (m_axis_tdata_reg) as one more stage with
    its own valid bit; no register is written and never read
[ ] every absolute value uses `abs(resize(x, N))` or an if/else on the sign;
    never a bare `-x`
[ ] m_axis_tdata keeps its value while m_axis_tvalid = '1' and
    m_axis_tready = '0' (every register holds when advance = '0')
[ ] on a retry: the new code differs from the previous attempt where the
    reported failure is; returning the same code with a new note is a failure
[ ] no process lists in its sensitivity list a signal that only it writes

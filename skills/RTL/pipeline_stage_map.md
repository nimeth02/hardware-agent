# Pipeline stage map: write the table BEFORE the code (every edition)

This rule exists because of one repeated bug: a stage read a register from
TWO stages back (`i_mmsb_s3 <= calc_mmsb(i_s1)` while stage 2 existed), so
the value arrived one clock early, was multiplied by the previous row's
`ns`, and every output beat was 0. The linter reports it as
`pipeline_stage_skip`; this file makes sure you never write it.

═══════════════════════════════════════════════════════════════════
1. THE ONE RULE
═══════════════════════════════════════════════════════════════════

A register named `<name>_s<K>` may read ONLY registers named `<x>_s<K-1>`
(and, for K = 1, the entity inputs), with two exceptions: a history chain
reads its own row (`x1_s2 <= x0_s2`, see below), and any row reads the loop
values of `p_loop` (`eta_r`, `yprev_i_r`, ...: no stage number, see LOOP
VALUES below) directly; they are not rows. Never `_s<K-2>` or older, never
the raw input in a stage above 1, and never a LATER row: `eta_s1 <= eta_s11`
is the ring (linter: `loop_state_ring`).

If a value is needed later but stage K has nothing to compute for it,
stage K still copies it: `i_s2 <= i_s1;`. Copying costs one register;
skipping breaks the result.

OLDER SAMPLES ARE A HISTORY CHAIN INSIDE ONE ROW, NOT OLDER ROWS.
`i_s1` is NOT "the sample before `i_s2`": it is the SAME sample, one
stage earlier. A filter, interpolator or timing-error detector that needs
the last N input values side by side keeps its own history chain in ONE
row, reset to 0, shifted only when the sample it takes in is valid, that is
under the valid bit of the row ABOVE (for a chain in s2: `valid_pipe(0)`,
the same bit p_loop uses for eta), and the next row reads the chain.
THE CHAIN IS AS LONG AS THE SPEC'S WINDOW: count from the newest sample the
spec reads to the oldest, over EVERY formula. y_sym reads x[m-1]..x[m+2] and
y_mid reads x[m-3]..x[m]; with m = n-2 that is x[n]..x[n-5], SIX registers
(x0_s2..x5_s2), not the four of one formula (the linter's history_too_short):

    s2: x0_s2 .. x5_s2 mu_s2 valid_pipe(1)   <- s1 (history chain, reset to 0) + p_loop (mu_s2)
        if rst = '1' then
          x0_s2 <= (others => '0');          -- one line per register of the chain
        elsif advance = '1' and valid_pipe(0) = '1' then
          x0_s2 <= i_s1;                     -- x[n]   = x[m+2]
          x1_s2 <= x0_s2;                    -- x[n-1] = x[m+1]
          x2_s2 <= x1_s2;                    -- x[n-2] = x[m]
          x3_s2 <= x2_s2;                    -- x[n-3] = x[m-1]
          x4_s2 <= x3_s2;                    -- x[n-4] = x[m-2]
          x5_s2 <= x4_s2;                    -- x[n-5] = x[m-3]
        end if;
    s3: ysym_s3 <= farrow(x3_s2, x2_s2, x1_s2, x0_s2, mu_s2)   <- s2, oldest first: x[m-1], x[m], x[m+1], x[m+2]
        ymid_s3 <= farrow(x5_s2, x4_s2, x3_s2, x2_s2, mu_s2)   <- the four OLDEST taps: x[m-3], x[m-2], x[m-1], x[m]

Inside the chain `x1_s2 <= x0_s2` reads its own row: that is a one-clock
delay and correct. Reaching for `i_s1` from stage 3 to get "an older
sample" is the pipeline_stage_skip bug and reads the wrong sample. Never
write "placeholder" or "needs history" in a comment: build the chain.

LOOP VALUES LIVE IN ONE PROCESS, p_loop. eta CHANGES EVERY INPUT SAMPLE;
acc, v AND y_prev CHANGE ONLY ON A STROBE.
A tracking loop (timing recovery, phase or gain control) keeps a few values
from one input sample to the next: the NCO phase `eta`, the loop filter
`acc` and `v`, the previous symbol `y_prev`. The spec updates eta once per
input sample (acc, v and y_prev once per strobe), so every sample must see
the value the sample before it wrote, one tick earlier. NEVER copy them
down the rows and back into row 1
(`eta_s1 <= eta_s11; eta_s2 <= eta_s1; ...`): with a new sample every tick
such a ring holds 11 different eta values at once, each sample works with
the value written 11 samples earlier, and the design runs 11 separate loops
that never lock (timing_recovery 2026-11-03: eta, acc, v and y_prev went
round all 11 rows; no checker saw it).

- Loop values have NO stage number: `eta_r`, `acc_r`, `v_r`, `yprev_i_r`,
  `yprev_q_r`. ONE clocked process, `p_loop`, is their only writer and
  resets them all; it runs under the same `elsif advance = '1'` as the rows
  (the loop stalls with the pipeline). They are not rows of the stage map;
  they get one extra `loop:` line at the end of it (example below).
- `eta_r` is updated in EVERY tick that brings a new sample, that is when
  the sample is in row s1 (`valid_pipe(0) = '1'`), from its own old value
  (`eta_next := eta_r - W;` ... `eta_r <= eta_next;`). Reading itself is
  correct: that is the loop's one-tick memory.
- The strobe and mu of that sample are decided in the SAME update and
  written into row s2, next to the sample: p_loop writes `strobe_s2` and
  `mu_s2` (the only _s names it writes; p_s2 does not write them). From s2
  on they travel down the rows like data (`strobe_s3 <= strobe_s2;`), so
  the output row holds the strobe and mu of its own sample.
- What a strobe produces further down (the error `e`, the new `y_prev`)
  goes back into p_loop from the row where it is ready, only when that
  row's valid bit and its strobe are both '1'. From the next tick on,
  eta_r uses the new v_r. That delay (9 ticks in the example, about 2
  symbols) is normal for a slow loop: the example locks within 30 symbols
  at +100 and -1000 ppm. State BOTH numbers in notes: the latency L = the
  number of rows from the sample in s1 to the output row (10 here: the
  strobe for sample n appears on m_axis_tvalid 10 clocks after sample n
  was presented), and the loop delay = the number of eta updates made with
  the old v after a strobe (9 here).
- `y_prev` is the symbol of the PREVIOUS strobe. p_loop stores y_sym from
  the row where it is final (`yprev_i_r <= ysym_i_s7;` when `valid_pipe(6)
  = '1' and strobe_s7 = '1'`), and the next row computes
  `d_i_s8 <= resize(ysym_i_s7, D_W) - resize(yprev_i_r, D_W);`. At that tick
  yprev_i_r still holds the previous strobe's symbol, as the formula needs.
- The history chain (x0_s2 .. x5_s2) holds samples from earlier ticks and
  feeds the loop, so it is reset to 0 like the loop values (`if rst = '1'
  then x0_i_s2 <= (others => '0'); ... elsif ...`). Without that the first
  strobes read undefined samples, `e` is 'X', and from then on v_r, eta_r
  and every strobe are 'X' for ever (2026-11-04 check of this example: 3
  output beats, then none). Plain data rows need no reset: they are
  overwritten with defined values before the first strobe reaches them.
- A loop without a strobe (PLL: the phase; AGC: the gain) keeps the same
  shape with less: the value every sample needs is the `_r` register itself
  (`phase_r`, `gain_r`), the row that uses it reads it directly, there is no
  strobe_s2 / mu_s2, and the error comes back into `p_loop` from the output
  row on every valid sample (`if valid_pipe(last) = '1' then`).

    stage map:
    s1: i_s1 q_s1 valid_pipe(0)   <- inputs
    s2: x0_i_s2 .. x5_i_s2 x0_q_s2 .. x5_q_s2 strobe_s2 mu_s2 valid_pipe(1)   <- s1 (history chain, reset to 0) + p_loop (strobe_s2 mu_s2)
    s3 .. s7: (five lines in a real map, one per row, each listing its registers) Farrow for y_sym and y_mid, strobe_sK mu_sK valid_pipe(K-1) carried along   <- row above
    s8: ysym_i_s8 ysym_q_s8 ymid_i_s8 ymid_q_s8 d_i_s8 d_q_s8 strobe_s8 mu_s8 valid_pipe(7)   <- s7 + yprev_i_r yprev_q_r
    s9: ysym_i_s9 ysym_q_s9 pr_i_s9 pr_q_s9 strobe_s9 mu_s9 valid_pipe(8)   <- s8
    s10: ysym_i_s10 ysym_q_s10 e_s10 strobe_s10 mu_s10 valid_pipe(9)   <- s9 (OUTPUT ROW, the last row; p_loop reads e_s10)
    loop: eta_r acc_r v_r yprev_i_r yprev_q_r   <- p_loop (eta every sample; yprev from s7; acc, v from e_s10)

    m_axis_tdata    <= std_logic_vector(ysym_i_s10) & std_logic_vector(ysym_q_s10);
    m_axis_tvalid_i <= valid_pipe(9) and strobe_s10;   -- the last row's bit and its own strobe
    m_axis_tuser    <= std_logic_vector(mu_s10(MU_F-1 downto MU_F-8));   -- mu as Q0.8, truncated (floor); never 1.0, see the clamp

  y_sym and mu are carried down to the row where e is ready, so the output
  row is the LAST row and the output flag is the last valid bit.

    -- architecture, before begin (eta_r, acc_r, v_r are signed(ETA_W-1 downto 0),
    -- mu_s2 .. mu_s10 are signed(MU_W-1 downto 0)):
    constant ETA_F   : integer := 30;                     -- fraction bits of eta, acc, v
    constant ETA_W   : integer := ETA_F + 2;              -- range -2 .. 2
    constant ONE     : signed(ETA_W-1 downto 0) := shift_left(to_signed(1, ETA_W), ETA_F);       -- 1.0
    constant QUARTER : signed(ETA_W-1 downto 0) := shift_left(to_signed(1, ETA_W), ETA_F - 2);   -- 1/N, N = 4
    constant MU_F    : integer := 16;                     -- fraction bits of mu
    constant MU_W    : integer := MU_F + 2;               -- 0 .. 1.0 plus a sign bit
    constant MU_ONE  : integer := 2**MU_F;                -- 1.0
    -- e_s10 has 22 fraction bits (Q1.11 * Q1.11), eta 30, so e counts 2^8 in eta's units:
    --   K2*e = e * 2^-15 * 2^8 = shift_right(e, 7);   K1*e = e * 2^-7 * 2^8 = shift_left(e, 1)
    -- e_s10 is E_W = 26 bits (12-bit Q1.11 y: d 13 bits, product 25, sum 26) and must stay
    -- narrower than ETA_W: if your y is wider, saturate y_sym and y_mid to 12 bits BEFORE
    -- the TED multiply, never e afterwards
    p_loop : process(aclk)
      variable eta_next : signed(ETA_W-1 downto 0);
      variable mu_v     : signed(ETA_W-1 downto 0);
      variable e_w      : signed(ETA_W-1 downto 0);
      variable acc_next : signed(ETA_W-1 downto 0);
    begin
      if rising_edge(aclk) then
        if rst = '1' then
          eta_r     <= (others => '0');
          acc_r     <= (others => '0');
          v_r       <= (others => '0');
          yprev_i_r <= (others => '0');
          yprev_q_r <= (others => '0');
          strobe_s2 <= '0';
          mu_s2     <= (others => '0');
        elsif advance = '1' then
          -- 1. NCO: every input sample (the sample in row s1)
          if valid_pipe(0) = '1' then
            eta_next := eta_r - (QUARTER + v_r);              -- eta - W, W = 1/N + v
            if eta_next < 0 then
              strobe_s2 <= '1';
              mu_v := shift_right(eta_r, ETA_F - MU_F - 2);   -- eta * 4, eta BEFORE the decrement
              if mu_v < 0 then
                mu_s2 <= (others => '0');
              elsif mu_v >= MU_ONE then
                mu_s2 <= to_signed(MU_ONE - 1, MU_W);   -- 0.99998, the largest mu below 1.0: an 8-bit mu reads 255, never 0
              else
                mu_s2 <= resize(mu_v, MU_W);
              end if;
              eta_next := eta_next + ONE;
            else
              strobe_s2 <= '0';
            end if;
            eta_r <= eta_next;
          end if;
          -- 2. y_prev: this strobe's symbol, for the NEXT strobe's d in row s8
          if valid_pipe(6) = '1' and strobe_s7 = '1' then
            yprev_i_r <= ysym_i_s7;
            yprev_q_r <= ysym_q_s7;
          end if;
          -- 3. loop filter: when a strobe's error reaches row s10
          if valid_pipe(9) = '1' and strobe_s10 = '1' then
            e_w      := resize(e_s10, ETA_W);
            acc_next := acc_r + shift_right(e_w, 7);         -- acc + K2*e
            acc_r    <= acc_next;
            v_r      <= shift_left(e_w, 1) + acc_next;       -- K1*e + acc
          end if;
        end if;
      end if;
    end process p_loop;

- Every loop value is COMPUTED in p_loop from the spec's rule (`eta_next =
  eta - W`, `strobe = eta_next < 0`, `mu = clamp(eta * N)`). A ring that
  only copies (`strobe_s1 <= strobe_s11`, `strobe_s2 <= strobe_s1`, ...)
  keeps the reset value for ever: strobe never fires, mu stays 0, every
  output is 0 (timing_recovery 2026-11-02 18:25; linter:
  `loop_value_never_computed`). Do not write a note claiming the rule is
  implemented unless the line exists.

═══════════════════════════════════════════════════════════════════
2. MANDATORY: THE STAGE MAP IN "notes", WRITTEN FIRST
═══════════════════════════════════════════════════════════════════

Before writing any VHDL, write the stage map as the FIRST entry of
"notes", one line per stage, in exactly this form:

    stage map:
    s1: i_s1 q_s1 ns_s1 valid_pipe(0)   <- inputs
    s2: abs_i_s2 abs_q_s2 i_s2 q_s2 ns_s2 valid_pipe(1)   <- s1
    s3: i_mmsb_s3 q_mmsb_s3 ns_s3 valid_pipe(2)   <- s2
    s4: i_mmsb_p_s4 q_mmsb_p_s4 valid_pipe(3)   <- s3

Then check the map, row by row, before coding:
- every register that a row's computation needs must be listed in the row
  directly above it. If it is not, ADD a copy register to the row above
  (`i_s2`), do not reach further up.
- every row up to the one the output reads from ends with its `valid_pipe`
  bit, so the valid chain and the data chain to the output have the same
  length.
- an output register (`m_axis_tdata_reg`) is a row of its own, with its
  own valid bit, and the output valid flag comes from THAT bit:
      s7: m_axis_tdata_reg valid_pipe(6)   <- s6      (m_axis_tvalid = valid_pipe(6))
  Leaving it out of the map is the bug that made every beat read as 0
  (linter: `valid_data_length_mismatch`). Prefer no output register at
  all: `m_axis_tdata <= std_logic_vector(b3_s6 & ...)` straight from the
  last row, outside the process.
- make the output row the LAST row: a tracking loop carries y_sym and mu
  down to the row where its error e is ready (LOOP VALUES example: output
  from s10, `m_axis_tvalid = valid_pipe(9) and strobe_s10`). Then the valid
  chain has one bit per row and the output flag is its last bit. No row may
  come after the output row: if you are writing one, it holds a loop value;
  move that value into `p_loop` as `<name>_r` (LOOP VALUES) and delete the
  row. (The rows s8..s11 that once came after the output were the eta ring;
  taking the valid flag from their last bit made it 3 clocks late,
  timing_recovery 2026-11-02 18:00.)
- a tracking loop adds ONE `loop:` line at the END of the map: every `_r`
  register, and after the arrow which row feeds each one
  (`loop: eta_r acc_r v_r yprev_i_r yprev_q_r   <- p_loop (eta every sample;
  yprev from s7; acc, v from e_s10)`). It is not a row: no valid bit, and
  nothing reads it "from the line above".
- a name that appears in a row but is never read by the next row (or by
  the output) is dead: delete it from the map and from the code.

Write ONE short clocked process per row of the map (`p_s1`, `p_s2`, ...,
under about 40 lines each, see streaming_datapath_recipe.md section 3),
each assignment reading only names from the row above, plus one process
`p_valid` that shifts `valid_pipe` one bit per row. The map is the
source of truth; if the code and the map disagree, the code is wrong.

═══════════════════════════════════════════════════════════════════
3. LAST CHECK BEFORE RETURNING THE JSON
═══════════════════════════════════════════════════════════════════

Scan every assignment line of the form `<name>_s<K> <= ...` in your code.
For each `_s<J>` on its right-hand side, J must equal K-1 (or K inside a
history chain, `x1_s2 <= x0_s2`). If any J is K-2 or smaller, you have
skipped a stage: insert the copy register(s) in the stage map and in the
code, then scan again. If any J is LARGER than K, you have written the
ring: move that value into `p_loop` (LOOP VALUES) and delete the copies.
Do not return until the scan finds nothing.

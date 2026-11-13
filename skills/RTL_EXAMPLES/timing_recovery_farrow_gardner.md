applies_when: farrow, gardner, timing recovery

# WORKED EXAMPLE: symbol timing recovery (NCO + parabolic Farrow + Gardner error + PI loop)

This is a COMPLETE design that passed the real simulation (2026-11-11: 801
input samples, 201 output beats, 0 mismatches against the MATLAB golden
model) and every lint check. It is given to you because the spec you were
handed is this kind of design. The rule files above say HOW to write RTL;
this file shows one finished answer that follows them.

How to use it:
- If your spec has the same formulas (compare them line by line with the
  spec lines quoted in the comments below), keep the arithmetic rows s3..s10
  and `p_loop` exactly as they are. They are the part that was wrong in
  every earlier attempt.
- Take the entity name, the port names, the port widths, the bit position
  of each field, N, K1 and K2 from YOUR spec, never from this file. Where
  your spec differs, change that line and every line that depends on it.
- Do not add rows, do not remove rows, do not merge rows.

The five mistakes this example exists to prevent (each one was returned by
an earlier attempt and failed the simulation):
1. `y = (v2*mu + v1)*mu + v0` needs TWO multiplies by mu, one after the
   other (rows s4 and s6). `v2*mu + v1*mu + v0` is a different formula.
2. y_mid is its OWN interpolation on the four OLDEST samples (x2..x5). It is
   never a copy of y_sym.
3. v2 and v1 are kept at TWICE their value (no 0.5 inside the row), and the
   factor 2 is removed ONCE, in row s7 (`MU_F + 1`). Dropping the 0.5 and
   never shifting it back doubles the result.
4. The weight 3 (1.5 doubled) belongs to x[m+1] (`x1` for y_sym, `x3` for
   y_mid), not to x[m].
5. `{I[11:0], Q[11:0]}` means I is the HIGH half: I = tdata(23 downto 12),
   Q = tdata(11 downto 0), on the input AND on the output.

The first entry of "notes" for this design:

    stage map:
    s1: i_s1 q_s1 valid_pipe(0)   <- inputs
    s2: x0_i_s2 .. x5_i_s2 x0_q_s2 .. x5_q_s2 strobe_s2 mu_s2 valid_pipe(1)   <- s1 (history chain, reset to 0) + p_loop (strobe_s2 mu_s2)
    s3: v2_i_s3 v2_q_s3 v1_i_s3 v1_q_s3 v0_i_s3 v0_q_s3 v2m_i_s3 v2m_q_s3 v1m_i_s3 v1m_q_s3 v0m_i_s3 v0m_q_s3 strobe_s3 mu_s3 valid_pipe(2)   <- s2
    s4: p1_i_s4 p1_q_s4 p1m_i_s4 p1m_q_s4 v1_i_s4 v1_q_s4 v1m_i_s4 v1m_q_s4 v0_i_s4 v0_q_s4 v0m_i_s4 v0m_q_s4 strobe_s4 mu_s4 valid_pipe(3)   <- s3
    s5: h_i_s5 h_q_s5 hm_i_s5 hm_q_s5 v0_i_s5 v0_q_s5 v0m_i_s5 v0m_q_s5 strobe_s5 mu_s5 valid_pipe(4)   <- s4
    s6: p2_i_s6 p2_q_s6 p2m_i_s6 p2m_q_s6 v0_i_s6 v0_q_s6 v0m_i_s6 v0m_q_s6 strobe_s6 mu_s6 valid_pipe(5)   <- s5
    s7: ysym_i_s7 ysym_q_s7 ymid_i_s7 ymid_q_s7 strobe_s7 mu_s7 valid_pipe(6)   <- s6
    s8: ysym_i_s8 ysym_q_s8 ymid_i_s8 ymid_q_s8 d_i_s8 d_q_s8 strobe_s8 mu_s8 valid_pipe(7)   <- s7 + yprev_i_r yprev_q_r
    s9: ysym_i_s9 ysym_q_s9 pr_i_s9 pr_q_s9 strobe_s9 mu_s9 valid_pipe(8)   <- s8
    s10: ysym_i_s10 ysym_q_s10 e_s10 strobe_s10 mu_s10 valid_pipe(9)   <- s9 (OUTPUT ROW, the last row; p_loop reads e_s10)
    loop: eta_r acc_r v_r yprev_i_r yprev_q_r   <- p_loop (eta every sample; yprev from s7; acc, v from e_s10)
    latency L = 10 (sample n in s1 -> its strobe on m_axis_tvalid 10 clocks later); loop delay = 9 eta updates

What each row does (one kind of work per row):

    s3  sums of samples     2*v2, 2*v1, v0 for y_sym (x0..x3) and for y_mid (x2..x5)
    s4  multiply            p1 = 2*v2 * mu
    s5  add                 h  = 2*(v2*mu + v1)        (p1 shifted back by MU_F first)
    s6  multiply            p2 = h * mu
    s7  add                 y  = (v2*mu + v1)*mu + v0  (p2 shifted back by MU_F + 1)
    s8  subtract, saturate  d = y_sym - y_prev; y_sym and y_mid saturated to 12 bits
    s9  multiply            pr = y_mid * d
    s10 add                 e  = pr_i + pr_q

The code:

```vhdl
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity timing_recovery is
  port (
    aclk          : in  std_logic;
    aresetn       : in  std_logic;
    s_axis_tvalid : in  std_logic;
    s_axis_tdata  : in  std_logic_vector(23 downto 0);
    s_axis_tready : out std_logic;
    m_axis_tvalid : out std_logic;
    m_axis_tdata  : out std_logic_vector(23 downto 0);
    m_axis_tuser  : out std_logic_vector(7 downto 0)
  );
end entity timing_recovery;

architecture rtl of timing_recovery is
  constant ETA_F   : integer := 30;                     -- fraction bits of eta, acc, v
  constant ETA_W   : integer := ETA_F + 2;              -- range -2 .. 2
  constant ONE     : signed(ETA_W-1 downto 0) := shift_left(to_signed(1, ETA_W), ETA_F);       -- 1.0
  constant QUARTER : signed(ETA_W-1 downto 0) := shift_left(to_signed(1, ETA_W), ETA_F - 2);   -- 1/N, N = 4
  constant MU_F    : integer := 16;                     -- fraction bits of mu
  constant MU_W    : integer := MU_F + 2;               -- 0 .. 1.0 plus a sign bit
  constant MU_ONE  : integer := 2**MU_F;                -- 1.0
  constant I_W     : integer := 12;                     -- one input sample, Q1.11
  constant V_W     : integer := 15;                     -- 2*v2 and 2*v1: a sum of up to 6 samples
  constant P1_W    : integer := V_W + MU_W;             -- 2*v2 * mu
  constant H_W     : integer := 17;                     -- 2*(v2*mu + v1)
  constant P2_W    : integer := H_W + MU_W;             -- h * mu
  constant Y_W     : integer := 15;                     -- y before saturation
  constant D_W     : integer := Y_W + 1;                -- y_sym - y_prev
  constant PR_W    : integer := I_W + D_W;              -- y_mid (12 bits) * d
  constant E_W     : integer := PR_W + 1;               -- 29 bits: narrower than ETA_W

  signal rst        : std_logic;
  signal advance    : std_logic;
  signal valid_pipe : std_logic_vector(9 downto 0);

  signal i_s1, q_s1 : signed(I_W-1 downto 0);

  signal x0_i_s2, x1_i_s2, x2_i_s2, x3_i_s2, x4_i_s2, x5_i_s2 : signed(I_W-1 downto 0);
  signal x0_q_s2, x1_q_s2, x2_q_s2, x3_q_s2, x4_q_s2, x5_q_s2 : signed(I_W-1 downto 0);
  signal strobe_s2 : std_logic;
  signal mu_s2     : signed(MU_W-1 downto 0);

  signal v2_i_s3, v2_q_s3, v2m_i_s3, v2m_q_s3 : signed(V_W-1 downto 0);
  signal v1_i_s3, v1_q_s3, v1m_i_s3, v1m_q_s3 : signed(V_W-1 downto 0);
  signal v0_i_s3, v0_q_s3, v0m_i_s3, v0m_q_s3 : signed(I_W-1 downto 0);
  signal strobe_s3 : std_logic;
  signal mu_s3     : signed(MU_W-1 downto 0);

  signal p1_i_s4, p1_q_s4, p1m_i_s4, p1m_q_s4 : signed(P1_W-1 downto 0);
  signal v1_i_s4, v1_q_s4, v1m_i_s4, v1m_q_s4 : signed(V_W-1 downto 0);
  signal v0_i_s4, v0_q_s4, v0m_i_s4, v0m_q_s4 : signed(I_W-1 downto 0);
  signal strobe_s4 : std_logic;
  signal mu_s4     : signed(MU_W-1 downto 0);

  signal h_i_s5, h_q_s5, hm_i_s5, hm_q_s5     : signed(H_W-1 downto 0);
  signal v0_i_s5, v0_q_s5, v0m_i_s5, v0m_q_s5 : signed(I_W-1 downto 0);
  signal strobe_s5 : std_logic;
  signal mu_s5     : signed(MU_W-1 downto 0);

  signal p2_i_s6, p2_q_s6, p2m_i_s6, p2m_q_s6 : signed(P2_W-1 downto 0);
  signal v0_i_s6, v0_q_s6, v0m_i_s6, v0m_q_s6 : signed(I_W-1 downto 0);
  signal strobe_s6 : std_logic;
  signal mu_s6     : signed(MU_W-1 downto 0);

  signal ysym_i_s7, ysym_q_s7, ymid_i_s7, ymid_q_s7 : signed(Y_W-1 downto 0);
  signal strobe_s7 : std_logic;
  signal mu_s7     : signed(MU_W-1 downto 0);

  signal ysym_i_s8, ysym_q_s8, ymid_i_s8, ymid_q_s8 : signed(I_W-1 downto 0);
  signal d_i_s8, d_q_s8 : signed(D_W-1 downto 0);
  signal strobe_s8 : std_logic;
  signal mu_s8     : signed(MU_W-1 downto 0);

  signal pr_i_s9, pr_q_s9     : signed(PR_W-1 downto 0);
  signal ysym_i_s9, ysym_q_s9 : signed(I_W-1 downto 0);
  signal strobe_s9 : std_logic;
  signal mu_s9     : signed(MU_W-1 downto 0);

  signal e_s10 : signed(E_W-1 downto 0);
  signal ysym_i_s10, ysym_q_s10 : signed(I_W-1 downto 0);
  signal strobe_s10 : std_logic;
  signal mu_s10     : signed(MU_W-1 downto 0);

  signal eta_r, acc_r, v_r    : signed(ETA_W-1 downto 0);
  signal yprev_i_r, yprev_q_r : signed(Y_W-1 downto 0);

  signal m_axis_tvalid_i : std_logic;

  -- a 15-bit y limited to the 12-bit Q1.11 range
  function sat12(x : signed(Y_W-1 downto 0)) return signed is
  begin
    if x > 2047 then
      return to_signed(2047, I_W);
    elsif x < -2048 then
      return to_signed(-2048, I_W);
    else
      return resize(x, I_W);
    end if;
  end function;

begin
  rst             <= not aresetn;
  advance         <= s_axis_tvalid;                      -- no m_axis_tready port: nothing can stall the output
  s_axis_tready   <= '1';
  m_axis_tvalid_i <= valid_pipe(9) and strobe_s10;       -- the last row's bit and its own strobe
  m_axis_tvalid   <= m_axis_tvalid_i;
  m_axis_tdata    <= std_logic_vector(ysym_i_s10) & std_logic_vector(ysym_q_s10);   -- {I, Q}: I is the HIGH half
  m_axis_tuser    <= std_logic_vector(mu_s10(MU_F-1 downto MU_F-8));                -- mu as Q0.8, truncated

  p_valid : process(aclk)
  begin
    if rising_edge(aclk) then
      if rst = '1' then
        valid_pipe <= (others => '0');
      elsif advance = '1' then
        valid_pipe(0) <= s_axis_tvalid;
        valid_pipe(1) <= valid_pipe(0);
        valid_pipe(2) <= valid_pipe(1);
        valid_pipe(3) <= valid_pipe(2);
        valid_pipe(4) <= valid_pipe(3);
        valid_pipe(5) <= valid_pipe(4);
        valid_pipe(6) <= valid_pipe(5);
        valid_pipe(7) <= valid_pipe(6);
        valid_pipe(8) <= valid_pipe(7);
        valid_pipe(9) <= valid_pipe(8);
      end if;
    end if;
  end process p_valid;

  -- s1: split the input word. {I[11:0], Q[11:0]}: I is the HIGH half.
  p_s1 : process(aclk)
  begin
    if rising_edge(aclk) then
      if advance = '1' then
        i_s1 <= signed(s_axis_tdata(23 downto 12));
        q_s1 <= signed(s_axis_tdata(11 downto 0));
      end if;
    end if;
  end process p_s1;

  -- s2: the last six samples side by side (strobe_s2 and mu_s2 are written by p_loop).
  -- With m = n - 2:  x0 = x[m+2]  x1 = x[m+1]  x2 = x[m]  x3 = x[m-1]  x4 = x[m-2]  x5 = x[m-3]
  p_s2 : process(aclk)
  begin
    if rising_edge(aclk) then
      if rst = '1' then
        x0_i_s2 <= (others => '0');
        x1_i_s2 <= (others => '0');
        x2_i_s2 <= (others => '0');
        x3_i_s2 <= (others => '0');
        x4_i_s2 <= (others => '0');
        x5_i_s2 <= (others => '0');
        x0_q_s2 <= (others => '0');
        x1_q_s2 <= (others => '0');
        x2_q_s2 <= (others => '0');
        x3_q_s2 <= (others => '0');
        x4_q_s2 <= (others => '0');
        x5_q_s2 <= (others => '0');
      elsif advance = '1' and valid_pipe(0) = '1' then
        x0_i_s2 <= i_s1;
        x1_i_s2 <= x0_i_s2;
        x2_i_s2 <= x1_i_s2;
        x3_i_s2 <= x2_i_s2;
        x4_i_s2 <= x3_i_s2;
        x5_i_s2 <= x4_i_s2;
        x0_q_s2 <= q_s1;
        x1_q_s2 <= x0_q_s2;
        x2_q_s2 <= x1_q_s2;
        x3_q_s2 <= x2_q_s2;
        x4_q_s2 <= x3_q_s2;
        x5_q_s2 <= x4_q_s2;
      end if;
    end if;
  end process p_s2;

  -- s3: the Farrow sums, kept at TWICE their value (the 0.5 is taken out in s7).
  --   spec: v2 = 0.5*(x[m+2] - x[m+1] - x[m] + x[m-1])            2*v2 =  x[m+2] -   x[m+1] - x[m] + x[m-1]
  --   spec: v1 = -0.5*x[m+2] + 1.5*x[m+1] - 0.5*x[m] - 0.5*x[m-1]  2*v1 = -x[m+2] + 3*x[m+1] - x[m] - x[m-1]
  --   spec: v0 = x[m]
  -- y_sym = farrow(x[m-1], x[m], x[m+1], x[m+2]) reads x3 x2 x1 x0.
  -- y_mid = farrow(x[m-3], x[m-2], x[m-1], x[m]) is the same formula two samples older: x5 x4 x3 x2.
  p_s3 : process(aclk)
  begin
    if rising_edge(aclk) then
      if advance = '1' then
        v2_i_s3  <= resize(x0_i_s2, V_W) - resize(x1_i_s2, V_W) - resize(x2_i_s2, V_W) + resize(x3_i_s2, V_W);
        v2_q_s3  <= resize(x0_q_s2, V_W) - resize(x1_q_s2, V_W) - resize(x2_q_s2, V_W) + resize(x3_q_s2, V_W);
        v1_i_s3  <= shift_left(resize(x1_i_s2, V_W), 1) + resize(x1_i_s2, V_W) - resize(x0_i_s2, V_W) - resize(x2_i_s2, V_W) - resize(x3_i_s2, V_W);
        v1_q_s3  <= shift_left(resize(x1_q_s2, V_W), 1) + resize(x1_q_s2, V_W) - resize(x0_q_s2, V_W) - resize(x2_q_s2, V_W) - resize(x3_q_s2, V_W);
        v0_i_s3  <= x2_i_s2;
        v0_q_s3  <= x2_q_s2;
        v2m_i_s3 <= resize(x2_i_s2, V_W) - resize(x3_i_s2, V_W) - resize(x4_i_s2, V_W) + resize(x5_i_s2, V_W);
        v2m_q_s3 <= resize(x2_q_s2, V_W) - resize(x3_q_s2, V_W) - resize(x4_q_s2, V_W) + resize(x5_q_s2, V_W);
        v1m_i_s3 <= shift_left(resize(x3_i_s2, V_W), 1) + resize(x3_i_s2, V_W) - resize(x2_i_s2, V_W) - resize(x4_i_s2, V_W) - resize(x5_i_s2, V_W);
        v1m_q_s3 <= shift_left(resize(x3_q_s2, V_W), 1) + resize(x3_q_s2, V_W) - resize(x2_q_s2, V_W) - resize(x4_q_s2, V_W) - resize(x5_q_s2, V_W);
        v0m_i_s3 <= x4_i_s2;
        v0m_q_s3 <= x4_q_s2;
        strobe_s3 <= strobe_s2;
        mu_s3     <= mu_s2;
      end if;
    end if;
  end process p_s3;

  -- s4: FIRST multiply by mu:  p1 = 2*v2 * mu   (MU_F fraction bits)
  p_s4 : process(aclk)
  begin
    if rising_edge(aclk) then
      if advance = '1' then
        p1_i_s4  <= v2_i_s3 * mu_s3;
        p1_q_s4  <= v2_q_s3 * mu_s3;
        p1m_i_s4 <= v2m_i_s3 * mu_s3;
        p1m_q_s4 <= v2m_q_s3 * mu_s3;
        v1_i_s4  <= v1_i_s3;
        v1_q_s4  <= v1_q_s3;
        v1m_i_s4 <= v1m_i_s3;
        v1m_q_s4 <= v1m_q_s3;
        v0_i_s4  <= v0_i_s3;
        v0_q_s4  <= v0_q_s3;
        v0m_i_s4 <= v0m_i_s3;
        v0m_q_s4 <= v0m_q_s3;
        strobe_s4 <= strobe_s3;
        mu_s4     <= mu_s3;
      end if;
    end if;
  end process p_s4;

  -- s5: h = 2*(v2*mu + v1). The product is shifted back by MU_F BEFORE v1 is added:
  -- p1 has MU_F fraction bits, v1 has none.
  p_s5 : process(aclk)
  begin
    if rising_edge(aclk) then
      if advance = '1' then
        h_i_s5   <= resize(shift_right(p1_i_s4, MU_F), H_W) + resize(v1_i_s4, H_W);
        h_q_s5   <= resize(shift_right(p1_q_s4, MU_F), H_W) + resize(v1_q_s4, H_W);
        hm_i_s5  <= resize(shift_right(p1m_i_s4, MU_F), H_W) + resize(v1m_i_s4, H_W);
        hm_q_s5  <= resize(shift_right(p1m_q_s4, MU_F), H_W) + resize(v1m_q_s4, H_W);
        v0_i_s5  <= v0_i_s4;
        v0_q_s5  <= v0_q_s4;
        v0m_i_s5 <= v0m_i_s4;
        v0m_q_s5 <= v0m_q_s4;
        strobe_s5 <= strobe_s4;
        mu_s5     <= mu_s4;
      end if;
    end if;
  end process p_s5;

  -- s6: SECOND multiply by mu:  p2 = h * mu = 2*(v2*mu + v1)*mu
  p_s6 : process(aclk)
  begin
    if rising_edge(aclk) then
      if advance = '1' then
        p2_i_s6  <= h_i_s5 * mu_s5;
        p2_q_s6  <= h_q_s5 * mu_s5;
        p2m_i_s6 <= hm_i_s5 * mu_s5;
        p2m_q_s6 <= hm_q_s5 * mu_s5;
        v0_i_s6  <= v0_i_s5;
        v0_q_s6  <= v0_q_s5;
        v0m_i_s6 <= v0m_i_s5;
        v0m_q_s6 <= v0m_q_s5;
        strobe_s6 <= strobe_s5;
        mu_s6     <= mu_s5;
      end if;
    end if;
  end process p_s6;

  -- s7: y = (v2*mu + v1)*mu + v0. Shift back MU_F for mu plus 1 for the factor 2 kept since s3.
  p_s7 : process(aclk)
  begin
    if rising_edge(aclk) then
      if advance = '1' then
        ysym_i_s7 <= resize(shift_right(p2_i_s6, MU_F + 1), Y_W) + resize(v0_i_s6, Y_W);
        ysym_q_s7 <= resize(shift_right(p2_q_s6, MU_F + 1), Y_W) + resize(v0_q_s6, Y_W);
        ymid_i_s7 <= resize(shift_right(p2m_i_s6, MU_F + 1), Y_W) + resize(v0m_i_s6, Y_W);
        ymid_q_s7 <= resize(shift_right(p2m_q_s6, MU_F + 1), Y_W) + resize(v0m_q_s6, Y_W);
        strobe_s7 <= strobe_s6;
        mu_s7     <= mu_s6;
      end if;
    end if;
  end process p_s7;

  -- s8: d = y_sym - y_prev (yprev_*_r still holds the PREVIOUS strobe's symbol here);
  -- y_sym and y_mid are saturated to 12 bits for the output and for the multiply in s9.
  p_s8 : process(aclk)
  begin
    if rising_edge(aclk) then
      if advance = '1' then
        d_i_s8    <= resize(ysym_i_s7, D_W) - resize(yprev_i_r, D_W);
        d_q_s8    <= resize(ysym_q_s7, D_W) - resize(yprev_q_r, D_W);
        ysym_i_s8 <= sat12(ysym_i_s7);
        ysym_q_s8 <= sat12(ysym_q_s7);
        ymid_i_s8 <= sat12(ymid_i_s7);
        ymid_q_s8 <= sat12(ymid_q_s7);
        strobe_s8 <= strobe_s7;
        mu_s8     <= mu_s7;
      end if;
    end if;
  end process p_s8;

  -- s9: the two products of e = Re{ y_mid * conj(y_sym - y_prev) } = ymid_i*d_i + ymid_q*d_q
  p_s9 : process(aclk)
  begin
    if rising_edge(aclk) then
      if advance = '1' then
        pr_i_s9   <= ymid_i_s8 * d_i_s8;
        pr_q_s9   <= ymid_q_s8 * d_q_s8;
        ysym_i_s9 <= ysym_i_s8;
        ysym_q_s9 <= ysym_q_s8;
        strobe_s9 <= strobe_s8;
        mu_s9     <= mu_s8;
      end if;
    end if;
  end process p_s9;

  -- s10: e, and the output row
  p_s10 : process(aclk)
  begin
    if rising_edge(aclk) then
      if advance = '1' then
        e_s10      <= resize(pr_i_s9, E_W) + resize(pr_q_s9, E_W);
        ysym_i_s10 <= ysym_i_s9;
        ysym_q_s10 <= ysym_q_s9;
        strobe_s10 <= strobe_s9;
        mu_s10     <= mu_s9;
      end if;
    end if;
  end process p_s10;

  -- The loop values. e_s10 has 22 fraction bits (Q1.11 * Q1.11), eta 30, so e counts 2^8 in
  -- eta's units:  K2*e = e * 2^-15 * 2^8 = shift_right(e, 7);  K1*e = e * 2^-7 * 2^8 = shift_left(e, 1)
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
              mu_s2 <= to_signed(MU_ONE - 1, MU_W);
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

end architecture rtl;
```

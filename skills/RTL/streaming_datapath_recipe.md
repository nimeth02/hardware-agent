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

A design that needs more than about 250 lines is almost always repeating
itself; shorten it before adding more.

═══════════════════════════════════════════════════════════════════
3. ONE CLOCKED PROCESS FOR THE PIPELINE
═══════════════════════════════════════════════════════════════════

Put all pipeline registers in ONE clocked process. Then each signal has one
writer by construction.

    process(clk)
    begin
      if rising_edge(clk) then
        if rst = '1' then                -- use the spec's reset polarity
          valid_pipe <= (others => '0');
        elsif advance = '1' then
          -- stage 1
          a_s1 <= ...;   valid_pipe(0) <= in_valid;
          -- stage 2 reads stage-1 registers
          b_s2 <= f(a_s1);   valid_pipe(1) <= valid_pipe(0);
          ...
        end if;
      end if;
    end process;

- Latency = number of stages. Count them against the spec's limit, but
  never merge stages to save latency if that breaks the timing rules in
  section 3b.
- `valid_pipe` is a std_logic_vector, one bit per stage, shifted every
  advance.
- Handshake (stall-safe, no data loss):
    advance       <= m_tready or not valid_pipe(last);  -- output free or empty
    s_tready_i    <= advance;                           -- accept only when advancing
    m_tvalid_i    <= valid_pipe(last);
  An input is accepted when `s_tvalid = '1' and advance = '1'`; when
  `advance = '0'` every register holds its value.
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
- Declare the product signal at the full width, then trim:
      signal prod : signed(19 downto 0);        -- 12 + 8
      prod  <= a * b;                           -- a: 12, b: 8
      small <= resize(prod, 16);                -- separate step
  Do NOT widen the inputs to the target width with resize() and then
  multiply: that makes the product twice as wide as the target.
- Shifts, powers of two and division (VHDL-93 + numeric_std):
    shift right (keeps sign for signed):  shift_right(x, 13)
    shift left:                           shift_left(x, 13)
  `sll`, `srl`, `sra` do NOT work on signed/unsigned; use the functions.
  Multiplying or dividing by a power of two (2, 4, ... 8192) is a shift,
  never a multiplier or divider. Never write `x * to_signed(8192, N)`,
  `x / 8192` or `x sra 13`, and never use `/` between two signals.
  "Round half up then divide by 8192" is:
      sum <= x + to_signed(4096, x'length);   -- constant sized like x
      res <= shift_right(sum, 13);
  If the maths seems to need a real division by a changing value, stop and
  re-read the spec: it is almost always a multiplication or a shift.
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
  Write the width of every piece in a comment next to the line.
- Mixed signedness: convert explicitly, e.g. a signed value times an
  unsigned one: `a * signed('0' & b)` (b gains one sign bit: 8 -> 9 bits).
- Shifts and rounding: `shift_right(x, n)` keeps the width; add the
  rounding constant BEFORE the shift, using a constant sized like x.
- Saturation: compare against constants, then `resize` (see the function
  above); do not rely on wrap-around.

═══════════════════════════════════════════════════════════════════
5. CHECK YOURSELF BEFORE ANSWERING
═══════════════════════════════════════════════════════════════════

[ ] every signal declared before `begin`, every variable inside its process
[ ] every function/type is declared before `begin`; no records for pipeline stages
[ ] every expression mixing `and`/`or` has brackets
[ ] each signal assigned in exactly one process / one concurrent line
[ ] every `*` target is exactly width(a) + width(b)
[ ] left and right side of every assignment (and every `&` join) have equal widths
[ ] stage count ≤ the spec's latency limit (without breaking section 3b)
[ ] every multiply result is registered; no stage mixes multiply/add/saturate
[ ] no process lists in its sensitivity list a signal that only it writes

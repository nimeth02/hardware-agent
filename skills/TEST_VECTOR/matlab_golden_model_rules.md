# Test-vector rules: MATLAB golden model, CSV layout, coverage

Load this before writing ANY test-vector script. The script runs through
the MATLAB MCP tools (generate_matlab_script / run_matlab_script) with
project_dir as its working directory, and the two CSVs it writes are
validated deterministically and later replayed by a testbench that never
sees them — it relies on the exact layout below.

═══════════════════════════════════════════════════════════════════
GOLDEN MODEL, NOT RTL
═══════════════════════════════════════════════════════════════════

- Model the spec's "behavior" cycle by cycle in plain MATLAB (loops,
  if/else, mod). Do not imitate HDL structure; compute what the outputs
  MUST be after each clock edge.
- Integers only: 0/1 for 1-bit signals, unsigned integers for buses
  (a 4-bit bus holds 0..15). Use mod(x, 2^N) for wrap-around.

═══════════════════════════════════════════════════════════════════
SCRIPT STRUCTURE (preallocate two matrices, convert once)
═══════════════════════════════════════════════════════════════════

  N = 40;                                        % cycles
  in_names  = {'cycle', 'clk', 'rst', 'en'};     % cycle, then INPUT ports in spec order
  out_names = {'cycle', 'count'};                % cycle, then OUTPUT ports in spec order
  ins  = zeros(N, numel(in_names));
  outs = zeros(N, numel(out_names));
  state = 0;                                     % model registers
  for c = 0:N-1
      rst = double(c < 2);                       % stimulus for this cycle
      en  = double(mod(c, 7) ~= 6);
      if rst, state = 0; elseif en, state = mod(state + 1, 16); end
      ins(c+1, :)  = [c, 1, rst, en];
      outs(c+1, :) = [c, state];                 % expected AFTER this cycle's edge
  end
  writetable(array2table(ins,  'VariableNames', in_names),  'test_vectors_in.csv');
  writetable(array2table(outs, 'VariableNames', out_names), 'test_vectors_out.csv');
  fprintf('wrote %d rows\n', N);

- project_dir is a TOOL ARGUMENT, not a MATLAB variable; use relative
  paths inside the script.
- Never build the tables from transposed column vectors; the preallocated
  matrices above avoid orientation errors.

═══════════════════════════════════════════════════════════════════
ONE FLAT SCRIPT, NO LOCAL FUNCTIONS (the #1 cause of failed runs now)
═══════════════════════════════════════════════════════════════════

- Do NOT define functions in the script (`function y = f(x) ... end`).
  Script variables are invisible inside them ("Unrecognized function or
  variable 'TWO'"), and a definition in the wrong place fails with
  "Function definitions are not supported in this context". Put the
  logic inline in the loop; for a small formula use an anonymous
  function that takes everything it needs as arguments:
      wrap  = @(x, nbits) mod(x, 2^nbits);
      demap = @(i, q, scale) ...;              % no free variables inside
- One row = one value per column name. Build the row from the SAME list
  the header uses and check it, so "size of the left side is 1-by-3 and
  the size of the right side is 1-by-4" cannot happen:
      row = [c, clk, rst, en];
      assert(numel(row) == numel(in_names), 'row/header mismatch');
      ins(c+1, :) = row;
  Every output port in out_names needs a value every cycle, even when it
  is "don't care" (write 0).
- Give EVERY per-cycle output variable its default on the first lines of
  the loop body, before any `if`, and never `continue` past the row
  writes. A variable set only inside `if ~rst ... end` or `if strobe ...
  end` does not exist on the first cycle, and the row line crashes with
  "Unrecognized function or variable 'mvalid'" (timing_recovery,
  2026-11-06, three drafts in a row):
      for c = 0:N-1
          rst = double(c < 4);
          sready = 1; mvalid = 0; mdata = 0; muser = 0;   % defaults first
          if ~rst
              ...                                          % may overwrite them
          end
          ins(c+1, :)  = [c, 1, 1-rst, tvalid, tdata];
          outs(c+1, :) = [c, sready, mvalid, mdata, muser];
      end

═══════════════════════════════════════════════════════════════════
ONE BEAT COUNT, ROW VECTORS ONLY (every failed run on 2026-10-30)
═══════════════════════════════════════════════════════════════════

Every run that crashed on 2026-10-30 died on a list that was the wrong
length or the wrong shape: 20 beats but 16 symbol indexes, 37 beat cycles
but 36 I values ("Index exceeds the number of array elements"), a column
vector joined to a row vector with `;` ("Dimensions of arrays being
concatenated are not consistent"). Hand-typed lists drift apart; derived
lists cannot.

- Decide the beat count ONCE and derive every per-beat list from it:
      nbeats      = 20;
      beat_cycles = 4 + 2*(0:nbeats-1);                              % row, nbeats entries
      levels      = [256, 768, 1280, 1792, -256, -768, -1280, -1792];
      Ivals       = levels(mod(0:nbeats-1, numel(levels)) + 1);      % wraps: never runs out
      Qvals       = levels(mod((0:nbeats-1) + 3, numel(levels)) + 1);
      nsvals      = ns_levels(mod(0:nbeats-1, numel(ns_levels)) + 1);
  Put the hand-picked edge cases (0, 2047, -2048, 512, -513 ...) into
  `levels` and let the mod(...) indexing spread them over the beats; never
  type a 30-number row by hand and count it yourself.
- Right after the lists, prove they agree (the message names the odd one):
      assert(numel(beat_cycles) == nbeats, 'beat_cycles has %d entries, want %d', numel(beat_cycles), nbeats);
      assert(numel(Ivals) == nbeats, 'Ivals has %d entries, want %d', numel(Ivals), nbeats);
      (one assert per per-beat list)
- Every list is a ROW vector: randi(n, 1, nbeats), never randi(n, nbeats, 1);
  append with a comma, `a = [a, b];`, never `[a; b]`. When a shape is not
  obvious, write `a = a(:)';` once after building it.
- Place beats with the cycle number itself: `k = find(beat_cycles == c, 1);`
  then `I = Ivals(k)`. Never keep a second index list (beat_idx) that has
  to be kept the same length as the first.
- Never read `list(i+1)` inside `for i = 1:n` unless n is `numel(list)-1`:
  on the last pass it asks for entry n+1 and MATLAB stops with "Index exceeds
  the number of array elements" (timing_recovery 2026-11-06: `inst(i+1)` with
  i running to numel(inst) cost four failed runs). Same for `list(i-1)` from
  i = 1 (entry 0: "Array indices must be positive"). Make room for the shift
  before the loop and prove it:
      for i = 1:numel(inst)-1                       % one shorter, or ...
      inst_next = inst(2:end);                      % ... shift the list once, read inst_next(i), or ...
      inst(end+1) = inst(end);                      % ... pad it by one
      assert(numel(inst) >= nIn+1, 'inst has %d entries, loop reads %d', numel(inst), nIn+1);
  The guard rejects a script with such a read before MATLAB runs it.
- `conv(a, b)` returns numel(a)+numel(b)-1 values, LONGER than either
  input. Adding it to a preallocated list of a different length stops MATLAB
  with "Arrays have incompatible sizes" (RRC pulse shaping, 2026-11-06:
  seven snippets in a row repeated the same mismatch). Decide the length
  once and force it before any + - .* ./ between two lists:
      N = nsym * sps;
      y = conv(upsample(sym, sps), rrc);          % numel = N + numel(rrc) - 1
      y = y(span*sps + (1:N));                     % trim to N (drop the filter delay)
      assert(numel(y) == N, 'y has %d samples, want %d', numel(y), N);
  When MATLAB reports incompatible sizes, print size() of both sides of
  that exact line and fix it in the script. Do NOT test smaller snippets
  with matlab_eval: every failed snippet counts as one of the 3 failed runs.

═══════════════════════════════════════════════════════════════════
NUMBER TYPES (the #1 cause of failed runs)
═══════════════════════════════════════════════════════════════════

MATLAB refuses to mix integer classes with each other or with non-scalar
doubles ("Integers can only be combined with integers of the same class",
"Inputs must be ... of the same class", "Invalid use of operator"). Avoid
the whole problem:

- Do ALL arithmetic in plain double. Never call int8/int16/int32/uint8/
  uint16/uint32, typecast, bitshift, bitsll, bitsra, idivide or intmin/
  intmax. Integers up to 2^53 are exact in double, so a 32-bit word fits.
- Wrap to N bits:              v = mod(x, 2^N);
- Two's-complement sign (N):   v = mod(x, 2^N); if v >= 2^(N-1), v = v - 2^N; end
- Arithmetic shift right k:    floor(x / 2^k)          (floors toward -inf like SRA)
  The spec writes shifts as `>>>`, `>>` or `<<`. MATLAB has NONE of these
  operators (nor `!=`, `+=`, `++`): the guard rejects the script before it
  runs. `(p + 4096) >>> 13` MUST become `floor((p + 4096) / 2^13)`, and
  `x != y` becomes `x ~= y`.
- Shift left k:                mod(x * 2^k, 2^N)
- Pack fields into one word:   word = hi * 2^(lo_bits) + lo;   (all non-negative first)
  The multiplier of a field is 2^(its START bit) taken from the spec's
  `port[hi:lo] = name`, NEVER from its width. Spec "s_axis_tdata[31:20] = I,
  [19:8] = Q, [7:0] = ns" is exactly
      tdata = mod(I,4096) * 2^20 + mod(Q,4096) * 2^8 + mod(ns,256);   % [31:20] [19:8] [7:0]
  (`mod(I,4096) * 2^12` puts I on top of Q: the guard rejects a script that
  never uses 2^lo for a field; write the [hi:lo] in a comment next to each term).
- EVERY FIELD VALUE MUST FIT ITS WIDTH BEFORE PACKING. `mod(I, 4096)` wraps
  silently: I = +2048 in a 12-bit signed field becomes -2048 on the wire,
  so the expected output belongs to a symbol the RTL never received
  (llr_demap16, 2026-10-29: two beats "wrong" while the RTL was right).
  For an n-bit signed field keep -2^(n-1) <= v <= 2^(n-1)-1, for an
  unsigned field 0 <= v <= 2^n-1, and prove it with an assert right
  before the pack line, one per input field, naming the field:
      assert(all(I >= -2^11 & I <= 2^11-1), 'I does not fit 12 bits');   % [31:20]
      assert(all(ns >= 0 & ns <= 2^8-1), 'ns does not fit 8 bits');      % [7:0]
  Random stimulus must be drawn inside that range (randi([-2048, 2047], ...)).
  The guard rejects a script without one assert per input field.
- Extract a field [hi:lo]:     field = mod(floor(word / 2^lo), 2^(hi-lo+1));
- Saturate:                    v = min(max(x, -2^(N-1)), 2^(N-1)-1);
- NEVER wrap a signed intermediate value with mod(..., 2^N) before you
  divide, floor, saturate or compare it. mod() makes every negative number
  positive (mod(-24576, 2^24) = 16752640), so a negative metric or LLR
  comes out as the positive saturation value (llr_demap16, 2026-10-30
  11:51: `floor((mod(m*ns, 2^24) + 4096) / 2^13)` made 14 of 20 expected
  LLRs +31 while the RTL was right). A double already holds the sign of a
  "signed 24-bit product": write
      p = m * ns;  r = floor((p + 4096) / 2^13);  v = min(max(r, -32), 31);
  and wrap only the FINAL value at the pack line: mod(v, 2^6). The guard
  rejects a script with mod(<product or difference>, ...) inside
  floor/ceil/round/fix/min/max.
- bitand/bitor/bitxor are fine on NON-NEGATIVE doubles; wrap signed values
  with mod(x, 2^N) first.
- Signed outputs in the CSV: write the unsigned N-bit pattern, mod(v, 2^N),
  because the validator checks 0 .. 2^N-1 for an N-bit port.
- A port of 32 bits or more is still ONE plain decimal column (its full
  unsigned value, up to 2^53 is exact); the testbench reads wide columns
  with a digit-by-digit reader, so do not split it into halves.
- Scripts that use the forbidden integer-class functions above are
  rejected before they are written; rewrite them in double arithmetic.

═══════════════════════════════════════════════════════════════════
PACKING FIELDS INTO A WIDE PORT (checked before the run and by MATLAB)
═══════════════════════════════════════════════════════════════════

When the spec puts several fields inside one port ("s_axis_tdata[31:20] =
I, [19:8] = Q, [7:0] = ns"), the bit position of each field is the ONLY
thing the design reads. A field packed in the wrong place gives CSVs that
agree with each other and are wrong on every beat: the design answers
correctly for the word it was given, the simulation reports every beat as
a mismatch, and the RTL gets blamed (run 170022, 2026-10-29: I was packed
with 2^12, on top of Q; the RTL was right and was rewritten three times).

- Copy the spec's [hi:lo] into a comment next to the packing line and use
  the literal 2^lo of each field (the script is rejected before it runs if
  a field's start bit never appears in it):
      % s_axis_tdata[31:20] = I (12 bits), [19:8] = Q (12 bits), [7:0] = ns (8 bits)
      word = mod(I, 2^12) * 2^20 + mod(Q, 2^12) * 2^8 + mod(ns, 2^8);
- Unpack the word straight after packing it and stop the script if a field
  does not come back (one assert per field):
      assert(mod(floor(word / 2^20), 2^12) == mod(I, 2^12), 'I is not at bit 20');
      assert(mod(floor(word / 2^8),  2^12) == mod(Q, 2^12), 'Q is not at bit 8');
      assert(mod(word, 2^8) == mod(ns, 2^8),                 'ns is not at bit 0');
- Compute the expected outputs from the SAME I, Q, ns variables that went
  into the word, never from a second copy of the values.
- Do the same for packed OUTPUT ports: the field order and the 2^lo of each
  expected value come from the spec's [hi:lo], and an unpack assert follows.

═══════════════════════════════════════════════════════════════════
WHAT YOU CAN AND CANNOT DO
═══════════════════════════════════════════════════════════════════

- The spec is your ONLY input. There is no reference model, Python file or
  golden source anywhere on disk; do not look for one, implement the
  behaviour from the spec text.
- run_matlab_script runs only the script you wrote with
  generate_matlab_script. It cannot list folders or run built-ins such as
  `dir`; such calls are rejected.
- Use matlab_help / lookfor_help for MATLAB function questions only.

═══════════════════════════════════════════════════════════════════
CSV LAYOUT (validated)
═══════════════════════════════════════════════════════════════════

- Two files, names exactly `test_vectors_in.csv` and `test_vectors_out.csv`,
  one header row each.
- test_vectors_in.csv:  `cycle`, then every INPUT port (direction "input"),
  in the order the spec lists them, exact port names. The clock is an
  input port: include its column (value 1).
- test_vectors_out.csv: `cycle`, then every OUTPUT port (direction
  "output"), in the order the spec lists them. Values are what is EXPECTED
  after that cycle's clock edge.
- Same number of rows in both files, same `cycle` values row by row
  (0, 1, 2, ...). No blank lines, no strings, no floats.
- Sort a port into a file by the spec's `direction` field ONLY, never by
  its name prefix. On AXI-Stream handshakes the `tready` wire travels
  against the data: `s_axis_tready` is an OUTPUT (goes in
  test_vectors_out.csv) and `m_axis_tready` is an INPUT (goes in
  test_vectors_in.csv), even though their names look the other way round.
- Active-low resets (`aresetn`, `rst_n`): 0 while in reset, 1 afterwards.
  Active-high resets (`rst`): 1 while in reset, 0 afterwards.
- No input beat on a reset row: while the reset is active, `s_axis_tvalid`
  (or `en`/`we`) is 0 and no output is expected (`m_axis_tvalid` = 0). The
  design drops anything sent during reset, so a beat there can never be
  matched. MATLAB indexes from 1: `tvalid(c+1)` is cycle c, so a beat list
  `beat_cycles = [4, ...]` written into `tvalid(4)` lands on cycle 3 — use
  `tvalid(c+1) = 1` with the cycle number, or check the printed CSV.
- STREAMING designs (the ports include s_axis_tvalid and m_axis_tvalid;
  s_axis_tready and m_axis_tready may be absent, e.g. "downstream is
  always ready"): the RTL answers several cycles after the input beat and
  the exact delay is not known here (a spec that says "a fixed latency L
  must be declared" leaves L to the RTL writer: never guess it), so the
  testbench compares output beats IN ORDER, not by cycle. Write the
  expected result of an input beat on the SAME row as that input, with
  m_axis_tvalid = 1 on that row and 0 on rows with no beat; the
  outputs-row cycle is only used to label errors. A design that produces
  fewer output beats than input beats (a decimator; timing recovery with
  about one symbol per 4 samples) writes one m_axis_tvalid = 1 row per
  OUTPUT beat, on the row of the input sample that produced it, and the
  NUMBER of such rows must equal the number of beats the model produces.
- CLOSED LOOPS (timing recovery, PLL, AGC: a value fed back into the next
  sample) run on REAL values, never on the integer codes. Q1.11 means
  value = code / 2048: convert the input codes to real values once
  (`xr = xI / 2048`), run the interpolator, the error e, acc, v and eta on
  those, and convert back (`round(y * 2048)`, saturate, pack) only at the
  output line. Every timing_recovery script that failed on 2026-11-06 fed
  codes (±1024) into e = Re{y_mid*conj(y_sym - y_prev)}: e came out near
  10^6, K1*e made v thousands instead of ~0.01, W = 1/N + v went negative
  or huge, and the strobe fired 1, 3, 4 or 8 times in 400..1600 samples
  (the validator then reports beat_rate_mismatch), or fired on EVERY
  cycle from the first strobe on (2026-11-06 11:45: 193 beats for 400
  samples, 190 of them the same number; the validator now rejects that as
  consecutive_output_beats and constant_output). HARD RULE: the two
  asserts below are mandatory in every closed-loop script, inside the
  loop and after it; a script without them is a wrong script:
      assert(abs(v) < 1/N, 'loop filter blew up at sample %d: v = %g', n, v);
      assert(nOut >= nIn/N/2 && nOut <= nIn/N*2, '%d beats for %d samples, want about %d', nOut, nIn, round(nIn/N));
  A stimulus with no symbol transitions (one constant symbol) keeps e = 0
  and tests nothing; 99 identical symbols plus one flipped one is the same
  thing (the validator rejects one value on 95 % of the input or output
  beats as constant_input / constant_output). Draw every symbol at random:
      sym = 2*randi([0 1], 2, nsym) - 1;        % rows I, Q in {-1, +1}
  then pulse-shape and scale to codes (e.g. 1024*sym) before packing.
- SAMPLE HISTORY (an interpolator or filter that reads x[n-1], x[n-2], ...
  or a base point m = n-2): keep the last K samples in a fixed list,
  newest LAST, and read it with FIXED slot numbers, never with the running
  sample count. With `xb = zeros(1, 8)` and `xb = [xb(2:8), x_new]` every
  sample: x[n] = xb(8), x[n-1] = xb(7), x[n-j] = xb(8-j). For base point
  m = n-2: x[m+2] = xb(8), x[m+1] = xb(7), x[m] = xb(6), x[m-1] = xb(5),
  x[m-2] = xb(4), x[m-3] = xb(3). Do NOT write `m = nIn - 2; xb(m)`: on
  the first strobe m is -1 ("Array indices must be positive"), after 8
  samples it is past the end (timing_recovery 2026-11-06 17:12 crashed
  three attempts this way; the guard now rejects such a script before it
  runs). Pick K so the oldest sample read exists (x[m-3] = x[n-5] needs
  K >= 6); the zeros the list starts with stand for samples before reset.
  On rows with m_axis_tvalid = 0 write 0 in every other output column
  (m_axis_tdata, m_axis_tuser, ...): the testbench reads them to keep the
  columns aligned but never compares them. Put 1 in s_axis_tready on every
  row after reset (that column is only read to keep the columns aligned,
  never compared cycle by cycle). Put m_axis_tready = 0 on a few rows to exercise
  backpressure, only when the spec has that port; the testbench holds its
  comparison while m_axis_tready = 0.
- Row count: the simulator's default run is 10 us (1000 cycles at 100 MHz)
  and the testbench drains 16 more cycles, so stay under 900 rows unless
  the behaviour needs more (a loop that must settle); a longer stimulus
  costs one extra simulation run with doubled time, not a failure.
- HARD RULE: a row with m_axis_tready = 0 MUST have s_axis_tvalid = 0.
  The design cannot accept a new beat while its output is blocked, so the
  testbench would wait forever for s_axis_tready and the simulation would
  end without a result ("no_marker"). The validator rejects such a row
  (code beat_while_blocked). Send the next beat on the first row where
  m_axis_tready is back to 1.

═══════════════════════════════════════════════════════════════════
COVERAGE
═══════════════════════════════════════════════════════════════════

- Reset asserted for the first cycles, then released.
- Normal operation for every item in "behavior".
- Every "constraint", and the boundary cases the behaviour implies:
  wrap-around (reach the maximum value at least once), enable low / hold,
  full/empty, overflow. Keep N large enough to reach them.
- Handshake chips (AXI-Stream): hold `m_axis_tready` at 0 for several
  cycles while data is pending (backpressure) — only when the spec has
  that port — and drop `s_axis_tvalid` for gaps between symbols unless the
  spec says the input is continuous. Expected outputs must show that
  nothing is lost or duplicated across the stall.

═══════════════════════════════════════════════════════════════════
ERRORS (fix the line, do not start over)
═══════════════════════════════════════════════════════════════════

- A failed run_matlab_script comes back with WHERE IT CRASHED (the exact
  source line, with the line before and after) and, for known messages,
  WHY in plain words. Read both before touching the script.
- Fix THAT line and whatever it depends on; keep every other line exactly
  as it was. Do not write a new script from scratch: on 2026-10-30 every
  rewrite brought a new bug, and three rewrites used up the three runs
  while the original bug was one list being one entry short.
- A script that is the same as one that already crashed (only comments or
  spacing changed) is rejected and does not count as a run. Re-running an
  unchanged script is rejected too. You have 3 failed runs per attempt.

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
- STREAMING designs (AXI-Stream tvalid/tready ports): the RTL answers
  several cycles after the input beat and the exact delay is not known
  here, so the testbench compares output beats IN ORDER, not by cycle.
  Write the expected result of an input beat on the SAME row as that
  input, with m_axis_tvalid = 1 on that row and 0 on rows with no beat;
  put 1 in s_axis_tready on every row after reset. The outputs-row cycle
  is only used to label errors. Put m_axis_tready = 0 on a few rows to
  exercise backpressure; the testbench holds its comparison while
  m_axis_tready = 0.
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
  cycles while data is pending (backpressure), and drop `s_axis_tvalid`
  for gaps between symbols. Expected outputs must show that nothing is
  lost or duplicated across the stall.

═══════════════════════════════════════════════════════════════════
ERRORS
═══════════════════════════════════════════════════════════════════

- If run_matlab_script reports an error, change the script with
  generate_matlab_script before running again; re-running an unchanged
  script is rejected.

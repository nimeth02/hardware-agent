# Test-vector rules: MATLAB golden model, CSV layout, coverage

Load this before writing ANY test-vector script. The script runs through
the MATLAB MCP tools (generate_matlab_script / run_matlab_script) with
project_dir as its working directory, and the CSV it writes is validated
deterministically and later replayed by a testbench.

═══════════════════════════════════════════════════════════════════
GOLDEN MODEL, NOT RTL
═══════════════════════════════════════════════════════════════════

- Model the spec's "behavior" cycle by cycle in plain MATLAB (loops,
  if/else, mod). Do not imitate HDL structure; compute what the outputs
  MUST be after each clock edge.
- Integers only: 0/1 for 1-bit signals, unsigned integers for buses
  (a 4-bit bus holds 0..15). Use mod(x, 2^N) for wrap-around.

═══════════════════════════════════════════════════════════════════
SCRIPT STRUCTURE (preallocate one matrix, convert once)
═══════════════════════════════════════════════════════════════════

  N = 40;                               % cycles
  names = {'cycle', 'clk', 'rst', 'en', 'count'};   % cycle, inputs, outputs
  rows = zeros(N, numel(names));
  state = 0;                            % model registers
  for c = 0:N-1
      rst = double(c < 2);              % stimulus for this cycle
      en  = double(mod(c, 7) ~= 6);
      if rst, state = 0; elseif en, state = mod(state + 1, 16); end
      rows(c+1, :) = [c, 1, rst, en, state];
  end
  T = array2table(rows, 'VariableNames', names);
  writetable(T, 'test_vectors.csv');     % relative: already in project_dir
  fprintf('wrote %d rows\n', N);

- project_dir is a TOOL ARGUMENT, not a MATLAB variable; use relative
  paths inside the script.
- Never build the table from transposed column vectors; the preallocated
  matrix above avoids orientation errors.

═══════════════════════════════════════════════════════════════════
CSV LAYOUT (validated)
═══════════════════════════════════════════════════════════════════

- File name exactly test_vectors.csv, one header row.
- Columns: `cycle`, then every INPUT port, then every OUTPUT port, using
  the exact port names from the spec. Output columns hold the EXPECTED
  value observed after that cycle's clock edge.
- One row per clock cycle, no blank lines, no strings, no floats.

═══════════════════════════════════════════════════════════════════
COVERAGE
═══════════════════════════════════════════════════════════════════

- Reset asserted for the first cycles, then released.
- Normal operation for every item in "behavior".
- Every "constraint", and the boundary cases the behaviour implies:
  wrap-around (reach the maximum value at least once), enable low / hold,
  full/empty, overflow. Keep N large enough to reach them.

═══════════════════════════════════════════════════════════════════
ERRORS
═══════════════════════════════════════════════════════════════════

- If run_matlab_script reports an error, change the script with
  generate_matlab_script before running again; re-running an unchanged
  script is rejected.

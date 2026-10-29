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
(and, for K = 1, the entity inputs). Never `_s<K-2>` or older, never the
raw input in a stage above 1.

If a value is needed later but stage K has nothing to compute for it,
stage K still copies it: `i_s2 <= i_s1;`. Copying costs one register;
skipping breaks the result.

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
- every row ends with its `valid_pipe` bit, so the valid chain and the
  data chain have the same length.
- an output register (`m_axis_tdata_reg`) is a row of its own, with its
  own valid bit, and the output valid flag comes from THAT bit:
      s7: m_axis_tdata_reg valid_pipe(6)   <- s6      (m_axis_tvalid = valid_pipe(6))
  Leaving it out of the map is the bug that made every beat read as 0
  (linter: `valid_data_length_mismatch`). Prefer no output register at
  all: `m_axis_tdata <= std_logic_vector(b3_s6 & ...)` straight from the
  last row, outside the process.
- a name that appears in a row but is never read by the next row (or by
  the output) is dead: delete it from the map and from the code.

Write the clocked process from the map: one block of assignments per row,
each assignment reading only names from the row above. The map is the
source of truth; if the code and the map disagree, the code is wrong.

═══════════════════════════════════════════════════════════════════
3. LAST CHECK BEFORE RETURNING THE JSON
═══════════════════════════════════════════════════════════════════

Scan every assignment line of the form `<name>_s<K> <= ...` in your code.
For each `_s<J>` on its right-hand side, J must equal K-1. If any J is
K-2 or smaller, you have skipped a stage: insert the copy register(s) in
the stage map and in the code, then scan again. Do not return until the
scan finds nothing.

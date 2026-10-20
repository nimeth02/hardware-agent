# projects/

Example projects the simulation and implementation agents run as-is. One
folder per design. Hand-written examples (`counter4/`) are committed; the
folders the orchestrator writes for each run
(`<requirement>_T-hh.mm_D-dd.mm.yyyy/`, see
`agents/orchestrator_agent/requirement_id.py`) have the same layout but are
git-ignored.

    projects/<name>/
      spec.json          the approved spec (entity name, clock -> XDC, vhdl_version 93|2008; given to the analysts)
      src/*.vhd          design files, compiled with xvhdl (--2008 when spec.vhdl_version is 2008) / read_vhdl
      tb/<entity>_tb.vhd the self-checking testbench, compiled with xvhdl --2008
      *.csv              optional golden vectors (test_vectors_in.csv + test_vectors_out.csv
                         from the test-vector agent, or a single test_vectors.csv as in
                         counter4); every CSV is copied to work_proj/ so the testbench
                         can open it with a relative path
      constraints/*.xdc  optional; when absent the implementation agent writes
                         one create_clock from spec.clock
      scripts/           synth_impl.tcl, written by the implementation agent
      impl_out/          utilization.rpt, timing_summary.rpt, post_route.dcp
      work_proj/         created by the Vivado tools (logs, xsim.dir, waves.vcd)

Run from the UI (Agents -> simulation_agent / implementation_agent, input
`{"project": "counter4"}`) or from Python: `run_simulation("counter4")`,
`run_implementation("counter4")`.

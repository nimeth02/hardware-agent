# projects/

Example projects the simulation and implementation agents run as-is. One
folder per design:

    projects/<name>/
      spec.json          the approved spec (entity name, clock -> XDC; given to the analysts)
      src/*.vhd          design files, compiled with xvhdl (VHDL-93) / read_vhdl
      tb/<entity>_tb.vhd the self-checking testbench, compiled with xvhdl --2008
      test_vectors.csv   optional golden vectors; copied to work_proj/ so the
                         testbench can open it with a relative path
      constraints/*.xdc  optional; when absent the implementation agent writes
                         one create_clock from spec.clock
      scripts/           synth_impl.tcl, written by the implementation agent
      impl_out/          utilization.rpt, timing_summary.rpt, post_route.dcp
      work_proj/         created by the Vivado tools (logs, xsim.dir, waves.vcd)

Run from the UI (Agents -> simulation_agent / implementation_agent, input
`{"project": "counter4"}`) or from Python: `run_simulation("counter4")`,
`run_implementation("counter4")`.

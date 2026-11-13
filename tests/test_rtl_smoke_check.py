# tests/test_rtl_smoke_check.py — the RTL agent's short dry run (no Vivado needed).

from __future__ import annotations

from agents.rtl_agent.lint import rtl_smoke_check as smoke

SPEC = {
    "entity_name": "demo",
    "clock": {"name": "clk"},
    "reset": {"name": "rstn", "active": "low"},
    "ports": [
        {"name": "clk", "direction": "input", "width": "1", "type": "clock"},
        {"name": "rstn", "direction": "input", "width": "1", "type": "reset"},
        {"name": "d", "direction": "input", "width": "8"},
        {"name": "q", "direction": "output", "width": "8"},
    ],
}


def test_testbench_drives_clock_restart_and_random_inputs_only():
    tb = smoke.build_smoke_testbench(SPEC)
    assert "dut : entity work.demo" in tb
    assert "clk <= not clk after 5 ns;" in tb
    assert "signal rstn : std_logic := '0';" in tb and "rstn <= '1'" in tb   # active low restart, released
    assert "d(k) <= " in tb and "q(k)" not in tb                            # outputs are never driven


def test_a_crash_in_the_dry_run_becomes_an_issue(monkeypatch):
    monkeypatch.setattr(smoke, "mcp_write_files", lambda *a, **k: None)
    monkeypatch.setattr(smoke, "mcp_run_xvhdl", lambda *a, **k: [{"status": "ok"}])
    monkeypatch.setattr(smoke, "simulate", lambda *a, **k: {
        "outcome": "error",
        "sim_output": "## echo ERROR: x\n"
                      "ERROR: Array sizes do not match, left array has 13 elements, right array has 12 elements\n"
                      "Time: 55 ns  Iteration: 1  Process: /smoke_tb/dut/line__3  File: C:/x/src/demo.vhd\n"})
    code = "entity demo is end;\narchitecture rtl of demo is begin\n  process(clk)\n  begin end process;\nend;"
    issues = smoke.smoke_issues(SPEC, code)
    assert issues[0]["code"] == "smoke_sim_crash"
    assert "Array sizes do not match" in issues[0]["detail"] and "## echo" not in issues[0]["detail"]
    # Vivado puts the location on the NEXT line; the LLM needs it to fix the right statement
    assert "[in the process starting at RTL line 3: `process(clk)`]" in issues[0]["detail"]


def test_crash_location_from_a_labelled_process_an_exact_line_and_none():
    out = "ERROR: boom\nTime: 5 ns  Iteration: 0  Process: /smoke_tb/dut/main_pipe  File: x.vhd"
    assert smoke._crash_excerpt(out, "x") == "ERROR: boom [in process `main_pipe`]"
    # newer Vivado names the exact statement on a third line; that beats the process start
    out = ("ERROR: boom\nTime: 5 ns  Iteration: 0  Process: /smoke_tb/dut/line__1  File: /x/src/demo.vhd\n"
           "HDL Line: /x/src/demo.vhd:2")
    assert smoke._crash_excerpt(out, "process(clk)\n  abs_i <= unsigned(-i_s);") == \
        "ERROR: boom [at RTL line 2: `abs_i <= unsigned(-i_s);`]"
    assert smoke._crash_excerpt("FATAL: no location line at all", "x") == "FATAL: no location line at all"


STREAM_SPEC = {
    "entity_name": "llr_demap16",
    "clock": {"name": "aclk"},
    "reset": {"name": "aresetn", "active": "low"},
    "ports": [
        {"name": "aclk", "direction": "input", "width": "1"},
        {"name": "aresetn", "direction": "input", "width": "1"},
        {"name": "s_axis_tvalid", "direction": "input", "width": "1"},
        {"name": "s_axis_tready", "direction": "output", "width": "1"},
        {"name": "s_axis_tdata", "direction": "input", "width": "32"},
        {"name": "m_axis_tvalid", "direction": "output", "width": "1"},
        {"name": "m_axis_tready", "direction": "input", "width": "1"},
        {"name": "m_axis_tdata", "direction": "output", "width": "24"},
    ],
}


def test_testbench_counts_beats_zero_beats_and_stalled_changes_of_an_output_stream():
    tb = smoke.build_smoke_testbench(STREAM_SPEC)
    assert "use ieee.numeric_std.all;" in tb
    assert "if m_axis_tvalid = '1' and m_axis_tready = '1' then" in tb          # a beat = valid and ready
    assert "if is_x(m_axis_tdata) or unsigned(m_axis_tdata) = 0 then zero_beats_0" in tb
    assert "prev_valid_0 = '1' and prev_ready_0 = '0' and m_axis_tdata /= prev_data_0" in tb
    assert 'report "SMOKE_STATS m_axis_tdata beats="' in tb
    assert tb.index("wait until rising_edge(aclk);") < tb.index("if i > 8 then") < tb.index("s_axis_tdata(k) <=")
    assert "SMOKE_STATS" not in smoke.build_smoke_testbench(SPEC)              # no stream, no monitor


def test_a_stream_that_is_zero_on_every_beat_or_moves_while_stalled_is_an_issue(monkeypatch):
    """The 'expected X got 0' runs of 2026-10-29: valid rose one clock before
    the output register was written. With random inputs the dry run sees it
    as every accepted beat carrying 0, long before a real simulation."""
    monkeypatch.setattr(smoke, "mcp_write_files", lambda *a, **k: None)
    monkeypatch.setattr(smoke, "mcp_run_xvhdl", lambda *a, **k: [{"status": "ok"}])
    log = "Note: SMOKE_STATS m_axis_tdata beats=131 zero_beats=131 valid_cycles=140 unstable=0\n"
    monkeypatch.setattr(smoke, "simulate", lambda *a, **k: {"outcome": "no_marker", "sim_output": log})
    issues = smoke.smoke_issues(STREAM_SPEC, "x")
    assert [i["code"] for i in issues] == ["smoke_output_stuck_at_zero"]
    assert "ALL 131 accepted beats" in issues[0]["detail"] and "LAST bit" in issues[0]["detail"]
    # timing_recovery 2026-11-11: the valid count was right and every term was
    # multiplied by mu (0 from reset); the message must not forbid a formula fix
    assert "multiplies EVERY term" in issues[0]["detail"]
    assert "do not change the formulas" not in issues[0]["detail"]

    assert [i["code"] for i in smoke.stream_issues(
        "SMOKE_STATS m_axis_tdata beats=131 zero_beats=0 valid_cycles=140 unstable=3")] == \
        ["smoke_stream_data_changed_while_stalled"]
    assert [i["code"] for i in smoke.stream_issues(
        "SMOKE_STATS m_axis_tdata beats=0 zero_beats=0 valid_cycles=0 unstable=0")] == ["smoke_output_never_valid"]
    # a healthy stream, a few zero beats, or too few beats to judge: nothing to report
    assert not smoke.stream_issues("SMOKE_STATS m_axis_tdata beats=131 zero_beats=2 valid_cycles=140 unstable=0")
    assert not smoke.stream_issues("SMOKE_STATS m_axis_tdata beats=3 zero_beats=3 valid_cycles=3 unstable=0")
    assert not smoke.stream_issues("no stats line at all")


def test_more_output_beats_than_input_beats_is_an_issue():
    """equalizer 2026-11-12 16:57: `advance <= s_axis_tvalid;` moved the valid
    chain only while an input arrived, so it never emptied: 20 inputs gave 108
    output beats and the last 3 results never came out. The dry run now counts
    accepted input beats next to the output beats."""
    tb = smoke.build_smoke_testbench(STREAM_SPEC)
    assert "variable in_beats : integer := 0;" in tb
    assert "if i > 5 and s_axis_tvalid = '1' and s_axis_tready = '1' then in_beats := in_beats + 1; end if;" in tb
    assert "& \" in_beats=\" & integer'image(in_beats) severity note;" in tb
    assert tb.index("in_beats := in_beats + 1") < tb.index("s_axis_tvalid <= '1'")     # sampled before new inputs are driven

    stuck = "SMOKE_STATS m_axis_tdata beats=390 zero_beats=0 valid_cycles=390 unstable=0 in_beats=295"
    [issue] = smoke.stream_issues(stuck, STREAM_SPEC)
    assert issue["code"] == "smoke_more_outputs_than_inputs"
    assert "accepted 295 input beats but raised 390 output beats" in issue["detail"]
    assert "`advance <= '1';`" in issue["detail"] and "Never `advance <= s_axis_tvalid;`" in issue["detail"]
    # one result per input, or fewer (latency, a strobe design): nothing to report
    assert not smoke.stream_issues(stuck.replace("beats=390", "beats=293", 1), STREAM_SPEC)
    # a spec that states its own output rate is judged by the test vectors' beat count
    assert not smoke.stream_issues(stuck, {**STREAM_SPEC, "behavior": ["one output per 4 inputs"]})
    # an old log line without the input count still reads
    assert not smoke.stream_issues("SMOKE_STATS m_axis_tdata beats=390 zero_beats=0 valid_cycles=390 unstable=0", STREAM_SPEC)


def test_clean_run_or_missing_vivado_gives_no_issue(monkeypatch):
    monkeypatch.setattr(smoke, "mcp_write_files", lambda *a, **k: None)
    monkeypatch.setattr(smoke, "mcp_run_xvhdl", lambda *a, **k: [{"status": "ok"}])
    monkeypatch.setattr(smoke, "simulate", lambda *a, **k: {"outcome": "no_marker", "sim_output": ""})
    assert smoke.smoke_issues(SPEC, "x") == []
    monkeypatch.setattr(smoke, "simulate", lambda *a, **k: {"outcome": "error", "error_class": "environment"})
    assert smoke.smoke_issues(SPEC, "x") == []

    def boom(*a, **k):
        raise RuntimeError("no MCP")
    monkeypatch.setattr(smoke, "mcp_run_xvhdl", boom)
    assert smoke.smoke_issues(SPEC, "x") == []


def test_crash_hint_falls_back_to_unsizable_multiplies_in_the_functions_the_process_calls():
    """timing_recovery 14:43 (2026-11-10): `p1 := v2 * mu` with `mu : signed`
    sized differently by two calls matches no width, so the hint lists the
    L-bit targets given a multiply nobody can size, inside the functions the
    crashing process calls, as 'likely here'."""
    from tests.test_rtl_agent import FARROW_UNSIZED_MU
    two = FARROW_UNSIZED_MU.replace("  signal mu_s6 : signed(MU_W-1 downto 0);",
                                    "  signal mu_s6 : signed(MU_W-1 downto 0);\n  signal mu_s5 : signed(19 downto 0);\n"
                                    "  signal y_s6 : signed(I_W-1 downto 0);")
    two = two.replace("  end process p_s7;", "  end process p_s7;\n  p_s6 : process(aclk) begin if rising_edge(aclk) "
                                             "then y_s6 <= farrow(x1_s6, x2_s6, x3_s6, x4_s6, mu_s5); end if; end process;")
    out = ("ERROR: Array sizes do not match, left array has 30 elements, right array has 31 elements\n"
           "Time: 55 ns  Iteration: 1  Process: /smoke_tb/dut/p_s7  File: x.vhd")
    text = smoke._crash_excerpt(out, two)
    assert "[in process `p_s7`]" in text
    assert "likely here: 30-bit target(s) given a multiply whose width cannot be worked out: " \
           "line 24: `p1 := v2 * mu;` (in function `farrow`, called from `p_s7`)" in text
    assert "give it its width (`mu : signed(MU_W-1 downto 0)`)" in text
    # a process that calls no multiplying function gets no guess; a sized input gives the exact match instead
    assert "likely here" not in smoke._crash_excerpt(out.replace("p_s7", "p_x"), two)
    exact = smoke._crash_excerpt(out, FARROW_UNSIZED_MU)
    assert "30-bit target given a 31-bit value; matching assignment(s): line 22: `p1 := v2 * mu;`" in exact

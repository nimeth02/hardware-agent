# tests/test_rtl_smoke_check.py — the RTL agent's short dry run (no Vivado needed).

from __future__ import annotations

from agents.rtl_agent import rtl_smoke_check as smoke

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
        "outcome": "error", "sim_output": "## echo ERROR: x\nERROR: Array sizes do not match, left 8, right 16"})
    issues = smoke.smoke_issues(SPEC, "entity demo is end;")
    assert issues[0]["code"] == "smoke_sim_crash"
    assert "Array sizes do not match" in issues[0]["detail"] and "## echo" not in issues[0]["detail"]


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

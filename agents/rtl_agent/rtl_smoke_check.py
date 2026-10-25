# agents/rtl_agent/rtl_smoke_check.py — a short dry run of generated RTL.
#
# Some mistakes (e.g. a multiplication result stored in a variable of the
# wrong width) compile fine and only stop the simulation once real inputs
# arrive. This module builds a tiny test program from the spec's ports
# (clock, restart, pseudo-random inputs), runs it for a few microseconds
# through the same Vivado helper the simulation agent uses, and reports any
# crash as lint issues. It never decides pass/fail on outputs: it only looks
# for a crash. Skipped silently when Vivado is not reachable.

from __future__ import annotations

import re
import tempfile

from agents.mcp_utils import mcp_run_xvhdl, mcp_write_files
from agents.simulation_agent.simulation_runner import design_std, simulate
from agents.vhdl_version import resolve_vhdl_version

SMOKE_TB = "smoke_tb"
SMOKE_CYCLES = 400
SMOKE_SIM_TIME = "5us"          # 400 cycles of a 10 ns clock = 4 us
_CRASH_LINE_RE = re.compile(r"ERROR|FATAL|Failure|mismatch|do not match", re.IGNORECASE)


def _vhdl_type(port: dict) -> str:
    width = int(port.get("width") or 1)
    return "std_logic" if width == 1 else f"std_logic_vector({width - 1} downto 0)"


def _port_role(port: dict, spec: dict) -> str:
    """clock | reset | input | output"""
    name = port.get("name")
    if name == (spec.get("clock") or {}).get("name") or port.get("type") == "clock":
        return "clock"
    if name == (spec.get("reset") or {}).get("name") or port.get("type") == "reset":
        return "reset"
    return "input" if str(port.get("direction", "")).lower().startswith("in") else "output"


def build_smoke_testbench(spec: dict) -> str:
    """VHDL-2008 test program: clock, a few cycles of restart, then pseudo-random
    values on every other input for SMOKE_CYCLES cycles. No checking."""
    ports = [p for p in spec.get("ports") or [] if p.get("name")]
    # The spec may list the clock and reset only under "clock" / "reset", not in
    # "ports"; the RTL still has them as input wires, so connect them too.
    for key, port_type in (("clock", "clock"), ("reset", "reset")):
        name = (spec.get(key) or {}).get("name")
        if name and not any(p["name"] == name for p in ports):
            ports.insert(0, {"name": name, "direction": "input", "width": "1", "type": port_type})
    roles = {p["name"]: _port_role(p, spec) for p in ports}
    clock = next((n for n, r in roles.items() if r == "clock"), None)
    reset = next((n for n, r in roles.items() if r == "reset"), None)
    active_low = str((spec.get("reset") or {}).get("active", "low")).lower() != "high"
    rst_on, rst_off = ("'0'", "'1'") if active_low else ("'1'", "'0'")
    clk = clock or "smoke_clk"

    decls = [f"  signal {p['name']} : {_vhdl_type(p)}"
             + (f" := {rst_on}" if p["name"] == reset else
                " := '0'" if p["name"] == clock else
                " := (others => '0')" if roles[p["name"]] == "input" and int(p.get("width") or 1) > 1 else
                " := '0'" if roles[p["name"]] == "input" else "") + ";"
             for p in ports]
    if not clock:
        decls.append("  signal smoke_clk : std_logic := '0';")

    drives = []
    for p in ports:
        if roles[p["name"]] != "input":
            continue
        name = p["name"]
        if int(p.get("width") or 1) == 1:
            drives.append(f"      seed := (seed * 75) mod 65537;\n"
                          f"      if seed mod 4 /= 0 then {name} <= '1'; else {name} <= '0'; end if;")
        else:
            drives.append(f"      for k in {name}'range loop\n"
                          f"        seed := (seed * 75) mod 65537;\n"
                          f"        if seed mod 2 = 1 then {name}(k) <= '1'; else {name}(k) <= '0'; end if;\n"
                          f"      end loop;")
    release = f"      if i = 5 then {reset} <= {rst_off}; end if;\n" if reset else ""
    maps = ",\n      ".join(f"{p['name']} => {p['name']}" for p in ports)
    entity = spec.get("entity_name") or "design"
    return (
        "library ieee;\nuse ieee.std_logic_1164.all;\n\n"
        f"entity {SMOKE_TB} is\nend entity {SMOKE_TB};\n\n"
        f"architecture sim of {SMOKE_TB} is\n" + "\n".join(decls) + "\nbegin\n"
        f"  dut : entity work.{entity}\n    port map (\n      {maps}\n    );\n\n"
        f"  {clk} <= not {clk} after 5 ns;\n\n"
        "  stim : process\n    variable seed : integer := 7;\n  begin\n"
        f"    for i in 0 to {SMOKE_CYCLES - 1} loop\n"
        f"      wait until rising_edge({clk});\n"
        + "\n".join(drives) + ("\n" if drives else "")
        + release + "    end loop;\n    wait;\n  end process stim;\n"
        "end architecture sim;\n"
    )


def _crash_excerpt(output: str, limit: int = 4) -> str:
    # xsim echoes its own script as "## ..." lines; those are not the crash
    lines = [ln.strip()[:200] for ln in output.splitlines()
             if _CRASH_LINE_RE.search(ln) and not ln.lstrip().startswith("##")]
    return " | ".join(lines[:limit]) or output.strip()[-300:]


def smoke_issues(spec: dict, code: str) -> list[dict]:
    """[] when the RTL survives the dry run (or Vivado is unavailable);
    otherwise one {code, detail} issue describing the crash."""
    entity = spec.get("entity_name")
    if not entity or not code.strip():
        return []
    src = f"src/{entity}.vhd"
    tb = f"tb/{SMOKE_TB}.vhd"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            mcp_write_files(tmp, {src: code, tb: build_smoke_testbench(spec)})
            std = design_std({"spec": spec})
            compiled = mcp_run_xvhdl(tmp, [(src, std)])
            if not compiled or compiled[0].get("status") != "ok":
                return []           # compile problems are reported by the normal compile step
            sim = simulate({"dir": tmp, "tb_file": tb, "top_entity": SMOKE_TB, "spec": spec}, SMOKE_SIM_TIME)
    except Exception:               # Vivado / MCP not reachable: skip, like the compile step does
        return []
    if sim.get("error_class") == "environment" or sim.get("outcome") != "error":
        return []
    output = sim.get("sim_output") or ""
    return [{"code": "smoke_sim_crash",
             "detail": "The RTL compiles but crashes in a short dry run with random inputs "
                       f"(first problem at the top): {_crash_excerpt(output)}"}]

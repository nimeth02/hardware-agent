# agents/simulation_agent/simulation_runner.py — deterministic tools over the
# Vivado MCP server: compile the design sources (run_xvhdl) and run the
# testbench (run_simulation). No LLM involved.

from __future__ import annotations

import re

from langchain_mcp_adapters.tools import load_mcp_tools

from agents.mcp_utils import mcp_run_xvhdl, run_sync, tool_json
from agents.vhdl_version import resolve_vhdl_version, xvhdl_std
from services.mcp_client import get_mcp_client

_SIM_TIME_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*$")
_ERROR_LINE_RE = re.compile(r"^ERROR:", re.MULTILINE)


def design_std(project: dict) -> str:
    """The VHDL edition the project's design files were written in, read
    from its spec.json (agents/vhdl_version.py decides the default)."""
    return xvhdl_std(resolve_vhdl_version(project.get("spec")))


def compile_design(project: dict) -> dict:
    """xvhdl every src file (in the spec's VHDL edition) into the project's
    work library, in listing order. {status, results, error_class}."""
    std = design_std(project)
    results = mcp_run_xvhdl(project["dir"], [(f, std) for f in project["src_files"]])
    ok = bool(results) and all(r.get("status") == "ok" for r in results)
    return {
        "status": "ok" if ok else "error",
        "results": results,
        "error_class": next((r.get("error_class") for r in results if r.get("error_class")), None),
    }


def outcome_of(sim: dict) -> str:
    """passed | failed | error | no_marker — from the markers the testbench
    prints and the tool's own failure reports."""
    output = sim.get("sim_output") or ""
    # sim_failed is read by the Vivado server on the FULL log; the capped
    # sim_output may have lost the marker (older servers cut the end off).
    if sim.get("sim_failed") or "SIMULATION FAILED" in output:
        return "failed"
    if sim.get("sim_passed") and sim.get("status") == "ok":
        return "passed"
    if "SIMULATION PASSED" in output:
        return "error"          # passed marker but the tool still flagged an error
    # "ERROR:" counts only at the start of a line: xsim echoes the TCL script
    # (which mentions ERROR: in its catch handler) as "## ..." lines.
    if (sim.get("error_class") or _ERROR_LINE_RE.search(output) or output.lstrip().startswith("[")
            or "timed out" in output or "not found" in output or "not available" in output):
        return "error"
    return "no_marker"          # ran to sim_time without printing a verdict


async def _asimulate(project: dict, sim_time: str) -> dict:
    client = get_mcp_client()
    async with client.session("vivado") as session:
        run_simulation = {t.name: t for t in await load_mcp_tools(session)}["run_simulation"]
        return tool_json(await run_simulation.ainvoke({
            "tb_file": project["tb_file"], "top_entity": project["top_entity"],
            "project_dir": project["dir"], "sim_time": sim_time,
            "src_vhdl_std": design_std(project),
        }))


def simulate(project: dict, sim_time: str) -> dict:
    """run_simulation result + {sim_time, outcome}."""
    sim = run_sync(_asimulate(project, sim_time))
    sim["sim_time"] = sim_time
    sim["outcome"] = outcome_of(sim)
    return sim


_TO_NS = {"ps": 1e-3, "ns": 1.0, "us": 1e3, "ms": 1e6}
# "constant clk_period : time := 10 ns;" in the testbench (the period the dry run really uses)
_CLK_PERIOD_RE = re.compile(r"constant\s+\w*(?:clk|clock)\w*period\w*\s*:\s*time\s*:=\s*(\d+(?:\.\d+)?)\s*(ps|ns|us|ms)\b", re.I)
DEFAULT_CLK_PERIOD_NS = 10.0          # 100 MHz when neither the testbench nor the spec says
SIM_TIME_MARGIN = 1.2                 # room for the pipeline to drain and the testbench to finish
SIM_TIME_EXTRA_CYCLES = 100


def _clk_period_ns(project: dict) -> float:
    """The testbench's clock period, else the spec's frequency, else 10 ns."""
    m = _CLK_PERIOD_RE.search(project.get("testbench_code") or "")
    if m:
        return float(m.group(1)) * _TO_NS[m.group(2).lower()]
    clock = (project.get("spec") or {}).get("clock") or {}
    try:
        mhz = float(clock.get("frequency_mhz") or 0)
    except (TypeError, ValueError):
        mhz = 0
    return 1000.0 / mhz if mhz > 0 else DEFAULT_CLK_PERIOD_NS


def _format_ns(ns: float) -> str:
    ns = int(ns + 0.999999)
    return f"{ns // 1000}us" if ns % 1000 == 0 else f"{ns}ns"


def sim_time_for(project: dict, requested: str) -> str:
    """The longer of `requested` and what the test vectors need: one clock
    per CSV row, plus a 20% margin and 100 extra clocks. timing_recovery,
    2026-11-05 19:02: the vectors were regenerated with 24,000 rows (240 us
    at 10 ns), the agent ran 10/20/40 us, the testbench never reached its
    marker and the silence was read as a frozen handshake three times."""
    csv_text = project.get("test_vectors_in_csv")
    m = _SIM_TIME_RE.match(requested or "")
    if not isinstance(csv_text, str) or not csv_text.strip() or not m or m.group(2).lower() not in _TO_NS:
        return requested
    rows = max(0, len(csv_text.strip().splitlines()) - 1)
    if rows == 0:
        return requested
    needed_ns = (rows * SIM_TIME_MARGIN + SIM_TIME_EXTRA_CYCLES) * _clk_period_ns(project)
    requested_ns = float(m.group(1)) * _TO_NS[m.group(2).lower()]
    return _format_ns(needed_ns) if needed_ns > requested_ns else requested


def double_sim_time(sim_time: str) -> str:
    """'10us' -> '20us'; unparsable values are returned unchanged."""
    m = _SIM_TIME_RE.match(sim_time or "")
    if not m:
        return sim_time
    value = float(m.group(1)) * 2
    # Plain decimal only: f"{8e-05}" would give "8e-05us", which xsim's
    # `run` rejects and which then hangs xsim at its prompt.
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    return f"{text or '0'}{m.group(2)}"

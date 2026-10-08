# agents/simulation_agent/simulation_runner.py — deterministic tools over the
# Vivado MCP server: compile the design sources (run_xvhdl) and run the
# testbench (run_simulation). No LLM involved.

from __future__ import annotations

import re

from langchain_mcp_adapters.tools import load_mcp_tools

from agents.mcp_utils import mcp_run_xvhdl, run_sync, tool_json
from services.mcp_client import get_mcp_client

_SIM_TIME_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*$")


def compile_design(project: dict) -> dict:
    """xvhdl every src file (VHDL-93) into the project's work library, in
    listing order. {status, results, error_class}."""
    results = mcp_run_xvhdl(project["dir"], [(f, "93") for f in project["src_files"]])
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
    if "SIMULATION FAILED" in output:
        return "failed"
    if sim.get("sim_passed") and sim.get("status") == "ok":
        return "passed"
    if "SIMULATION PASSED" in output:
        return "error"          # passed marker but the tool still flagged an error
    if (sim.get("error_class") or "ERROR:" in output or output.lstrip().startswith("[")
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
        }))


def simulate(project: dict, sim_time: str) -> dict:
    """run_simulation result + {sim_time, outcome}."""
    sim = run_sync(_asimulate(project, sim_time))
    sim["sim_time"] = sim_time
    sim["outcome"] = outcome_of(sim)
    return sim


def double_sim_time(sim_time: str) -> str:
    """'10us' -> '20us'; unparsable values are returned unchanged."""
    m = _SIM_TIME_RE.match(sim_time or "")
    if not m:
        return sim_time
    value = float(m.group(1)) * 2
    return f"{int(value) if value.is_integer() else value}{m.group(2)}"

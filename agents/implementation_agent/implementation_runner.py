# agents/implementation_agent/implementation_runner.py — deterministic tools:
# run the synth -> route TCL through the Vivado MCP server (run_vivado_batch)
# and parse the reports it leaves in impl_out/ (read via the file-server
# MCP). No LLM involved.

from __future__ import annotations

import re

from langchain_mcp_adapters.tools import load_mcp_tools

from agents.implementation_agent.state import BATCH_TIMEOUT_S
from agents.mcp_utils import mcp_read_files, run_sync, tool_json
from services.mcp_client import get_mcp_client

REPORTS_DIR = "impl_out"
_NUM = r"(-?\d+(?:\.\d+)?)"


async def _arun_batch(project: dict, scripts: dict) -> dict:
    client = get_mcp_client()
    async with client.session("vivado") as session:
        run_vivado_batch = {t.name: t for t in await load_mcp_tools(session)}["run_vivado_batch"]
        return tool_json(await run_vivado_batch.ainvoke(
            {"tcl_file": scripts["tcl"], "project_dir": project["dir"], "timeout": BATCH_TIMEOUT_S}))


def run_batch(project: dict, scripts: dict) -> dict:
    """run_vivado_batch result: {status, journal, log_excerpt, reports_dir, error_class}."""
    return run_sync(_arun_batch(project, scripts))


# ── report parsing ────────────────────────────────────────────────────────

def parse_timing_summary(text: str) -> dict:
    """WNS/TNS/WHS and failing endpoints from report_timing_summary."""
    out: dict = {"wns_ns": None, "tns_ns": None, "whs_ns": None, "failing_endpoints": None}
    m = re.search(r"Design Timing Summary.*?WNS\(ns\)\s+TNS\(ns\).*?\n\s*-+.*?\n\s*" + r"\s+".join([_NUM] * 4), text, re.S)
    if m:
        out["wns_ns"], out["tns_ns"] = float(m.group(1)), float(m.group(2))
        out["failing_endpoints"] = int(float(m.group(3)))
        # columns: WNS TNS TNS-failing-endpoints TNS-total-endpoints WHS ...
        m2 = re.search(r"WNS\(ns\)\s+TNS\(ns\).*?\n\s*-+.*?\n\s*" + r"\s+".join([_NUM] * 5), text, re.S)
        if m2:
            out["whs_ns"] = float(m2.group(5))
    elif re.search(r"There are no user specified timing constraints", text):
        out["unconstrained"] = True
    return out


def parse_utilization(text: str) -> dict:
    """Used / % from the report_utilization tables (7-series row names)."""
    def row(name: str) -> tuple[float | None, float | None]:
        # Util% is printed as "<0.01" for tiny designs -- accept the "<".
        m = re.search(rf"^\|\s*{name}[^|]*\|\s*{_NUM}\s*\|[^|]*\|\s*{_NUM}\s*\|\s*<?\s*{_NUM}\s*\|", text, re.M)
        return (float(m.group(1)), float(m.group(3))) if m else (None, None)

    lut, lut_pct = row(r"Slice LUTs\*?")
    ff, ff_pct = row(r"Slice Registers")
    bram, bram_pct = row(r"Block RAM Tile")
    dsp, dsp_pct = row(r"DSPs")
    return {
        "lut_used": lut, "lut_pct": lut_pct, "ff_used": ff, "ff_pct": ff_pct,
        "bram_used": bram, "bram_pct": bram_pct, "dsp_used": dsp, "dsp_pct": dsp_pct,
    }


def metrics_from_reports(reports: dict[str, str]) -> dict:
    """{report name: text} -> metrics; {} when there are no reports."""
    metrics: dict = {}
    if "timing_summary.rpt" in reports:
        metrics.update(parse_timing_summary(reports["timing_summary.rpt"]))
    if "utilization.rpt" in reports:
        metrics.update(parse_utilization(reports["utilization.rpt"]))
    if reports:
        metrics["reports"] = sorted(reports)
    return metrics


def collect_metrics(project: dict) -> dict:
    """Read impl_out/*.rpt through the file-server MCP and parse them."""
    return metrics_from_reports(mcp_read_files(project["dir"], REPORTS_DIR, ".rpt"))

# agents/implementation_agent/script_writer.py — tool: the clock constraint
# (XDC, from the spec) and the non-project synth -> route TCL, written into
# the project through the file-server MCP. Deterministic, no LLM.

from __future__ import annotations

from agents.implementation_agent.state import DEFAULT_CLOCK_MHZ
from agents.mcp_utils import mcp_write_files

TCL_FILE = "scripts/synth_impl.tcl"


def make_xdc(spec: dict | None) -> str:
    """One create_clock from the spec; a comment only when there is no clock."""
    clock = (spec or {}).get("clock") or {}
    if not clock.get("required") or not clock.get("name"):
        return "# no clock in spec -- timing not constrained\n"
    mhz = clock.get("frequency_mhz") or DEFAULT_CLOCK_MHZ
    period = 1000.0 / float(mhz)
    return (
        f"# {mhz} MHz clock ({'from spec' if clock.get('frequency_mhz') else 'default'})\n"
        f"create_clock -period {period:.3f} -name {clock['name']} [get_ports {clock['name']}]\n"
    )


def make_tcl(entity: str, src_files: list[str], xdc_file: str, part: str) -> str:
    # Non-project batch flow. Only write_* / file mkdir are used -- the MCP
    # server rejects any TCL containing file delete/rename or exec rm.
    reads = "".join(f"read_vhdl {{{f}}}\n" for f in src_files)
    return (
        f"{reads}"
        f"read_xdc {{{xdc_file}}}\n"
        f"synth_design -top {entity} -part {part}\n"
        "opt_design\n"
        "place_design\n"
        "route_design\n"
        "file mkdir impl_out\n"
        "report_utilization -file impl_out/utilization.rpt\n"
        "report_timing_summary -file impl_out/timing_summary.rpt\n"
        "write_checkpoint -force impl_out/post_route.dcp\n"
    )


def plan_scripts(project: dict, part: str) -> dict:
    """What to write, without writing it: {files: {rel path: content},
    xdc, tcl, constraints, part, generated_xdc}. An XDC already in the
    project is used as-is and never overwritten."""
    xdc_file = project.get("xdc_file")
    generated = xdc_file is None
    files: dict[str, str] = {}
    constraints = None
    if generated:
        xdc_file = f"constraints/{project['top_entity']}.xdc"
        constraints = make_xdc(project.get("spec"))
        files[xdc_file] = constraints
    files[TCL_FILE] = make_tcl(project["top_entity"], project["src_files"], xdc_file, part)
    return {"files": files, "xdc": xdc_file, "tcl": TCL_FILE, "constraints": constraints,
            "part": part, "generated_xdc": generated}


def write_scripts(project: dict, part: str) -> dict:
    plan = plan_scripts(project, part)
    mcp_write_files(project["dir"], plan.pop("files"))
    return plan

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
from agents.rtl_agent.rtl_widths import assignments, declared_widths, expr_width, source_line
from agents.simulation_agent.simulation_runner import design_std, simulate
from agents.vhdl_version import resolve_vhdl_version

SMOKE_TB = "smoke_tb"
SMOKE_CYCLES = 400
SMOKE_SIM_TIME = "5us"          # 400 cycles of a 10 ns clock = 4 us
_CRASH_LINE_RE = re.compile(r"ERROR|FATAL|Failure|mismatch|do not match", re.IGNORECASE)
_PROCESS_RE = re.compile(r"Process:\s*(\S+)")      # xsim's location line: "Time: 55 ns  Iteration: 1  Process: /smoke_tb/dut/line__142  File: ..."
_LINE_LABEL_RE = re.compile(r"^line__(\d+)$")      # xsim names an unlabelled process after the RTL line it starts on
_HDL_LINE_RE = re.compile(r"HDL Line:\s*\S+?:(\d+)")  # newer xsim adds the exact statement: "HDL Line: /x/src/demo.vhd:142"


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


def _streams(ports: list[dict], roles: dict[str, str]) -> list[dict]:
    """Output streams: an output `<p>tvalid` with an output `<p>tdata`, plus
    the input `<p>tready` when there is one (AXI-Stream names)."""
    by_name = {p["name"]: p for p in ports}
    found = []
    for p in ports:
        name = p["name"]
        if roles[name] != "output" or not name.lower().endswith("tvalid"):
            continue
        prefix = name[:-len("tvalid")]
        data = by_name.get(prefix + "tdata")
        if not data or roles[data["name"]] != "output":
            continue
        ready = by_name.get(prefix + "tready")
        found.append({"valid": name, "data": data["name"], "width": int(data.get("width") or 1),
                      "ready": ready["name"] if ready and roles[ready["name"]] == "input" else None})
    return found


def _stream_monitor(s: dict, idx: int) -> tuple[str, str, str]:
    """(variable declarations, per-clock sampling, final report) for one stream.
    Sampled right after the clock edge, before new inputs are driven, so the
    values are the ones the edge consumed. Counts: beats (valid and ready
    both high), zero_beats (data all 0 or still 'U'/'X' on such a beat),
    valid_cycles, unstable (data changed although valid was high and ready
    low at the previous edge, which AXI-Stream forbids)."""
    v, d, r, w = s["valid"], s["data"], s["ready"], s["width"]
    ready = f"{r} = '1'" if r else "true"
    ready_now = r if r else "'1'"                       # no ready port: every valid beat is consumed
    zero = f"is_x({d}) or unsigned({d}) = 0" if w > 1 else f"{d} /= '1'"
    dtype = f"std_logic_vector({w - 1} downto 0)" if w > 1 else "std_logic"
    decls = (f"    variable beats_{idx}, zero_beats_{idx}, valid_cycles_{idx}, unstable_{idx} : integer := 0;\n"
             f"    variable prev_valid_{idx}, prev_ready_{idx} : std_logic := '0';\n"
             f"    variable prev_data_{idx} : {dtype};\n")
    sample = (f"      if i > 8 then\n"
              f"        if {v} = '1' then valid_cycles_{idx} := valid_cycles_{idx} + 1; end if;\n"
              f"        if {v} = '1' and {ready} then\n"
              f"          beats_{idx} := beats_{idx} + 1;\n"
              f"          if {zero} then zero_beats_{idx} := zero_beats_{idx} + 1; end if;\n"
              f"        end if;\n"
              f"        if prev_valid_{idx} = '1' and prev_ready_{idx} = '0' and {d} /= prev_data_{idx} then\n"
              f"          unstable_{idx} := unstable_{idx} + 1;\n"
              f"        end if;\n"
              f"      end if;\n"
              f"      prev_valid_{idx} := {v}; prev_ready_{idx} := {ready_now}; prev_data_{idx} := {d};\n")
    report = (f"    report \"SMOKE_STATS {d} beats=\" & integer'image(beats_{idx})\n"
              f"      & \" zero_beats=\" & integer'image(zero_beats_{idx})\n"
              f"      & \" valid_cycles=\" & integer'image(valid_cycles_{idx})\n"
              f"      & \" unstable=\" & integer'image(unstable_{idx}) severity note;\n")
    return decls, sample, report


def build_smoke_testbench(spec: dict) -> str:
    """VHDL-2008 test program: clock, a few cycles of restart, then pseudo-random
    values on every other input for SMOKE_CYCLES cycles. No pass/fail checking
    of values; for every output stream it counts beats, all-zero beats and
    data changes during a stall, and prints them as one SMOKE_STATS line."""
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
    monitors = [_stream_monitor(s, i) for i, s in enumerate(_streams(ports, roles))]
    maps = ",\n      ".join(f"{p['name']} => {p['name']}" for p in ports)
    entity = spec.get("entity_name") or "design"
    return (
        "library ieee;\nuse ieee.std_logic_1164.all;\nuse ieee.numeric_std.all;\n\n"
        f"entity {SMOKE_TB} is\nend entity {SMOKE_TB};\n\n"
        f"architecture sim of {SMOKE_TB} is\n" + "\n".join(decls) + "\nbegin\n"
        f"  dut : entity work.{entity}\n    port map (\n      {maps}\n    );\n\n"
        f"  {clk} <= not {clk} after 5 ns;\n\n"
        "  stim : process\n    variable seed : integer := 7;\n"
        + "".join(m[0] for m in monitors) + "  begin\n"
        f"    for i in 0 to {SMOKE_CYCLES - 1} loop\n"
        f"      wait until rising_edge({clk});\n"
        + "".join(m[1] for m in monitors)
        + "\n".join(drives) + ("\n" if drives else "")
        + release + "    end loop;\n"
        + "".join(m[2] for m in monitors)
        + "    wait;\n  end process stim;\n"
        "end architecture sim;\n"
    )


# "SMOKE_STATS m_axis_tdata beats=131 zero_beats=131 valid_cycles=140 unstable=0"
_STATS_RE = re.compile(r"SMOKE_STATS\s+(\w+)\s+beats=(\d+)\s+zero_beats=(\d+)\s+valid_cycles=(\d+)\s+unstable=(\d+)")
MIN_BEATS_FOR_ZERO_VERDICT = 8


def stream_issues(output: str) -> list[dict]:
    """Issues read from the SMOKE_STATS lines of a dry run that did not crash.
    Random inputs give random outputs, so a stream whose data is 0 (or 'U')
    on EVERY accepted beat has its valid flag out of step with its data (the
    llr_demap16 'expected X got 0' runs of 2026-10-29); data that changes
    while valid is high and ready low breaks the AXI-Stream hold rule; a
    valid that never rises is a dead pipeline."""
    issues = []
    for data, beats, zero, valid_cycles, unstable in _STATS_RE.findall(output or ""):
        beats, zero, valid_cycles, unstable = int(beats), int(zero), int(valid_cycles), int(unstable)
        if valid_cycles == 0:
            issues.append({"code": "smoke_output_never_valid",
                           "detail": f"In a {SMOKE_CYCLES}-cycle dry run with random inputs (valid on 3 of 4 cycles, "
                                     f"reset released after 5 cycles) the valid flag for '{data}' never rose. The "
                                     f"valid chain is never loaded or is held in reset: check that valid_pipe(0) "
                                     f"<= s_axis_tvalid runs under advance = '1', that advance can be '1' after "
                                     f"reset (output empty), and that the reset polarity matches the spec."})
            continue
        if beats >= MIN_BEATS_FOR_ZERO_VERDICT and zero == beats:
            issues.append({"code": "smoke_output_stuck_at_zero",
                           "detail": f"In a dry run with random inputs, '{data}' was 0 (or still undefined) on ALL "
                                     f"{beats} accepted beats while its valid flag was high. Random inputs cannot "
                                     f"give all-zero results, so the valid flag is out of step with the data: valid "
                                     f"rises before the register that drives '{data}' is written. Count the "
                                     f"registers from the input to '{data}' (an output register written inside the "
                                     f"process counts as one more stage): valid_pipe must have exactly that many "
                                     f"bits, the output valid flag must come from its LAST bit, and the output "
                                     f"register must be written under the same advance = '1' condition as that bit. "
                                     f"This is not an arithmetic problem; do not change the formulas."})
        if unstable:
            issues.append({"code": "smoke_stream_data_changed_while_stalled",
                           "detail": f"In a dry run, '{data}' changed {unstable} time(s) while its valid flag was "
                                     f"high and ready was low. AXI-Stream requires the data to hold until the beat "
                                     f"is accepted: every register, including the output register, may be written "
                                     f"only when advance = '1' (output ready or empty)."})
    return issues


def _source_line(code: str, n: int) -> str:
    src = code.splitlines()
    return src[n - 1].strip() if 0 < n <= len(src) else ""


def _where(location: str, code: str) -> str:
    """Where in the RTL a crash happened, from xsim's location text:
    '[at RTL line N: `<that line>`]' when xsim names the statement,
    '[in the process starting at RTL line N: `<that line>`]' from an
    unlabelled process's name, '[in process `label`]' for a labelled one,
    '' when the text names neither."""
    exact = _HDL_LINE_RE.search(location)
    if exact:
        n = int(exact.group(1))
        text = _source_line(code, n)
        return f"[at RTL line {n}" + (f": `{text}`]" if text else "]")
    m = _PROCESS_RE.search(location)
    if not m:
        return ""
    label = m.group(1).rsplit("/", 1)[-1]
    by_line = _LINE_LABEL_RE.match(label)
    if not by_line:
        return f"[in process `{label}`]"
    n = int(by_line.group(1))
    text = _source_line(code, n)
    return f"[in the process starting at RTL line {n}" + (f": `{text}`]" if text else "]")


_SIZES_RE = re.compile(r"left array has (\d+) elements?, right array has (\d+) elements?", re.IGNORECASE)


def _width_mismatch_hint(message: str, code: str) -> str:
    """For 'Array sizes do not match, left array has L elements, right array
    has R elements': the assignment lines whose target is L bits wide and
    whose right-hand side works out to R bits. xsim 2020.1 names only the
    process, and a function called from it (calc_mmsb) is where the fault
    sat for three attempts on 2026-10-29."""
    m = _SIZES_RE.search(message)
    if not m or not code:
        return ""
    left, right = int(m.group(1)), int(m.group(2))
    widths = declared_widths(code)
    hits = [(n, target) for n, target, _, expr in assignments(code)
            if widths.get(target.lower()) == left and expr_width(expr, widths) == right]
    if not hits:
        return ""
    where = "; ".join(f"line {n}: `{source_line(code, n)}`" for n, _ in hits[:3])
    return (f"[{left}-bit target given a {right}-bit value; matching assignment(s): {where}. "
            f"A product is as wide as both factors added (x * 4 doubles x's width): compute at the natural "
            f"width, then resize]")


def _crash_excerpt(output: str, code: str = "", limit: int = 4) -> str:
    """The first crash messages, each with where in the RTL it happened.
    xsim prints the location on the line AFTER the message; without it the
    LLM has guessed the wrong statement (observed: run T-16.48, a 12-bit
    `-x` stored in a 13-bit signal was blamed on a multiply three stages
    later, and two fix attempts changed nothing)."""
    # xsim echoes its own script as "## ..." lines; those are not the crash
    lines = [ln.strip() for ln in output.splitlines() if not ln.lstrip().startswith("##")]
    found: list[str] = []
    for i, line in enumerate(lines):
        if not _CRASH_LINE_RE.search(line):
            continue
        # xsim: message, then "Time: ... Process: ... File: ...", then (newer
        # versions) "HDL Line: file:N"; look at the message and the two lines after
        following = " ".join(ln for ln in lines[i + 1:i + 3] if not _CRASH_LINE_RE.search(ln))
        where = _where(following, code) or _where(line, code)
        hint = _width_mismatch_hint(line, code)
        found.append(line[:200] + (" " + where if where else "") + (" " + hint if hint else ""))
        if len(found) == limit:
            break
    return " | ".join(found) or output.strip()[-300:]


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
    if sim.get("error_class") == "environment":
        return []
    output = sim.get("sim_output") or ""
    if sim.get("outcome") != "error":
        return stream_issues(output)
    return [{"code": "smoke_sim_crash",
             "detail": "The RTL compiles but crashes in a short dry run with random inputs "
                       f"(first problem at the top): {_crash_excerpt(output, code)}"}]

# mcp_servers/vivado_server.py — MCP server for Vivado tool invocation
#
# Supports two execution modes, selected automatically at startup:
#
#   LOCAL  — Vivado binaries are on the local machine (or in PATH after
#             sourcing settings64.sh).  Uses subprocess, same as before.
#
#   REMOTE — VIVADO_REMOTE_HOST is set in .env.  Project files are
#             uploaded via SFTP; xvhdl/xelab/xsim are executed over SSH.
#             No local Vivado installation required.
#
# Environment setup happens once at server startup (module import time),
# not per-call — sourcing settings64.sh / SSH connect check is too slow
# to repeat on every tool invocation.

import re
import sys
import subprocess
import tempfile
from pathlib import Path
from mcp.server.fastmcp import FastMCP


sys.path.insert(0, str(Path(__file__).resolve().parent))
from vivado_env import (
    ensure_vivado_env, find_vivado_bin, run_vivado_cmd,
    is_remote_mode, ensure_remote_vivado_env,
    _make_ssh_client, _remote_run, _detect_remote_settings64,
)

mcp = FastMCP("vivado-server")

# ── Failure classification ───────────────────────────────────────────
# Lets callers (the design subagent's routing, agent/nodes.py) tell an
# environment/licensing problem apart from an ordinary design/compile
# error — retrying a license failure can never succeed, so it shouldn't
# be treated the same as a fixable RTL/TCL mistake worth another attempt.
_LICENSE_ERROR_MARKERS = (
    "valid license was not found",
    "failed to get the license",
    "no license found",
    "license checkout failed",
    "[common 17-345]",
    "[common 17-348]",
)


def _classify_failure(text: str) -> str | None:
    """Best-effort classification of a Vivado failure. Returns "license"
    for a recognized license/entitlement error, else None (including for
    ordinary compile/design errors — those ARE worth retrying)."""
    lowered = text.lower()
    if any(marker in lowered for marker in _LICENSE_ERROR_MARKERS):
        return "license"
    return None

# ── Environment setup at import time ───────────────────────────────────
# Print to stderr, not stdout — stdout is the MCP JSON-RPC pipe.
if is_remote_mode():
    _env_result = ensure_remote_vivado_env()
    if _env_result["ok"]:
        print(
            f"[vivado_server] REMOTE mode — host: {_env_result['host']} "
            f"settings64: {_env_result['settings64']}",
            file=sys.stderr,
        )
    else:
        print(
            f"[vivado_server] WARNING: Remote Vivado unavailable — {_env_result['reason']}",
            file=sys.stderr,
        )
else:
    _env_result = ensure_vivado_env()
    if _env_result["ok"]:
        print(f"[vivado_server] LOCAL mode — Vivado: {_env_result['vivado']}", file=sys.stderr)
    else:
        print(
            f"[vivado_server] WARNING: Vivado not available — {_env_result['reason']}",
            file=sys.stderr,
        )


# Vivado/xvhdl prefixes all diagnostic errors with "ERROR:" — used to count
# real failures distinctly from warnings ("WARNING:") or info messages.
_ERROR_LINE_RE = re.compile(r'^ERROR:', re.MULTILINE)


def _cap(text: str, limit: int = 4000) -> str:
    """Cap output at limit chars — xvhdl logs can be long."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars] ..."


# ── Remote execution helpers ────────────────────────────────────────────

def _remote_xvhdl(file: str, project_dir: str, vhdl_std: str) -> dict:
    """
    Upload a single VHDL file via SFTP and run xvhdl on the remote host.
    Uses a fresh tmpdir per call — stateless, safe for concurrent use.
    """
    target_file = Path(project_dir) / file
    if not target_file.is_file():
        return {"status": "error", "stdout": "", "stderr": f"File not found: {file}", "errors_found": -1}

    try:
        client = _make_ssh_client()
    except Exception as e:
        return {"status": "error", "stdout": "", "stderr": f"SSH connect failed: {e}", "errors_found": -1}

    try:
        settings = _detect_remote_settings64(client)
        if not settings:
            return {"status": "error", "stdout": "", "stderr": "No settings64.sh on remote host", "errors_found": -1}

        # Create remote tmpdir
        out, _, _ = _remote_run(client, "mktemp -d /tmp/vivado_xvhdl_XXXX")
        tmpdir = out.strip()

        # Upload file preserving subdirectory (e.g. src/ or tb/)
        sftp = client.open_sftp()
        remote_file = f"{tmpdir}/{Path(file).name}"
        sftp.put(str(target_file), remote_file)
        sftp.close()

        std_flag = "--2008" if vhdl_std == "2008" else ""
        cmd = f"source {settings} && mkdir -p {tmpdir}/work && cd {tmpdir}/work && xvhdl {std_flag} {remote_file} 2>&1"
        stdout, stderr, exit_code = _remote_run(client, cmd, timeout=120)

        _remote_run(client, f"rm -rf {tmpdir}")
    finally:
        client.close()

    errors_found = len(_ERROR_LINE_RE.findall(stdout)) + len(_ERROR_LINE_RE.findall(stderr))
    return {
        "status": "ok" if (exit_code == 0 and errors_found == 0) else "error",
        "stdout": _cap(stdout),
        "stderr": _cap(stderr),
        "errors_found": errors_found,
    }


def _remote_simulation(tb_file: str, top_entity: str, project_dir: str, sim_time: str) -> dict:
    """
    Full remote simulation pipeline: upload src/* + tb file, xvhdl x2,
    xelab, xsim. Compiles all .vhd files in src/ so the design library
    is fully populated before elaboration — no dependency on prior xvhdl calls.
    """
    project_root = Path(project_dir)
    tb_path = project_root / tb_file
    if not tb_path.is_file():
        return {"status": "error", "sim_passed": False, "sim_output": f"Testbench not found: {tb_file}", "vcd_path": None}

    src_files = sorted((project_root / "src").glob("*.vhd")) if (project_root / "src").is_dir() else []

    try:
        client = _make_ssh_client()
    except Exception as e:
        return {"status": "error", "sim_passed": False, "sim_output": f"SSH connect failed: {e}", "vcd_path": None}

    try:
        settings = _detect_remote_settings64(client)
        if not settings:
            return {"status": "error", "sim_passed": False, "sim_output": "No settings64.sh on remote host", "vcd_path": None}

        out, _, _ = _remote_run(client, "mktemp -d /tmp/vivado_sim_XXXX")
        tmpdir = out.strip()

        sftp = client.open_sftp()
        sftp.mkdir(f"{tmpdir}/src")
        sftp.mkdir(f"{tmpdir}/tb")
        sftp.mkdir(f"{tmpdir}/work")

        remote_src_files = []
        for src in src_files:
            remote_path = f"{tmpdir}/src/{src.name}"
            sftp.put(str(src), remote_path)
            remote_src_files.append(remote_path)

        remote_tb = f"{tmpdir}/tb/{tb_path.name}"
        sftp.put(str(tb_path), remote_tb)

        xsim_tcl = (
            "open_vcd waves.vcd\n"
            "if {[catch {log_vcd /*/dut/*} err]} { puts \"log_vcd: $err -- continuing\" }\n"
            f"run {sim_time}\n"
            "close_vcd\n"
            "quit\n"
        )
        with sftp.open(f"{tmpdir}/work/xsim_run.tcl", "w") as f:
            f.write(xsim_tcl)
        sftp.close()

        src_list = " ".join(remote_src_files)
        s = settings
        w = f"{tmpdir}/work"

        # Stage 1: compile design files (VHDL-93)
        if src_list:
            out1, err1, rc1 = _remote_run(client,
                f"source {s} && cd {w} && xvhdl {src_list} 2>&1", timeout=120)
            if rc1 != 0 or "ERROR:" in out1:
                _remote_run(client, f"rm -rf {tmpdir}")
                return {"status": "error", "sim_passed": False,
                        "sim_output": _cap(f"[xvhdl src failed]\n{out1}"), "vcd_path": None}

        # Stage 2: compile testbench (VHDL-2008)
        out2, err2, rc2 = _remote_run(client,
            f"source {s} && cd {w} && xvhdl --2008 {remote_tb} 2>&1", timeout=120)
        if rc2 != 0 or "ERROR:" in out2:
            _remote_run(client, f"rm -rf {tmpdir}")
            return {"status": "error", "sim_passed": False,
                    "sim_output": _cap(f"[xvhdl tb failed]\n{out2}"), "vcd_path": None}

        # Stage 3: elaborate
        sim_name = f"{top_entity}_sim"
        out3, err3, rc3 = _remote_run(client,
            f"source {s} && cd {w} && xelab {top_entity} -s {sim_name} --debug typical 2>&1",
            timeout=180)
        if rc3 != 0 or "ERROR:" in out3:
            _remote_run(client, f"rm -rf {tmpdir}")
            return {"status": "error", "sim_passed": False,
                    "sim_output": _cap(f"[xelab failed]\n{out3}"), "vcd_path": None}

        # Stage 4: simulate
        out4, err4, rc4 = _remote_run(client,
            f"source {s} && cd {w} && xsim {sim_name} -t xsim_run.tcl 2>&1",
            timeout=600)

        sim_passed = "SIMULATION PASSED" in out4
        sim_failed = "SIMULATION FAILED" in out4
        has_error  = "ERROR:" in out4
        status = "ok" if (sim_passed and not sim_failed and not has_error and rc4 == 0) else "error"

        _remote_run(client, f"rm -rf {tmpdir}")
        return {"status": status, "sim_passed": sim_passed, "sim_output": _cap(out4), "vcd_path": None}

    finally:
        client.close()


def _remote_vivado_batch(tcl_file: str, project_dir: str, timeout: int) -> dict:
    """
    Upload all project files and the TCL script, run vivado -mode batch on remote.
    """
    project_root = Path(project_dir)
    tcl_path = project_root / tcl_file
    if not tcl_path.is_file():
        return {"status": "error", "journal": "", "log_excerpt": f"TCL not found: {tcl_file}", "reports_dir": None}

    forbidden = _check_tcl_for_deletion(tcl_path)
    if forbidden:
        return {"status": "error", "journal": "", "log_excerpt": forbidden, "reports_dir": None}

    try:
        client = _make_ssh_client()
    except Exception as e:
        return {"status": "error", "journal": "", "log_excerpt": f"SSH connect failed: {e}", "reports_dir": None}

    try:
        settings = _detect_remote_settings64(client)
        if not settings:
            return {"status": "error", "journal": "", "log_excerpt": "No settings64.sh on remote", "reports_dir": None}

        out, _, _ = _remote_run(client, "mktemp -d /tmp/vivado_batch_XXXX")
        tmpdir = out.strip()

        # Build remote_tcl from the actual relative path of tcl_file under
        # project_root so it works regardless of which subdirectory (or
        # nested path) the caller passes.  e.g. "scripts/sub/synth.tcl",
        # "my_tcl/run.tcl", or even "run.tcl" at the project root all map
        # to the correct location under tmpdir.
        rel_tcl = tcl_path.relative_to(project_root)
        remote_tcl = f"{tmpdir}/{rel_tcl.as_posix()}"

        sftp = client.open_sftp()
        for sub in ("src", "tb", "scripts", "impl_out"):
            local_sub = project_root / sub
            if local_sub.is_dir():
                sftp.mkdir(f"{tmpdir}/{sub}")
                for f in local_sub.iterdir():
                    if f.is_file():
                        sftp.put(str(f), f"{tmpdir}/{sub}/{f.name}")

        # Always upload the TCL file at its resolved remote path.  This
        # covers TCL files outside the fixed subfolders, inside nested
        # subdirectories, or at the project root — none of which the
        # loop above would reach.  For TCL files already uploaded by
        # the loop this is an idempotent overwrite, which is harmless.
        remote_tcl_parent = remote_tcl.rsplit("/", 1)[0]
        _remote_run(client, f"mkdir -p {remote_tcl_parent}")
        sftp.put(str(tcl_path), remote_tcl)

        sftp.mkdir(f"{tmpdir}/work")
        # launcher cd's into tmpdir so relative paths in TCL resolve correctly
        launcher = f"{tmpdir}/work/_launcher.tcl"
        with sftp.open(launcher, "w") as f:
            f.write(f"cd {{{tmpdir}}}\nsource {{{remote_tcl}}}\n")
        sftp.close()

        cmd = (
            f"source {settings} && cd {tmpdir}/work && "
            f"vivado -mode batch -source {launcher} -nojournal -nolog 2>&1"
        )
        stdout, stderr, exit_code = _remote_run(client, cmd, timeout=timeout)
        has_error = "ERROR:" in stdout or "ERROR:" in stderr

        # Download any reports produced into impl_out/ before the remote
        # tmpdir is deleted.  The TCL launcher sets cwd to tmpdir, so
        # relative writes such as "impl_out/timing_summary.rpt" land at
        # {tmpdir}/impl_out/ on the remote.  We mirror that tree locally
        # so downstream WNS checks can open the same files as local mode.
        local_impl_out = project_root / "impl_out"
        reports_dir_str: str | None = None
        remote_impl_out = f"{tmpdir}/impl_out"
        out_ls, _, rc_ls = _remote_run(
            client,
            f"test -d {remote_impl_out} && find {remote_impl_out} -maxdepth 1 -type f",
        )
        remote_report_files = [
            line.strip() for line in out_ls.splitlines() if line.strip()
        ]
        if remote_report_files:
            local_impl_out.mkdir(parents=True, exist_ok=True)
            sftp2 = client.open_sftp()
            for remote_path in remote_report_files:
                local_dest = local_impl_out / Path(remote_path).name
                try:
                    sftp2.get(remote_path, str(local_dest))
                except Exception as dl_err:
                    # Log but do not abort — partial results are better than none
                    print(
                        f"[vivado_server] WARNING: could not download {remote_path}: {dl_err}",
                        file=sys.stderr,
                    )
            sftp2.close()
            reports_dir_str = str(local_impl_out)

        _remote_run(client, f"rm -rf {tmpdir}")
        return {
            "status": "ok" if (exit_code == 0 and not has_error) else "error",
            "journal": "",
            "log_excerpt": _cap(stdout + ("\n--- stderr ---\n" + stderr if stderr else "")),
            "reports_dir": reports_dir_str,
        }
    finally:
        client.close()


@mcp.tool()
def run_xvhdl(file: str, project_dir: str, vhdl_std: str = "93") -> dict:
    """
    Run xvhdl — Vivado's fast VHDL syntax and elaboration checker.
    Use this before synthesis to catch errors quickly (seconds, not minutes).

    Args:
        file:        Relative path to the .vhd file (relative to project_dir).
        project_dir: Absolute path to the project root.
        vhdl_std:    "93" (default, for design files) or "2008" (for testbenches).

    Returns:
        status:       "ok" or "error"
        stdout:       xvhdl output (capped at 4000 chars)
        stderr:       xvhdl stderr (capped at 4000 chars)
        errors_found: count of lines starting with "ERROR:" — 0 means clean compile
    """
    if is_remote_mode():
        return _remote_xvhdl(file, project_dir, vhdl_std)

    if not _env_result["ok"]:
        return {
            "status": "error",
            "stdout": "",
            "stderr": f"Vivado environment not available: {_env_result['reason']}",
            "errors_found": -1,
            "error_class": "environment",
        }

    project_root = Path(project_dir)
    target_file  = project_root / file

    if not target_file.is_file():
        return {
            "status": "error",
            "stdout": "",
            "stderr": f"File not found: {file} (resolved to {target_file})",
            "errors_found": -1,
        }

    # work_proj/ keeps xvhdl.log and xvhdl.pb out of src/ and tb/ — same
    # pattern as v1, so the agent's view of project files stays clean.
    work_dir = project_root / "work_proj"
    work_dir.mkdir(parents=True, exist_ok=True)

    cmd = [find_vivado_bin("xvhdl")]
    if vhdl_std == "2008":
        cmd.append("--2008")
    # vhdl_std == "93" passes no flag — xvhdl's default is VHDL-93
    cmd.append(str(target_file.resolve()))   # absolute path so cwd doesn't matter for file lookup

    try:
        result = run_vivado_cmd(
            cmd,
            cwd=str(work_dir),       # xvhdl.log/.pb land here, not in src/
            capture_output=True,
            text=True,
            timeout=120,             # xvhdl is fast — 2 minutes is generous
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "stdout": "",
            "stderr": "xvhdl timed out after 120s.",
            "errors_found": -1,
        }
    except FileNotFoundError:
        return {
            "status": "error",
            "stdout": "",
            "stderr": "xvhdl binary not found — check Vivado installation.",
            "errors_found": -1,
        }

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    # Count ERROR: lines across both streams — xvhdl sometimes writes
    # diagnostics to stderr instead of stdout depending on the error type.
    errors_found = len(_ERROR_LINE_RE.findall(stdout)) + len(_ERROR_LINE_RE.findall(stderr))

    ok = result.returncode == 0 and errors_found == 0
    return {
        "status":       "ok" if ok else "error",
        "stdout":       _cap(stdout),
        "stderr":       _cap(stderr),
        "errors_found": errors_found,
        "error_class":  None if ok else _classify_failure(stdout + stderr),
    }

# ── TCL deletion guard ──────────────────────────────────────────────────
# Layer 3 of the no-delete invariant. Layer 1 = no delete tool in
# file_server.py. Layer 2 = python_exec preamble blocks os.remove etc.
# This layer blocks the same intent expressed as TCL/exec commands.
_TCL_FORBIDDEN_PATTERNS = [
    r'\bfile\s+delete\b',
    r'\bfile\s+rename\b',
    r'\bexec\s+rm\b',
    r'\bexec\s+rmdir\b',
    r'\bexec\s+unlink\b',
    r'\bexec\s+shred\b',
]


def _check_tcl_for_deletion(tcl_path: Path) -> str | None:
    """Return None if the TCL is clean, or a reason string if it isn't."""
    try:
        content = tcl_path.read_text(encoding="utf-8")
    except OSError as e:
        return f"Could not read TCL file: {e}"

    for pattern in _TCL_FORBIDDEN_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f"TCL contains forbidden deletion pattern: {pattern}"
    return None


@mcp.tool()
def run_simulation(tb_file: str, top_entity: str, project_dir: str,
                   sim_time: str = "10us") -> dict:
    """
    Run the full simulation pipeline: compile design + testbench, elaborate,
    simulate. Looks for "SIMULATION PASSED" / "SIMULATION FAILED" markers
    in the xsim output — the convention used by self-checking testbenches.

    Args:
        tb_file:     Relative path to the testbench file (e.g. "tb/tb_counter.vhd").
        top_entity:  Name of the top-level testbench entity (e.g. "tb_counter").
                     This must instantiate the design via `entity work.NAME`,
                     so the design file does not need to be passed separately —
                     it's pulled in automatically during compilation if it's
                     already in the same work library.
        project_dir: Absolute path to the project root.
        sim_time:    Simulation duration (default "10us").

    Returns:
        status:      "ok" or "error"
        sim_passed:  bool — True only if "SIMULATION PASSED" found and no errors
        sim_output:  tail of xsim stdout (capped at 4000 chars)
        vcd_path:    path to the waveform file, or None if not generated
    """
    if is_remote_mode():
        return _remote_simulation(tb_file, top_entity, project_dir, sim_time)

    if not _env_result["ok"]:
        return {
            "status": "error", "sim_passed": False,
            "sim_output": f"Vivado environment not available: {_env_result['reason']}",
            "vcd_path": None,
            "error_class": "environment",
        }

    project_root = Path(project_dir)
    tb_path = project_root / tb_file

    if not tb_path.is_file():
        return {
            "status": "error", "sim_passed": False,
            "sim_output": f"Testbench file not found: {tb_file}",
            "vcd_path": None,
        }

    work_dir = project_root / "work_proj"
    work_dir.mkdir(parents=True, exist_ok=True)

    # ── Stage 1: compile testbench (VHDL-2008) ──────────────────────────
    # The design file is assumed already compiled into the work library
    # by a prior run_xvhdl call — this mirrors v1's pattern where src and
    # tb are compiled as separate stages but share one xsim work library.
    cmd_compile = [find_vivado_bin("xvhdl"), "--2008", str(tb_path.resolve())]
    try:
        r1 = run_vivado_cmd(cmd_compile, cwd=str(work_dir),
                            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"status": "error", "sim_passed": False,
                "sim_output": "xvhdl (testbench compile) timed out.", "vcd_path": None}

    if r1.returncode != 0 or "ERROR:" in (r1.stdout or ""):
        return {
            "status": "error", "sim_passed": False,
            "sim_output": _cap(f"[xvhdl tb compile failed]\n{r1.stdout}\n{r1.stderr}"),
            "vcd_path": None,
        }

    # ── Stage 2: elaborate ───────────────────────────────────────────────
    sim_name = f"{top_entity}_sim"
    cmd_elab = [find_vivado_bin("xelab"), top_entity, "-s", sim_name, "--debug", "typical"]
    try:
        r2 = run_vivado_cmd(cmd_elab, cwd=str(work_dir),
                            capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"status": "error", "sim_passed": False,
                "sim_output": "xelab timed out.", "vcd_path": None}

    if r2.returncode != 0 or "ERROR:" in (r2.stdout or ""):
        return {
            "status": "error", "sim_passed": False,
            "sim_output": _cap(f"[xelab failed]\n{r2.stdout}\n{r2.stderr}"),
            "vcd_path": None,
        }

    # ── Stage 3: simulate with VCD capture ───────────────────────────────
    # xsim runs a TCL script that opens VCD logging, runs for sim_time,
    # then quits. Bare "waves.vcd" resolves relative to cwd (= work_dir).
    xsim_tcl = work_dir / "xsim_run.tcl"
    xsim_tcl.write_text(
        "open_vcd waves.vcd\n"
        "if {[catch {log_vcd /*/dut/*} err]} { puts \"log_vcd: $err -- continuing\" }\n"
        f"run {sim_time}\n"
        "close_vcd\n"
        "quit\n",
        encoding="utf-8",
    )

    cmd_sim = [find_vivado_bin("xsim"), sim_name, "-t", "xsim_run.tcl"]
    try:
        r3 = run_vivado_cmd(cmd_sim, cwd=str(work_dir),
                            capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"status": "error", "sim_passed": False,
                "sim_output": "xsim timed out.", "vcd_path": None}

    sim_stdout = r3.stdout or ""
    sim_passed = "SIMULATION PASSED" in sim_stdout
    sim_failed = "SIMULATION FAILED" in sim_stdout
    has_error  = "ERROR:" in sim_stdout

    # Only a clean PASSED with no contradicting FAILED/ERROR counts as success
    status = "ok" if (sim_passed and not sim_failed and not has_error and r3.returncode == 0) else "error"

    vcd_file = work_dir / "waves.vcd"
    vcd_path = str(vcd_file) if vcd_file.exists() else None

    return {
        "status":     status,
        "sim_passed": sim_passed,
        "sim_output": _cap(sim_stdout),
        "vcd_path":   vcd_path,
    }


@mcp.tool()
def run_vivado_batch(tcl_file: str, project_dir: str, timeout: int = 1200) -> dict:
    """
    Run Vivado in batch mode against a TCL script (synthesis, implementation,
    report generation). The TCL is scanned for deletion commands before
    execution — this is layer 3 of the no-delete invariant.

    The TCL script is sourced via a small launcher that `cd`s to project_dir
    first, so relative paths inside the TCL (e.g. "src/counter.vhd",
    "impl_out/timing_summary.rpt") resolve against the project root, not
    against work_proj/ where Vivado's own intermediates land.

    Args:
        tcl_file:    Relative path to the TCL script (e.g. "scripts/synth_impl.tcl").
        project_dir: Absolute path to the project root.
        timeout:     Max execution time in seconds (default 1200 = 20 min).

    Returns:
        status:      "ok" or "error"
        journal:     reserved for future use — currently empty string
        log_excerpt: tail of vivado stdout/stderr (capped at 4000 chars)
        reports_dir: path to impl_out/, if it exists after the run
    """
    if is_remote_mode():
        return _remote_vivado_batch(tcl_file, project_dir, timeout)

    if not _env_result["ok"]:
        return {
            "status": "error", "journal": "",
            "log_excerpt": f"Vivado environment not available: {_env_result['reason']}",
            "reports_dir": None,
            "error_class": "environment",
        }

    project_root = Path(project_dir)
    tcl_path = project_root / tcl_file

    if not tcl_path.is_file():
        return {
            "status": "error", "journal": "",
            "log_excerpt": f"TCL script not found: {tcl_file}",
            "reports_dir": None,
        }

    # Deletion guard runs on the actual TCL the agent wrote — not the launcher
    forbidden = _check_tcl_for_deletion(tcl_path)
    if forbidden:
        return {
            "status": "error", "journal": "",
            "log_excerpt": (
                f"{forbidden}. Remove file delete/rename and exec rm/rmdir/"
                f"unlink/shred commands from the TCL. write_file (truncate-"
                f"overwrite) is sufficient for any Vivado workflow."
            ),
            "reports_dir": None,
        }

    work_dir = project_root / "work_proj"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Launcher cd's into project_dir before sourcing the agent's TCL, so
    # relative paths like "impl_out/foo.rpt" resolve from project root
    # even though Vivado itself runs with cwd = work_proj/.
    abs_project_root = str(project_root.resolve())
    abs_tcl = str(tcl_path.resolve())
    launcher_path = work_dir / "_batch_launcher.tcl"
    launcher_path.write_text(
        f"cd {{{abs_project_root}}}\n"
        f"source {{{abs_tcl}}}\n",
        encoding="utf-8",
    )

    cmd = [
        find_vivado_bin("vivado"), "-mode", "batch",
        "-source", str(launcher_path),
        "-nojournal", "-nolog",
    ]

    try:
        result = run_vivado_cmd(
            cmd, cwd=str(work_dir),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error", "journal": "",
            "log_excerpt": f"vivado batch run timed out after {timeout}s.",
            "reports_dir": None,
        }

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    has_error = "ERROR:" in stdout or "ERROR:" in stderr
    combined = stdout + ("\n--- stderr ---\n" + stderr if stderr else "")

    reports_dir = project_root / "impl_out"
    reports_dir_str = str(reports_dir) if reports_dir.exists() else None

    ok = result.returncode == 0 and not has_error
    return {
        "status":      "ok" if ok else "error",
        "journal":     "",   # reserved — -nojournal means no .jou file is produced
        "log_excerpt": _cap(combined),
        "reports_dir": reports_dir_str,
        "error_class": None if ok else _classify_failure(combined),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
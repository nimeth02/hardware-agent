# mcp_servers/vivado_env.py — Local and remote Vivado environment setup
#
# LOCAL MODE (default):
#   Finds and sources settings64.sh into the current process environment so
#   subprocess calls to xvhdl/xelab/xsim work without the user manually
#   running `source settings64.sh` first.
#
# REMOTE MODE (when VIVADO_REMOTE_HOST is set in .env):
#   All Vivado commands are executed over SSH on the remote machine.
#   Files are transferred via SFTP. No local Vivado installation needed.
#
# Remote env vars:
#   VIVADO_REMOTE_HOST      — hostname or IP of the machine with Vivado
#   VIVADO_REMOTE_USER      — SSH username
#   VIVADO_REMOTE_PASSWORD  — SSH password (or leave blank to use key auth)
#   VIVADO_REMOTE_KEY       — path to SSH private key (optional)
#   VIVADO_REMOTE_SETTINGS64 — absolute path to settings64.sh on the remote
#                              (auto-detected if blank)

import os
import glob
import re
import shutil
import subprocess
from pathlib import Path

from dotenv import load_dotenv

# This module is also spawned standalone (as an MCP server subprocess, or
# directly by tests) without going through main.py / agent/config.py, so
# it can't rely on a parent process having already loaded the repo .env
# into os.environ. Load it here too — same REPO_ROOT depth as
# agent/config.py — so VIVADO_SETTINGS64 etc. work regardless of caller.
_REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=False)

# Matches one PATH entry: a Windows drive-letter path (C:\... or C:/...) or a
# POSIX-style path (/...). Xilinx's Windows settings64.sh scripts export PATH
# colon-joined even though the entries themselves are Windows paths, so a
# naive split(":") shreds "C:\Xilinx\..." at the drive-letter colon too.
_PATH_ENTRY_RE = re.compile(r'[A-Za-z]:[\\/][^:]*|/[^:]*')


def _candidate_settings_paths() -> list[Path]:
    """
    Ordered list of settings64.sh candidates, first hit wins.
    1. Pinned 2020.1 path (this project's target version)
    2. Any /tools/Xilinx/Vivado/*/settings64.sh (newest first)
    3. Any /opt/Xilinx/Vivado/*/settings64.sh (alt install location)
    """
    candidates = []

    pinned_settings64 = os.getenv("VIVADO_SETTINGS64", "").strip()
    pinned_version = os.getenv("VIVADO_VERSION_PINNED", "2020.1").strip()
    if pinned_settings64:
        pinned = Path(pinned_settings64)
    else:
        pinned = Path(f"/tools/Xilinx/Vivado/{pinned_version}/settings64.sh")
    if pinned.is_file():
        candidates.append(pinned)

    for p in sorted(glob.glob("/tools/Xilinx/Vivado/*/settings64.sh"), reverse=True):
        pp = Path(p)
        if pp.is_file() and pp not in candidates:
            candidates.append(pp)

    for p in sorted(glob.glob("/opt/Xilinx/Vivado/*/settings64.sh"), reverse=True):
        pp = Path(p)
        if pp.is_file() and pp not in candidates:
            candidates.append(pp)

    return candidates


def _source_scripts_into_environ(script_paths: list[str]) -> bool:
    """
    Run `bash -c 'source X && env -0'` in a subshell, parse the NUL-delimited
    env dump, merge into this process's os.environ.

    NUL-separator (`env -0`) is used because some Xilinx env vars contain
    newlines — a plain newline-separated dump would corrupt parsing.
    """
    if not script_paths:
        return False

    quoted = " && ".join(f"source '{p}'" for p in script_paths)
    # Resolve `env` to an absolute path *before* sourcing: Xilinx's Windows
    # settings64.sh scripts prepend a bundled gnuwin bin/ to PATH, whose
    # env.exe doesn't understand `-0` and shadows the real coreutils env.
    cmd = f"__vivado_env_bin=$(command -v env) && {quoted} && \"$__vivado_env_bin\" -0"

    try:
        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

    if result.returncode != 0:
        return False

    raw = result.stdout or ""
    if not raw:
        return False

    for entry in raw.split("\0"):
        if "=" not in entry:
            continue
        k, _, v = entry.partition("=")
        if not k:
            continue
        if k == "PATH":
            parts = _PATH_ENTRY_RE.findall(v)
            if parts:
                v = os.pathsep.join(parts)
        os.environ[k] = v

    return True


def ensure_vivado_env() -> dict:
    """
    Make `vivado`/`xvhdl`/`xelab`/`xsim` reachable on PATH for this process.

    Returns a dict (not bool) so the MCP server can log structured detail:
        {"ok": True,  "source": "pre-sourced" | "settings64.sh", "vivado": "/path/to/vivado"}
        {"ok": False, "reason": "..."}
    """
    # (1) Already on PATH?
    pre = shutil.which("vivado")
    if pre:
        return {"ok": True, "source": "pre-sourced", "vivado": pre}

    # (2) Find a settings64.sh
    candidates = _candidate_settings_paths()
    if not candidates:
        return {
            "ok": False,
            "reason": "no settings64.sh found under /tools/Xilinx/Vivado/* or /opt/Xilinx/Vivado/*",
        }

    chosen = candidates[0]
    to_source = [str(chosen)]

    # Also source the matching Vitis settings64.sh if it exists alongside
    vitis_sib = chosen.parent.parent.parent / "Vitis" / chosen.parent.name / "settings64.sh"
    if vitis_sib.is_file():
        to_source.append(str(vitis_sib))

    ok = _source_scripts_into_environ(to_source)
    if not ok:
        return {"ok": False, "reason": f"failed to source {to_source}"}

    # (3) Confirm
    found = shutil.which("vivado")
    if found:
        return {"ok": True, "source": "settings64.sh", "scripts": to_source, "vivado": found}

    return {"ok": False, "reason": f"sourced {to_source} but 'vivado' still not on PATH"}


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a process and every descendant. On Windows the Vivado .BAT
    wrapper spawns cmd.exe -> xsim.exe -> xsimk.exe; killing only cmd.exe
    (what subprocess.run's timeout does) leaves the real simulator alive,
    still holding xsim.dir/* locked and our stdout pipe open."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, text=True)
    else:
        proc.kill()


def run_vivado_cmd(cmd: list, timeout: float | None = None, **kwargs) -> subprocess.CompletedProcess:
    """
    subprocess.run()-compatible wrapper for invoking a resolved Vivado binary.

    On Windows, xvhdl/xelab/xsim/vivado are .BAT wrapper scripts. Windows'
    CreateProcess (what subprocess uses with shell=False, the default)
    cannot execute a batch file directly — it must go through cmd.exe, or
    subprocess raises FileNotFoundError even though the file exists.

    Two safeguards beyond plain subprocess.run:
      * stdin is /dev/null, so a tool that drops to an interactive prompt
        (xsim after a TCL error) sees EOF and exits instead of waiting
        forever on the MCP server's stdin.
      * on timeout the whole process tree is killed before collecting
        output, so no orphaned xsim keeps the pipe (and xsim.dir) open.
    Raises subprocess.TimeoutExpired like subprocess.run does.
    """
    exe = str(cmd[0])
    if os.name == "nt" and exe.lower().endswith((".bat", ".cmd")):
        cmd = ["cmd", "/c", *cmd]
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    check = kwargs.pop("check", False)
    with subprocess.Popen(cmd, **kwargs) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def find_vivado_bin(name: str) -> str:
    """
    Resolve a Vivado binary name (vivado, xvhdl, xelab, xsim) to its full path.
    Assumes ensure_vivado_env() has already been called so PATH is set up.
    Falls back to the bare name if not found — subprocess will then fail
    with a clear "command not found" error rather than silently misbehaving.
    """
    found = shutil.which(name)
    return found if found else name


# ── Remote SSH mode ────────────────────────────────────────────────────────

# Remote connection config — read once at module import time.
VIVADO_REMOTE_HOST     = os.getenv("VIVADO_REMOTE_HOST", "").strip()
VIVADO_REMOTE_USER     = os.getenv("VIVADO_REMOTE_USER", "").strip()
VIVADO_REMOTE_PASSWORD = os.getenv("VIVADO_REMOTE_PASSWORD", "").strip()
VIVADO_REMOTE_KEY      = os.getenv("VIVADO_REMOTE_KEY", "").strip()
VIVADO_REMOTE_SETTINGS64 = os.getenv("VIVADO_REMOTE_SETTINGS64", "").strip()


def is_remote_mode() -> bool:
    """True when VIVADO_REMOTE_HOST is configured — remote SSH mode is active."""
    return bool(VIVADO_REMOTE_HOST)


def _make_ssh_client():
    """
    Open and return an authenticated paramiko SSHClient.
    Caller is responsible for calling client.close().
    Raises on connection / auth failure.
    """
    import paramiko  # imported here so paramiko is only required when remote mode is used

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = {"timeout": 15}
    if VIVADO_REMOTE_PASSWORD:
        connect_kwargs["password"] = VIVADO_REMOTE_PASSWORD
    if VIVADO_REMOTE_KEY:
        connect_kwargs["key_filename"] = VIVADO_REMOTE_KEY

    client.connect(VIVADO_REMOTE_HOST, username=VIVADO_REMOTE_USER or None, **connect_kwargs)
    return client


def _remote_run(client, cmd: str, timeout: int = 120) -> tuple[str, str, int]:
    """
    Run *cmd* on the already-connected SSH client.
    Returns (stdout, stderr, exit_code).
    """
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return stdout.read().decode(), stderr.read().decode(), exit_code


def _detect_remote_settings64(client) -> str | None:
    """
    Return the path to settings64.sh on the remote host, or None if not found.
    Checks VIVADO_REMOTE_SETTINGS64 first, then common install locations.
    """
    if VIVADO_REMOTE_SETTINGS64:
        out, _, rc = _remote_run(client, f"test -f {VIVADO_REMOTE_SETTINGS64} && echo ok")
        if "ok" in out:
            return VIVADO_REMOTE_SETTINGS64

    pinned_version = os.getenv("VIVADO_VERSION_PINNED", "2020.1").strip()
    candidates_cmd = (
        f"ls /opt/Xilinx/Vivado/{pinned_version}/settings64.sh 2>/dev/null; "
        f"ls /tools/Xilinx/Vivado/{pinned_version}/settings64.sh 2>/dev/null; "
        f"ls /opt/Xilinx/Vivado/*/settings64.sh 2>/dev/null | head -1; "
        f"ls /tools/Xilinx/Vivado/*/settings64.sh 2>/dev/null | head -1"
    )
    out, _, _ = _remote_run(client, candidates_cmd)
    for line in out.splitlines():
        line = line.strip()
        if line.endswith("settings64.sh"):
            return line
    return None


def ensure_remote_vivado_env() -> dict:
    """
    Validate remote Vivado connectivity and return a status dict:
        {"ok": True,  "mode": "remote", "host": ..., "settings64": ...}
        {"ok": False, "mode": "remote", "reason": ...}
    """
    if not VIVADO_REMOTE_HOST:
        return {"ok": False, "mode": "remote", "reason": "VIVADO_REMOTE_HOST not set"}

    try:
        client = _make_ssh_client()
    except Exception as e:
        return {"ok": False, "mode": "remote", "reason": f"SSH connect failed: {e}"}

    try:
        settings = _detect_remote_settings64(client)
        if not settings:
            return {
                "ok": False, "mode": "remote",
                "reason": f"No settings64.sh found on {VIVADO_REMOTE_HOST}",
            }
        return {
            "ok": True,
            "mode": "remote",
            "host": VIVADO_REMOTE_HOST,
            "user": VIVADO_REMOTE_USER,
            "settings64": settings,
        }
    finally:
        client.close()
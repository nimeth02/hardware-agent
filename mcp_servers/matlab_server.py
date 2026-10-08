# mcp_servers/matlab_server.py — MCP server for MATLAB script generation and execution
#
# Transport: stdio (launched as subprocess by the client).
# One warm MATLAB engine is shared across all tool calls (see matlab_engine.py).
# File tools are scoped to a project_dir — no operation can escape it.
#
# Tools mirror the reference matlab_agent: generate a .m script, run it,
# fetch `help` / `lookfor` text, plus a raw `matlab_eval` for short snippets
# and `matlab_status` for the engine state.

import os
import re
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent))
from matlab_engine import engine_error, engine_ready, get_engine, warm_engine  # noqa: E402

mcp = FastMCP("matlab-server")

_OUTPUT_CAP = 4000
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text) -> str:
    # MATLAB error messages and lookfor output embed HTML links
    # (<a href="matlab: opentoline(...)">, <strong>) meant for its desktop UI.
    return _HTML_TAG_RE.sub("", str(text or ""))


def _cap(text, limit: int = _OUTPUT_CAP) -> str:
    text = _clean(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def _resolve_safe(path: str, project_dir: str) -> Path:
    """Resolve `path` under `project_dir`, rejecting absolute paths and '..'
    escapes (same guard as file_server.py). Raises ValueError on violation."""
    root = Path(project_dir).resolve()
    target = (root / path).resolve()
    if os.path.commonpath([root, target]) != str(root):
        raise ValueError(f"Path escape detected: '{path}' resolves outside project root.")
    return target


def _engine_unavailable() -> dict | None:
    """None if the engine can be used, else an error dict to return as-is."""
    try:
        get_engine()
        return None
    except RuntimeError as e:
        return {"status": "error", "output": "", "error": str(e), "error_class": "environment"}


# ── Engine warm-up at import time ─────────────────────────────────────────
# Print to stderr, not stdout — stdout is the MCP JSON-RPC pipe.
warm_engine()
print("[matlab_server] MATLAB engine warming in background.", file=sys.stderr)


@mcp.tool()
def matlab_status() -> dict:
    """
    Report whether the shared MATLAB engine is ready.

    Returns:
        ready:   True once MATLAB has started (first start takes 1–2 minutes)
        version: MATLAB release string when ready, else ""
        error:   startup error message, if MATLAB could not be started
    """
    if engine_ready():
        try:
            version = str(get_engine().version())
        except Exception as e:
            version = f"unknown ({e})"
        return {"ready": True, "version": version, "error": None}
    return {"ready": False, "version": "", "error": engine_error()}


@mcp.tool()
def generate_matlab_script(filename: str, code: str, project_dir: str) -> dict:
    """
    Write a MATLAB .m script into project_dir.

    Args:
        filename:    Script name, with or without ".m" (relative to project_dir).
        code:        Full MATLAB source for the script.
        project_dir: Absolute path to the project root. Writes are scoped here.

    Returns:
        status: "ok" or "error"
        path:   absolute path of the written file
        error:  reason when status is "error"
    """
    name = (filename or "").strip()
    body = (code or "").strip()
    if not name or not body:
        return {"status": "error", "path": "", "error": "filename and code must not be empty"}
    if not name.endswith(".m"):
        name += ".m"
    try:
        target = _resolve_safe(name, project_dir)
    except ValueError as e:
        return {"status": "error", "path": "", "error": str(e)}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body + "\n", encoding="utf-8")
    return {"status": "ok", "path": str(target), "error": None}


@mcp.tool()
def run_matlab_script(script_name: str, project_dir: str) -> dict:
    """
    Run a MATLAB .m script by name inside project_dir and capture its output.

    Args:
        script_name: Script name without path, with or without ".m".
        project_dir: Absolute path to the folder containing the script; MATLAB
                     cd's here first so relative paths in the script resolve.

    Returns:
        status:      "ok" or "error"
        output:      everything the script printed (capped at 4000 chars)
        error:       MATLAB error message when status is "error"
        duration_ms: wall time of the run
    """
    name = (script_name or "").strip()
    if name.endswith(".m"):
        name = name[:-2]
    if not name:
        return {"status": "error", "output": "", "error": "script_name must not be empty"}

    project_root = Path(project_dir)
    if not (project_root / f"{name}.m").is_file():
        return {"status": "error", "output": "", "error": f"Script not found: {name}.m in {project_dir}"}

    unavailable = _engine_unavailable()
    if unavailable:
        return unavailable

    eng = get_engine()
    started = time.perf_counter()
    try:
        eng.cd(str(project_root.resolve()), nargout=0)
        # evalc captures everything the script prints; calling the script by
        # name as a function (getattr) would lose that output.
        output = eng.evalc(name)
    except Exception as e:
        return {
            "status": "error",
            "output": "",
            "error": _cap(str(e)),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    return {
        "status": "ok",
        "output": _cap(str(output)),
        "error": None,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }


@mcp.tool()
def matlab_eval(code: str, project_dir: str = "") -> dict:
    """
    Evaluate a short MATLAB snippet in the shared engine and capture its output.
    Use for quick checks (e.g. "disp(ver('signal'))"); use generate + run for real work.

    Args:
        code:        MATLAB statements to evaluate.
        project_dir: Optional folder to cd into first.

    Returns:
        status: "ok" or "error"
        output: printed output (capped at 4000 chars)
        error:  MATLAB error message when status is "error"
    """
    if not (code or "").strip():
        return {"status": "error", "output": "", "error": "code must not be empty"}
    unavailable = _engine_unavailable()
    if unavailable:
        return unavailable
    eng = get_engine()
    try:
        if project_dir:
            eng.cd(str(Path(project_dir).resolve()), nargout=0)
        output = eng.evalc(code)
    except Exception as e:
        return {"status": "error", "output": "", "error": _cap(str(e))}
    return {"status": "ok", "output": _cap(str(output)), "error": None}


@mcp.tool()
def matlab_help(function_list: str) -> dict:
    """
    Fetch MATLAB `help` text for one or more functions.

    Args:
        function_list: Comma-separated function names, e.g. "awgn, comm.RayleighChannel".

    Returns:
        status: "ok" or "error"
        help:   {function_name: help text (capped)} — a missing function maps to "ERROR: ..."
        error:  set only when the engine itself is unavailable
    """
    funcs = [f.strip() for f in (function_list or "").split(",") if f.strip()]
    if not funcs:
        return {"status": "error", "help": {}, "error": "Provide at least one function name"}
    unavailable = _engine_unavailable()
    if unavailable:
        return {"status": "error", "help": {}, "error": unavailable["error"]}
    eng = get_engine()
    result: dict[str, str] = {}
    for func in funcs:
        try:
            text = _cap(eng.help(func), 2500)
            # MATLAB returns "" (not an error) for an unknown name
            result[func] = text if text.strip() else f"ERROR: no help found for '{func}'"
        except Exception as e:
            result[func] = f"ERROR: {_clean(e)}"
    return {"status": "ok", "help": result, "error": None}


@mcp.tool()
def lookfor_help(keywords: str) -> dict:
    """
    Discover MATLAB functions by keyword using `lookfor`.

    Args:
        keywords: Comma-separated keyword phrases, e.g. "qam modulation, awgn".

    Returns:
        status:  "ok" or "error"
        results: {phrase: lookfor output (capped)}
        error:   set only when the engine itself is unavailable
    """
    phrases = [k.strip() for k in (keywords or "").split(",") if k.strip()]
    if not phrases:
        return {"status": "error", "results": {}, "error": "Provide at least one keyword phrase"}
    unavailable = _engine_unavailable()
    if unavailable:
        return {"status": "error", "results": {}, "error": unavailable["error"]}
    eng = get_engine()
    results: dict[str, str] = {}
    for phrase in phrases:
        escaped = phrase.replace("'", "''")  # MATLAB string literal escaping
        try:
            results[phrase] = _cap(eng.evalc(f"lookfor('{escaped}')"), 2500)
        except Exception as e:
            results[phrase] = f"ERROR: {_clean(e)}"
    return {"status": "ok", "results": results, "error": None}


if __name__ == "__main__":
    mcp.run(transport="stdio")

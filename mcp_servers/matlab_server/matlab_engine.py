# mcp_servers/matlab_server/matlab_engine.py — shared warm MATLAB engine singleton
#
# Starting MATLAB takes 1–2 minutes, so one engine process is started per
# server lifetime and shared across all tool calls (same pattern as the
# reference matlab_agent). warm_engine() kicks the start off in a background
# thread so the MCP handshake isn't blocked; get_engine() waits for it.
#
# Env vars (repo .env):
#   MATLAB_STARTUP_OPTIONS — extra options for start_matlab, e.g. "-nodesktop"

import atexit
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]   # mcp_servers/matlab_server/ -> repo root
load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=False)

MATLAB_STARTUP_OPTIONS = os.getenv("MATLAB_STARTUP_OPTIONS", "").strip()

_engine = None
_engine_error: str | None = None
_lock = threading.Lock()


def get_engine():
    """Return the shared engine, starting it on first use. Raises RuntimeError
    if MATLAB could not be started (message explains why)."""
    global _engine, _engine_error
    with _lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            raise RuntimeError(_engine_error)
        try:
            import matlab.engine  # imported lazily: only needed when MATLAB is actually used
        except ImportError as e:
            _engine_error = f"MATLAB Engine for Python not installed ({e}). Run: pip install matlabengine"
            raise RuntimeError(_engine_error) from e
        print("[matlab_server] starting MATLAB engine (first start can take 1–2 minutes)...", file=sys.stderr)
        try:
            _engine = (
                matlab.engine.start_matlab(MATLAB_STARTUP_OPTIONS)
                if MATLAB_STARTUP_OPTIONS
                else matlab.engine.start_matlab()
            )
        except Exception as e:
            _engine_error = f"MATLAB engine failed to start: {e}"
            raise RuntimeError(_engine_error) from e
        print("[matlab_server] MATLAB engine ready.", file=sys.stderr)
        return _engine


def warm_engine() -> None:
    """Start MATLAB in the background so the first tool call doesn't look hung."""

    def _start() -> None:
        try:
            get_engine()
        except RuntimeError as e:
            print(f"[matlab_server] WARNING: {e}", file=sys.stderr)

    threading.Thread(target=_start, name="matlab-warmup", daemon=True).start()


def engine_ready() -> bool:
    return _engine is not None


def engine_error() -> str | None:
    return _engine_error


def shutdown_engine() -> None:
    global _engine
    with _lock:
        if _engine is not None:
            try:
                _engine.quit()
            except Exception:
                pass
            _engine = None


atexit.register(shutdown_engine)

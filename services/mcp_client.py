# services/mcp_client.py — MCP client for the project's stdio MCP servers
#
# Agents open a session per run (`async with client.session("matlab")`) and
# load the server's tools into it. Keeping one session for the whole run
# matters for MATLAB: a per-tool-call session would spawn a fresh server —
# and a fresh MATLAB engine — for every call.

from __future__ import annotations

import os
import sys

from langchain_mcp_adapters.client import MultiServerMCPClient

from services.config import REPO_ROOT  # importing it also loads the repo .env into os.environ

MCP_SERVERS_DIR = REPO_ROOT / "mcp_servers"


def _stdio(server_file: str) -> dict:
    # The MCP stdio client passes only a minimal environment (PATH, TEMP, ...)
    # to the child by default, which would strip VIVADO_SETTINGS64 /
    # MATLAB_STARTUP_OPTIONS loaded from .env — so forward ours explicitly.
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(MCP_SERVERS_DIR / server_file)],
        "env": dict(os.environ),
    }


MCP_SERVERS = {
    "files": _stdio("file_server.py"),
    "matlab": _stdio("matlab_server.py"),
    "vivado": _stdio("vivado_server.py"),
}


def get_mcp_client() -> MultiServerMCPClient:
    return MultiServerMCPClient(MCP_SERVERS)

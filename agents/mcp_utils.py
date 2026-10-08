# agents/mcp_utils.py — small helpers for calling the project's MCP servers
# from synchronous agent code.

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from typing import Any

from langchain_mcp_adapters.tools import load_mcp_tools

from services.mcp_client import get_mcp_client


def text(out: Any) -> str:
    """MCP tools return text content blocks; flatten to a string."""
    if isinstance(out, list):
        return "".join(b.get("text", "") for b in out if isinstance(b, dict))
    return str(out)


def run_sync(coro):
    """Run a coroutine from sync code, even if a loop is already running in
    this thread (then it runs on a helper thread)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _awrite_files(project_dir: str, files: dict[str, str]) -> None:
    client = get_mcp_client()
    async with client.session("files") as session:
        write_file = {t.name: t for t in await load_mcp_tools(session)}["write_file"]
        for path, content in files.items():
            out = text(await write_file.ainvoke({"path": path, "content": content, "project_dir": project_dir}))
            if out.startswith("ERROR"):
                raise RuntimeError(f"file server could not write {path}: {out}")


def mcp_write_files(project_dir: str, files: dict[str, str]) -> None:
    """Write {relative path: content} under project_dir through the
    file-server MCP (scoped, no delete, visible as tool calls)."""
    run_sync(_awrite_files(project_dir, files))


async def _aread_files(project_dir: str, directory: str, suffix: str) -> dict[str, str]:
    client = get_mcp_client()
    async with client.session("files") as session:
        tools = {t.name: t for t in await load_mcp_tools(session)}
        listing = text(await tools["list_files"].ainvoke({"path": directory, "project_dir": project_dir}))
        if listing.startswith("ERROR"):
            return {}
        files: dict[str, str] = {}
        for name in (n.strip() for n in listing.splitlines() if n.strip()):
            if not name.lower().endswith(suffix):
                continue
            content = text(await tools["read_file"].ainvoke({"path": f"{directory}/{name}", "project_dir": project_dir}))
            if content.startswith("ERROR"):
                raise RuntimeError(f"file server could not read {directory}/{name}: {content}")
            files[name] = content
    return files


async def _aread_file(project_dir: str, path: str) -> str | None:
    client = get_mcp_client()
    async with client.session("files") as session:
        read_file = {t.name: t for t in await load_mcp_tools(session)}["read_file"]
        out = text(await read_file.ainvoke({"path": path, "project_dir": project_dir}))
    if out.startswith("ERROR: File not found"):
        return None
    if out.startswith("ERROR"):
        raise RuntimeError(f"file server could not read {path}: {out}")
    return out


def mcp_read_file(project_dir: str, path: str) -> str | None:
    """Content of one file under project_dir through the file-server MCP;
    None when it does not exist."""
    return run_sync(_aread_file(project_dir, path))


def mcp_read_files(project_dir: str, directory: str, suffix: str = "") -> dict[str, str]:
    """{file name: content} for the files of `directory` (under project_dir)
    ending in `suffix`, through the file-server MCP. {} if the directory
    does not exist."""
    return run_sync(_aread_files(project_dir, directory, suffix))


def tool_json(out: Any) -> dict:
    """Tools that return a dict serialise it as JSON text; parse it back."""
    raw = text(out)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"status": "error", "stderr": raw}


async def _arun_xvhdl(project_dir: str, files: list[tuple[str, str]]) -> list[dict]:
    client = get_mcp_client()
    results: list[dict] = []
    async with client.session("vivado") as session:
        run_xvhdl = {t.name: t for t in await load_mcp_tools(session)}["run_xvhdl"]
        for path, vhdl_std in files:
            r = tool_json(await run_xvhdl.ainvoke({"file": path, "project_dir": project_dir, "vhdl_std": vhdl_std}))
            results.append(r)
            if r.get("status") != "ok":
                break  # later files cannot compile against a broken earlier one
    return results


def mcp_run_xvhdl(project_dir: str, files: list[tuple[str, str]]) -> list[dict]:
    """Compile [(relative path, vhdl_std), ...] in order into one work
    library through the Vivado MCP server's run_xvhdl. Returns run_xvhdl's
    result dict per file compiled (stops after the first failure)."""
    return run_sync(_arun_xvhdl(project_dir, files))

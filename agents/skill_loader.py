# agents/skill_loader.py — tool: load a directory of skill files (markdown
# rule documents) through the file-server MCP.
#
# Going through the MCP server (rather than open()) keeps file access
# scoped to the project and makes every read visible as a tool call in the
# run's observability record. Agents subclass or instantiate it with their
# own skills directory.

from __future__ import annotations

import asyncio
import concurrent.futures
from pathlib import Path

from langchain_mcp_adapters.tools import load_mcp_tools

from services.config import REPO_ROOT
from services.mcp_client import get_mcp_client


def _text(out) -> str:
    """MCP tools return text content blocks; flatten to a string."""
    if isinstance(out, list):
        return "".join(b.get("text", "") for b in out if isinstance(b, dict))
    return str(out)


def _run(coro):
    """Run a coroutine from sync code, even if a loop is already running in
    this thread (then it runs on a helper thread)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class SkillLoader:
    def __init__(self, skills_dir: str, project_dir: Path = REPO_ROOT):
        self.skills_dir = skills_dir
        self.project_dir = str(project_dir)

    def load(self) -> dict[str, str]:
        """{file name: content} for every file in skills_dir. Raises
        RuntimeError when the directory cannot be listed or a file read."""
        return _run(self.aload())

    async def aload(self) -> dict[str, str]:
        client = get_mcp_client()
        async with client.session("files") as session:
            tools = {t.name: t for t in await load_mcp_tools(session)}
            listing = _text(await tools["list_files"].ainvoke(
                {"path": self.skills_dir, "project_dir": self.project_dir}))
            if listing.startswith("ERROR"):
                raise RuntimeError(f"cannot list {self.skills_dir}: {listing}")
            skills: dict[str, str] = {}
            for name in (n.strip() for n in listing.splitlines() if n.strip()):
                content = _text(await tools["read_file"].ainvoke(
                    {"path": f"{self.skills_dir}/{name}", "project_dir": self.project_dir}))
                if content.startswith("ERROR"):
                    raise RuntimeError(f"cannot read {name}: {content}")
                skills[name] = content
        return skills

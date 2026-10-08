# agents/implementation_agent/project_loader.py — tool: read a project
# folder (src/, spec.json, constraints/) through the file-server MCP and
# describe it for the implementation tools.

from __future__ import annotations

import json
import re
from pathlib import Path

from langchain_mcp_adapters.tools import load_mcp_tools

from agents.mcp_utils import run_sync, text
from services.mcp_client import get_mcp_client

_ENTITY_RE = re.compile(r"\bentity\s+(\w+)\s+is", re.IGNORECASE)


class ImplementationProjectLoader:
    def load(self, project_dir: str) -> dict:
        """{name, dir, src_files, top_entity, spec, xdc_file}. Raises
        RuntimeError when the folder is unusable."""
        return run_sync(self.aload(project_dir))

    async def aload(self, project_dir: str) -> dict:
        client = get_mcp_client()
        async with client.session("files") as session:
            tools = {t.name: t for t in await load_mcp_tools(session)}

            async def listing(path: str, required: bool = True) -> list[str]:
                out = text(await tools["list_files"].ainvoke({"path": path, "project_dir": project_dir}))
                if out.startswith("ERROR"):
                    if required:
                        raise RuntimeError(f"cannot list {path}: {out}")
                    return []
                return [n.strip() for n in out.splitlines() if n.strip()]

            async def read(path: str) -> str:
                out = text(await tools["read_file"].ainvoke({"path": path, "project_dir": project_dir}))
                if out.startswith("ERROR"):
                    raise RuntimeError(f"cannot read {path}: {out}")
                return out

            src_files = [f"src/{n}" for n in await listing("src") if n.lower().endswith(".vhd")]
            if not src_files:
                raise RuntimeError("project has no src/*.vhd design files")

            spec = None
            if "spec.json" in await listing("."):
                try:
                    spec = json.loads(await read("spec.json"))
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"spec.json is not valid JSON: {e}") from e

            top_entity = (spec or {}).get("entity_name")
            if not top_entity:
                match = _ENTITY_RE.search(await read(src_files[0]))
                if not match:
                    raise RuntimeError(f"{src_files[0]} declares no entity and spec.json names none")
                top_entity = match.group(1)

            xdc_files = [f"constraints/{n}" for n in await listing("constraints", required=False)
                         if n.lower().endswith(".xdc")]

        return {
            "name": Path(project_dir).name,
            "dir": project_dir,
            "src_files": src_files,
            "top_entity": top_entity,
            "spec": spec,
            "xdc_file": xdc_files[0] if xdc_files else None,
        }

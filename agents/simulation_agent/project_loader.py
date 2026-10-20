# agents/simulation_agent/project_loader.py — tool: read a project folder
# (src/, tb/, spec.json, test_vectors_in.csv + test_vectors_out.csv) through
# the file-server MCP and describe it for the simulation tools.
#
# Going through the MCP server keeps file access scoped to the project and
# makes every read visible as a tool call in the run's observability record.

from __future__ import annotations

import json
import re
from pathlib import Path

from langchain_mcp_adapters.tools import load_mcp_tools

from agents.mcp_utils import run_sync, text
from services.mcp_client import get_mcp_client

_ENTITY_RE = re.compile(r"\bentity\s+(\w+)\s+is", re.IGNORECASE)


class SimulationProjectLoader:
    def load(self, project_dir: str) -> dict:
        """{name, dir, src_files, tb_file, top_entity, spec, test_vectors
        (the CSV names staged for the testbench), testbench_code}. Raises
        RuntimeError when the folder is unusable."""
        return run_sync(self.aload(project_dir))

    async def aload(self, project_dir: str) -> dict:
        client = get_mcp_client()
        async with client.session("files") as session:
            tools = {t.name: t for t in await load_mcp_tools(session)}

            async def listing(path: str) -> list[str]:
                out = text(await tools["list_files"].ainvoke({"path": path, "project_dir": project_dir}))
                if out.startswith("ERROR"):
                    raise RuntimeError(f"cannot list {path}: {out}")
                return [n.strip() for n in out.splitlines() if n.strip()]

            async def read(path: str) -> str:
                out = text(await tools["read_file"].ainvoke({"path": path, "project_dir": project_dir}))
                if out.startswith("ERROR"):
                    raise RuntimeError(f"cannot read {path}: {out}")
                return out

            src_files = [f"src/{n}" for n in await listing("src") if n.lower().endswith(".vhd")]
            tb_files = [f"tb/{n}" for n in await listing("tb") if n.lower().endswith(".vhd")]
            if not src_files:
                raise RuntimeError("project has no src/*.vhd design files")
            if not tb_files:
                raise RuntimeError("project has no tb/*.vhd testbench")
            root_files = await listing(".")

            tb_file = tb_files[0]
            testbench_code = await read(tb_file)
            match = _ENTITY_RE.search(testbench_code)
            if not match:
                raise RuntimeError(f"{tb_file} declares no entity")

            spec = None
            if "spec.json" in root_files:
                try:
                    spec = json.loads(await read("spec.json"))
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"spec.json is not valid JSON: {e}") from e

            test_vectors = [n for n in root_files if n.lower().endswith(".csv")]
            for csv_name in test_vectors:
                # xsim runs with work_proj/ as cwd: put the CSVs where a
                # relative open() in the testbench will find them.
                out = text(await tools["write_file"].ainvoke(
                    {"path": f"work_proj/{csv_name}", "content": await read(csv_name),
                     "project_dir": project_dir}))
                if out.startswith("ERROR"):
                    raise RuntimeError(f"cannot stage {csv_name} into work_proj: {out}")

        return {
            "name": Path(project_dir).name,
            "dir": project_dir,
            "src_files": src_files,
            "tb_file": tb_file,
            "top_entity": match.group(1),
            "spec": spec,
            "test_vectors": test_vectors,
            "testbench_code": testbench_code,
        }

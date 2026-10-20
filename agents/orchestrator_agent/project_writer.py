# agents/orchestrator_agent/project_writer.py — assemble a project folder
# (projects/<requirement id>/: spec.json, src/, tb/, the two test-vector
# CSVs) from the agents' results, through the file-server MCP. This is the
# layout the simulation and implementation agents read.

from __future__ import annotations

import json

from agents.mcp_utils import mcp_read_file, mcp_write_files
from services.config import REPO_ROOT

CSV_KEYS = ("input_csv", "output_csv")


def write_project(requirement_id: str, spec: dict, rtl: dict, testbench: dict,
                  test_vectors: dict | None = None) -> dict:
    """Returns {"status", "project_dir", "files"}. test_vectors is the whole
    test-vector agent result; its CSVs are copied in when it is COMPLETE."""
    name = requirement_id
    files = {
        f"projects/{name}/spec.json": json.dumps(spec, indent=2),
        f"projects/{name}/src/{rtl['entity_name']}.vhd": rtl["code"],
        f"projects/{name}/tb/{testbench['entity_name']}.vhd": testbench["code"],
    }
    vectors = test_vectors or {}
    if vectors.get("status") == "COMPLETE":
        for key in CSV_KEYS:
            csv_name = (vectors.get("result") or {}).get(key)
            content = mcp_read_file(vectors["run_dir"], csv_name) if csv_name else None
            if content is not None:
                files[f"projects/{name}/{csv_name}"] = content
    mcp_write_files(str(REPO_ROOT), files)
    return {"status": "COMPLETE", "project_dir": str(REPO_ROOT / "projects" / name), "files": list(files)}

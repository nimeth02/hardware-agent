# agents/testbench_agent/testbench_skill_loader.py — tool: the testbench
# rule skills (skills/TESTBENCH/*), read through the file-server MCP.

from __future__ import annotations

from pathlib import Path

from agents.skill_loader import SkillLoader
from agents.testbench_agent.state import SKILLS_DIR
from services.config import REPO_ROOT


class TestbenchSkillLoader(SkillLoader):
    def __init__(self, skills_dir: str = SKILLS_DIR, project_dir: Path = REPO_ROOT):
        super().__init__(skills_dir, project_dir)

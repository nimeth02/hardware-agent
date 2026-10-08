# agents/rtl_agent/rtl_skill_loader.py — tool: the RTL design-rule skills
# (skills/RTL/*), read through the file-server MCP.

from __future__ import annotations

from pathlib import Path

from agents.rtl_agent.state import SKILLS_DIR
from agents.skill_loader import SkillLoader
from services.config import REPO_ROOT


class RTLSkillLoader(SkillLoader):
    def __init__(self, skills_dir: str = SKILLS_DIR, project_dir: Path = REPO_ROOT):
        super().__init__(skills_dir, project_dir)

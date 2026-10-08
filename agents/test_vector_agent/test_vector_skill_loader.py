# agents/test_vector_agent/test_vector_skill_loader.py — tool: the
# test-vector rule skills (skills/TEST_VECTOR/*), read through the
# file-server MCP.

from __future__ import annotations

from pathlib import Path

from agents.skill_loader import SkillLoader
from agents.test_vector_agent.state import SKILLS_DIR
from services.config import REPO_ROOT


class TestVectorSkillLoader(SkillLoader):
    def __init__(self, skills_dir: str = SKILLS_DIR, project_dir: Path = REPO_ROOT):
        super().__init__(skills_dir, project_dir)

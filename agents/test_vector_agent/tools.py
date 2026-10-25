# agents/test_vector_agent/tools.py — the complete, restricted toolset of
# the Test Vector agent, bundled so tests can inject fakes:
#
#   generator     test_vector_generator.TestVectorGenerator     (LLM + MATLAB MCP tool loop)
#   decider       test_vector_decider.TestVectorDecider         (LLM, picks the next action)
#   skill_loader  test_vector_skill_loader.TestVectorSkillLoader (file-server MCP, read-only)
#   validator     test_vector_validator.validate_test_vectors   (deterministic)
#
# Nothing here can write HDL, run simulation/Vivado, or touch files outside
# the run directory.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from agents.test_vector_agent.test_vector_decider import TestVectorDecider
from agents.test_vector_agent.test_vector_generator import TestVectorGenerator
from agents.test_vector_agent.test_vector_skill_loader import TestVectorSkillLoader
from agents.test_vector_agent.test_vector_validator import validate_test_vectors

Validator = Callable[[dict, str, dict], dict]   # (spec, run_dir, result) -> {csv, num_vectors, columns, issues}


@dataclass
class TestVectorAgentTools:
    generator: TestVectorGenerator = field(default_factory=TestVectorGenerator)
    decider: TestVectorDecider = field(default_factory=TestVectorDecider)
    skill_loader: TestVectorSkillLoader = field(default_factory=TestVectorSkillLoader)
    validator: Validator = validate_test_vectors

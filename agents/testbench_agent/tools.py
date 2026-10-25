# agents/testbench_agent/tools.py — the complete, restricted toolset of the
# Testbench agent, bundled so tests can inject fakes:
#
#   generator     testbench_generator.TestbenchGenerator       (LLM)
#   decider       testbench_decider.TestbenchDecider           (LLM, picks the next action)
#   skill_loader  testbench_skill_loader.TestbenchSkillLoader  (file-server MCP, read-only)
#   linter        testbench_linter.lint_testbench              (deterministic)
#
# Nothing here can run simulation/Vivado or write project files.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from agents.testbench_agent.testbench_decider import TestbenchDecider
from agents.testbench_agent.testbench_generator import TestbenchGenerator
from agents.testbench_agent.testbench_linter import lint_testbench
from agents.testbench_agent.testbench_skill_loader import TestbenchSkillLoader

Linter = Callable[[dict, dict, dict], dict]


@dataclass
class TestbenchAgentTools:
    generator: TestbenchGenerator = field(default_factory=TestbenchGenerator)
    decider: TestbenchDecider = field(default_factory=TestbenchDecider)
    skill_loader: TestbenchSkillLoader = field(default_factory=TestbenchSkillLoader)
    linter: Linter = lint_testbench

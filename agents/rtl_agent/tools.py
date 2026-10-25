# agents/rtl_agent/tools.py — the complete, restricted toolset of the RTL
# agent, bundled so tests can inject fakes:
#
#   generator     rtl_generator.RTLGenerator        (LLM)
#   decider       rtl_decider.RTLDecider            (LLM, picks the next action)
#   skill_loader  rtl_skill_loader.RTLSkillLoader   (file-server MCP, read-only)
#   linter        rtl_linter.lint_rtl               (deterministic)
#
# Nothing here can run simulation/Vivado or write project files.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from agents.rtl_agent.rtl_decider import RTLDecider
from agents.rtl_agent.rtl_generator import RTLGenerator
from agents.rtl_agent.rtl_linter import lint_rtl
from agents.rtl_agent.rtl_skill_loader import RTLSkillLoader

Linter = Callable[[dict, dict], dict]


@dataclass
class RTLAgentTools:
    generator: RTLGenerator = field(default_factory=RTLGenerator)
    decider: RTLDecider = field(default_factory=RTLDecider)
    skill_loader: RTLSkillLoader = field(default_factory=RTLSkillLoader)
    linter: Linter = lint_rtl

# agents/spec_analyser_agent/tools.py — the complete, restricted toolset of
# the Spec Analyzer agent, bundled so tests can inject fakes:
#
#   generator  spec_generator.SpecGenerator   (LLM)
#   decider    spec_decider.SpecDecider       (LLM planner)
#   evaluator  spec_evaluator.evaluate_spec   (deterministic)
#
# Nothing here can write HDL, run tools, or touch project files.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from agents.spec_analyser_agent.spec_decider import SpecDecider
from agents.spec_analyser_agent.spec_evaluator import evaluate_spec
from agents.spec_analyser_agent.spec_generator import SpecGenerator

Evaluator = Callable[[dict], list[dict]]


@dataclass
class SpecAnalyzerTools:
    generator: SpecGenerator = field(default_factory=SpecGenerator)
    decider: SpecDecider = field(default_factory=SpecDecider)
    evaluator: Evaluator = evaluate_spec

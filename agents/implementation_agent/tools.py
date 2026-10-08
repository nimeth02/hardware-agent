# agents/implementation_agent/tools.py — the complete, restricted toolset of
# the Implementation agent, bundled so tests can inject fakes:
#
#   project_loader  project_loader.ImplementationProjectLoader  (file-server MCP, read-only)
#   script_writer   script_writer.write_scripts                 (file-server MCP write: XDC + TCL)
#   runner          implementation_runner.run_batch             (Vivado MCP run_vivado_batch)
#   metrics         implementation_runner.collect_metrics       (file-server MCP read of impl_out/*.rpt)
#   analyst         implementation_analyst.ImplementationAnalyst (deterministic on pass, LLM otherwise)
#   decider         implementation_decider.ImplementationDecider (LLM planner)
#
# Nothing here can modify the design sources.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from agents.implementation_agent.implementation_analyst import ImplementationAnalyst
from agents.implementation_agent.implementation_decider import ImplementationDecider
from agents.implementation_agent.implementation_runner import collect_metrics, run_batch
from agents.implementation_agent.project_loader import ImplementationProjectLoader
from agents.implementation_agent.script_writer import write_scripts

ScriptWriter = Callable[[dict, str], dict]
Runner = Callable[[dict, dict], dict]
Metrics = Callable[[dict], dict]


@dataclass
class ImplementationAgentTools:
    project_loader: ImplementationProjectLoader = field(default_factory=ImplementationProjectLoader)
    script_writer: ScriptWriter = write_scripts
    runner: Runner = run_batch
    metrics: Metrics = collect_metrics
    analyst: ImplementationAnalyst = field(default_factory=ImplementationAnalyst)
    decider: ImplementationDecider = field(default_factory=ImplementationDecider)

# agents/simulation_agent/tools.py — the complete, restricted toolset of the
# Simulation agent, bundled so tests can inject fakes:
#
#   project_loader  project_loader.SimulationProjectLoader   (file-server MCP, read + stage CSV)
#   compiler        simulation_runner.compile_design         (Vivado MCP run_xvhdl, deterministic)
#   simulator       simulation_runner.simulate               (Vivado MCP run_simulation, deterministic)
#   analyst         simulation_analyst.SimulationAnalyst     (deterministic on pass, LLM otherwise)
#   decider         simulation_decider.SimulationDecider     (LLM planner)
#
# Nothing here can write or modify RTL/testbench files.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from agents.simulation_agent.project_loader import SimulationProjectLoader
from agents.simulation_agent.simulation_analyst import SimulationAnalyst
from agents.simulation_agent.simulation_decider import SimulationDecider
from agents.simulation_agent.simulation_runner import compile_design, simulate

Compiler = Callable[[dict], dict]
Simulator = Callable[[dict, str], dict]


@dataclass
class SimulationAgentTools:
    project_loader: SimulationProjectLoader = field(default_factory=SimulationProjectLoader)
    compiler: Compiler = compile_design
    simulator: Simulator = simulate
    analyst: SimulationAnalyst = field(default_factory=SimulationAnalyst)
    decider: SimulationDecider = field(default_factory=SimulationDecider)

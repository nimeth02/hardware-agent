# agents/orchestrator_agent/tools.py — the existing agents, exposed to the
# orchestrator LLM as tools. Each tool runs one agent, stores its full
# result in the run's `results` dict and returns it (as JSON) to the LLM.
# Nothing here decides anything: which tool runs next is the LLM's call.

from __future__ import annotations

import json
import time

from langchain_core.tools import tool

from agents.implementation_agent.implementation_agent import run_implementation
from agents.mcp_utils import mcp_read_file, mcp_write_files
from agents.rtl_agent.rtl_agent import run_rtl_generation
from agents.simulation_agent.simulation_agent import run_simulation
from agents.spec_analyser_agent.spec_analyzer_agent import resume_spec_analysis, run_spec_analysis
from agents.test_vector_agent.test_vector_agent import run_test_vector_generation
from agents.testbench_agent.testbench_agent import run_testbench_generation
from services.config import REPO_ROOT, VIVADO_PART


def make_tools(results: dict) -> list:
    def done(name: str, fn) -> str:
        """Run one agent; store and return its result. A missing prerequisite
        or a crash becomes a FAILED result the LLM can reason about."""
        try:
            out = fn()
        except KeyError as e:
            out = {"status": "FAILED", "error": f"missing prerequisite: no {e.args[0]} result yet"}
        except Exception as e:
            out = {"status": "FAILED", "error": f"{type(e).__name__}: {e}"}
        else:
            repeated = results.get(name) == out   # same inputs -> same agent output: tell the LLM, don't let it loop
            results[name] = out
            if repeated:
                out = {**out, "note": "identical to this tool's previous result: its inputs did not change, "
                                      "so calling it again cannot help"}
        return json.dumps(out, default=str)

    @tool
    def analyze_spec(requirement_text: str) -> str:
        """Requirement text -> structured spec. May return WAITING_FOR_USER with a
        pending_question: answer it yourself with answer_spec_question."""
        return done("spec", lambda: run_spec_analysis(requirement_text))

    @tool
    def answer_spec_question(answer: str) -> str:
        """Answer the spec analyzer's pending question (you are the user: pick a
        reasonable interpretation and say so) and continue the analysis."""
        return done("spec", lambda: resume_spec_analysis(results["spec"]["session_id"], answer))

    @tool
    def generate_rtl() -> str:
        """Spec -> lint-clean VHDL RTL."""
        return done("rtl", lambda: run_rtl_generation(results["spec"]["spec"]))

    @tool
    def generate_testbench() -> str:
        """Spec + RTL -> lint-clean self-checking testbench."""
        return done("testbench", lambda: run_testbench_generation(results["spec"]["spec"], results["rtl"]["rtl"]))

    @tool
    def generate_test_vectors() -> str:
        """Spec -> golden test-vector CSV via MATLAB (optional; simulation does not need it)."""
        return done("test_vectors", lambda: run_test_vector_generation(results["spec"]["spec"]))

    def _write_project() -> dict:
        spec, rtl, tb = results["spec"]["spec"], results["rtl"]["rtl"], results["testbench"]["testbench"]
        name = f"{spec.get('entity_name') or 'design'}_{time.strftime('%Y%m%d_%H%M%S')}"
        files = {
            f"projects/{name}/spec.json": json.dumps(spec, indent=2),
            f"projects/{name}/src/{rtl['entity_name']}.vhd": rtl["code"],
            f"projects/{name}/tb/{tb['entity_name']}.vhd": tb["code"],
        }
        vectors = results.get("test_vectors") or {}
        if vectors.get("status") == "COMPLETE":
            csv = mcp_read_file(vectors["run_dir"], vectors["result"]["csv_file"])
            if csv is not None:
                files[f"projects/{name}/test_vectors.csv"] = csv
        mcp_write_files(str(REPO_ROOT), files)
        return {"status": "COMPLETE", "project_dir": str(REPO_ROOT / "projects" / name), "files": list(files)}

    @tool
    def write_project() -> str:
        """Write spec, RTL, testbench (and CSV if generated) as projects/<name>/ for
        the simulation and implementation agents. Returns the project folder."""
        return done("project", _write_project)

    @tool
    def simulate(sim_time: str = "10us") -> str:
        """xvhdl + xsim on the project folder -> pass/fail/error verdict with analysis."""
        return done("simulation", lambda: run_simulation(results["project"]["project_dir"], sim_time))

    @tool
    def implement(part: str = VIVADO_PART) -> str:
        """synth -> place -> route on the project folder -> timing/utilization verdict."""
        return done("implementation", lambda: run_implementation(results["project"]["project_dir"], part))

    return [analyze_spec, answer_spec_question, generate_rtl, generate_testbench, generate_test_vectors,
            write_project, simulate, implement]

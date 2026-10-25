# agents/orchestrator_agent/tools.py — the existing agents, exposed to the
# orchestrator LLM as tools. Each tool says only two things: what it reads
# from the run's state (state.py) and which agent it runs on that. Writing
# the answer back and logging the step happen in steps.py. The LLM passes
# only small settings (sim_time, part), never the requirement or the
# artifacts. Nothing here decides anything: which tool runs next is the
# LLM's call.

from __future__ import annotations

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from langgraph.types import Command

from agents.implementation_agent.implementation_agent import run_implementation
from agents.orchestrator_agent import human_input, project_writer
from agents.orchestrator_agent.state import Stage
from agents.orchestrator_agent.steps import attempt_history_of, attempt_of, run_stage
from agents.rtl_agent.rtl_agent import run_rtl_generation
from agents.simulation_agent.simulation_agent import run_simulation
from agents.spec_analyser_agent.spec_analyzer_agent import resume_spec_analysis, run_spec_analysis
from agents.test_vector_agent.test_vector_agent import run_test_vector_generation
from agents.testbench_agent.testbench_agent import run_testbench_generation
from services.config import VIVADO_PART


def analyze_spec_with_user(requirement_text: str) -> dict:
    """Run the spec analyzer; whenever it asks a question, wait for the person's
    answer (when this run can be answered, i.e. it is a recorded run) and let it
    continue. Without a person to ask, or if the run is stopped while waiting,
    the WAITING_FOR_USER result is returned as it is."""
    result = run_spec_analysis(requirement_text)
    run_id = human_input.current_run_id()
    while run_id and result.get("status") == "WAITING_FOR_USER":
        answer = human_input.ask(run_id, result.get("pending_question") or "", result.get("suggested_answers"))
        if not answer:
            break
        result = resume_spec_analysis(result["session_id"], answer)
    return result


def make_tools() -> list:
    @tool
    def analyze_spec(runtime: ToolRuntime) -> Command:
        """The recorded requirement -> structured spec. If the requirement is unclear it
        asks the user and waits for the answer."""
        return run_stage(runtime, Stage.ANALYZE_SPEC, analyze_spec_with_user, runtime.state["requirement_text"], part="spec")

    @tool
    def generate_rtl(runtime: ToolRuntime) -> Command:
        """The spec -> lint-clean VHDL RTL. The RTL agent is told which run this is,
        whether it is its 1st, 2nd, ... call for it, how its earlier calls went, and
        is handed the RTL it delivered last time so a retry revises it instead of
        starting over."""
        s = runtime.state
        return run_stage(runtime, Stage.GENERATE_RTL, run_rtl_generation, s["spec"], s["requirement_id"],
                         attempt_of(s, Stage.GENERATE_RTL), attempt_history_of(s, Stage.GENERATE_RTL), s.get("rtl"),
                         part="rtl")

    @tool
    def generate_testbench(runtime: ToolRuntime) -> Command:
        """The spec + RTL -> lint-clean testbench that replays test_vectors_in.csv /
        test_vectors_out.csv during simulation. The testbench agent is told which run
        this is, whether it is its 1st, 2nd, ... call for it, and how its earlier calls went."""
        s = runtime.state
        return run_stage(runtime, Stage.GENERATE_TESTBENCH, run_testbench_generation, s["spec"], s["rtl"],
                         s["requirement_id"], attempt_of(s, Stage.GENERATE_TESTBENCH),
                         attempt_history_of(s, Stage.GENERATE_TESTBENCH), part="testbench")

    @tool
    def generate_test_vectors(runtime: ToolRuntime) -> Command:
        """The spec -> golden test vectors via MATLAB (an independent model of the spec):
        test_vectors_in.csv (stimulus) + test_vectors_out.csv (expected). The test vector
        agent is told which run this is, whether it is its 1st, 2nd, ... call for it, and
        how its earlier calls went. run_dir (2nd input) is None: the agent names it."""
        s = runtime.state
        return run_stage(runtime, Stage.GENERATE_TEST_VECTORS, run_test_vector_generation, s["spec"], None,
                         s["requirement_id"], attempt_of(s, Stage.GENERATE_TEST_VECTORS),
                         attempt_history_of(s, Stage.GENERATE_TEST_VECTORS))

    @tool
    def write_project(runtime: ToolRuntime) -> Command:
        """Write the spec, RTL, testbench (and the CSVs, if test vectors were generated)
        as projects/<requirement id>/ for simulate and implement."""
        s = runtime.state
        return run_stage(runtime, Stage.WRITE_PROJECT, project_writer.write_project,
                         s["requirement_id"], s["spec"], s["rtl"], s["testbench"], s["test_vectors"], part="project_dir")

    @tool
    def simulate(runtime: ToolRuntime, sim_time: str = "10us") -> Command:
        """xvhdl + xsim on the project folder -> pass/fail/error verdict with analysis."""
        return run_stage(runtime, Stage.SIMULATE, run_simulation, runtime.state["project_dir"], sim_time)

    @tool
    def implement(runtime: ToolRuntime, part: str = VIVADO_PART) -> Command:
        """synth -> place -> route on the project folder -> timing/utilization verdict."""
        return run_stage(runtime, Stage.IMPLEMENT, run_implementation, runtime.state["project_dir"], part)

    return [analyze_spec, generate_rtl, generate_testbench, generate_test_vectors, write_project, simulate, implement]

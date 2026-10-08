# agents/orchestrator_agent/orchestrator_agent.py — simple, fully autonomous
# orchestrator.
#
# One LLM in a tool-calling loop. The existing agents are its tools
# (tools.py); it reads their results and decides on its own what to run
# next, what to redo and when to stop. No fixed order, no decision node,
# no guard rules: the only bound is MAX_STEPS.

from __future__ import annotations

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError

from agents.json_utils import extract_json
from agents.orchestrator_agent.tools import make_tools
from observability.callback import ObservabilityCallbackHandler
from prompts.orchestrator_prompt import ORCHESTRATOR_SYSTEM_PROMPT
from services.llm_client import get_llm

AGENT_NAME = "orchestrator"
MAX_STEPS = 60


def run_orchestration(requirement_text: str, max_steps: int = MAX_STEPS) -> dict:
    """Returns {"status", "summary", "next_steps", "results", "error"}."""
    results: dict = {}
    agent = create_agent(get_llm(), make_tools(results), system_prompt=ORCHESTRATOR_SYSTEM_PROMPT)
    handler = ObservabilityCallbackHandler(source=AGENT_NAME)
    final, error = {}, None
    try:
        out = agent.invoke({"messages": [HumanMessage(content=requirement_text)]},
                           config={"callbacks": [handler], "recursion_limit": max_steps})
        final = extract_json(out["messages"][-1].content)
    except GraphRecursionError:
        error = f"step limit ({max_steps}) reached before the orchestrator finished"
    except Exception as e:  # LLM unreachable, final answer not JSON, ...
        error = f"orchestration failed: {e}"
    status = final.get("status") if final.get("status") in ("COMPLETE", "NEEDS_REVIEW", "FAILED") else "FAILED"
    return {"status": status, "summary": final.get("summary"), "next_steps": final.get("next_steps"),
            "results": results, "error": error}

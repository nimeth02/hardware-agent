# Hardware Agent

LLM-driven VHDL design automation. Type a hardware requirement and a set of
agents turns it into a reviewed specification, synthesizable VHDL, a
self-checking testbench, golden test vectors, an xsim simulation verdict and
a Vivado place-and-route verdict. Everything runs locally: the language model
through Ollama, the EDA tools through small MCP servers, with a web UI to run
single agents or the whole flow and inspect every step.

## What it does

```
requirement text
   │
   ▼
spec_analyzer ──► rtl_agent ──► testbench_agent ──► project folder ──► simulation_agent ──► implementation_agent
      (asks you when            │                  (src/, tb/, spec.json,        (xvhdl + xsim)            (synth → route,
       the spec has gaps)       └─► test_vector_agent  test_vectors.csv)                                   timing + utilization)
                                     (MATLAB golden model)
```

| Agent | Input → output | How it decides it is done |
|---|---|---|
| `spec_analyzer` | requirement text → structured spec (JSON) | deterministic evaluator finds no gaps; pauses with a question when it does |
| `rtl_agent` | spec → VHDL-93 RTL | static lint + `xvhdl` compile are clean |
| `testbench_agent` | spec + RTL → VHDL-2008 self-checking testbench | static lint + `xvhdl` compile of DUT and testbench are clean |
| `test_vector_agent` | spec → `test_vectors.csv` via a MATLAB golden model | CSV validator accepts header, rows, ranges |
| `simulation_agent` | project folder → pass / fail / error + analysis | testbench prints `SIMULATION PASSED` |
| `implementation_agent` | project folder → WNS, utilization + analysis | flow completes, timing met, part not over-used |
| `orchestrator` | requirement text → the whole flow, autonomously | one LLM calls the agents above as tools and decides what to run next |

Every bounded agent follows the same pattern: an LLM **planner** proposes
the next action, deterministic **guard rules** correct it, a deterministic
**checker** (linter, validator, analyst) says when the result is good, and
iteration/stall limits end the run as `COMPLETE`, `NEEDS_REVIEW` or `FAILED`.
Generators load rule files from `skills/` so project conventions are
enforced, not hoped for.

## What we use

| Layer | Technology |
|---|---|
| Agents | Python 3.13, LangChain 1.x, LangGraph 1.x (state graphs, `interrupt` for human-in-the-loop, `create_agent` tool loop for the orchestrator) |
| LLM | Ollama serving `qwen3-coder-next` (80B, 262k context) through its OpenAI-compatible API (`langchain-openai`) |
| Tool servers | MCP (`mcp<2`, FastMCP) over stdio: file server (scoped read/list/write, no delete), Vivado server (`run_xvhdl`, `run_simulation`, `run_vivado_batch`), MATLAB server (`generate_matlab_script`, `run_matlab_script`) via `langchain-mcp-adapters` |
| EDA | Xilinx Vivado 2020.1 (`xvhdl`, `xelab`, `xsim`, batch synth/impl), MATLAB R2026a + `matlabengine` |
| Backend / UI | FastAPI + uvicorn, Server-Sent Events for live run timelines, vanilla HTML/CSS/JS (no build step) |
| Observability | SQLite (`observability/traces.db`): LLM traces, per-node step timings, agent runs/events/tool calls, queried from the UI |
| Persistence | LangGraph SQLite checkpointers in `checkpoints/` (resumable spec-analysis sessions) |
| Tests | pytest, fake tools only (no LLM, MCP, Vivado or MATLAB needed) |

## Repository layout

```
agents/                 one package per agent (state, tools, planner, guard, graph) + orchestrator_agent/
prompts/                system and planner prompts, one file per agent
skills/                 markdown rule files loaded into generator prompts (RTL, TESTBENCH, TEST_VECTOR)
mcp_servers/            file_server.py, vivado_server.py (+ vivado_env.py), matlab_server.py (+ matlab_engine.py)
services/               .env config, LLM client, MCP client, checkpoint store
observability/          tracing callback, run store, SQLite schema
server/                 FastAPI app (server/app.py) and static UI (server/static/)
projects/               example projects the simulation/implementation agents run as-is (see projects/README.md)
tests/                  unit tests per agent
.claude/agents/         Claude Code subagents used during development (ui-backend, project-guide, source-control)
```

## Setup guide

### 1. Prerequisites

| Requirement | Notes |
|---|---|
| Windows 11 with **Git Bash** | Vivado's `settings64.sh` is sourced through `bash`, so run the server from Git Bash (or make sure `bash` is on PATH) |
| **Python 3.13** | global install is fine; no virtual environment is required |
| **Ollama** with the model pulled | `ollama pull qwen3-coder-next` (any tool-calling model works; set `LLM_MODEL`) |
| **Vivado 2020.1** (WebPACK is enough) | needed by the RTL/testbench linters, simulation and implementation agents; without it those steps report `compiler: null` / environment errors |
| **MATLAB R2026a** + `matlabengine` | only for `test_vector_agent`; the rest of the flow works without MATLAB |

### 2. Install

```bash
git clone https://git.thakshana.com/Nimdinu/hardware-agent.git
cd hardware-agent
pip install -r requirements.txt
cp .env.example .env        # then edit the paths/model for your machine
```

### 3. Run

```bash
# from Git Bash, in the repo root; do NOT use --reload (unreliable on Windows)
python -m uvicorn server.app:app --port 8000
```

Open <http://localhost:8000>:

- **Agents** tab: pick an agent, use an example chip or paste your own input,
  run it, and follow the live timeline (planner decisions, LLM calls, tool
  calls, lint results). When the spec analyzer asks a question, answer it in
  place and the same run continues. "Use as input for …" hands one agent's
  result to the next. For `simulation_agent` and `implementation_agent` the
  input is a project folder name under `projects/` (try `counter4`).
- **Orchestrator**: enter a requirement and let one LLM run the whole flow;
  each tool call expands to show the sub-agent's own steps.
- **Previous runs** / **Observability**: every run, step timing, prompt and
  response is stored and searchable.

### 4. Test

```bash
python -m pytest tests -q
```

The suite uses fake tools, so it needs no LLM, Vivado or MATLAB.

### 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| run ends `FAILED: Connection error` | Ollama is not running or the model is not pulled |
| `Vivado environment not available: failed to source …settings64.sh` | `bash` not on PATH (start from Git Bash) or wrong `VIVADO_SETTINGS64` |
| `compiler: null` in lint results | Vivado unavailable; static checks still ran |
| `No SIMULATION PASSED/FAILED marker` | testbench ran past `sim_time` or never prints the marker; the agent reruns once with a longer `sim_time` |
| `The "Vhdl 2008 to_string Operator" is not supported` | xsim 2020.1 limitation; the testbench skill forbids `to_string` |
| UI looks stale after an update | hard refresh (Ctrl+F5); static files are not versioned |

## Adding a project to simulate or implement

Create `projects/<name>/` with `spec.json`, `src/*.vhd`, `tb/<entity>_tb.vhd`
and optionally `test_vectors.csv`; see `projects/README.md` and the
`counter4` example. Vivado output (`work_proj/`, `impl_out/`, generated
`scripts/` and `constraints/`) is git-ignored.

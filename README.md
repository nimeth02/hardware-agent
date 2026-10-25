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
       the spec has gaps)       └─► test_vector_agent  test_vectors_in/out.csv)                            timing + utilization)
                                     (MATLAB golden model)
```

| Agent | Input → output | How it decides it is done |
|---|---|---|
| `spec_analyzer` | requirement text → structured spec (JSON, incl. `vhdl_version`: `93` default or `2008`) | deterministic evaluator finds no gaps; pauses with a question when it does |
| `rtl_agent` | spec → VHDL RTL in the spec's edition (93 or 2008, rulebook picked by `agents/vhdl_version.py`) | static lint + `xvhdl` compile are clean |
| `testbench_agent` | spec + RTL → VHDL-2008 self-checking testbench | static lint + `xvhdl` compile of DUT and testbench are clean |
| `test_vector_agent` | spec → `test_vectors_in.csv` + `test_vectors_out.csv` via a MATLAB golden model | CSV validator accepts header, rows, ranges |
| `simulation_agent` | project folder → pass / fail / error + analysis | testbench prints `SIMULATION PASSED` |
| `implementation_agent` | project folder → WNS, utilization + analysis | flow completes, timing met, part not over-used |
| `pipeline` | requirement text → spec (you approve it) → RTL, test vectors, testbench, in a fixed order | every step reports `COMPLETE`; the pause for your approval survives a server restart |
| `orchestrator` | requirement text → the whole flow, autonomously | one LLM calls the agents above as tools and decides what to run next; each stage is recorded in its state and written to `projects/<requirement id>/` |

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
| EDA | Xilinx Vivado 2020.1 (`xvhdl`, `xelab`, `xsim`, batch synth/impl) local, or on another machine over SSH (`VIVADO_REMOTE_*` in `.env`, needs `pip install paramiko`); MATLAB R2026a + `matlabengine` |
| Backend / UI | FastAPI + uvicorn, Server-Sent Events for live run timelines, a Stop button that ends a live run at its next step, vanilla HTML/CSS/JS (no build step), plus an alternative Streamlit UI (`server/streamlit_app.py`) |
| Observability | SQLite (`observability/traces.db`): LLM traces, per-node step timings, agent runs/events/tool calls (incl. the orchestrator's `agent_input` / `agent_output` / `state` events), queried from the UI |
| Persistence | LangGraph SQLite checkpointers in `checkpoints/` (resumable spec-analysis sessions) |
| Tests | pytest, fake tools only (no LLM, MCP, Vivado or MATLAB needed) |

## Repository layout

```
agents/                 one package per agent (state, tools, planner, guard, graph) + orchestrator_agent/ (state, steps, tools, tracing, project_writer)
                        + pipeline_agent/ (fixed-order flow) + vhdl_version.py (which VHDL edition a design uses)
prompts/                system and planner prompts, one file per agent
skills/                 markdown rule files loaded into generator prompts (RTL: one rulebook per VHDL edition, TESTBENCH, TEST_VECTOR)
mcp_servers/            file_server.py, vivado_server.py (+ vivado_env.py), matlab_server.py (+ matlab_engine.py)
services/               .env config, LLM client, MCP client, checkpoint store
observability/          tracing callback, run store, SQLite schema
server/                 FastAPI app (server/app.py), static UI (server/static/) and Streamlit UI (server/streamlit_app.py)
projects/               example projects the simulation/implementation agents run as-is (see projects/README.md)
tests/                  unit tests per agent
.claude/agents/         Claude Code subagents used during development (ui-backend, project-guide, source-control)
.claude/hooks/          Claude Code hooks: pre_commit_tests.py runs the unit tests before any git commit Claude issues
CLAUDE.md               how Claude Code must explain things to the owner (plain words, no jargon)
```

## Setup guide

### 1. Prerequisites

| Requirement | Notes |
|---|---|
| Windows 11 with **Git Bash** | Vivado's `settings64.sh` is sourced through `bash`, so run the server from Git Bash (or make sure `bash` is on PATH) |
| **Python 3.13** | global install is fine; no virtual environment is required |
| **Ollama** with the model pulled | `ollama pull qwen3-coder-next` (any tool-calling model works; set `LLM_MODEL`) |
| **Vivado 2020.1** (WebPACK is enough) | needed by the RTL/testbench linters, simulation and implementation agents; without it those steps report `compiler: null` / environment errors. It may live on another machine: set `VIVADO_REMOTE_HOST/USER/PASSWORD/KEY/SETTINGS64` in `.env` and `pip install paramiko` |
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

# optional alternative UI (needs the backend above), then open http://localhost:8501
python -m streamlit run server/streamlit_app.py --server.port 8501
```

Open <http://localhost:8000>:

- **Agents** tab: pick an agent, use an example chip or paste your own input
  (plain text for the orchestrator / spec analyzer, JSON for the others),
  run it, and follow the live timeline (planner decisions, LLM calls, tool
  calls, lint results). When the spec analyzer asks a question, answer it in
  place and the same run continues. "Use as input for …" hands one agent's
  result to the next. For `simulation_agent` and `implementation_agent` the
  input is a project folder name under `projects/` (try `counter4`). A
  **Stop** button asks a live run to end at its next step (a tool already
  running finishes first); the run then shows as `stopped`.
- **Orchestrator** (in the Agents tab): enter a requirement and let one LLM
  run the whole flow; each tool call expands to show what was handed to the
  sub-agent, what came back, the sub-agent's own steps and the
  orchestrator's state before/after. A "What was produced" list sums up the
  result; files land in `projects/<requirement id>/`.
- **Pipeline** tab: the same steps in a fixed order with a pause for you to
  approve the spec before RTL, test vectors and testbench are generated.
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
| run ends `stopped by user` | you pressed Stop; nothing is wrong, start a new run |
| `ModuleNotFoundError: paramiko` | `VIVADO_REMOTE_HOST` is set but paramiko is not installed (`pip install paramiko`) |

## Adding a project to simulate or implement

Create `projects/<name>/` with `spec.json`, `src/*.vhd`, `tb/<entity>_tb.vhd`
and optionally the test-vector CSVs (`test_vectors_in.csv` +
`test_vectors_out.csv`, or a single `test_vectors.csv` as in `counter4`);
see `projects/README.md`. Vivado output (`work_proj/`, `impl_out/`, generated
`scripts/` and `constraints/`) is git-ignored, and so are the folders the
orchestrator generates (`projects/<name>_T-hh.mm_D-dd.mm.yyyy/`); only
hand-written examples such as `counter4` are committed.

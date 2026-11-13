# Hardware Agent: how the agents' memory is managed

*Status as of 2026-11-09. Companion to `state_handover.md`, which describes the state objects themselves. This document describes what each agent remembers, for how long, where it is kept, and what it is deliberately not allowed to remember.*

## 1. Summary

The system has no learned long-term memory. Nothing an agent experiences in one run is read by an agent in a later run. Instead memory is split into five layers with different lifetimes, and the long-term layers are maintained by people, not by the agents.

| Layer | Memory type | Lifetime | Where it lives | Who reads it |
|---|---|---|---|---|
| Prompt context | working memory | one LLM call | the messages list built for that call | the LLM |
| Agent state | short-term episodic | one agent call (one `run_*` invocation) | LangGraph `TypedDict` state, in process | the agent's nodes, planner and writer LLM |
| Orchestrator state + chat | run-level episodic | one orchestrator run | `OrchestratorState` and the `messages` list, in process | the manager LLM, the guard, blame |
| Durable records | long-term episodic | forever | `observability/traces.db`, `checkpoints/*.db`, `projects/<name>/`, `runs/` | people, the web UI, resume logic. Never an agent |
| Rules and checkers | semantic + procedural | forever, edited by hand | `skills/*.md`, `prompts/*.py`, the deterministic linters, validators, guards and analyst rules | every agent, on every run |

```mermaid
flowchart TB
    subgraph longterm[Long-term, hand-maintained]
        SK[skills/*.md rule files]
        PR[prompts/*.py system prompts]
        CH[deterministic checkers and guards]
    end
    subgraph run[One orchestrator run, in memory]
        OS[OrchestratorState: artifacts, stages, attempt_history, downstream notes]
        MSG[Manager chat: HumanMessage + ToolMessages]
    end
    subgraph call[One agent call, in memory]
        AS[Agent state: attempt_history, previous_*, lint, stall_count]
        LLM[LLM call: system prompt + skills + payload]
    end
    subgraph durable[Durable records, write-only for agents]
        TR[traces.db: agent_runs, agent_events, llm_calls, tool_calls]
        CK[checkpoints: spec_analyzer.db, pipeline.db]
        PJ[projects/<name>/ + history/, runs/test_vectors/]
    end
    SK --> LLM
    PR --> LLM
    CH --> AS
    OS -->|inputs| AS
    AS -->|result dict| OS
    OS --> MSG
    AS --> LLM
    run --> TR
    call --> TR
    OS --> PJ
    AS --> PJ
    PJ -.->|read back by simulation and implementation agents only| AS
```

## 2. Working memory: what one LLM call sees

Every LLM call is stateless. The code rebuilds the full message list each time; the model keeps nothing between calls. Each agent has two LLM roles with different views.

| Role | Sees | Does not see |
|---|---|---|
| Writer (generator) | system prompt + rule files (section 6) + spec + feedback (`attempt_history`, `previous_code`, `lint_issues`) | other agents' states, earlier runs |
| Planner (decider) | a compact picture of the run: `spec`, `skill_names`, `has_rtl`, `lint`, `iteration`, `max_iterations` (`rtl_agent/dto.py:decider_input_dto`) | the code itself, the chat, any log |
| Manager (orchestrator) | its system prompt + a rebuilt status board + the whole chat of tool answers | agent-internal state, skills |
| Simulation analyst | spec, failing beats (max 8), log head 3,000 chars + tail 6,000 chars, python check output (4,000 chars) | the full log |

The planner deliberately never receives code. The manager deliberately never receives code inside the status board; code reaches it only as tool answers already in the chat (`orchestrator_agent/status_board.py`).

## 3. Short-term episodic memory inside one agent call

Each agent's LangGraph state is created fresh by `initial_state(...)` and discarded when the graph ends. Five agents compile their graph with no checkpointer (`rtl_agent.py:35`, `testbench_agent.py:39`, `test_vector_agent.py:46`, `simulation_agent.py:47`, `implementation_agent.py:46`), so a process restart loses the call entirely. Within the call the state remembers:

| Field group | Example fields | What it remembers |
|---|---|---|
| Its own drafts | `rtl`, `testbench`, `test_vectors`, `lint`, `validation` | the current draft and the checker's verdict on it |
| Fix loop | `previous_code`, `lint_issues` (passed to the writer) | the draft the checker rejected and why, so the writer repairs it rather than restarting |
| Stall detection | `issue_signature`, `stall_count` | the fingerprint of the last issue list; two unchanged repairs end the call (`MAX_STALLED_FIXES = 2`) |
| Hand-over from earlier calls | `attempt`, `attempt_history`, `previous_rtl` / `previous_testbench` | what the orchestrator learned from this agent's earlier calls in the same run (section 4) |
| Budget | `iteration`, `MAX_ITERATIONS = 10` | how many planner decisions are left |

The test-vector agent holds one extra piece of short-term memory: a single MATLAB process kept alive for the whole agent call (`matlab_session.py`). Every attempt's script runs inside it, so MATLAB's two-minute boot is paid once. Each script that crashes is copied to `runs/test_vectors/<run>/gen_test_vectors.failedN.m` before the next attempt overwrites the file, and a guard rejects a resend of a script identical to one that already crashed (`MAX_IDENTICAL_REJECTS = 2`).

## 4. Run-level episodic memory in the orchestrator

The orchestrator remembers across agent calls within one run, and this is the only place where one agent's experience reaches another agent.

### 4.1 Structured memory: `stages[...]`

Every agent call appends one entry to `stages[tool].attempt_history`: `{attempt, status, error, issues, downstream?, human_note?}`. On the next call of the same agent the whole list is handed over as `attempt_history`, together with the last delivered artifact as `previous_rtl` / `previous_testbench`. The writer prompt then switches to revise mode and receives the old code as `previous_code`.

Blame is written into this memory by `blame.apply_blame`: when a simulation fails, the analyst's `likely_cause` selects a stage and a `downstream` note is appended to that stage's latest entry. The note carries the analyst's `summary` and `suggested_fix`, a `fixed_rule` when a deterministic rule fired, up to 8 `failing_beats` rows and up to 4,000 characters of `log_excerpt`. When the same stage has been blamed `MAX_RETRIES_PER_STAGE = 2` times, a `second_opinion` note goes to the other candidate stage as well.

### 4.2 Conversational memory: the `messages` list

The manager is a LangChain `create_agent` loop. Its chat starts with one `HumanMessage` (the requirement) and grows by one `ToolMessage` per agent call, containing the agent's full result as JSON (`steps.py:55`). The chat is never summarised or trimmed. Two things limit its growth:

- the hard budget of `MAX_ITERATIONS = 25` tool calls, enforced by the LangGraph recursion limit of 2 × 25 + 1;
- the status board, rebuilt before every manager call from `stages`, which keeps the manager's picture of the run current without re-reading old tool answers. Each error is cut to 120 characters there.

The orchestrator graph has no checkpointer. A pending human question lives in a process-local dict (`human_input.py`), so a server restart loses the run and the question.

### 4.3 Human memory

When a stage has used its three calls, the guard asks the person. Their answer is stored in the latest `attempt_history` entry as `human_note` and the stage's `retry_limit` is raised by 2 (`guard.py:93-101`). The note travels to the agent on its next call like any other history entry.

## 5. Durable records: long-term episodic memory that agents never read

Everything below survives restarts and is written on every run. None of it is consulted by any agent at decision time; it exists for people, the UI and resume.

| Store | Tables / layout | Written by | Read by |
|---|---|---|---|
| `observability/traces.db` | `agent_runs` (294 rows), `agent_events` (97,626), `llm_calls` (15,528), `tool_calls` (23,242), legacy `traces` (89,488), `steps` | `AgentRunRecorder`, a LangChain callback injected by `observe_run(...)`; the orchestrator adds `agent_input`, `agent_output` and `state` snapshots (`tracing.py`) | web UI, Streamlit, people debugging a run |
| `checkpoints/spec_analyzer.db` | LangGraph `checkpoints`, `writes` (1,816 / 9,499 rows) | `SqliteSaver`, one row per node step, keyed by `thread_id` = session id | `resume(session_id, answer)` to continue a session that is waiting for the user |
| `checkpoints/pipeline.db` | same schema (68 / 249 rows) | the older linear pipeline agent | its own resume |
| `projects/<name>/` | `spec.json`, `src/*.vhd`, `tb/*.vhd`, `test_vectors_*.csv`, `work_proj/` (xsim output), `history/<src|tb>_<file>_<HHMMSS>.vhd` | `project_writer`, which copies any overwritten file into `history/` first | simulation and implementation agents load the current files; `history/` is for people |
| `runs/test_vectors/<run>/` | `gen_test_vectors.m`, `gen_test_vectors.failedN.m` | MATLAB guard | people |

Every `llm_calls` row stores the full input message list, the output, token counts and a `prompt_version`: an 8-character hash of the system prompt, so runs can be compared across prompt edits. Tool payloads are stored whole up to 300,000 characters; beyond that the head and the last 20,000 characters are kept, where a testbench prints its result marker.

Only the spec analyser is checkpointed, because it is the one agent that pauses for a human mid-graph (`interrupt()` in `ask_user_node`). Its state keeps `user_feedback` as a growing list of answers and `answers_applied` as the count already used by the generator, so a resumed session knows which answers are new.

## 6. Semantic memory: what the agents know

Knowledge is stored as text files and loaded on every call. Nothing is embedded or retrieved by similarity; the whole relevant set is placed in the system prompt.

| Agent | Knowledge source | Selection rule |
|---|---|---|
| RTL | `skills/RTL/*.md` (6 files: language rules, 93 and 2008 design rules, Vivado guidelines, pipeline stage map, streaming datapath recipe) | `skill_matches_version(name, version)`: the file for the other VHDL version is skipped, the rest are loaded (`rtl_agent.py:65`) |
| Testbench | `skills/TESTBENCH/vhdl_2008_testbench_rules.md` | all files |
| Test vectors | `skills/TEST_VECTOR/matlab_golden_model_rules.md` | all files |
| Spec analyser, simulation, implementation | system prompt only | none |

Files are read through the file-server MCP, not `open()`, so every read appears as a tool call in the run record (`agents/skill_loader.py`). A load failure is a warning, not a failure: the agent continues with `skills = {}`. The loaded text is appended to the system prompt under one heading per file. The planner is told only the file names (`skill_names`).

The spec itself is the shared factual memory of a run. It is handed to every agent unchanged and written to `projects/<name>/spec.json`.

## 7. Procedural memory: lessons encoded as code

The project's learning loop is manual: a failed run is diagnosed from `traces.db`, and the lesson is written into one of three places. The agents then apply it on every later run without needing to remember the incident.

| Where the lesson goes | Examples | Effect at run time |
|---|---|---|
| Rule file (`skills/`) | product widths as named constants, one process per pipeline stage, closed loops on real values, sample-history indexing | the writer LLM reads it as an instruction |
| Deterministic checker | RTL linter width checks, testbench linter, vector validator (`constant_input`, `beat_rate_mismatch`, `consecutive_output_beats`), MATLAB guard (`loop_index_overruns`, `missing_beat_count_assert`) | the draft is rejected with a `fix` text before it leaves the agent |
| Analyst rule | `fixed_rule_hint`, `forced_vector_cause`, `python_check`, `mismatches_capped` | the simulation verdict and blame are decided by code when the pattern is recognised, and the exact rule text travels to the blamed agent word for word |

Guards are the procedural memory of the planners: deterministic rules that override the planner LLM's choice (no finalize while issues remain, a question must be present for `ask_user`, iteration and stall caps). They apply the same way on every call.

## 8. Forgetting: the caps that keep memory small

| Boundary | Cap | Reason |
|---|---|---|
| Blame note to the recalled agent | 8 failing rows, 4,000 chars of log, 20 error lines | enough to see the pattern, small enough for the writer prompt |
| Analyst log | 3,000 chars head + 6,000 chars tail | the result marker and totals are at the end |
| Status board | 120 chars per error | the board is appended to every manager call |
| Human question | 300 chars per attempt reason | one readable question |
| Stored tool payload | 300,000 chars, else head + 20,000-char tail | the UI shows the whole xsim log |
| Decider input | no code at all | the planner judges progress, not design |
| Agent call budget | 10 planner decisions, 2 stalled fixes | ends loops that do not converge |
| Manager budget | 25 tool calls, 3 calls per artifact stage | bounds the chat and the cost |

## 9. What is not there

- **No cross-run memory.** A new run of the same requirement starts from an empty state; it does not look at `projects/` or `traces.db`. Two runs of the same design on the same day cannot learn from each other except through a person editing the rule files.
- **No retrieval.** Rule files are loaded whole. Nothing is embedded, indexed or searched.
- **No chat compaction.** The manager's `messages` list grows until the run ends. The budget bounds it, not a summariser.
- **Checkpointing only where a human pause exists.** The orchestrator and the four worker agents are not resumable after a restart.
- **Observability is write-only for agents.** `llm_calls` and `tool_calls` are the richest record in the system and no agent consults them.

Candidate next steps, in order of value: a per-requirement summary of past runs handed to the manager as a tool answer; trimming or summarising old `ToolMessage`s once the chat passes a token threshold; a checkpointer for the orchestrator so a stopped run can be resumed with its `stages` intact.

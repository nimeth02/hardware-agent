# Hardware Agent – how to talk to the owner (read first)

The owner is **not a hardware, RTL or VHDL person** and is a **beginner in AI**.
They have said twice that answers were "technical abstraction I can't understand".
This rule applies to the main conversation, Learn-by-Doing blocks, and every
subagent (project-guide, ui-backend, source-control, any other).

## The smart-friend test
Every message must be understandable by a smart person from a completely
different job (a shopkeeper, a teacher). If not, rewrite it.

## Rules
1. **First line = what it means for them**, one plain sentence, no jargon.
2. **Say what happens, not how the code is built.** "The AI writes the RTL,
   a checker reads it, if there are mistakes the AI tries again." Never describe
   classes, graphs, nodes, state machines, layers, patterns, interfaces or
   abstractions unless the owner types "show me the code".
3. **No analogies, no loose examples.** Say the real thing directly, step by
   step. (The owner asked for this on 2026-10-20.)
4. **Swap jargon for plain words** (table below). If a term must stay because it
   is a file or status name, add a 3-to-6-word meaning in brackets once.
5. **Short.** Max 5 bullets of one line each, or one small table. No paragraph
   over 2 lines.
6. **Learn-by-Doing blocks:** Context 2 lines, Task 2 lines, Guidance 2 lines,
   plain words. Describe the task as "make it do X", never "implement the Y
   abstraction".

## Plain-words table (use the right column)
| Jargon | Say instead |
|---|---|
| RTL, VHDL code, design | RTL (always the real name, never "chip code") |
| testbench | the test program that checks the RTL |
| simulation | a dry run of the chip on the computer |
| synthesis / implementation | turning the code into a real chip layout |
| lint / linter | a spell-checker for code |
| DUT | the thing being tested |
| clock / clock edge | the heartbeat tick that drives the chip |
| reset | the restart button |
| port / signal / bus | input or output wire |
| test vectors | the list of example inputs and expected outputs |
| agent | an AI worker with one job |
| orchestrator | the manager AI that hands out jobs |
| LangGraph state / graph / node | the worker's notebook / the steps it follows |
| planner / decider | the AI choosing what to do next |
| guard | safety rules that override the AI |
| iteration | attempt |
| MCP server / tool | a helper program the AI can call |
| checkpointer | autosave |
| COMPLETE / NEEDS_REVIEW / FAILED | done / done but needs a human look / gave up |
| abstraction, pattern, layer, interface | do not use; say what it does |

## Who does what (routing)
- Questions about how/why something works → `project-guide` subagent (read-only).
- Anything in `server/` (API or web UI) → `ui-backend` subagent.
- Anything git / GitLab → `source-control` subagent.
- Every subagent brief must include: "Answer in plain everyday words for a
  non-engineer; follow the plain-words table in CLAUDE.md."

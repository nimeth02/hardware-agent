// server/static/app.js — tiny vanilla-JS UI: agent lab, pipeline, observability viewer

function $(sel) { return document.querySelector(sel); }
function $all(sel) { return document.querySelectorAll(sel); }

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function renderSuggestions(listSel, suggestions) {
  const list = $(listSel);
  list.innerHTML = "";
  const items = suggestions || [];
  if (items.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "No issues found — spec looks structurally complete.";
    list.appendChild(li);
    return;
  }
  items.forEach((s) => {
    const li = document.createElement("li");
    li.textContent = s;
    list.appendChild(li);
  });
}

function formatMs(ms) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;
}

function renderTimings(listSel, timings) {
  const list = $(listSel);
  list.innerHTML = "";
  const items = timings || [];
  if (items.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "No step timings recorded.";
    list.appendChild(li);
    return;
  }
  let total = 0;
  items.forEach((t) => {
    total += t.duration_ms || 0;
    const li = document.createElement("li");
    const statusLabel = t.status === "interrupted" ? "paused" : t.status || "";
    li.innerHTML = `
      <span class="t-step">${escapeHtml(t.step)}</span>
      <span class="t-status ${escapeHtml(statusLabel)}">${escapeHtml(statusLabel)}</span>
      <span class="t-ms">${formatMs(t.duration_ms || 0)}</span>`;
    list.appendChild(li);
  });
  const totalLi = document.createElement("li");
  totalLi.className = "t-total";
  totalLi.innerHTML = `<span class="t-step">total</span><span class="t-status"></span><span class="t-ms">${formatMs(total)}</span>`;
  list.appendChild(totalLi);
}

function renderLint(listSel, lint) {
  fillLintList($(listSel), lint);
}

function fillLintList(list, lint) {
  list.innerHTML = "";
  const issues = (lint && lint.issues) || [];
  const via = lint && lint.compiler ? `static checks + ${lint.compiler}` : "static checks only";
  if (issues.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = `No lint issues (${via}).`;
    list.appendChild(li);
    return;
  }
  issues.forEach((i) => {
    const li = document.createElement("li");
    li.textContent = `[${i.code}] ${i.detail}`;
    list.appendChild(li);
  });
}

// ── Tabs ─────────────────────────────────────────────────────────────
$all("header .tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    $all("header .tab-btn").forEach((b) => b.classList.remove("active"));
    $all("main > .tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "observability") loadTraces();
    if (btn.dataset.tab === "agents") initAgentsTab();
  });
});

// ── Agents tab (single-agent lab) ─────────────────────────────────────
let agentsLoaded = false;
let agentCatalog = [];

// Sub-navigation inside the Agents tab: "Run" (input + current run) and
// "Previous runs" (history). Opening a run from history jumps back to Run.
function showAgentsView(name) {
  $all(".subnav .tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.subtab === name));
  $all("#tab-agents .subtab").forEach((v) => v.classList.toggle("active", v.id === `agents-view-${name}`));
  if (name === "history") loadAgentRuns();
}
$all(".subnav .tab-btn").forEach((btn) => btn.addEventListener("click", () => showAgentsView(btn.dataset.subtab)));

async function initAgentsTab() {
  if (agentsLoaded) return;
  agentsLoaded = true;
  agentCatalog = await (await fetch("/api/agents")).json();
  const sel = $("#agent-select");
  agentCatalog.forEach((a) => {
    const opt = document.createElement("option");
    opt.value = a.name;
    opt.textContent = `${a.name} v${a.version}`;
    sel.appendChild(opt);
  });
  sel.addEventListener("change", onAgentChange);
  onAgentChange();
}

// xsim's `run <time>` wants a plain decimal plus unit ("10us", "2.5 ms");
// anything else (scientific notation, bare numbers) hangs the simulator.
// Same pattern as the server (app.py) and the Vivado MCP server.
const SIM_TIME_RE = /^\s*\d+(\.\d+)?\s*(fs|ps|ns|us|ms|s)\s*$/i;
const SIM_TIME_HINT = "sim_time must be a decimal number followed by fs/ps/ns/us/ms/s, e.g. 10us or 2.5ms";
function simTimeError(input) {
  if (!input || typeof input !== "object" || !("sim_time" in input)) return null;
  const v = input.sim_time;
  if (v == null || v === "") return null;  // the agent applies its default (10us)
  return typeof v === "string" && SIM_TIME_RE.test(v) ? null : `Invalid sim_time ${JSON.stringify(v)}: ${SIM_TIME_HINT}.`;
}

function onAgentChange() {
  const a = agentCatalog.find((x) => x.name === $("#agent-select").value);
  if (!a) return;
  $("#agent-desc").textContent = a.description;
  const template = { ...(a.input_template || {}) };
  if ("sim_time" in template && !template.sim_time) template.sim_time = "10us";
  $("#agent-input").value = JSON.stringify(template, null, 2);
  $("#agent-input-hint").textContent = "sim_time" in template ? `${SIM_TIME_HINT} (default 10us).` : "";
  // "Fill from latest runs" only makes sense when the template has a field an
  // upstream agent produces (spec / rtl / testbench).
  const fillable = Object.keys(PRODUCERS).some((k) => k in (a.input_template || {}));
  $("#agent-fill-btn").classList.toggle("hidden", !fillable);
  renderAgentExamples(a);
  loadAgentRuns();
}

// Whole-input examples for agents whose input is not free text: a chip
// replaces the JSON input with the example object.
const AGENT_EXAMPLES = {
  simulation_agent: [
    { label: "counter4 project (10us)", input: { project: "counter4", sim_time: "10us" } },
  ],
  implementation_agent: [
    { label: "counter4 project (xc7a35tcpg236-1)", input: { project: "counter4", part: "xc7a35tcpg236-1" } },
  ],
  orchestrator: [
    { label: "4-bit up counter (full flow)", input: { requirement_text: "4-bit up counter: clk 100 MHz, rst active-high synchronous, en input, count output, wraps from 15 to 0." } },
  ],
};

// Example requirements for agents that take free text (spec_text): a chip
// fills that field of the JSON input, keeping the rest of the template.
function renderAgentExamples(agent) {
  const row = $("#agent-examples-row");
  row.innerHTML = "";
  (AGENT_EXAMPLES[agent.name] || []).forEach((ex) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "example-btn";
    btn.textContent = ex.label;
    btn.addEventListener("click", () => {
      $("#agent-input").value = JSON.stringify(ex.input, null, 2);
    });
    row.appendChild(btn);
  });
  if (!("spec_text" in agent.input_template)) return;
  EXAMPLE_SPECS.forEach((ex) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "example-btn";
    btn.textContent = ex.label;
    btn.addEventListener("click", () => {
      $("#agent-input").value = JSON.stringify({ ...agent.input_template, spec_text: ex.text }, null, 2);
    });
    row.appendChild(btn);
  });
}

// ── Chaining agents: feed one agent's result into another's input ─────
// Input keys that are produced by an upstream agent, and the result status
// values under which the produced value is usable.
const PRODUCERS = {
  spec: { agent: "spec_analyzer", statuses: ["COMPLETE"] },
  rtl: { agent: "rtl_agent", statuses: ["COMPLETE", "NEEDS_REVIEW"] },
  testbench: { agent: "testbench_agent", statuses: ["COMPLETE", "NEEDS_REVIEW"] },
};

function producedValue(run, key) {
  const fr = run && run.final_result;
  const p = PRODUCERS[key];
  if (!p || !fr || typeof fr !== "object" || run.agent_name !== p.agent) return undefined;
  if (!p.statuses.includes(fr.status) || fr[key] == null) return undefined;
  return fr[key];
}

function runLabel(run) {
  return `run ${run.id.slice(0, 8)} (${new Date(run.started_at * 1000).toLocaleString()})`;
}

// Name of the single string field of an agent's template (orchestrator ->
// requirement_text, spec_analyzer -> spec_text), or null when the agent
// takes a richer input.
function plainTextField(agent) {
  const keys = Object.keys((agent && agent.input_template) || {});
  return keys.length === 1 && typeof agent.input_template[keys[0]] === "string" ? keys[0] : null;
}

// Parse the input box for `agent`. Valid JSON is used as-is; anything else
// is wrapped into the agent's single text field when it has one. Returns
// { input } or { error }.
function parseAgentInput(agent) {
  const raw = $("#agent-input").value;
  try {
    return { input: JSON.parse(raw) };
  } catch (err) {
    const field = plainTextField(agent);
    const text = raw.trim();
    if (field && text) return { input: { ...agent.input_template, [field]: text } };
    if (field) return { error: `Input is empty: type the requirement text for ${agent.name}.` };
    const fields = Object.keys((agent && agent.input_template) || {}).join(", ");
    const textAgents = agentCatalog.filter(plainTextField).map((a) => a.name).join(", ");
    return {
      error: `Input must be JSON with fields: ${fields || "(none)"} — or pick an agent that takes plain text (${textAgents}). JSON error: ${err.message}`,
    };
  }
}

// Current textarea input if usable (JSON object or wrapped plain text), else
// the selected agent's template.
function currentAgentInput() {
  const a = agentCatalog.find((x) => x.name === $("#agent-select").value);
  const v = parseAgentInput(a).input;
  if (v && typeof v === "object" && !Array.isArray(v)) return v;
  return a ? { ...a.input_template } : {};
}

// Latest run of the producer whose final_result carries `key` with an
// acceptable status; null when none of the recent runs qualifies.
async function findLatestProduced(key) {
  const p = PRODUCERS[key];
  const runs = await (await fetch(`/api/agent-runs?agent_name=${encodeURIComponent(p.agent)}&limit=20`)).json();
  for (const r of runs) {
    const res = await fetch(`/api/agent-runs/${r.id}`);
    if (!res.ok) continue;
    const run = await res.json();
    const value = producedValue(run, key);
    if (value !== undefined) return { run, value };
  }
  return null;
}

$("#agent-fill-btn").addEventListener("click", async () => {
  const status = $("#agent-run-status");
  const btn = $("#agent-fill-btn");
  status.classList.remove("error");
  const input = currentAgentInput();
  const keys = Object.keys(PRODUCERS).filter((k) => k in input);
  if (!keys.length) {
    status.textContent = "This input has no spec / rtl / testbench field to fill.";
    return;
  }
  status.textContent = `Looking up latest ${keys.join(", ")}...`;
  btn.disabled = true;
  const filled = [];
  const missing = [];
  try {
    for (const key of keys) {
      const hit = await findLatestProduced(key);
      if (hit) {
        input[key] = hit.value;
        filled.push(`${key} from ${runLabel(hit.run)}`);
      } else {
        missing.push(`${key} (no ${PRODUCERS[key].agent} run with a usable result)`);
      }
    }
    $("#agent-input").value = JSON.stringify(input, null, 2);
    status.textContent = (filled.length ? `Filled ${filled.join("; ")}.` : "Nothing filled.") +
      (missing.length ? ` Not found: ${missing.join("; ")}.` : "");
    status.classList.toggle("error", filled.length === 0);
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    status.classList.add("error");
  } finally {
    btn.disabled = false;
  }
});

// On a displayed producer run: one "Use as input for <agent>" button per
// agent whose template takes the produced key.
function renderUseAsInput(run) {
  const row = $("#agent-use-row");
  row.innerHTML = "";
  const key = Object.keys(PRODUCERS).find((k) => PRODUCERS[k].agent === run.agent_name);
  const value = key ? producedValue(run, key) : undefined;
  const consumers = value === undefined ? [] : agentCatalog.filter((a) => a.input_template && key in a.input_template);
  if (!consumers.length) { row.classList.add("hidden"); return; }
  consumers.forEach((a) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "example-btn";
    btn.textContent = `Use as input for ${a.name}`;
    btn.addEventListener("click", () => {
      const sel = $("#agent-select");
      if (sel.value !== a.name) {
        sel.value = a.name;
        sel.dispatchEvent(new Event("change"));
      }
      const input = currentAgentInput();
      input[key] = value;
      $("#agent-input").value = JSON.stringify(input, null, 2);
      const status = $("#agent-run-status");
      status.classList.remove("error");
      status.textContent = `${key} filled from run ${run.id.slice(0, 8)}`;
      showAgentsView("run");
      $("#agent-input").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    row.appendChild(btn);
  });
  row.classList.remove("hidden");
}

$("#agent-run-btn").addEventListener("click", async () => {
  const status = $("#agent-run-status");
  status.classList.remove("error");
  const name = $("#agent-select").value;
  const parsed = parseAgentInput(agentCatalog.find((x) => x.name === name));
  if (parsed.error) {
    status.textContent = parsed.error;
    status.classList.add("error");
    return;
  }
  const input = parsed.input;
  const simErr = simTimeError(input);
  if (simErr) {
    status.textContent = simErr;
    status.classList.add("error");
    return;
  }
  status.textContent = `Starting ${name}...`;
  $("#agent-run-btn").disabled = true;
  try {
    const res = await fetch(`/api/agents/${name}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Request failed");
    watchRun(data.run_id, status, $("#agent-run-btn"), name);
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    status.classList.add("error");
    $("#agent-run-btn").disabled = false;
  }
});

// Follow a running agent over SSE: redraw the run on every recorded event,
// finish on `done`. Returns nothing; the status span/button are updated here.
let runStream = null;

function watchRun(runId, statusEl, buttonEl, agentName) {
  if (runStream) runStream.close();
  const started = performance.now();
  let redrawPending = false;
  const redraw = () => {
    if (redrawPending) return;
    redrawPending = true;
    showAgentRun(runId).finally(() => { redrawPending = false; });
  };

  $("#agent-run-panel").classList.remove("hidden");
  redraw();
  statusEl.textContent = "running... (live)";
  statusEl.classList.remove("cancelled");
  showStopButton(runId);
  runStream = new EventSource(`/api/agent-runs/${runId}/stream`);
  runStream.addEventListener("step", (e) => {
    const ev = JSON.parse(e.data);
    const verb = stopRequested ? "stopping… finishing the current step:" : "running...";
    statusEl.textContent = `${verb} ${ev.event_type}${ev.data && (ev.data.node || ev.data.stage) ? " " + (ev.data.node || ev.data.stage) : ""}` +
      (ev.event_type === "llm_call" ? ` — ${ctxText(llmContext(ev))}` : "");
    redraw();
  });
  runStream.addEventListener("done", (e) => {
    const run = JSON.parse(e.data);
    runStream.close();
    runStream = null;
    hideStopButton();
    const agent = run.final_result || {};
    const stopped = run.status === "stopped";
    statusEl.textContent = `${run.status}${agent.status ? " · " + agent.status : ""} in ${((performance.now() - started) / 1000).toFixed(1)}s`;
    statusEl.classList.toggle("error", run.status === "failed" || (!stopped && ["FAILED", "NEEDS_REVIEW"].includes(agent.status)));
    statusEl.classList.toggle("cancelled", stopped || agent.status === "CANCELLED");
    showAgentRun(runId).then(loadAgentRuns);
    buttonEl.disabled = false;
  });
  runStream.addEventListener("error", () => {
    statusEl.textContent = "stream lost — click Refresh to reload";
    statusEl.classList.add("error");
    if (runStream) { runStream.close(); runStream = null; }
    hideStopButton();
    buttonEl.disabled = false;
  });
}

// ── Stop button: visible only while watchRun follows a live run ───────
// Stopping is cooperative: the server flips a flag the agent checks at its
// next step (LLM call, tool call or graph node), so a step already in
// flight finishes first; the run then ends with status "stopped".
let stopRequested = false;

function showStopButton(runId) {
  const btn = $("#agent-stop-btn");
  stopRequested = false;
  btn.dataset.runId = runId;
  btn.textContent = "Stop";
  btn.disabled = false;
  btn.classList.remove("hidden");
}

function hideStopButton() {
  const btn = $("#agent-stop-btn");
  btn.classList.add("hidden");
  delete btn.dataset.runId;
  stopRequested = false;
}

$("#agent-stop-btn").addEventListener("click", async () => {
  const btn = $("#agent-stop-btn");
  const runId = btn.dataset.runId;
  if (!runId) return;
  const status = $("#agent-run-status");
  btn.disabled = true;
  btn.textContent = "stopping…";
  try {
    const res = await fetch(`/api/agent-runs/${runId}/stop`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Request failed");
    stopRequested = true;
    status.classList.remove("error");
    status.textContent = "stopping… the current step finishes first";
  } catch (err) {
    status.textContent = `Could not stop: ${err.message}`;
    status.classList.add("error");
    btn.disabled = false;
    btn.textContent = "Stop";
  }
});

$("#agent-runs-refresh").addEventListener("click", loadAgentRuns);
$("#agent-orch-expand-all").addEventListener("click", () => setAllOrchGroups(true));
$("#agent-orch-collapse-all").addEventListener("click", () => setAllOrchGroups(false));
$("#agent-events-expand-all").addEventListener("click", () => setAllEventDetails(true));
$("#agent-events-collapse-all").addEventListener("click", () => setAllEventDetails(false));

async function loadAgentRuns() {
  const name = $("#agent-select").value;
  const runs = await (await fetch(`/api/agent-runs?agent_name=${encodeURIComponent(name)}&limit=30`)).json();
  const tbody = $("#agent-runs");
  tbody.innerHTML = "";
  runs.forEach((r) => {
    const tr = document.createElement("tr");
    const agentBad = r.agent_status && ["FAILED", "NEEDS_REVIEW"].includes(r.agent_status);
    const stopped = r.status === "stopped" || r.agent_status === "CANCELLED";
    const cls = stopped ? "badge-cancelled"
      : r.status === "failed" || agentBad ? "badge-error"
      : r.status === "completed" ? "badge-ok" : "";
    // A run that "completed" but whose report says FAILED is shown as FAILED, not completed.
    const statusText = r.agent_status
      ? (r.status === "completed" && r.agent_status === "FAILED" ? r.agent_status : `${r.status} · ${r.agent_status}`)
      : r.status;
    const errorText = r.error || r.agent_error || r.stop_reason || "";
    tr.innerHTML = `
      <td>${new Date(r.started_at * 1000).toLocaleString()}</td>
      <td>${escapeHtml(r.agent_name)} v${escapeHtml(r.agent_version || "")}</td>
      <td><span class="${cls}">${escapeHtml(statusText)}</span></td>
      <td>${r.duration_ms != null ? formatMs(r.duration_ms) : "-"}</td>
      <td class="${errorText ? (stopped ? "badge-cancelled" : "badge-error") : ""}">${escapeHtml(errorText.slice(0, 80))}</td>`;
    tr.dataset.runId = r.id;
    tr.addEventListener("click", async () => {
      await showAgentRun(r.id);
      showAgentsView("run");
      $("#agent-run-panel").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    tbody.appendChild(tr);
  });
}

function markSelectedRun(runId) {
  $all("#agent-runs tr").forEach((tr) => tr.classList.toggle("selected", tr.dataset.runId === runId));
}

// The recorder stores short previews in the event itself ("... [truncated N
// chars]"); the row's detail panel shows the full record, so the marker is
// dropped from the one-line summary.
function stripTruncMarker(s) { return String(s ?? "").replace(/\.{3} \[truncated \d+ chars\]\s*$/, "…"); }

// Context size of an LLM call: how much the model was sent (input tokens),
// how much it answered (output tokens) and how many chat messages went in.
// New runs carry these on the llm_call event itself; older runs only have
// them on the stored run.llm_calls record (evtCtx.llms), so fall back to it.
const CTX_WARN_TOKENS = 16000, CTX_HIGH_TOKENS = 32000;
function llmContext(e) {
  const d = e.data || {};
  const rec = (evtCtx.run && evtCtx.llms[e.step]) || {};
  const pick = (a, b) => (a != null ? a : b != null ? b : null);
  return {
    input: pick(d.input_tokens, rec.input_tokens),
    output: pick(d.output_tokens, rec.output_tokens),
    messages: pick(d.messages, Array.isArray(rec.input) ? rec.input.length : null),
  };
}
function fmtTok(n) { return n == null ? "?" : Number(n).toLocaleString(); }
function ctxText(c) { return `ctx ${fmtTok(c.input)}${c.input != null ? " tok" : ""}`; }
function ctxClass(c) {
  return c.input == null ? "" : c.input > CTX_HIGH_TOKENS ? "ctx-high" : c.input > CTX_WARN_TOKENS ? "ctx-warn" : "";
}
function ctxBadgeHtml(e) {
  const c = llmContext(e);
  const parts = [ctxText(c)];
  if (c.output != null) parts.push(`out ${fmtTok(c.output)}`);
  if (c.messages != null) parts.push(`${fmtTok(c.messages)} msgs`);
  return `<span class="ctx-badge ${ctxClass(c)}" title="context sent to the model (input tokens) | answer size | chat messages sent">${escapeHtml(parts.join(" | "))}</span>`;
}

function eventDetail(e) {
  const d = e.data || {};
  switch (e.event_type) {
    case "agent_step": return `node: ${d.node}`;
    case "llm_call": return (d.tool_calls && d.tool_calls.length ? `-> tools: ${d.tool_calls.join(", ")}` : "") +
      (d.output_preview ? ` ${stripTruncMarker(d.output_preview)}` : "");
    case "tool_call": return `${d.tool}(${JSON.stringify(d.input).slice(0, 160)})`;
    case "tool_result": return `${d.tool}: ${stripTruncMarker(d.output_preview)}`;
    case "error": return d.error || d.llm_error || "";
    case "run_resumed": return `answer: ${(d.input && d.input.answer) || ""}`;
    case "question": return `The AI asks: ${d.question || ""}` +
      (d.suggestions && d.suggestions.length ? `  [suggested: ${d.suggestions.join(" | ")}]` : "");
    case "answer": return `You answered: ${d.answer || ""}`;
    case "agent_input":
    case "agent_output":
    case "state": return orchMetaSummary(e);
    default: return d.agent || d.error || "";
  }
}

function detailsBlock(title, body) {
  const det = document.createElement("details");
  det.className = "msg-block";
  const sum = document.createElement("summary");
  sum.textContent = title;
  const pre = document.createElement("pre");
  pre.style.margin = "8px 0 0";
  pre.style.whiteSpace = "pre-wrap";
  pre.textContent = body;
  det.appendChild(sum);
  det.appendChild(pre);
  return det;
}

// Copy text to the clipboard; falls back to a hidden textarea + execCommand
// for non-secure contexts (plain http on a LAN address).
async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall through */ }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  document.body.removeChild(ta);
  return ok;
}

// Render a "Generated code" section for every top-level key of a result whose
// value is an object with a string `code` field (e.g. rtl, testbench).
function renderCodeBlocks(containerSel, result) {
  const box = $(containerSel);
  box.innerHTML = "";
  if (!appendCodeBlocks(box, result)) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
}

// Append "Generated code" + one copyable block per `{code}` entry of `result`
// to `box`; returns the number of blocks added.
function appendCodeBlocks(box, result) {
  const entries = result && typeof result === "object"
    ? Object.entries(result).filter(([, v]) => v && typeof v === "object" && typeof v.code === "string" && v.code.length)
    : [];
  if (!entries.length) return 0;
  const label = document.createElement("label");
  label.textContent = "Generated code";
  box.appendChild(label);
  entries.forEach(([key, v]) => {
    const head = document.createElement("div");
    head.className = "code-block-head";
    const title = document.createElement("span");
    title.textContent = `${key} — entity ${v.entity_name || "?"}`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.textContent = "Copy";
    btn.addEventListener("click", async () => {
      const ok = await copyText(v.code);
      btn.textContent = ok ? "Copied" : "Copy failed";
      setTimeout(() => { btn.textContent = "Copy"; }, 1500);
    });
    head.appendChild(title);
    head.appendChild(btn);
    const pre = document.createElement("pre");
    pre.className = "result-box code-block vhdl-code";
    pre.textContent = v.code;
    box.appendChild(head);
    box.appendChild(pre);
  });
  return entries.length;
}

// A titled, copyable code block (same head + Copy pattern as renderCodeBlocks).
// `html`, when given, is the (already escaped) markup shown instead of the
// plain text; Copy still copies `text`.
function copyableCodeBlock(title, text, html, extraClass) {
  const frag = document.createDocumentFragment();
  const head = document.createElement("div");
  head.className = "code-block-head";
  const span = document.createElement("span");
  span.textContent = title;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-btn";
  btn.textContent = "Copy";
  btn.addEventListener("click", async () => {
    const ok = await copyText(text);
    btn.textContent = ok ? "Copied" : "Copy failed";
    setTimeout(() => { btn.textContent = "Copy"; }, 1500);
  });
  head.appendChild(span);
  head.appendChild(btn);
  const pre = document.createElement("pre");
  pre.className = "result-box code-block" + (extraClass ? " " + extraClass : "");
  if (html != null) pre.innerHTML = html; else pre.textContent = text;
  frag.appendChild(head);
  frag.appendChild(pre);
  return frag;
}

// ── Small element builders shared by the simulation / implementation panels ──
function panelLine(text, cls) {
  const p = document.createElement("p");
  p.className = cls || "status";
  p.style.margin = "6px 0";
  p.textContent = text;
  return p;
}
function panelLabel(text) {
  const l = document.createElement("label");
  l.style.marginTop = "10px";
  l.textContent = text;
  return l;
}
function panelList(items, emptyText) {
  const ul = document.createElement("ul");
  ul.className = "suggestions-list";
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = emptyText;
    ul.appendChild(li);
  }
  items.forEach((it) => {
    const li = document.createElement("li");
    li.textContent = typeof it === "string" ? it : JSON.stringify(it);
    ul.appendChild(li);
  });
  return ul;
}
// Verdict pill + "STATUS · iteration N · decision_reason" line.
function verdictHead(fr) {
  const verdict = fr.verdict || (fr.status === "FAILED" ? "error" : null);
  const head = document.createElement("div");
  head.className = "row";
  head.style.margin = "0";
  head.style.justifyContent = "flex-start";
  const badge = document.createElement("span");
  badge.className = `verdict-badge verdict-${verdict || "none"}`;
  badge.textContent = verdict ? verdict.toUpperCase() : "NO VERDICT";
  head.appendChild(badge);
  head.appendChild(panelLine(`${fr.status || ""}` + (fr.iteration != null ? ` · iteration ${fr.iteration}` : "") +
    (fr.decision_reason ? ` · ${fr.decision_reason}` : "")));
  return head;
}
// Analysis block (summary, likely cause, failures, suggested fix) appended via `add`.
function appendAnalysis(add, an) {
  if (!an) return;
  add(panelLabel("Analysis"));
  if (an.summary) add(panelLine(an.summary, "sim-text"));
  if (an.likely_cause) add(panelLine(`Likely cause: ${an.likely_cause}`));
  const failures = Array.isArray(an.failures) ? an.failures : [];
  if (failures.length || an.verdict !== "pass") {
    add(panelLabel("Failures"));
    add(panelList(failures, "No failures reported"));
  }
  if (an.suggested_fix) {
    add(panelLabel("Suggested fix"));
    add(panelLine(an.suggested_fix, "sim-text"));
  }
}

// Simulation agent (v2): verdict badge, analysis, project info, simulator
// output, compile errors and warnings from its final_result.
function renderSimulationPanel(run) {
  const block = $("#agent-sim-block");
  const fr = run.final_result;
  if (run.agent_name !== "simulation_agent" || !fr || typeof fr !== "object") { block.classList.add("hidden"); return; }
  const body = $("#agent-sim-body");
  body.innerHTML = "";
  fillSimulationBody(body, fr);
  block.classList.remove("hidden");
}

function fillSimulationBody(body, fr) {
  const add = (el) => body.appendChild(el);
  const line = panelLine;
  const label = panelLabel;
  const list = panelList;

  add(verdictHead(fr));
  if (fr.error) add(line(`Error: ${fr.error}`, "status error"));

  const p = fr.project;
  if (p) {
    add(line(`project ${p.name || "?"} · top ${p.top_entity || "?"} · tb ${p.tb_file || "?"}` +
      ` · src ${(p.src_files || []).join(", ") || "-"} · vectors ${p.test_vectors || "-"}`));
  } else if (fr.project_dir) {
    add(line(`project dir ${fr.project_dir}`));
  }

  appendAnalysis(add, fr.analysis);

  const sim = fr.sim;
  if (sim) {
    add(label("Simulation"));
    add(line(`status ${sim.status || "?"} · outcome ${sim.outcome || "?"} · sim_time ${sim.sim_time || "?"}` +
      (sim.vcd_path ? ` · vcd ${sim.vcd_path}` : "")));
    if (sim.sim_output) add(copyableCodeBlock("sim_output", sim.sim_output));
  }

  const c = fr.compile;
  if (c) {
    add(label("Compile"));
    add(line(`status ${c.status || "?"}` + (c.error_class ? ` · ${c.error_class}` : "")));
    if (c.status === "error") {
      const srcs = (p && p.src_files) || [];
      (c.results || []).forEach((r, i) => {
        if (r.status !== "error" && !r.errors_found) return;
        const name = srcs[i] || `file #${i + 1}`;
        const text = [r.stdout, r.stderr].filter((s) => s && s.trim()).join("\n--- stderr ---\n");
        add(copyableCodeBlock(`${name} (${r.error_class || "error"})`, text || "(no compiler output)"));
      });
    }
  }

  const warnings = Array.isArray(fr.warnings) ? fr.warnings : [];
  if (warnings.length) {
    add(label("Warnings"));
    add(list(warnings, ""));
  }
}

// Implementation agent (v2): verdict badge, project, timing/utilisation
// metrics, analysis, constraints, batch log and warnings from its final_result.
function renderImplementationPanel(run) {
  const block = $("#agent-impl-block");
  const fr = run.final_result;
  if (run.agent_name !== "implementation_agent" || !fr || typeof fr !== "object") { block.classList.add("hidden"); return; }
  const body = $("#agent-impl-body");
  body.innerHTML = "";
  fillImplementationBody(body, fr);
  block.classList.remove("hidden");
}

function fillImplementationBody(body, fr) {
  const add = (el) => body.appendChild(el);

  add(verdictHead(fr));
  if (fr.error) add(panelLine(`Error: ${fr.error}`, "status error"));

  const p = fr.project;
  const part = fr.part || (fr.scripts && fr.scripts.part) || "?";
  if (p) {
    add(panelLine(`project ${p.name || "?"} · top ${p.top_entity || "?"} · src ${(p.src_files || []).join(", ") || "-"}` +
      ` · xdc ${p.xdc_file || "-"} · part ${part}`));
  } else if (fr.project_dir) {
    add(panelLine(`project dir ${fr.project_dir} · part ${part}`));
  }

  // Metrics table: one row per figure; "-" when the metric is absent.
  const m = fr.metrics;
  if (m && typeof m === "object" && Object.keys(m).length) {
    add(panelLabel("Metrics"));
    if (m.unconstrained) add(panelLine("unconstrained: no timing constraints applied, timing figures are not meaningful", "status warn"));
    const num = (v, unit) => (v == null || v === "" ? "-" : `${v}${unit || ""}`);
    const util = (used, pct) => (used == null && pct == null ? "-" : `${num(used)}${pct != null ? ` (${pct}%)` : ""}`);
    const rows = [
      ["WNS", num(m.wns_ns, " ns")], ["TNS", num(m.tns_ns, " ns")], ["WHS", num(m.whs_ns, " ns")],
      ["Failing endpoints", num(m.failing_endpoints)],
      ["LUT", util(m.lut_used, m.lut_pct)], ["FF", util(m.ff_used, m.ff_pct)],
      ["BRAM", util(m.bram_used, m.bram_pct)], ["DSP", util(m.dsp_used, m.dsp_pct)],
    ];
    const table = document.createElement("table");
    table.className = "metrics-table";
    const tbody = document.createElement("tbody");
    rows.forEach(([k, v]) => {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.textContent = k;
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(th);
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    add(table);
    const reports = Array.isArray(m.reports) ? m.reports : [];
    if (reports.length) {
      add(panelLabel("Reports"));
      add(panelList(reports, ""));
    }
  } else if (m && typeof m === "object") {
    add(panelLine("Metrics: none collected"));
  }

  appendAnalysis(add, fr.analysis);

  const s = fr.scripts;
  if (s) {
    add(panelLabel("Constraints"));
    if (s.generated_xdc === false) {
      add(panelLine(`project XDC: ${s.xdc || (p && p.xdc_file) || "?"}`));
    } else if (s.constraints) {
      add(copyableCodeBlock(s.xdc || "generated.xdc", s.constraints));
    } else {
      add(panelLine(`xdc ${s.xdc || "-"} · no constraints text`));
    }
    if (s.tcl) add(panelLine(`tcl ${s.tcl}`));
  }

  const b = fr.batch;
  if (b) {
    add(panelLabel("Batch"));
    add(panelLine(`status ${b.status || "?"}` + (b.error_class ? ` · ${b.error_class}` : "") +
      (b.journal ? ` · journal ${b.journal}` : "") + (b.reports_dir ? ` · reports ${b.reports_dir}` : ""),
      b.status === "error" ? "status error" : "status"));
    if (b.log_excerpt) add(copyableCodeBlock("log_excerpt", b.log_excerpt));
  }

  const warnings = Array.isArray(fr.warnings) ? fr.warnings : [];
  if (warnings.length) {
    add(panelLabel("Warnings"));
    add(panelList(warnings, ""));
  }
}

// Minimal CSV line splitter (handles double-quoted fields with "" escapes).
function splitCsvLine(line) {
  const out = [];
  let cur = "";
  let q = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (q) {
      if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++; }
      else if (ch === '"') { q = false; }
      else { cur += ch; }
    } else if (ch === '"') { q = true; }
    else if (ch === ",") { out.push(cur); cur = ""; }
    else { cur += ch; }
  }
  out.push(cur);
  return out;
}

let vectorsCsvText = "";

// Test vectors panel: CSV preview (first 50 rows) + validation issues.
async function renderTestVectors(validation) {
  const block = $("#agent-vectors-block");
  if (!validation || !validation.csv) { block.classList.add("hidden"); return; }
  const meta = $("#agent-vectors-meta");
  const thead = $("#agent-vectors-table thead");
  const tbody = $("#agent-vectors-table tbody");
  thead.innerHTML = "";
  tbody.innerHTML = "";
  vectorsCsvText = "";
  meta.textContent = "loading...";
  block.classList.remove("hidden");

  fillVectorIssues($("#agent-vectors-issues"), validation.issues);
  vectorsCsvText = await loadVectorsInto(thead, tbody, meta, validation);
}

// Validation issues list ("[code] detail" rows, or an empty marker).
function fillVectorIssues(list, issues) {
  list.innerHTML = "";
  const items = Array.isArray(issues) ? issues : [];
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "No validation issues";
    list.appendChild(li);
    return;
  }
  items.forEach((i) => {
    const li = document.createElement("li");
    li.textContent = `[${i.code}] ${i.detail}`;
    list.appendChild(li);
  });
}

// Fetch the CSV named by `validation.csv`, fill the given table head/body
// (first 50 rows) and meta line; resolves to the CSV text ("" on failure).
async function loadVectorsInto(thead, tbody, meta, validation) {
  let res;
  try { res = await fetch(`/api/run-files?path=${encodeURIComponent(validation.csv)}`); } catch (_) { res = null; }
  if (!res || !res.ok) {
    meta.textContent = `could not load CSV (${res ? res.status : "network error"}): ${validation.csv}`;
    return "";
  }
  const file = await res.json();
  const csvText = file.content || "";
  const lines = csvText.split(/\r?\n/).filter((l) => l.trim().length);
  const header = lines.length ? splitCsvLine(lines[0]) : [];
  const rows = lines.slice(1);
  const trh = document.createElement("tr");
  header.forEach((h) => { const th = document.createElement("th"); th.textContent = h; trh.appendChild(th); });
  thead.appendChild(trh);
  rows.slice(0, 50).forEach((line) => {
    const tr = document.createElement("tr");
    splitCsvLine(line).forEach((c) => { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); });
    tbody.appendChild(tr);
  });
  const cols = (Array.isArray(validation.columns) && validation.columns.length ? validation.columns : header).join(", ");
  meta.textContent = `${rows.length} rows · columns: ${cols}` +
    (rows.length > 50 ? " (showing first 50)" : "") + (file.truncated ? " · file truncated at 2 MB" : "");
  return csvText;
}

$("#agent-vectors-copy").addEventListener("click", async () => {
  const btn = $("#agent-vectors-copy");
  const ok = await copyText(vectorsCsvText);
  btn.textContent = ok ? "Copied" : "Copy failed";
  setTimeout(() => { btn.textContent = "Copy CSV"; }, 1500);
});

$("#agent-vectors-download").addEventListener("click", () => {
  const blob = new Blob([vectorsCsvText], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "test_vectors.csv";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

// ── Orchestrator panel ───────────────────────────────────────────────
// "status · iteration N · decision_reason" line for a sub-agent result.
function statusLine(fr) {
  return panelLine(`${fr.status || "?"}` + (fr.iteration != null ? ` · iteration ${fr.iteration}` : "") +
    (fr.decision_reason ? ` · ${fr.decision_reason}` : ""), fr.status === "FAILED" ? "status error" : "status");
}
function appendWarnings(add, fr) {
  const warnings = Array.isArray(fr.warnings) ? fr.warnings : [];
  if (!warnings.length) return;
  add(panelLabel("Warnings"));
  add(panelList(warnings, ""));
}

// spec_analyzer result: status, pending question, suggestions, spec JSON.
function fillSpecBody(body, fr) {
  const add = (el) => body.appendChild(el);
  add(statusLine(fr));
  if (fr.error) add(panelLine(`Error: ${fr.error}`, "status error"));
  if (fr.pending_question) add(panelLine(`Pending question: ${fr.pending_question}`, "status warn"));
  if (Array.isArray(fr.suggestions)) {
    add(panelLabel("Suggestions"));
    add(panelList(fr.suggestions, "No issues found — spec looks structurally complete."));
  }
  if (fr.spec) add(copyableCodeBlock(`spec.json — entity ${fr.spec.entity_name || "?"}`, JSON.stringify(fr.spec, null, 2)));
}

// rtl_agent / testbench_agent result: status, code block(s), notes, lint, warnings.
function fillCodeAgentBody(body, fr, key) {
  const add = (el) => body.appendChild(el);
  add(statusLine(fr));
  if (fr.error) add(panelLine(`Error: ${fr.error}`, "status error"));
  appendCodeBlocks(body, fr);
  const notes = fr[key] && Array.isArray(fr[key].notes) ? fr[key].notes : [];
  if (notes.length) {
    add(panelLabel("Notes"));
    add(panelList(notes, ""));
  }
  if (fr.lint) {
    add(panelLabel("Lint"));
    const ul = document.createElement("ul");
    ul.className = "suggestions-list";
    fillLintList(ul, fr.lint);
    add(ul);
  }
  appendWarnings(add, fr);
}

// test_vector_agent result: status, run dir, validation issues, CSV preview + Copy.
function fillTestVectorsBody(body, fr) {
  const add = (el) => body.appendChild(el);
  add(statusLine(fr));
  if (fr.error) add(panelLine(`Error: ${fr.error}`, "status error"));
  if (fr.run_dir) add(panelLine(`run dir ${fr.run_dir}`));
  const v = fr.validation;
  if (v) {
    add(panelLabel("Validation"));
    const ul = document.createElement("ul");
    ul.className = "suggestions-list";
    fillVectorIssues(ul, v.issues);
    add(ul);
  }
  if (v && v.csv) {
    const head = document.createElement("div");
    head.className = "code-block-head";
    const meta = document.createElement("span");
    meta.textContent = "loading...";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.textContent = "Copy CSV";
    head.appendChild(meta);
    head.appendChild(btn);
    add(head);
    const wrap = document.createElement("div");
    wrap.className = "vectors-table-wrap";
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const tbody = document.createElement("tbody");
    table.appendChild(thead);
    table.appendChild(tbody);
    wrap.appendChild(table);
    add(wrap);
    let csvText = "";
    btn.addEventListener("click", async () => {
      const ok = await copyText(csvText);
      btn.textContent = ok ? "Copied" : "Copy failed";
      setTimeout(() => { btn.textContent = "Copy CSV"; }, 1500);
    });
    loadVectorsInto(thead, tbody, meta, v).then((text) => { csvText = text; });
  } else if (fr.result) {
    add(copyableCodeBlock("result", JSON.stringify(fr.result, null, 2)));
  }
  appendWarnings(add, fr);
}

// write_project result: status, project dir, files written.
function fillProjectBody(body, fr) {
  const add = (el) => body.appendChild(el);
  add(statusLine(fr));
  if (fr.error) add(panelLine(`Error: ${fr.error}`, "status error"));
  if (fr.project_dir) add(panelLine(`project dir ${fr.project_dir}`));
  add(panelLabel("Files"));
  add(panelList(Array.isArray(fr.files) ? fr.files : [], "No files listed"));
}

// One plain line per item the manager's helpers produced, as far as the run
// got (final_result.results). Missing or null keys are skipped; code text is
// never shown here — the collapsible sections below carry it.
function producedItems(results) {
  const r = results || {};
  const obj = (v) => v && typeof v === "object";
  const items = [];
  if (obj(r.spec)) items.push(`Spec${r.spec.entity_name ? ` for ${r.spec.entity_name}` : ""}`);
  if (obj(r.rtl)) items.push(`Chip code${r.rtl.entity_name ? ` (${r.rtl.entity_name})` : ""}`);
  if (obj(r.testbench)) items.push(`Test program${r.testbench.entity_name ? ` (${r.testbench.entity_name})` : ""}`);
  if (obj(r.test_vectors)) items.push(`Test vectors${r.test_vectors.num_vectors != null ? ` (${r.test_vectors.num_vectors} rows)` : ""}`);
  if (typeof r.project_dir === "string" && r.project_dir) items.push(`Project folder: ${r.project_dir}`);
  if (obj(r.simulation)) items.push(`Dry run: ${r.simulation.verdict || "no verdict"}`);
  if (obj(r.implementation)) items.push(`Chip layout: ${r.implementation.verdict || "no verdict"}`);
  return items;
}

// Order and renderer for each key the orchestrator may put in `results`.
const ORCH_SECTIONS = [
  ["spec", "Spec", fillSpecBody],
  ["rtl", "RTL", (body, fr) => fillCodeAgentBody(body, fr, "rtl")],
  ["testbench", "Testbench", (body, fr) => fillCodeAgentBody(body, fr, "testbench")],
  ["test_vectors", "Test vectors", fillTestVectorsBody],
  ["project", "Project", fillProjectBody],
  ["simulation", "Simulation", fillSimulationBody],
  ["implementation", "Implementation", fillImplementationBody],
];

// Plain-words reason a run ended: the report's stop_reason, or "you pressed Stop"
// for a stopped run with no saved report. Empty string for old runs.
function stopReasonOf(run) {
  const fr = run && run.final_result;
  if (fr && typeof fr === "object" && typeof fr.stop_reason === "string" && fr.stop_reason) return fr.stop_reason;
  if (run && run.status === "stopped" && !(fr && typeof fr === "object" && Object.keys(fr).length)) return "you pressed Stop";
  return "";
}

// Orchestrator: status badge, summary, next steps, error, then one
// collapsible section per sub-agent result present in `results`.
function renderOrchestratorPanel(run) {
  const block = $("#agent-orch-block");
  const fr = run.final_result;
  if (run.agent_name !== "orchestrator" || !fr || typeof fr !== "object") { block.classList.add("hidden"); return; }
  const body = $("#agent-orch-body");
  body.innerHTML = "";
  const add = (el) => body.appendChild(el);

  const head = document.createElement("div");
  head.className = "row";
  head.style.margin = "0";
  head.style.justifyContent = "flex-start";
  const badge = document.createElement("span");
  const status = fr.status || "UNKNOWN";
  badge.className = `verdict-badge orch-${status.toLowerCase()}`;
  badge.textContent = status;
  head.appendChild(badge);
  add(head);
  const why = stopReasonOf(run);
  if (why) add(panelLine(`Why it stopped: ${why}`, "status"));
  if (fr.summary) {
    add(panelLabel("Summary"));
    add(panelLine(fr.summary, "sim-text"));
  }
  if (fr.next_steps) {
    add(panelLabel("Next steps"));
    add(panelLine(fr.next_steps, "sim-text"));
  }
  if (fr.error) add(panelLine(`Error: ${fr.error}`, "status error"));

  const results = fr.results && typeof fr.results === "object" ? fr.results : {};
  const produced = producedItems(results);
  if (produced.length) {
    add(panelLabel("What was produced"));
    add(panelList(produced, ""));
  }
  const present = ORCH_SECTIONS.filter(([key]) => results[key] && typeof results[key] === "object");
  if (!present.length) add(panelLine("No agent results recorded.", "status"));
  present.forEach(([key, title, fill]) => {
    const det = document.createElement("details");
    det.className = "orch-section";
    det.open = true;
    const sum = document.createElement("summary");
    const sub = results[key];
    sum.textContent = `${title} (${key})` + (sub.status ? ` — ${sub.status}` : "");
    det.appendChild(sum);
    const secBody = document.createElement("div");
    secBody.className = "orch-section-body";
    try {
      fill(secBody, sub);
    } catch (err) {
      secBody.appendChild(panelLine(`Could not render: ${err.message}`, "status error"));
      secBody.appendChild(copyableCodeBlock(`${key} (raw)`, JSON.stringify(sub, null, 2)));
    }
    det.appendChild(secBody);
    add(det);
  });
  block.classList.remove("hidden");
}

// ── Event timeline rows (all agents and the orchestrator) ────────────
// One compact row per event; a click opens a full-width detail row below it
// holding the complete record: the event's own data plus the matching
// run.tool_calls / run.llm_calls entry (full arguments, full tool output,
// full prompt and response). Expansion state is keyed
// "<run id>:<event type>:<step>:<ordinal>" and survives the table rebuilds
// watchRun triggers on every SSE event.
const evtOpen = new Map();
let evtRows = [];                                   // rows on screen, for Expand/Collapse all
let evtCtx = { run: null, tools: {}, llms: {}, seen: {} };

function setEventContext(run) {
  const tools = {}, llms = {};
  (run.tool_calls || []).forEach((t) => { (tools[t.step] = tools[t.step] || []).push(t); });
  (run.llm_calls || []).forEach((c) => { llms[c.step] = c; });
  evtCtx = { run, tools, llms, seen: {} };
  evtRows = [];
}
function setAllEventDetails(open) {
  evtRows.forEach((r) => { evtOpen.set(r.key, open); r.refresh(); });
}
// Hide/show a timeline row together with its detail row (used by the
// orchestrator's collapsible groups).
function setRowHidden(tr, hidden) {
  if (tr._evt) tr._evt.setHidden(hidden);
  else tr.classList.toggle("hidden", hidden);
}

function eventRow(e, typeLabel, detail) {
  const tr = document.createElement("tr");
  tr.className = "evt-row";
  const cls = ["ok", "COMPLETE"].includes(e.status) ? "badge-ok" : ["error", "FAILED"].includes(e.status) ? "badge-error"
    : ["NEEDS_REVIEW", "WAITING_FOR_USER"].includes(e.status) ? "badge-warn"
    : ["stopped", "requested"].includes(e.status) ? "badge-cancelled" : "";
  tr.innerHTML = `
      <td data-label="#">${e.step ?? ""}</td>
      <td data-label="Event">${escapeHtml(typeLabel || e.event_type)}</td>
      <td data-label="Detail"><span class="evt-toggle">▸</span>${e.event_type === "llm_call" ? ctxBadgeHtml(e) : ""}<span class="evt-summary">${escapeHtml(detail != null ? detail : eventDetail(e))}</span></td>
      <td data-label="Status"><span class="${cls}">${escapeHtml(e.status || "")}</span></td>
      <td data-label="Duration">${e.duration_ms != null ? formatMs(e.duration_ms) : ""}</td>`;
  const k = `${e.event_type}:${e.step}`;
  const n = (evtCtx.seen[k] = (evtCtx.seen[k] || 0) + 1);
  const key = `${evtCtx.run ? evtCtx.run.id : ""}:${k}:${n}`;
  let detailRow = null;
  const row = { key, tr, isOpen: () => evtOpen.get(key) === true };
  // The detail row is built lazily on first open (LLM prompts can be large)
  // and only once the summary row is in the table.
  row.refresh = () => {
    const open = row.isOpen();
    tr.querySelector(".evt-toggle").textContent = open ? "▾" : "▸";
    tr.classList.toggle("evt-open", open);
    if (open && !detailRow && tr.parentNode) {
      detailRow = document.createElement("tr");
      detailRow.className = "evt-detail";
      tr.classList.forEach((c) => { if (c.startsWith("orch-agent-") || c === "orch-nested") detailRow.classList.add(c); });
      const td = document.createElement("td");
      td.colSpan = 5;
      td.appendChild(buildEventDetail(e));
      detailRow.appendChild(td);
      tr.insertAdjacentElement("afterend", detailRow);
    }
    if (detailRow) detailRow.classList.toggle("hidden", !open || tr.classList.contains("hidden"));
  };
  row.setHidden = (hidden) => {
    tr.classList.toggle("hidden", hidden);
    if (detailRow) detailRow.classList.toggle("hidden", hidden || !row.isOpen());
  };
  tr.addEventListener("click", (ev) => {
    if (ev.target.closest("button, a") || (window.getSelection && String(window.getSelection()))) return;
    evtOpen.set(key, !row.isOpen());
    row.refresh();
  });
  tr._evt = row;
  evtRows.push(row);
  return tr;
}

function prettyJson(v) { return typeof v === "string" ? v : JSON.stringify(v, null, 2); }
function messageText(m) {
  const c = m && m.content;
  return typeof c === "string" ? c : (c == null ? "" : JSON.stringify(c, null, 2));
}

// Tool output as recorded from the LangChain ToolMessage: a plain string or
// an MCP content list [{type:"text", text:...}]. Returns the joined text.
function unwrapToolOutput(output) {
  if (Array.isArray(output)) {
    return output.map((p) => (p && typeof p === "object" && typeof p.text === "string") ? p.text
      : (typeof p === "string" ? p : JSON.stringify(p, null, 2))).join("\n");
  }
  if (output && typeof output === "object") return JSON.stringify(output, null, 2);
  return output == null ? "" : String(output);
}
// String fields of a tool result that are console logs (xsim / xvhdl / Vivado
// batch output): shown as their own log blocks with real newlines.
const LOG_FIELDS = ["sim_output", "stdout", "stderr", "log_excerpt", "log", "elaboration_output", "compile_output"];
function collectLogFields(obj, path, out) {
  if (!obj || typeof obj !== "object") return;
  if (Array.isArray(obj)) { obj.forEach((v, i) => collectLogFields(v, `${path}[${i}]`, out)); return; }
  Object.keys(obj).forEach((k) => {
    const v = obj[k];
    const p = path ? `${path}.${k}` : k;
    if (LOG_FIELDS.includes(k) && typeof v === "string" && v.trim()) out.push({ path: p, text: v });
    else if (v && typeof v === "object") collectLogFields(v, p, out);
  });
}
const TRUNC_NOTE = "This text was cut when it was captured (marker in the text): the recorder stores at most 4000 chars " +
  "of a tool output and the Vivado MCP server caps console output at 4000 chars. Raise those caps for new runs; old runs cannot be recovered.";
function noteIfTruncated(box, text) {
  if (/\[truncated[, ]/.test(text)) box.appendChild(panelLine(TRUNC_NOTE, "status warn"));
}

// True when a tool result carries generated VHDL (rtl / testbench `code` strings).
function hasGeneratedCode(v) {
  return Boolean(v && typeof v === "object" && Object.values(v).some((x) => x && typeof x === "object" && typeof x.code === "string" && x.code.length));
}
function renderToolOutput(box, output) {
  const text = unwrapToolOutput(output);
  let parsed = null;
  try { parsed = JSON.parse(text); } catch (_) { parsed = null; }
  if (parsed && typeof parsed === "object") {
    const logs = [];
    collectLogFields(parsed, "", logs);
    logs.forEach((l) => box.appendChild(copyableCodeBlock(`${l.path} (${l.text.length.toLocaleString()} chars)`, l.text)));
    if (hasGeneratedCode(parsed)) addJsonWithFoldedCode(box, "result (JSON)", parsed, "", true);
    else box.appendChild(copyableCodeBlock("result (JSON)", JSON.stringify(parsed, null, 2)));
  } else {
    box.appendChild(copyableCodeBlock(`output (${text.length.toLocaleString()} chars)`, text || "(empty)"));
  }
  noteIfTruncated(box, text);
}

// ── Orchestrator hand-off / notebook events ──────────────────────────
// Around every helper call the orchestrator records exactly what it handed
// over (agent_input), exactly what came back (agent_output) and its notebook
// (state) before and after. One-line summaries for the timeline rows and
// full panels below; long multi-line strings (VHDL, logs) are folded into
// collapsed blocks so the JSON stays readable.
const ORCH_META_EVENTS = ["agent_input", "agent_output", "state"];
const toJson = (v) => (v === undefined ? "undefined" : JSON.stringify(v, null, 2));

function isCodeString(v) { return typeof v === "string" && (v.split("\n").length >= 4 || v.length > 400); }
function codeLabel(s) {
  const n = s.split("\n").length;
  const kind = /\b(entity|architecture|library\s+ieee|std_logic)\b/i.test(s) ? "VHDL code" : "text";
  return `${kind}, ${n} line${n === 1 ? "" : "s"}`;
}
// Deep copy of `v` with every long string replaced by a short placeholder;
// the strings themselves are pushed to `out` as {path, text}.
function foldCode(v, path, out) {
  if (isCodeString(v)) { out.push({ path, text: v }); return `<< ${codeLabel(v)} — expand below >>`; }
  if (Array.isArray(v)) return v.map((x, i) => foldCode(x, `${path}[${i}]`, out));
  if (v && typeof v === "object") {
    const o = {};
    Object.keys(v).forEach((k) => { o[k] = foldCode(v[k], path ? `${path}.${k}` : k, out); });
    return o;
  }
  return v;
}
function dedupeFolded(list) {
  const seen = new Set();
  return list.filter((f) => !seen.has(f.path) && seen.add(f.path));
}
// Closed <details> holding a copyable code block.
function collapsedCodeBlock(title, text) {
  const det = document.createElement("details");
  det.className = "msg-block code-fold";
  const sum = document.createElement("summary");
  sum.textContent = `${title} (click to expand)`;
  det.appendChild(sum);
  det.appendChild(copyableCodeBlock(title, text));
  return det;
}
// Pretty JSON block followed by one collapsed block per folded string.
function addJsonWithFoldedCode(box, title, value, rootPath, openCode) {
  const folded = [];
  const shown = foldCode(value, rootPath || "", folded);
  box.appendChild(copyableCodeBlock(title, toJson(shown)));
  dedupeFolded(folded).forEach((f) => {
    if (openCode && /(^|\.)code$/.test(f.path)) box.appendChild(copyableCodeBlock(`${f.path} — ${codeLabel(f.text)}`, f.text, null, "vhdl-code"));
    else box.appendChild(collapsedCodeBlock(`${f.path || title} — ${codeLabel(f.text)}`, f.text));
  });
}
// Short description of one value for a one-line summary.
function describeValue(v) {
  if (v == null) return "nothing";
  if (isCodeString(v)) return codeLabel(v);
  if (typeof v === "string") return `"${v.length > 40 ? v.slice(0, 40) + "…" : v}"`;
  if (Array.isArray(v)) return `list of ${v.length}`;
  if (typeof v === "object") {
    const keys = Object.keys(v);
    return `{${keys.slice(0, 5).join(", ")}${keys.length > 5 ? ", …" : ""}}`;
  }
  return String(v);
}
// Keys whose value differs between before and after; at the start (no
// before) the keys that hold something.
function stateChangedKeys(d) {
  const before = d.before, after = d.after || {};
  const empty = (v) => v == null || (Array.isArray(v) && !v.length) || (typeof v === "object" && !Object.keys(v).length);
  if (!before || typeof before !== "object") return Object.keys(after).filter((k) => !empty(after[k]));
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  return [...keys].filter((k) => JSON.stringify(before[k]) !== JSON.stringify(after[k]));
}
function orchMetaSummary(e) {
  const d = e.data || {};
  const stage = d.stage || "?";
  switch (e.event_type) {
    case "agent_input": {
      const items = Array.isArray(d.input) ? d.input : (d.input == null ? [] : [d.input]);
      return `Handed to ${stage}: ${items.length ? items.map(describeValue).join(", ") : "nothing (reads the notebook)"}`;
    }
    case "agent_output": {
      const out = (d.output && typeof d.output === "object") ? d.output : {};
      const status = e.status || out.status || "done";
      const err = out.error ? ` — error: ${String(out.error).slice(0, 120)}` : "";
      const keys = Object.keys(out).filter((k) => !["status", "error"].includes(k) && out[k] != null);
      return `${stage} answered: ${status}${err}${keys.length ? ` (${keys.join(", ")})` : ""}`;
    }
    case "state": {
      const changed = stateChangedKeys(d);
      const what = d.before ? "changed" : "set";
      return `Notebook after ${stage} — ${changed.length ? `${what}: ${changed.join(", ")}` : "nothing changed"}`;
    }
    default: return "";
  }
}
// Side-by-side before / after of the changed notebook fields.
function stateDiffTable(before, after, keys, folded) {
  const table = document.createElement("table");
  table.className = "state-diff";
  table.innerHTML = "<thead><tr><th>field</th><th>before</th><th>after</th></tr></thead>";
  const tbody = document.createElement("tbody");
  keys.forEach((k) => {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.textContent = k;
    tr.appendChild(th);
    [["before", before[k]], ["after", after[k]]].forEach(([side, v]) => {
      const td = document.createElement("td");
      const pre = document.createElement("pre");
      pre.textContent = v === undefined ? "(not set)" : toJson(foldCode(v, `${side}.${k}`, folded));
      td.appendChild(pre);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}
// The whole notebook after the step, changed fields highlighted.
function stateAfterBlock(after, changed, folded) {
  const keys = Object.keys(after);
  const lines = keys.map((k, i) => {
    const text = `  ${JSON.stringify(k)}: ${toJson(foldCode(after[k], `after.${k}`, folded)).replace(/\n/g, "\n  ")}${i < keys.length - 1 ? "," : ""}`;
    return changed.includes(k) ? `<mark class="state-changed">${escapeHtml(text)}</mark>` : escapeHtml(text);
  });
  return copyableCodeBlock("notebook after this step (JSON, changed fields highlighted)", toJson(after), `{\n${lines.join("\n")}\n}`);
}
function buildOrchMetaDetail(box, e) {
  const d = e.data || {};
  const stage = d.stage || "?";
  const add = (n) => box.appendChild(n);
  switch (e.event_type) {
    case "agent_input": {
      const items = Array.isArray(d.input) ? d.input : (d.input == null ? [] : [d.input]);
      add(panelLine(`Handed to ${stage} — ${items.length} item(s)`, "evt-head"));
      if (!items.length) add(panelLine("No arguments: the helper read what it needed from the notebook.", "status"));
      items.forEach((v, i) => addJsonWithFoldedCode(box, `input ${i + 1} of ${items.length}: ${describeValue(v)}`, v, `input[${i}]`));
      break;
    }
    case "agent_output": {
      const out = d.output;
      const status = e.status || (out && out.status) || "done";
      add(panelLine(`${stage} answered — ${status}`, "evt-head"));
      if (out && out.error) add(copyableCodeBlock("error", String(out.error)));
      addJsonWithFoldedCode(box, "answer (JSON)", out === undefined ? null : out, "output", true);
      break;
    }
    case "state": {
      const after = (d.after && typeof d.after === "object") ? d.after : {};
      const changed = stateChangedKeys(d);
      const folded = [];
      add(panelLine(`Notebook after ${stage}`, "evt-head"));
      add(panelLine(changed.length ? `${d.before ? "Changed" : "Set"}: ${changed.join(", ")}` : "Nothing changed.", "status"));
      if (d.before && typeof d.before === "object" && changed.length) add(stateDiffTable(d.before, after, changed, folded));
      add(stateAfterBlock(after, changed, folded));
      dedupeFolded(folded).forEach((f) => add(collapsedCodeBlock(`${f.path} — ${codeLabel(f.text)}`, f.text)));
      break;
    }
    default: break;
  }
}

function buildEventDetail(e) {
  const d = e.data || {};
  const box = document.createElement("div");
  box.className = "evt-panel";
  const add = (n) => box.appendChild(n);
  const toolRec = (evtCtx.tools[e.step] || []).find((t) => !d.tool || t.tool_name === d.tool) || null;
  try {
    switch (e.event_type) {
      case "tool_call": {
        const input = toolRec ? toolRec.input : d.input;
        add(panelLine(`${d.tool || "tool"} — arguments`, "evt-head"));
        add(copyableCodeBlock("arguments (JSON)", prettyJson(input)));
        if (typeof input === "string") noteIfTruncated(box, input);
        break;
      }
      case "tool_result": {
        add(panelLine(`${d.tool || "tool"} — result` + (toolRec ? ` | ${formatMs(toolRec.latency_ms || 0)} | ${toolRec.status}` : ""), "evt-head"));
        if (!toolRec) {
          add(panelLine("Full result not recorded yet (tool still running or record missing) — showing the event preview.", "status"));
          add(copyableCodeBlock("output preview", String(d.output_preview || "")));
          break;
        }
        if (toolRec.error) add(copyableCodeBlock("error", String(toolRec.error)));
        renderToolOutput(box, toolRec.output);
        break;
      }
      case "llm_call": {
        const rec = evtCtx.llms[e.step];
        if (!rec) {
          add(panelLine("Full LLM record not stored yet — showing the event preview.", "status"));
          add(copyableCodeBlock("output preview", String(d.output_preview || "")));
          break;
        }
        add(panelLine(`${rec.model || ""} | ${formatMs(rec.latency_ms || 0)} | tokens in ${rec.input_tokens ?? "?"} / out ${rec.output_tokens ?? "?"}` +
          ` | prompt ${rec.prompt_version || "-"} | ${rec.status}`, "status"));
        add(panelLabel(`Prompt — ${(rec.input || []).length} message(s)`));
        (rec.input || []).forEach((m, i) => {
          const calls = m.tool_calls && m.tool_calls.length ? ` — calls ${m.tool_calls.map((t) => t.name).join(", ")}` : "";
          const body = messageText(m) + (m.tool_calls && m.tool_calls.length ? "\n\n[tool_calls]\n" + JSON.stringify(m.tool_calls, null, 2) : "");
          add(copyableCodeBlock(`[${i + 1}] ${m.role}${calls}`, body || "(empty)"));
        });
        add(panelLabel("Response"));
        const out = rec.output || {};
        if (rec.error) add(copyableCodeBlock("error", String(rec.error)));
        add(copyableCodeBlock("content", messageText(out) || "(empty)"));
        if (out.tool_calls && out.tool_calls.length) add(copyableCodeBlock("tool calls (JSON)", JSON.stringify(out.tool_calls, null, 2)));
        break;
      }
      case "error":
        add(copyableCodeBlock("error", String(d.error || d.llm_error || "")));
        if (toolRec && toolRec.output != null) renderToolOutput(box, toolRec.output);
        break;
      case "agent_input":
      case "agent_output":
      case "state":
        buildOrchMetaDetail(box, e);
        break;
      default:
        break;
    }
  } catch (err) {
    add(panelLine(`Could not render: ${err.message}`, "status error"));
  }
  add(detailsBlock("event record (raw JSON)", JSON.stringify(e, null, 2)));
  return box;
}

// ── Orchestrator timeline ────────────────────────────────────────────
// Every orchestrator tool is one of the other agents: the events recorded
// between a tool_call (started) and its tool_result belong to that
// sub-agent. The orchestrator's own events are the `model` / `tools` graph
// steps, the llm_calls naming one of these tools, and the tool_call /
// tool_result pairs for them.
const ORCH_TOOL_AGENTS = {
  analyze_spec: "spec_analyzer",
  answer_spec_question: "spec_analyzer",
  generate_rtl: "rtl_agent",
  generate_testbench: "testbench_agent",
  generate_test_vectors: "test_vector_agent",
  write_project: null,
  simulate: "simulation_agent",
  implement: "implementation_agent",
};
function isOrchTool(name) { return Object.prototype.hasOwnProperty.call(ORCH_TOOL_AGENTS, name); }

// Expansion state of tool-call groups, keyed "<run id>:<step>"; kept across
// the redraws watchRun triggers on every SSE event. Groups are open unless
// the user collapsed them (or pressed "Collapse all").
const orchGroupOpen = new Map();
// Groups of the timeline currently on screen, for Expand all / Collapse all.
let orchGroups = [];
function setAllOrchGroups(open) {
  orchGroups.forEach((g) => { orchGroupOpen.set(g.key, open); g.refresh(); });
}
// CSS class carrying the per-agent colour band; "write_project" has no agent.
function orchAgentClass(agent) { return `orch-agent-${agent || "none"}`; }

// Is `e` the orchestrator's own event (vs. one nested in the open sub-agent
// call `parent`)? Events come sorted by step, and a tool_result shares the
// step of its tool_call, so it precedes the nested events: a group is
// therefore closed by timestamp (parent.endTs), not by the tool_result.
function isOrchestratorEvent(e, parent) {
  const d = e.data || {};
  switch (e.event_type) {
    case "agent_step": return d.node === "model" || d.node === "tools";
    case "tool_call":
    case "tool_result": return isOrchTool(d.tool);
    case "llm_call":
      if (d.tool_calls && d.tool_calls.length) return d.tool_calls.some(isOrchTool);
      // Text-only reply: the orchestrator's final answer when no sub-agent
      // call is in progress, otherwise a sub-agent's structured output.
      return !parent || (parent.endTs != null && e.timestamp > parent.endTs);
    case "agent_input":   // what the orchestrator handed to a helper
    case "agent_output":  // what the helper answered
    case "state": return true; // the orchestrator's notebook before/after
    default: return true; // run_started / run_completed / error / run_resumed
  }
}

// {label, cls} summarising a finished orchestrator tool call from the JSON
// result's status / verdict / error (full output from run.tool_calls when
// available, else the truncated event preview).
function orchToolOutcome(e, rec) {
  const d = e.data || {};
  let out = rec && rec.output;
  if (typeof out === "string") { try { out = JSON.parse(out); } catch (_) { out = null; } }
  if (!out || typeof out !== "object") {
    out = {};
    const prev = d.output_preview || "";
    ["status", "verdict", "error"].forEach((k) => {
      const m = prev.match(new RegExp(`"${k}"\\s*:\\s*"([^"]*)"`));
      if (m) out[k] = m[1];
    });
  }
  const error = (rec && rec.error) || d.error || (typeof out.error === "string" ? out.error : "");
  const parts = [];
  if (out.status) parts.push(String(out.status));
  if (out.verdict) parts.push(String(out.verdict));
  if (error) parts.push(`error: ${String(error).slice(0, 80)}`);
  if (!parts.length) parts.push(e.status || "done");
  const bad = e.status === "error" || Boolean(error) || out.status === "FAILED" || out.verdict === "fail";
  const warn = !bad && (["NEEDS_REVIEW", "WAITING_FOR_USER"].includes(out.status) || out.verdict === "error");
  return { label: parts.join(" · "), cls: bad ? "badge-error" : warn ? "badge-warn" : "badge-ok" };
}

// Hierarchical timeline: a top-level row per orchestrator LLM decision and a
// collapsible group row per orchestrator tool call ("<tool> → <agent>") that
// holds the sub-agent's steps, LLM calls and MCP tool calls. Works on a
// finished run and on the partial event list of a live one (an unfinished
// group stays the parent until its tool_result arrives).
function renderOrchestratorTimeline(tbody, run) {
  const records = {};
  (run.tool_calls || []).forEach((t) => { if (isOrchTool(t.tool_name)) records[`${t.step}:${t.tool_name}`] = t; });
  orchGroups = [];

  const startGroup = (e) => {
    const d = e.data || {};
    const agent = ORCH_TOOL_AGENTS[d.tool];
    const key = `${run.id}:${e.step}`;
    const agentCls = orchAgentClass(agent);
    const tr = document.createElement("tr");
    tr.className = `orch-group ${agentCls}`;
    tr.innerHTML = `
      <td data-label="#">${e.step ?? ""}</td>
      <td data-label="Event">tool_call</td>
      <td data-label="Detail"><span class="orch-toggle"></span><strong>${escapeHtml(d.tool)}</strong> →
        <span class="orch-agent-badge ${agentCls}">${escapeHtml(agent || "write_project (file writes, no agent)")}</span><span class="orch-count"></span></td>
      <td data-label="Status"><span class="orch-status">running...</span></td>
      <td data-label="Duration" class="orch-ms"></td>`;
    tbody.appendChild(tr);
    const g = { key, tool: d.tool, agentCls, step: e.step, endTs: null, tr, children: [] };
    // Default: expanded so the sub-agent's inner work is visible; a click (or
    // Expand/Collapse all) overrides that for the rest of the session.
    g.isOpen = () => (orchGroupOpen.has(key) ? orchGroupOpen.get(key) : true);
    g.refresh = () => {
      const open = g.isOpen();
      tr.querySelector(".orch-toggle").textContent = open ? "▾" : "▸";
      tr.querySelector(".orch-count").textContent = ` (${g.children.length} nested event${g.children.length === 1 ? "" : "s"})`;
      tr.classList.toggle("orch-group-open", open);
      g.children.forEach((c) => setRowHidden(c, !open));
    };
    g.addChild = (child) => {
      g.children.push(child);
      child.classList.add(agentCls);
      setRowHidden(child, !g.isOpen());
      tr.querySelector(".orch-count").textContent = ` (${g.children.length} nested event${g.children.length === 1 ? "" : "s"})`;
    };
    tr.addEventListener("click", () => { orchGroupOpen.set(key, !g.isOpen()); g.refresh(); });
    g.refresh();
    orchGroups.push(g);
    return g;
  };
  const finishGroup = (g, e) => {
    g.endTs = e.timestamp;
    const o = orchToolOutcome(e, records[`${e.step}:${g.tool}`]);
    const st = g.tr.querySelector(".orch-status");
    st.textContent = o.label;
    st.className = `orch-status ${o.cls}`;
    g.tr.querySelector(".orch-ms").textContent = e.duration_ms != null ? formatMs(e.duration_ms) : "";
    g.refresh();
  };

  let parent = null;
  run.events.forEach((e) => {
    const d = e.data || {};
    const orch = isOrchestratorEvent(e, parent);
    // An orchestrator event recorded after the open call returned closes it
    // (the `tools` wrapper step, the tool_result itself and the hand-off /
    // notebook events written from inside the tool call do not).
    if (parent && orch && e.event_type !== "tool_result" && !(e.event_type === "agent_step" && d.node === "tools") &&
        !ORCH_META_EVENTS.includes(e.event_type) &&
        (parent.endTs == null || e.timestamp > parent.endTs)) parent = null;

    if (orch && e.event_type === "tool_call") { parent = startGroup(e); return; }
    if (orch && e.event_type === "tool_result" && parent && parent.tool === d.tool && parent.step === e.step) {
      finishGroup(parent, e);
      return;
    }
    if (orch) {
      if (e.event_type === "agent_step") return; // model / tools wrappers: folded into the rows below
      let tr;
      if (e.event_type === "llm_call") {
        const detail = d.tool_calls && d.tool_calls.length
          ? `call ${d.tool_calls.join(", ")}`
          : `final answer: ${stripTruncMarker(d.output_preview) || "(empty)"}`;
        tr = eventRow(e, "decision", detail);
      } else {
        tr = eventRow(e);
        if (ORCH_META_EVENTS.includes(e.event_type)) tr.classList.add("orch-meta");
      }
      tr.classList.add("orch-top");
      tbody.appendChild(tr);
      return;
    }
    // Sub-agent event: graph nodes show their node name ("decide",
    // "generate_rtl", ...) as the detail, like the standalone timelines.
    const tr = e.event_type === "agent_step" ? eventRow(e, "node", d.node || "") : eventRow(e);
    tbody.appendChild(tr);
    if (parent) { tr.classList.add("orch-nested"); parent.addChild(tr); }
  });
}

// Only the latest showAgentRun call may touch the DOM: a redraw from an SSE
// step and the final refetch on `done` can overlap (each awaits the run and
// the live state), and two callers appending after their awaits rendered the
// timeline twice.
let showRunSeq = 0;
// " | largest context N tok, total input M tok" from the stored LLM records
// (empty when no call reported its token counts).
function contextSummary(llmCalls) {
  const sizes = (llmCalls || []).map((c) => c.input_tokens).filter((n) => n != null);
  if (!sizes.length) return "";
  const max = Math.max(...sizes), total = sizes.reduce((a, b) => a + b, 0);
  return ` | largest context ${fmtTok(max)} tok, total input ${fmtTok(total)} tok`;
}
async function showAgentRun(runId) {
  const seq = ++showRunSeq;
  const res = await fetch(`/api/agent-runs/${runId}`);
  if (!res.ok || seq !== showRunSeq) return;
  const run = await res.json();
  if (seq !== showRunSeq) return;
  $("#agent-run-panel").classList.remove("hidden");
  markSelectedRun(runId);
  $("#agent-run-meta").textContent =
    `run ${run.id.slice(0, 8)} | ${new Date(run.started_at * 1000).toLocaleString()} | ` +
    `${run.agent_name} v${run.agent_version || ""} | ${run.status} | ${run.duration_ms != null ? formatMs(run.duration_ms) : "-"}` +
    ` | model ${run.model || ""} | ${run.llm_calls.length} LLM call(s), ${run.tool_calls.length} tool call(s)` +
    contextSummary(run.llm_calls) +
    (run.error ? ` | error: ${run.error}` : "");
  const agentResult = run.final_result || {};
  const agentError = run.error || agentResult.error;
  const meta = $("#agent-run-meta");
  const cancelled = agentResult.status === "CANCELLED" || run.status === "stopped";
  meta.classList.toggle("error", Boolean(agentError) && !cancelled);
  meta.classList.toggle("cancelled", cancelled);
  if (agentResult.status) {
    meta.textContent += ` | agent: ${agentResult.status}` + (agentResult.error && !run.error ? ` — ${agentResult.error}` : "");
  }
  const whyStopped = stopReasonOf(run);
  if (whyStopped) meta.textContent += ` | Why it stopped: ${whyStopped}`;

  const isOrch = run.agent_name === "orchestrator";
  $("#agent-orch-tl-tools").classList.toggle("hidden", !isOrch);
  // Clear and refill the timeline in one synchronous pass (no await between),
  // so every render replaces the rows rather than adding to them.
  const tbody = $("#agent-events");
  tbody.innerHTML = "";
  setEventContext(run);
  if (isOrch) renderOrchestratorTimeline(tbody, run);
  else { orchGroups = []; run.events.forEach((e) => tbody.appendChild(eventRow(e))); }
  // Rows are in the table now: reopen the detail panels the user had open.
  evtRows.forEach((r) => r.refresh());

  const llm = $("#agent-llm-calls");
  llm.innerHTML = "";
  run.llm_calls.forEach((c, i) => {
    const out = c.output || {};
    const title = `#${c.step} ${c.model || ""} | ${formatMs(c.latency_ms || 0)} | tokens in ${c.input_tokens ?? "?"} / out ${c.output_tokens ?? "?"}` +
      ` | prompt ${c.prompt_version || "-"} | ${c.status}` + (out.tool_calls && out.tool_calls.length ? ` | calls ${out.tool_calls.map((t) => t.name).join(", ")}` : "");
    const body = "── INPUT ──\n" + (c.input || []).map((m) => `[${m.role}] ${typeof m.content === "string" ? m.content : JSON.stringify(m.content)}`).join("\n\n") +
      "\n\n── OUTPUT ──\n" + (c.error ? `ERROR: ${c.error}` : (typeof out.content === "string" ? out.content : JSON.stringify(out.content))) +
      (out.tool_calls && out.tool_calls.length ? "\n\n── TOOL CALLS ──\n" + JSON.stringify(out.tool_calls, null, 2) : "");
    llm.appendChild(detailsBlock(title, body));
  });
  if (!run.llm_calls.length) llm.textContent = "none";

  const tools = $("#agent-tool-calls");
  tools.innerHTML = "";
  run.tool_calls.forEach((t) => {
    const title = `#${t.step} ${t.tool_name} | ${formatMs(t.latency_ms || 0)} | ${t.status}`;
    const body = "── INPUT ──\n" + JSON.stringify(t.input, null, 2) + "\n\n── OUTPUT ──\n" +
      (t.error ? `ERROR: ${t.error}` : (typeof t.output === "string" ? t.output : JSON.stringify(t.output, null, 2)));
    let parsedOut = null;
    if (!t.error) { try { parsedOut = JSON.parse(unwrapToolOutput(t.output)); if (typeof parsedOut === "string") parsedOut = JSON.parse(parsedOut); } catch (_) { parsedOut = null; } }
    if (hasGeneratedCode(parsedOut)) {       // generator tools: the VHDL as a real code block
      const det = detailsBlock(title, "── INPUT ──\n" + JSON.stringify(t.input, null, 2));
      appendCodeBlocks(det, parsedOut);
      const rest = {}; Object.keys(parsedOut).forEach((k) => { rest[k] = (parsedOut[k] && typeof parsedOut[k] === "object" && typeof parsedOut[k].code === "string") ? { ...parsedOut[k], code: "(shown above)" } : parsedOut[k]; });
      det.appendChild(detailsBlock("rest of the output (JSON)", JSON.stringify(rest, null, 2)));
      tools.appendChild(det);
    } else tools.appendChild(detailsBlock(title, body));
  });
  if (!run.tool_calls.length) tools.textContent = "none";

  $("#agent-result").textContent = run.final_result != null ? JSON.stringify(run.final_result, null, 2) : (run.error || "(no result)");
  renderCodeBlocks("#agent-code-blocks", run.final_result);
  renderUseAsInput(run);
  renderSimulationPanel(run);
  renderImplementationPanel(run);
  renderOrchestratorPanel(run);
  renderTestVectors(run.final_result && run.final_result.validation);

  // Agents that evaluate a spec return suggestions: show them as a list too.
  const suggestionsBlock = $("#agent-suggestions-block");
  if (run.final_result && Array.isArray(run.final_result.suggestions)) {
    renderSuggestions("#agent-suggestions", run.final_result.suggestions);
    suggestionsBlock.classList.remove("hidden");
  } else {
    suggestionsBlock.classList.add("hidden");
  }

  // Resumable agents: a WAITING_FOR_USER result carries the question and the
  // session to continue.
  const fr = run.final_result || {};
  const clarify = $("#agent-clarify");
  if (fr.status === "WAITING_FOR_USER" && fr.session_id) {
    agentSession = { agent: run.agent_name, sessionId: fr.session_id };
    $("#agent-question").textContent = fr.pending_question || "";
    $("#agent-answer").value = "";
    $("#agent-answer-status").textContent = "";
    // Clickable suggestions: the agent's suggested answers if it sends them,
    // otherwise its fix hints. Clicking copies the text into the answer box.
    fillAnswerOptions((Array.isArray(fr.suggested_answers) && fr.suggested_answers.length
      ? fr.suggested_answers : (fr.suggestions || [])));
    $("#agent-answer-btn").textContent = "Answer & resume session";
    clarify.classList.remove("hidden");
  } else if (run.status === "running") {
    // A live run may be paused on a question (orchestrator + spec analyzer).
    let pending = null;
    try { pending = (await (await fetch(`/api/agent-runs/${runId}/question`)).json()).pending; } catch (_) {}
    if (seq !== showRunSeq) return;
    if (pending) {
      const fresh = !agentSession || !agentSession.live || agentSession.runId !== runId || agentSession.question !== pending.question;
      agentSession = { live: true, runId, question: pending.question };
      $("#agent-question").textContent = pending.question || "";
      if (fresh) {
        $("#agent-answer").value = "";
        $("#agent-answer-status").textContent = "";
        fillAnswerOptions(pending.suggestions);
      }
      $("#agent-answer-btn").disabled = false;
      $("#agent-answer-btn").textContent = "Send answer";
      clarify.classList.remove("hidden");
    } else {
      agentSession = null;
      clarify.classList.add("hidden");
    }
  } else {
    agentSession = null;
    clarify.classList.add("hidden");
  }
  renderRunErrors(runId, run, seq);
}

// ── Errors for the FPGA engineer ─────────────────────────────────────
// Every problem the run recorded (GET /api/agent-runs/{id}/errors), grouped
// by requirement, then step, then attempt. Redrawn on every live update and
// shown for every ending (done, needs review, failed, stopped, crashed).
let errCtx = null;   // {runId, run, data, waiting}

// Green "Fixed in ..." tag, red "Still open" tag, grey "Continues" for an error that is still there next round.
function stateTag(i) {
  if (i.state === "fixed") return ` <span class="err-tag fixed">${escapeHtml(i.fixed_label || "Fixed")}</span>`;
  if (i.state === "open") return ' <span class="err-tag open">Still open</span>';
  if (i.state === "carried") return ' <span class="err-tag carried">Continues</span>';
  return "";
}

function specList(items) {
  return `<ul>${items.map((t) => `<li>${escapeHtml(typeof t === "string" ? t : JSON.stringify(t))}</li>`).join("")}</ul>`;
}

// The requirement as a title plus labelled parts, folded when long.
function renderRequirementBody(req) {
  const sp = req.spec;
  let parts = "";
  const line = (label, value) => value === undefined || value === null || value === "" ? "" :
    `<div class="req-line"><span class="req-label">${escapeHtml(label)}</span> ${escapeHtml(String(value))}</div>`;
  const listBlock = (label, v) => `<div class="req-line"><span class="req-label">${escapeHtml(label)} (${v.length})</span></div>${specList(v)}`;
  if (sp) {
    const used = new Set(["status"]);
    const take = (k) => { used.add(k); return sp[k]; };
    parts += line("Name", take("entity_name"));
    parts += line("What it must do", take("description"));
    parts += line("VHDL version", take("vhdl_version"));
    const clk = take("clock"), rst = take("reset");
    if (clk && typeof clk === "object") parts += line("Clock", `${clk.name || ""}${clk.frequency_mhz ? `, ${clk.frequency_mhz} MHz` : ""}${clk.required === false ? " (not used)" : ""}`);
    if (rst && typeof rst === "object") parts += line("Restart", `${rst.name || ""}${rst.type ? `, ${rst.type}` : ""}${rst.active ? `, active ${rst.active}` : ""}`);
    const ports = take("ports");
    if (Array.isArray(ports) && ports.length) {
      parts += listBlock("Inputs and outputs", ports.map((p) =>
        typeof p === "object" ? `${p.name} - ${p.direction || ""}, ${p.width || "?"} bit${p.type ? `, ${p.type}` : ""}` : p));
    }
    [["behavior", "Behaviour"], ["constraints", "Limits"], ["assumptions", "Assumptions"], ["ambiguities", "Unclear points"]].forEach(([k, label]) => {
      const v = take(k);
      if (Array.isArray(v) && v.length) parts += listBlock(label, v);
    });
    Object.keys(sp).filter((k) => !used.has(k) && sp[k] !== null && sp[k] !== "").forEach((k) => {
      const v = sp[k];
      parts += Array.isArray(v) ? (v.length ? listBlock(k, v) : "") : line(k, typeof v === "object" ? JSON.stringify(v) : v);
    });
  } else if (req.text) {
    parts = `<div class="req-text">${escapeHtml(req.text)}</div>`;
  }
  if (!parts) return "";
  const long = parts.length > 900;
  return `<div class="req-card ${long ? "folded" : ""}"><div class="req-body">${parts}</div>` +
    (long ? '<button type="button" class="req-toggle" data-req-toggle>Show more</button>' : "") + "</div>";
}

document.addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-req-toggle]");
  if (!b) return;
  const card = b.closest(".req-card");
  card.classList.toggle("folded");
  b.textContent = card.classList.contains("folded") ? "Show more" : "Show less";
});

async function renderRunErrors(runId, run, seq) {
  const block = $("#agent-errors-block");
  let data;
  try {
    const res = await fetch(`/api/agent-runs/${runId}/errors`);
    if (!res.ok) throw new Error("no data");
    data = await res.json();
  } catch (_) { block.classList.add("hidden"); return; }
  if (seq !== showRunSeq) return;
  const running = run.status === "running";
  const waiting = Boolean(agentSession && agentSession.live && agentSession.runId === runId);
  errCtx = { runId, run, data, waiting };
  block.classList.remove("hidden");
  block.classList.toggle("clean", data.total === 0);
  const fixedTotal = data.fixed_total || 0;
  $("#agent-errors-summary").textContent =
    (data.total === 0 ? "No open errors" : `${data.total} open error${data.total === 1 ? "" : "s"}`) +
    (fixedTotal ? `, ${fixedTotal} fixed` : "") +
    (running ? " so far (updating live)" : ` | run ${run.status}${data.requirements[0].status ? " · " + data.requirements[0].status : ""}`);

  let html = "";
  const stepsWithErrors = [];
  if (data.stop_reason) html += `<div class="status"><b>Why it stopped:</b> ${escapeHtml(data.stop_reason)}</div>`;
  data.requirements.forEach((req) => {
    const badge = `<span class="err-badge ${req.count ? "" : "zero"}">${req.count} open</span>` +
      (req.fixed_count ? ` <span class="err-badge zero">${req.fixed_count} fixed</span>` : "");
    html += `<div class="err-req">Requirement: ${escapeHtml(req.title)} ${badge} ${req.count ? "" : '<span class="err-ok">no open errors</span>'}</div>`;
    html += renderRequirementBody(req);
    req.notes.forEach((n) => { html += `<div class="status" style="margin-left:8px">${escapeHtml(n)}</div>`; });
    if (req.run_level.length) {
      html += `<details class="err-step" open><summary>Whole run (manager level) <span class="err-badge">${req.run_level.length}</span></summary><div class="err-attempt"><ul>` +
        req.run_level.map((t) => `<li>${escapeHtml(t)}</li>`).join("") + "</ul></div></details>";
    }
    req.steps.forEach((st) => {
      if (st.count) stepsWithErrors.push(st);
      const found = st.found_count || 0;
      html += `<details class="err-step ${st.count ? "" : "zero"}" ${st.count ? "open" : ""}><summary>${escapeHtml(st.label)} ` +
        `<span class="err-badge ${st.count ? "" : "zero"}">${st.count}</span>` +
        (found ? ` <span class="status">${found} found, ${st.fixed_count || 0} fixed, ${st.count} still open</span>` : "") +
        `${st.status ? ` <span class="status">(${escapeHtml(st.status)})</span>` : ""}</summary>`;
      st.attempts.forEach((a) => {
        const lint = a.lint || [];
        const issues = a.issues || [];
        const issueItems = issues.map((i) =>
          `<li class="lint ${i.state === "fixed" ? "fixed" : ""}"><span class="err-code">${escapeHtml(i.code)}</span>${stateTag(i)}<span class="err-detail">${escapeHtml(i.detail)}</span></li>`);
        const lintLi = (i) => i.note
          ? `<li class="note lint"><b>Note, not an error:</b> ${escapeHtml(i.detail)}</li>`
          : `<li class="lint ${i.state === "fixed" ? "fixed" : ""}"><span class="err-code">${escapeHtml(i.code)}</span>` +
            `${i.location ? ` <span class="err-loc">${escapeHtml(i.location)}</span>` : ""}${stateTag(i)}` +
            `<span class="err-detail">${escapeHtml(i.detail)}</span></li>`;
        const lintItems = lint.map(lintLi);
        const rounds = a.rounds || [];
        const roundHtml = rounds.map((r) =>
          `<div class="err-explain"><b>Fix round ${escapeHtml(String(r.round))}${r.max ? ` of ${escapeHtml(String(r.max))}` : ""}` +
          `${r.compiler ? ` (${escapeHtml(r.compiler)})` : ""}:</b><ul>${r.lint.map(lintLi).join("")}</ul></div>`).join("");
        const items = a.errors.map((t, k) => {
          const es = (a.error_states || [])[k] || {};
          return `<li class="${es.state === "fixed" ? "fixed" : ""}">${stateTag(es)}${escapeHtml(t)}</li>`;
        }).concat(issueItems, lintItems, a.notes.map((t) => `<li class="note">${escapeHtml(t)}</li>`));
        const realCount = a.errors.length + issues.length + lint.filter((i) => !i.note).length +
          rounds.reduce((n, r) => n + r.lint.filter((i) => !i.note).length, 0);
        html += `<div class="err-attempt">${a.attempt != null ? `Attempt ${a.attempt}` : "During the run"}` +
          `${a.status ? ` (${escapeHtml(a.status)})` : ""}: ${realCount ? "" : '<span class="err-ok">no errors</span>'}` +
          (items.length ? `<ul>${items.join("")}</ul>` : "") + roundHtml +
          (a.explanation ? `<div class="err-explain"><b>The AI's own explanation:</b> ${escapeHtml(a.explanation)}</div>` : "") +
          (a.suggested_fix ? `<div class="err-explain"><b>The AI suggests:</b> ${escapeHtml(a.suggested_fix)}</div>` : "") + "</div>";
      });
      html += "</details>";
    });
  });
  $("#agent-errors-body").innerHTML = html;

  // Step chooser for "Retry from this step": keep the engineer's choice across redraws.
  const sel = $("#agent-errors-step");
  const previous = sel.value;
  const options = stepsWithErrors.length ? stepsWithErrors : data.requirements.flatMap((r) => r.steps);
  const seenStages = new Set();   // RTL and RTL lint retry from the same step: list it once
  const uniqueOptions = options.filter((s) => !seenStages.has(s.stage) && seenStages.add(s.stage));
  sel.innerHTML = uniqueOptions.map((s) => `<option value="${escapeHtml(s.stage)}">${escapeHtml(s.label)}</option>`).join("");
  if (uniqueOptions.some((s) => s.stage === previous)) sel.value = previous;

  const canAct = waiting || !running;
  $all("#agent-errors-block [data-err-action], #agent-errors-send").forEach((b) => { b.disabled = !canAct; });
  const st = $("#agent-errors-status");
  if (!canAct) {
    st.textContent = "The AI is still working. These buttons switch on when it asks you something or when the run ends.";
    st.classList.remove("error");
  } else if (st.textContent.startsWith("The AI is still working")) {
    st.textContent = waiting ? "The AI is waiting for your answer: your choice is sent to it." : "";
  }
}

// What each button tells the AI: the answer to a waiting run, or the extra
// instruction added to a fresh run.
function errorsInstruction(action) {
  const stepEl = $("#agent-errors-step");
  const stepName = stepEl.value || "";
  const stepLabel = stepEl.selectedOptions[0] ? stepEl.selectedOptions[0].textContent : stepName;
  const extra = $("#agent-errors-text").value.trim();
  const base = {
    "retry-req": "Retry this requirement from the beginning.",
    "retry-step": `Retry starting from the step "${stepName}" (${stepLabel}); keep what came before it.`,
    "skip": "Skip this requirement: stop working on it and report what you have.",
    "manual": "The FPGA engineer will fix this by hand. Stop now and report what is left to fix.",
    "send": "",
  }[action];
  return [base, extra && `Instructions from the FPGA engineer: ${extra}`].filter(Boolean).join(" ");
}

$("#agent-errors-block").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-err-action], #agent-errors-send");
  if (!btn || !errCtx) return;
  const action = btn.id === "agent-errors-send" ? "send" : btn.dataset.errAction;
  const status = $("#agent-errors-status");
  status.classList.remove("error");
  const text = errorsInstruction(action);
  if (!text) { status.textContent = "Type instructions first, or press one of the buttons."; status.classList.add("error"); return; }
  const { runId, run, waiting } = errCtx;
  try {
    if (waiting) {                       // the AI is paused on a question: answer it
      const res = await fetch(`/api/agent-runs/${runId}/answer`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ answer: text }) });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Request failed");
      agentSession = null;
      $("#agent-clarify").classList.add("hidden");
      status.textContent = "Sent to the AI. The run continues.";
      return;
    }
    if (action === "skip" || action === "manual") {   // run is over: nothing on the server to change
      status.textContent = action === "skip"
        ? "Noted here only: the system keeps no 'skipped' flag for finished runs, nothing was changed."
        : "Noted here only. Fix the files by hand, then press 'Retry this requirement' to run again.";
      return;
    }
    const input = { ...(run.input || {}) };      // finished run: start a new run with the same input
    if (run.agent_name === "orchestrator" && input.requirement_text && (action !== "retry-req" || $("#agent-errors-text").value.trim())) {
      input.requirement_text += `\n\n--- Instructions from the FPGA engineer (previous run ${runId.slice(0, 8)}) ---\n${text}`;
    }
    const res = await fetch(`/api/agents/${run.agent_name}/run`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ input }) });
    const data = await res.json();
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
    status.textContent = `Started a new run ${data.run_id.slice(0, 8)}.`;
    $("#agent-run-btn").disabled = true;
    watchRun(data.run_id, $("#agent-run-status"), $("#agent-run-btn"), run.agent_name);
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    status.classList.add("error");
  }
});

// Clickable suggested answers: clicking copies the text into the answer box.
function fillAnswerOptions(suggestions) {
  const options = (suggestions || []).filter((s) => typeof s === "string" && s.trim());
  const optList = $("#agent-answer-options");
  optList.innerHTML = "";
  options.forEach((text) => {
    const li = document.createElement("li");
    li.textContent = text;
    li.title = "Click to put this in the answer box";
    li.addEventListener("click", () => {
      const box = $("#agent-answer");
      box.value = text;
      box.focus();
    });
    optList.appendChild(li);
  });
  $("#agent-answer-options-label").classList.toggle("hidden", !options.length);
}

let agentSession = null;

$("#agent-answer-btn").addEventListener("click", async () => {
  const status = $("#agent-answer-status");
  const answer = $("#agent-answer").value;
  status.classList.remove("error");
  if (!agentSession) { status.textContent = "No session waiting."; status.classList.add("error"); return; }
  if (!answer.trim()) { status.textContent = "Type an answer first."; status.classList.add("error"); return; }
  if (agentSession.live) {
    // The run is still going and waiting: hand it the answer, same timeline.
    $("#agent-answer-btn").disabled = true;
    try {
      const res = await fetch(`/api/agent-runs/${agentSession.runId}/answer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Request failed");
      agentSession = null;
      $("#agent-clarify").classList.add("hidden");
      $("#agent-run-status").textContent = "answer sent — the run continues...";
    } catch (err) {
      status.textContent = `Error: ${err.message}`;
      status.classList.add("error");
      $("#agent-answer-btn").disabled = false;
    }
    return;
  }
  status.textContent = "Resuming...";
  $("#agent-answer-btn").disabled = true;
  try {
    const res = await fetch(`/api/agents/${agentSession.agent}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: agentSession.sessionId, answer }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Request failed");
    $("#agent-clarify").classList.add("hidden");
    watchRun(data.run_id, $("#agent-run-status"), $("#agent-answer-btn"), agentSession.agent);
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    status.classList.add("error");
    $("#agent-answer-btn").disabled = false;
  }
});

// ── Example requirements (agent lab + pipeline) ───────────────────────
const EXAMPLE_SPECS = [
  {
    label: "Synchronous FIFO",
    text: `Design a synchronous FIFO in VHDL.
- DATA_WIDTH parameter, default 8
- DEPTH parameter, default 16
- Single clock domain, clk, active-high synchronous reset rst
- Inputs: wr_en, rd_en, din[DATA_WIDTH-1:0]
- Outputs: dout[DATA_WIDTH-1:0], full, empty
- Must not allow writes when full or reads when empty`,
  },
  {
    label: "UART Transmitter",
    text: `Design a UART transmitter entity.
- Inputs: clk (50 MHz), rst_n (active-low async reset), tx_start, tx_data[7:0]
- Output: tx_serial, tx_busy
- Configurable baud rate via BAUD_RATE parameter, default 115200
- Frame format: 1 start bit, 8 data bits, no parity, 1 stop bit
- tx_busy must be asserted for the full duration of a transmission
- Must ignore tx_start while a transmission is in progress`,
  },
  {
    label: "8-bit Up/Down Counter",
    text: `Design an 8-bit up/down counter.
- Inputs: clk, rst (active-high sync reset), en, up_down (1 = count up, 0 = count down)
- Output: count[7:0]
- Counter should saturate at 0xFF and 0x00 rather than wrapping around
- Only increments/decrements when en is asserted`,
  },
  {
    label: "SPI Master",
    text: `Design an SPI master controller, Mode 0 (CPOL=0, CPHA=0).
- Inputs: clk (100 MHz system clock), rst_n, start, data_in[7:0], miso
- Outputs: sclk, mosi, cs_n, data_out[7:0], done
- SCLK_DIV parameter to derive sclk frequency from system clk, default divide-by-4
- Single chip-select cs_n, active-low, asserted for the whole 8-bit transfer
- done pulses for one clk cycle when the 8-bit transfer completes`,
  },
];

function renderExampleButtons(rowSel, targetSel) {
  const row = $(rowSel);
  EXAMPLE_SPECS.forEach((ex) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "example-btn";
    btn.textContent = ex.label;
    btn.addEventListener("click", () => {
      $(targetSel).value = ex.text;
    });
    row.appendChild(btn);
  });
}
renderExampleButtons("#pipeline-examples-row", "#pipeline-input");

// ── Pipeline tab ─────────────────────────────────────────────────────
let currentThreadId = null;
let reviewShownAt = null;

function resetPipelinePanels() {
  $("#pipeline-timings-panel").classList.add("hidden");
  $("#pipeline-review-panel").classList.add("hidden");
  $("#pipeline-rtl-panel").classList.add("hidden");
  $("#pipeline-tv-panel").classList.add("hidden");
  $("#pipeline-tb-panel").classList.add("hidden");
  $("#pipeline-approve-status").textContent = "";
  $("#pipeline-approve-status").classList.remove("error");
}

function fillNotesList(list, notes) {
  list.innerHTML = "";
  (notes || []).forEach((n) => {
    const li = document.createElement("li");
    li.textContent = n;
    list.appendChild(li);
  });
}

function pipelineBadgeClass(status) {
  if (status === "COMPLETE") return "badge-ok";
  if (status === "NEEDS_REVIEW") return "badge-warn";
  return "badge-error";
}

// Test-vectors panel: one line when it worked, plain-words fallback otherwise
// (MATLAB missing is normal — the testbench falls back to hand-written checks).
function renderPipelineTestVectors(tv) {
  tv = tv || {};
  const st = tv.status || "FAILED";
  const statusEl = $("#pipeline-tv-status");
  statusEl.textContent = `Status: ${st}`;
  statusEl.className = `status ${pipelineBadgeClass(st)}`;
  const metaEl = $("#pipeline-tv-meta");
  const v = tv.validation || {};
  const csv = (tv.result && tv.result.csv_file) || "";
  const csvName = csv ? csv.split(/[\\/]/).pop() : "";
  if (st === "COMPLETE") {
    const cols = (v.columns || []).join(", ");
    metaEl.textContent =
      `${v.num_vectors ?? "?"} rows, columns: ${cols || "?"}` + (csvName ? ` (${csvName})` : "");
  } else {
    const reasons = [tv.error, ...(tv.warnings || []), ...(v.issues || [])].filter(Boolean);
    metaEl.textContent =
      "Not generated — the testbench will use hand-written checks instead." +
      (reasons.length ? ` ${reasons.join(" | ")}` : "");
  }
  $("#pipeline-tv-panel").classList.remove("hidden");
}

function renderPipelineTestbench(tbRes) {
  tbRes = tbRes || {};
  const st = tbRes.status || "FAILED";
  const tb = tbRes.testbench || {};
  const metaEl = $("#pipeline-tb-meta");
  metaEl.textContent =
    `Status: ${st} — Entity: ${tb.entity_name || "?"}` +
    (tbRes.error ? ` — Error: ${tbRes.error}` : "");
  metaEl.className = `status ${pipelineBadgeClass(st)}`;
  $("#pipeline-tb-code").textContent = tb.code || "(no code returned)";
  renderLint("#pipeline-tb-lint", tbRes.lint);
  fillNotesList($("#pipeline-tb-notes"), [...(tb.notes || []), ...(tbRes.warnings || [])]);
  $("#pipeline-tb-panel").classList.remove("hidden");
}

$("#pipeline-start-btn").addEventListener("click", async () => {
  const requirementText = $("#pipeline-input").value;
  const status = $("#pipeline-start-status");

  if (!requirementText.trim()) {
    status.textContent = "Enter a requirement first.";
    status.classList.add("error");
    return;
  }

  status.classList.remove("error");
  status.textContent = "Analyzing spec...";
  $("#pipeline-start-btn").disabled = true;
  resetPipelinePanels();

  try {
    const res = await fetch("/api/pipeline/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ requirement_text: requirementText }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Request failed");

    currentThreadId = data.thread_id;
    if (data.status === "awaiting_review") {
      $("#pipeline-spec-json").value = JSON.stringify(data.review.spec, null, 2);
      renderSuggestions("#pipeline-suggestions", data.review.suggestions);
      renderTimings("#pipeline-timings", data.timings);
      $("#pipeline-timings-panel").classList.remove("hidden");
      $("#pipeline-review-panel").classList.remove("hidden");
      reviewShownAt = performance.now();
      status.textContent = "Spec ready — review below, then approve.";
    } else {
      status.textContent = "Unexpected pipeline status: " + data.status;
      status.classList.add("error");
    }
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    status.classList.add("error");
  } finally {
    $("#pipeline-start-btn").disabled = false;
  }
});

$("#pipeline-approve-btn").addEventListener("click", async () => {
  const status = $("#pipeline-approve-status");
  status.classList.remove("error");

  let spec;
  try {
    spec = JSON.parse($("#pipeline-spec-json").value);
  } catch (err) {
    status.textContent = "Spec JSON is invalid: " + err.message;
    status.classList.add("error");
    return;
  }

  if (!currentThreadId) {
    status.textContent = "No pipeline in progress — start one first.";
    status.classList.add("error");
    return;
  }

  status.textContent = "Generating RTL, test vectors and testbench...";
  $("#pipeline-approve-btn").disabled = true;

  try {
    const res = await fetch(`/api/pipeline/${currentThreadId}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Request failed");

    if (data.status === "done") {
      const rtl = data.rtl || {};
      $("#pipeline-rtl-meta").textContent =
        `Entity: ${rtl.entity_name || "?"}`;
      $("#pipeline-rtl-code").textContent = rtl.code || "(no code returned)";
      renderLint("#pipeline-rtl-lint", data.lint);

      // Human review time is measured client-side: it's the gap between the
      // review panel appearing and Approve being clicked.
      const timings = [...(data.timings || [])];
      if (reviewShownAt != null) {
        let insertAt = timings.length;
        for (let i = timings.length - 1; i >= 0; i--) {
          if (timings[i].status === "interrupted") { insertAt = i + 1; break; }
        }
        timings.splice(insertAt, 0, {
          step: "human_review (wait)",
          status: "human",
          duration_ms: performance.now() - reviewShownAt,
        });
      }
      renderTimings("#pipeline-timings", timings);
      fillNotesList($("#pipeline-rtl-notes"), rtl.notes);
      $("#pipeline-rtl-panel").classList.remove("hidden");
      renderPipelineTestVectors(data.test_vectors);
      renderPipelineTestbench(data.testbench);
      status.textContent = "RTL, test vectors and testbench generated.";
    } else {
      status.textContent = "Unexpected pipeline status: " + data.status;
      status.classList.add("error");
    }
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    status.classList.add("error");
  } finally {
    $("#pipeline-approve-btn").disabled = false;
  }
});

// ── Observability tab ────────────────────────────────────────────────
$("#refresh-traces-btn").addEventListener("click", loadTraces);
$("#close-detail-btn").addEventListener("click", () => {
  $("#trace-detail").classList.add("hidden");
});

async function loadTraces() {
  const tbody = $("#traces-tbody");
  const res = await fetch("/api/traces?limit=100");
  const traces = await res.json();

  tbody.innerHTML = "";
  for (const t of traces) {
    const tr = document.createElement("tr");
    tr.dataset.id = t.id;

    const time = new Date(t.created_at * 1000).toLocaleTimeString();
    const duration = t.duration_ms != null ? t.duration_ms.toFixed(0) : "-";
    const statusHtml = t.error
      ? `<span class="badge-error">error</span>`
      : `<span class="badge-ok">ok</span>`;
    const preview = escapeHtml((t.output_text || t.error || "").slice(0, 80));

    tr.innerHTML = `
      <td>${time}</td>
      <td>${escapeHtml(t.source || "")}</td>
      <td>${escapeHtml(t.model || "")}</td>
      <td>${duration}</td>
      <td>${statusHtml}</td>
      <td>${preview}</td>
    `;
    tr.addEventListener("click", () => showTraceDetail(t.id));
    tbody.appendChild(tr);
  }
}

async function showTraceDetail(id) {
  const res = await fetch(`/api/traces/${id}`);
  if (!res.ok) return;
  const t = await res.json();

  const panel = $("#trace-detail");
  const body = $("#trace-detail-body");

  const messagesHtml = (t.input_messages || [])
    .map(
      (m) => `
      <div class="msg-block">
        <div class="msg-role">${escapeHtml(m.role)}</div>
        ${escapeHtml(m.content)}
      </div>`
    )
    .join("");

  const usageHtml = t.usage
    ? `<div class="msg-block"><div class="msg-role">usage</div>${escapeHtml(
        JSON.stringify(t.usage, null, 2)
      )}</div>`
    : "";

  const outputHtml = t.error
    ? `<div class="msg-block"><div class="msg-role">error</div>${escapeHtml(t.error)}</div>`
    : `<div class="msg-block"><div class="msg-role">output</div>${escapeHtml(t.output_text)}</div>`;

  body.innerHTML = `
    <p><strong>Model:</strong> ${escapeHtml(t.model || "")} &nbsp;
       <strong>Source:</strong> ${escapeHtml(t.source || "")} &nbsp;
       <strong>Duration:</strong> ${t.duration_ms != null ? t.duration_ms.toFixed(0) : "-"} ms</p>
    <label>Input messages</label>
    ${messagesHtml}
    <label>Output</label>
    ${outputHtml}
    ${usageHtml}
  `;
  panel.classList.remove("hidden");
}

// Agents is the default tab.
initAgentsTab();

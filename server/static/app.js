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

function onAgentChange() {
  const a = agentCatalog.find((x) => x.name === $("#agent-select").value);
  if (!a) return;
  $("#agent-desc").textContent = a.description;
  $("#agent-input").value = JSON.stringify(a.input_template, null, 2);
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

// Current textarea JSON if valid, else the selected agent's template.
function currentAgentInput() {
  try {
    const v = JSON.parse($("#agent-input").value);
    if (v && typeof v === "object" && !Array.isArray(v)) return v;
  } catch (_) { /* fall back to template */ }
  const a = agentCatalog.find((x) => x.name === $("#agent-select").value);
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
  let input;
  try {
    input = JSON.parse($("#agent-input").value);
  } catch (err) {
    status.textContent = "Input JSON is invalid: " + err.message;
    status.classList.add("error");
    return;
  }
  const name = $("#agent-select").value;
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
    watchRun(data.run_id, status, $("#agent-run-btn"));
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    status.classList.add("error");
    $("#agent-run-btn").disabled = false;
  }
});

// Follow a running agent over SSE: redraw the run on every recorded event,
// finish on `done`. Returns nothing; the status span/button are updated here.
let runStream = null;

function watchRun(runId, statusEl, buttonEl) {
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
  runStream = new EventSource(`/api/agent-runs/${runId}/stream`);
  runStream.addEventListener("step", (e) => {
    const ev = JSON.parse(e.data);
    statusEl.textContent = `running... ${ev.event_type}${ev.data && ev.data.node ? " " + ev.data.node : ""}`;
    redraw();
  });
  runStream.addEventListener("done", (e) => {
    const run = JSON.parse(e.data);
    runStream.close();
    runStream = null;
    const agent = run.final_result || {};
    statusEl.textContent = `${run.status}${agent.status ? " · " + agent.status : ""} in ${((performance.now() - started) / 1000).toFixed(1)}s`;
    statusEl.classList.toggle("error", run.status === "failed" || ["FAILED", "NEEDS_REVIEW"].includes(agent.status));
    showAgentRun(runId).then(loadAgentRuns);
    buttonEl.disabled = false;
  });
  runStream.addEventListener("error", () => {
    statusEl.textContent = "stream lost — click Refresh to reload";
    statusEl.classList.add("error");
    if (runStream) { runStream.close(); runStream = null; }
    buttonEl.disabled = false;
  });
}

$("#agent-runs-refresh").addEventListener("click", loadAgentRuns);

async function loadAgentRuns() {
  const name = $("#agent-select").value;
  const runs = await (await fetch(`/api/agent-runs?agent_name=${encodeURIComponent(name)}&limit=30`)).json();
  const tbody = $("#agent-runs");
  tbody.innerHTML = "";
  runs.forEach((r) => {
    const tr = document.createElement("tr");
    const agentBad = r.agent_status && ["FAILED", "NEEDS_REVIEW"].includes(r.agent_status);
    const cls = r.status === "failed" || agentBad ? "badge-error" : r.status === "completed" ? "badge-ok" : "";
    const statusText = r.agent_status ? `${r.status} · ${r.agent_status}` : r.status;
    const errorText = r.error || r.agent_error || "";
    tr.innerHTML = `
      <td>${new Date(r.started_at * 1000).toLocaleString()}</td>
      <td>${escapeHtml(r.agent_name)} v${escapeHtml(r.agent_version || "")}</td>
      <td><span class="${cls}">${escapeHtml(statusText)}</span></td>
      <td>${r.duration_ms != null ? formatMs(r.duration_ms) : "-"}</td>
      <td class="${errorText ? "badge-error" : ""}">${escapeHtml(errorText.slice(0, 80))}</td>`;
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

function eventDetail(e) {
  const d = e.data || {};
  switch (e.event_type) {
    case "agent_step": return `node: ${d.node}`;
    case "llm_call": return (d.tool_calls && d.tool_calls.length ? `-> tools: ${d.tool_calls.join(", ")}` : "") +
      (d.output_preview ? ` ${d.output_preview}` : "");
    case "tool_call": return `${d.tool}(${JSON.stringify(d.input).slice(0, 120)})`;
    case "tool_result": return `${d.tool}: ${d.output_preview || ""}`;
    case "error": return d.error || d.llm_error || "";
    case "run_resumed": return `answer: ${(d.input && d.input.answer) || ""}`;
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
    pre.className = "result-box code-block";
    pre.textContent = v.code;
    box.appendChild(head);
    box.appendChild(pre);
  });
  return entries.length;
}

// A titled, copyable code block (same head + Copy pattern as renderCodeBlocks).
function copyableCodeBlock(title, text) {
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
  pre.className = "result-box code-block";
  pre.textContent = text;
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

// One flat timeline row (#, event, detail, status, duration).
function eventRow(e, typeLabel, detail) {
  const tr = document.createElement("tr");
  const cls = e.status === "ok" ? "badge-ok" : e.status === "error" ? "badge-error" : "";
  tr.innerHTML = `
      <td>${e.step ?? ""}</td>
      <td>${escapeHtml(typeLabel || e.event_type)}</td>
      <td>${escapeHtml(detail != null ? detail : eventDetail(e))}</td>
      <td><span class="${cls}">${escapeHtml(e.status || "")}</span></td>
      <td>${e.duration_ms != null ? formatMs(e.duration_ms) : ""}</td>`;
  return tr;
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
// the redraws watchRun triggers on every SSE event.
const orchGroupOpen = new Map();

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

  const startGroup = (e) => {
    const d = e.data || {};
    const agent = ORCH_TOOL_AGENTS[d.tool];
    const key = `${run.id}:${e.step}`;
    const tr = document.createElement("tr");
    tr.className = "orch-group";
    tr.innerHTML = `
      <td>${e.step ?? ""}</td>
      <td>tool_call</td>
      <td><span class="orch-toggle"></span><strong>${escapeHtml(d.tool)}</strong> → ${escapeHtml(agent || "file writes, no agent")}<span class="orch-count"></span></td>
      <td><span class="orch-status">running...</span></td>
      <td class="orch-ms"></td>`;
    tbody.appendChild(tr);
    const g = { tool: d.tool, step: e.step, endTs: null, tr, children: [] };
    // Default: open while the call is in progress, collapsed once finished;
    // a click overrides that for the rest of the session.
    g.isOpen = () => (orchGroupOpen.has(key) ? orchGroupOpen.get(key) : g.endTs == null);
    g.refresh = () => {
      const open = g.isOpen();
      tr.querySelector(".orch-toggle").textContent = open ? "▾" : "▸";
      tr.querySelector(".orch-count").textContent = ` (${g.children.length} nested event${g.children.length === 1 ? "" : "s"})`;
      g.children.forEach((c) => c.classList.toggle("hidden", !open));
    };
    g.addChild = (child) => {
      g.children.push(child);
      child.classList.toggle("hidden", !g.isOpen());
      tr.querySelector(".orch-count").textContent = ` (${g.children.length} nested event${g.children.length === 1 ? "" : "s"})`;
    };
    tr.addEventListener("click", () => { orchGroupOpen.set(key, !g.isOpen()); g.refresh(); });
    g.refresh();
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
    // (the `tools` wrapper step and the tool_result itself do not).
    if (parent && orch && e.event_type !== "tool_result" && !(e.event_type === "agent_step" && d.node === "tools") &&
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
          : `final answer: ${d.output_preview || "(empty)"}`;
        tr = eventRow(e, "decision", detail);
      } else {
        tr = eventRow(e);
      }
      tr.classList.add("orch-top");
      tbody.appendChild(tr);
      return;
    }
    const tr = eventRow(e);
    tbody.appendChild(tr);
    if (parent) { tr.classList.add("orch-nested"); parent.addChild(tr); }
  });
}

async function showAgentRun(runId) {
  const res = await fetch(`/api/agent-runs/${runId}`);
  if (!res.ok) return;
  const run = await res.json();
  $("#agent-run-panel").classList.remove("hidden");
  markSelectedRun(runId);
  $("#agent-run-meta").textContent =
    `run ${run.id.slice(0, 8)} | ${new Date(run.started_at * 1000).toLocaleString()} | ` +
    `${run.agent_name} v${run.agent_version || ""} | ${run.status} | ${run.duration_ms != null ? formatMs(run.duration_ms) : "-"}` +
    ` | model ${run.model || ""} | ${run.llm_calls.length} LLM call(s), ${run.tool_calls.length} tool call(s)` +
    (run.error ? ` | error: ${run.error}` : "");
  const agentResult = run.final_result || {};
  const agentError = run.error || agentResult.error;
  const meta = $("#agent-run-meta");
  meta.classList.toggle("error", Boolean(agentError));
  if (agentResult.status) {
    meta.textContent += ` | agent: ${agentResult.status}` + (agentResult.error && !run.error ? ` — ${agentResult.error}` : "");
  }

  const tbody = $("#agent-events");
  tbody.innerHTML = "";
  if (run.agent_name === "orchestrator") renderOrchestratorTimeline(tbody, run);
  else run.events.forEach((e) => tbody.appendChild(eventRow(e)));

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
    tools.appendChild(detailsBlock(title, body));
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
    clarify.classList.remove("hidden");
  } else {
    agentSession = null;
    clarify.classList.add("hidden");
  }
}

let agentSession = null;

$("#agent-answer-btn").addEventListener("click", async () => {
  const status = $("#agent-answer-status");
  const answer = $("#agent-answer").value;
  status.classList.remove("error");
  if (!agentSession) { status.textContent = "No session waiting."; status.classList.add("error"); return; }
  if (!answer.trim()) { status.textContent = "Type an answer first."; status.classList.add("error"); return; }
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
    watchRun(data.run_id, $("#agent-run-status"), $("#agent-answer-btn"));
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
  $("#pipeline-approve-status").textContent = "";
  $("#pipeline-approve-status").classList.remove("error");
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

  status.textContent = "Generating RTL...";
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
      const notesList = $("#pipeline-rtl-notes");
      notesList.innerHTML = "";
      (rtl.notes || []).forEach((n) => {
        const li = document.createElement("li");
        li.textContent = n;
        notesList.appendChild(li);
      });
      $("#pipeline-rtl-panel").classList.remove("hidden");
      status.textContent = "RTL generated.";
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

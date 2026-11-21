"use strict";
/* The dashboard's other sections: approvals, PC load, emergency controls, personalities, AI settings, message limits, history and
 * logs. Shares helpers ($, el, api, ...) with app.js. Same rule as there: text from the API goes on the page with textContent
 * (through el()), never as markup.
 */

let pendingApprovals = 0;
let currentTab = "live";
let logTimer = null;

function section(parent, id, items) {
  const box = $(id);
  box.replaceChildren(...items);
  return box;
}

function say(id, text, level) {
  const p = $(id);
  p.textContent = text;
  p.className = `hint ${level || ""}`.trim();
}

function number(input) { return input.value === "" ? NaN : Number(input.value); }

function labelled(text, input, hint) {
  return el("label", { class: "field" }, [el("span", { text }), input, hint ? el("small", { text: hint }) : document.createTextNode("")]);
}

function numberInput(value, min, max, step) {
  const input = el("input", { type: "number", step: step || "any" });
  if (min !== undefined) input.min = String(min);
  if (max !== undefined) input.max = String(max);
  input.value = String(value);
  return input;
}

/* ------------------------------------------------------------------ live: PC load */
function meter(label, percent, text) {
  const bar = el("progress", { max: "100" });
  bar.value = percent === null || percent === undefined ? 0 : percent;
  return el("div", { class: "meter" }, [
    el("span", { class: "meter-label", text: label }), bar, el("span", { class: "meter-text", text }),
  ]);
}

const gb = (mb) => (mb / 1024).toFixed(1);

function renderSystem(d) {
  const rows = [];
  rows.push(meter("CPU", d.cpu_percent, d.cpu_percent === null ? "-" : `${d.cpu_percent}%`));
  if (d.ram) rows.push(meter("RAM", d.ram.percent, `${gb(d.ram.used_mb)} / ${gb(d.ram.total_mb)} GB`));
  if (d.gpu && d.gpu.available) {
    rows.push(meter("GPU", d.gpu.utilization_percent, d.gpu.utilization_percent === null ? "-" : `${d.gpu.utilization_percent}%`));
    const vramPct = d.gpu.vram_total_mb ? (100 * d.gpu.vram_used_mb) / d.gpu.vram_total_mb : null;
    rows.push(meter("VRAM", vramPct, d.gpu.vram_total_mb ? `${gb(d.gpu.vram_used_mb)} / ${gb(d.gpu.vram_total_mb)} GB` : "-"));
    const extra = [d.gpu.name, d.gpu.temperature_c !== null ? `${d.gpu.temperature_c} °C` : null, d.gpu.power_w !== null ? `${d.gpu.power_w} W` : null];
    rows.push(el("p", { class: "hint", text: extra.filter(Boolean).join(" · ") }));
  } else {
    rows.push(el("p", { class: "hint", text: "No NVIDIA GPU readings available." }));
  }
  const info = el("dl", { class: "kv" });
  info.append(el("dt", { text: "Stream length" }), el("dd", { text: fmtDuration(d.session.duration_s) }));
  if (d.process) info.append(el("dt", { text: "Bot memory" }), el("dd", { text: `${d.process.private_mb} MB` }));
  rows.push(info);
  section(null, "system", rows);
}

/* ------------------------------------------------------------------ live: manual approval */
const approvalNodes = new Map();

function buildApprovalNode(item) {
  const input = el("textarea", { rows: "2", maxlength: "1000", "aria-label": "The message that will be sent (you can edit it)" });
  input.value = item.text;
  const timer = el("span", { class: "tag" });
  const approve = el("button", { type: "button", class: "go", text: "Approve and send" });
  const reject = el("button", { type: "button", class: "quiet", text: "Reject" });
  const who = item.viewer ? `${item.viewer} wrote: ${item.viewer_message || ""}` : `Started by the bot (${item.category || "proactive"})`;
  const root = el("li", { class: "approval" }, [
    el("div", { class: "meta" }, [el("span", { class: "who", text: item.platform }), el("span", { text: item.personality || "bot" }), timer]),
    el("div", { class: "body", text: who }),
    input,
    el("div", { class: "row" }, [approve, reject]),
  ]);
  const edited = () => (input.value.trim() !== item.text ? input.value.trim() : null);
  approve.addEventListener("click", () => decide(item.id, "approve", edited(), root));
  reject.addEventListener("click", () => decide(item.id, "reject", null, root));
  return { root, timer };
}

function renderApprovals(d) {
  pendingApprovals = d.items.length;
  $("card-approval").hidden = !(d.enabled || d.items.length > 0);
  $("approval-count").textContent = d.items.length ? `(${d.items.length})` : "";
  $("approval-empty").hidden = d.items.length > 0;
  const ids = new Set(d.items.map((i) => i.id));
  for (const [id, node] of approvalNodes) {
    if (!ids.has(id)) { node.root.remove(); approvalNodes.delete(id); }
  }
  for (const item of d.items) {
    let node = approvalNodes.get(item.id);
    if (!node) { node = buildApprovalNode(item); approvalNodes.set(item.id, node); $("approvals").append(node.root); }
    node.timer.textContent = `expires in ${fmtDuration(item.expires_in_s)}`;
  }
}

async function decide(id, action, text, root) {
  for (const b of root.querySelectorAll("button")) b.disabled = true;
  const r = await api(`/api/approvals/${id}/${action}`, { method: "POST", body: text ? { text } : {} });
  if (r.ok) say("approval-msg", action === "approve" ? "Sent." : "Rejected.", "ok");
  else {
    say("approval-msg", (r.data && r.data.detail) || `Could not ${action} (HTTP ${r.status})`, "error");
    for (const b of root.querySelectorAll("button")) b.disabled = false; // blocked (paused, rate limit): it is still waiting
  }
  await refresh();
}

/* ------------------------------------------------------------------ live: emergency controls */
let emergencySig = "";

function renderEmergency(status) {
  const entries = Object.entries(status.adapters).filter(([name]) => name !== "simulator");
  const sig = JSON.stringify(entries.map(([n, a]) => [n, a.state]));
  if (sig === emergencySig) return;
  emergencySig = sig;
  if (!entries.length) {
    section(null, "emergency-platforms", [el("p", { class: "hint", text: "No platform is configured yet (YouTube is switched on in backend\\.env)." })]);
    return;
  }
  section(null, "emergency-platforms", entries.map(([name, a]) => {
    const connected = a.state === "connected" || a.state === "connecting";
    const button = el("button", { type: "button", text: connected ? "Disconnect" : "Connect", class: connected ? "danger" : "" });
    button.addEventListener("click", () => setPlatform(name, connected ? "disconnect" : "connect"));
    return el("div", { class: "row spread" }, [el("span", { class: "name", text: `${name}: ${a.state}` }), button]);
  }));
}

async function setPlatform(name, action) {
  if (action === "disconnect" && !confirm(`Disconnect ${name}? The bot stops reading and writing there until you connect it again.`)) return;
  const r = await api(`/api/platforms/${encodeURIComponent(name)}/${action}`, { method: "POST" });
  say("stop-msg", r.ok ? `${name}: ${r.data.state}` : (r.data && r.data.detail) || `Could not ${action} ${name}`, r.ok ? "ok" : "error");
  emergencySig = "";
  await refresh();
}

async function stopBot(restart) {
  const question = restart ? "Restart the bot? It is offline for a few seconds." : "Stop the bot completely? You will have to start it again with scripts\\start.bat.";
  if (!confirm(question)) return;
  const r = await api("/api/control/stop", { method: "POST", body: { restart } });
  if (r.status === 202) say("stop-msg", restart ? "Restarting... this page reconnects by itself." : "Stopping. You can close this page.", "ok");
  else say("stop-msg", (r.data && r.data.detail) || `Could not stop (HTTP ${r.status})`, "error");
}

async function refreshLive(status) {
  const [approvals, system] = await Promise.all([api("/api/approvals"), api("/api/system")]);
  if (approvals.ok) renderApprovals(approvals.data);
  if (system.ok) renderSystem(system.data);
  if (status) renderEmergency(status);
}

/* ------------------------------------------------------------------ personalities */
async function loadPersonalities() {
  const r = await api("/api/personalities");
  if (!r.ok) return;
  section(null, "pers-cards", r.data.map(buildPersonalityCard));
  setText("pers-warning", r.data.some((p) => p.enabled) ? "" : "Every personality is off: the bot will not write AI replies.");
}

function buildPersonalityCard(p) {
  const enabled = el("input", { type: "checkbox", "aria-label": `Enable ${p.name}` });
  enabled.checked = p.enabled;
  const description = el("input", { type: "text", maxlength: "300" });
  description.value = p.description || "";
  const prompt = el("textarea", { rows: "9", maxlength: "4000", spellcheck: "true" });
  prompt.value = p.system_prompt;
  const cooldown = numberInput(p.cooldown_seconds, 0, 86400, "1");
  const frequency = numberInput(p.frequency, 0, 10, "any");
  const priority = numberInput(p.priority, 0, 100, "1");
  const msg = el("p", { class: "hint", role: "status" });
  const trySelect = el("select", { "aria-label": "Message type to try" });
  for (const c of p.categories) trySelect.append(el("option", { value: c, text: humanize(c) }));
  const tryText = el("input", { type: "text", maxlength: "200", placeholder: "A chat message to answer" });
  tryText.value = "what game is this?";
  const tryOut = el("p", { class: "hint", role: "status" });
  const save = el("button", { type: "button", text: "Save" });
  const reset = el("button", { type: "button", class: "quiet", text: "Reset prompt to default" });
  const preview = el("button", { type: "button", text: "Preview a reply" });

  const note = (text, level) => { msg.textContent = text; msg.className = `hint ${level || ""}`.trim(); };
  enabled.addEventListener("change", async () => {
    const r = await api(`/api/personalities/${encodeURIComponent(p.name)}/enabled`, { method: "POST", body: { enabled: enabled.checked } });
    if (!r.ok) enabled.checked = !enabled.checked;
    note(r.ok ? (enabled.checked ? "Enabled." : "Disabled.") : "Could not change that", r.ok ? "ok" : "error");
  });
  save.addEventListener("click", async () => {
    const r = await api(`/api/personalities/${encodeURIComponent(p.name)}/settings`, {
      method: "POST",
      body: { system_prompt: prompt.value, description: description.value, cooldown_seconds: number(cooldown), frequency: number(frequency), priority: number(priority) },
    });
    note(r.ok ? "Saved. It is used from the next message." : `Not saved: ${formatProblems(r.data && r.data.detail)}`, r.ok ? "ok" : "error");
  });
  reset.addEventListener("click", async () => {
    if (!confirm(`Put ${p.name}'s prompt and description back to the shipped default?`)) return;
    const r = await api(`/api/personalities/${encodeURIComponent(p.name)}/reset`, { method: "POST" });
    if (r.ok) { prompt.value = r.data.system_prompt; description.value = r.data.description; }
    note(r.ok ? "Reset to the default." : "Could not reset", r.ok ? "ok" : "error");
  });
  preview.addEventListener("click", async () => {
    tryOut.textContent = "Writing a sample (nothing is sent)...";
    const r = await api(`/api/personalities/${encodeURIComponent(p.name)}/preview`, {
      method: "POST", body: { category: trySelect.value, message: tryText.value, username: "viewer" },
    });
    tryOut.className = `hint ${r.ok ? "" : "error"}`.trim();
    tryOut.textContent = r.ok ? `${r.data.text}  (${r.data.model}, ${r.data.latency_ms} ms)` : (r.data && r.data.detail) || "No sample available";
  });

  return el("article", { class: "card pers" }, [
    el("h3", {}, [enabled, document.createTextNode(" " + p.name)]),
    el("p", { class: "hint", text: `Handles: ${p.categories.map(humanize).join(", ") || "nothing"}` }),
    labelled("Description", description),
    labelled("System prompt (who it is and how it talks)", prompt),
    el("div", { class: "fields three" }, [
      labelled("Cooldown (seconds)", cooldown, "pause between its own messages"),
      labelled("Frequency", frequency, "weight when several fit"),
      labelled("Priority", priority, "higher goes first"),
    ]),
    el("div", { class: "row" }, [save, reset]),
    msg,
    el("div", { class: "row" }, [trySelect, tryText, preview]),
    tryOut,
  ]);
}

/* ------------------------------------------------------------------ AI settings */
const AI_LABELS = {
  num_ctx: ["Context window (tokens)", "how much chat history the model sees"],
  num_predict: ["Longest reply the model may write (tokens)", "about 1.3 tokens per word"],
  temperature: ["Creativity (temperature)", "0 = predictable, 2 = wild"],
  generation_timeout_s: ["Give up on one attempt after (seconds)", "slow GPU or battery: keep it generous"],
  reply_max_words: ["Longest accepted reply (words)", "longer replies are rejected and rewritten"],
};
let aiInputs = {};

async function loadAiSettings() {
  const r = await api("/api/ai/settings");
  if (!r.ok) return;
  aiInputs = {};
  const rows = Object.keys(AI_LABELS).map((key) => {
    const [lo, hi] = r.data.limits[key];
    const input = numberInput(r.data.values[key], lo, hi, "any");
    aiInputs[key] = input;
    return labelled(AI_LABELS[key][0], input, `${AI_LABELS[key][1]} (${lo}-${hi})`);
  });
  section(null, "ai-fields", rows);
}

async function saveAiSettings(event) {
  event.preventDefault();
  const body = {};
  for (const [key, input] of Object.entries(aiInputs)) body[key] = number(input);
  const r = await api("/api/ai/settings", { method: "POST", body });
  say("ai-msg", r.ok ? "Saved. It applies from the next reply." : `Not saved: ${formatProblems(r.data && r.data.detail)}`, r.ok ? "ok" : "error");
  if (r.ok) await loadAiSettings();
}

async function testAi() {
  say("ai-test-result", "Asking the model (nothing is sent to chat)...", "");
  const r = await api("/api/ai/test", { method: "POST" });
  if (r.ok) {
    const speed = r.data.tokens_per_s ? `, ${r.data.tokens_per_s} tokens/s` : "";
    say("ai-test-result", `"${r.data.text}"  (${r.data.model}, ${r.data.latency_ms} ms${speed}${r.data.used_fallback ? ", backup model" : ""})`, "ok");
  } else say("ai-test-result", (r.data && r.data.detail) || `The test failed (HTTP ${r.status})`, "error");
}

/* ------------------------------------------------------------------ message limits */
const LIMIT_LABELS = {
  min_gap_s: ["Gap between messages (s)"],
  max_per_window: ["Max messages per window"],
  window_s: ["Window length (s)"],
  user_cooldown_s: ["Same-viewer cooldown (s)"],
  max_chars: ["Longest message (characters)"],
};

async function loadLimits() {
  const r = await api("/api/rate-limits");
  if (!r.ok) return;
  const order = ["youtube", "kick", "simulator"];
  const names = Object.keys(r.data.platforms).sort((a, b) => order.indexOf(a) - order.indexOf(b));
  section(null, "limits-forms", names.map((name) => {
    const inputs = {};
    const fields = Object.keys(LIMIT_LABELS).map((key) => {
      const [lo, hi] = r.data.limits[key];
      inputs[key] = numberInput(r.data.platforms[name][key], lo, hi, "any");
      return labelled(LIMIT_LABELS[key][0], inputs[key]);
    });
    const save = el("button", { type: "button", text: "Save" });
    save.addEventListener("click", async () => {
      const body = {};
      for (const [key, input] of Object.entries(inputs)) body[key] = number(input);
      const res = await api(`/api/rate-limits/${encodeURIComponent(name)}`, { method: "POST", body });
      say("limits-msg", res.ok ? `${name}: saved. It applies at once.` : `${name}: not saved: ${formatProblems(res.data && res.data.detail)}`, res.ok ? "ok" : "error");
    });
    return el("div", { class: "platform" }, [el("h3", { text: name }), el("div", { class: "fields three" }, fields), save]);
  }));
}

/* ------------------------------------------------------------------ history and logs */
function fmtDateTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

async function loadSessions() {
  const r = await api("/api/sessions?limit=30");
  if (!r.ok) return;
  $("sessions-empty").hidden = r.data.length > 0;
  const body = $("sessions-table").querySelector("tbody");
  body.replaceChildren(...r.data.map((s) => {
    const st = s.stats || {};
    const errors = (st.send_failures || 0) + (st.processing_errors || 0) + (st.ai_unavailable || 0);
    const speed = st.ai_latency_ms_median ? `${Math.round(st.ai_latency_ms_median)} ms${st.ai_latency_ms_p95 ? ` (p95 ${Math.round(st.ai_latency_ms_p95)})` : ""}` : "-";
    const cells = [fmtDateTime(s.started_at), s.duration_seconds === null ? "-" : fmtDuration(s.duration_seconds),
      s.current ? "running" : s.status, String(st.messages_received || 0), String(st.replies_sent || 0), String(errors), speed];
    const row = el("tr", { class: s.current ? "current" : s.status === "crashed" ? "crashed" : "" }, cells.map((c) => el("td", { text: c })));
    return row;
  }));
}

async function loadConnLogs() {
  const r = await api("/api/connection-logs?limit=60");
  if (!r.ok) return;
  $("conn-empty").hidden = r.data.length > 0;
  section(null, "conn-logs", r.data.map((e) => el("li", {}, [
    el("div", { class: "grow" }, [
      el("div", { class: "name", text: `${e.platform}: ${e.event}` }),
      el("div", { class: "desc", text: e.message || "" }),
    ]),
    el("span", { class: "tag", text: fmtDateTime(e.timestamp) }),
  ])));
}

async function loadLogs() {
  const level = $("log-level").value;
  const lines = Math.min(1000, Math.max(10, Number($("log-lines").value) || 150));
  const r = await api(`/api/logs?lines=${lines}&level=${encodeURIComponent(level)}`);
  if (!r.ok) return;
  const view = $("log-view");
  const stick = view.scrollTop + view.clientHeight >= view.scrollHeight - 30; // follow the end only if the reader is already there
  view.replaceChildren(...r.data.map((e) => el("div", { class: `logline level-${e.level}` }, [
    el("span", { class: "logtime", text: e.time.slice(11) }),
    el("span", { class: "loglevel", text: e.level }),
    el("span", { class: "logname", text: e.logger.replace(/^app\./, "") }),
    el("span", { class: "logmsg", text: e.message }),
  ])));
  if (stick) view.scrollTop = view.scrollHeight;
}

function setLogTimer() {
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
  if ($("log-auto").checked) logTimer = setInterval(() => { if (currentTab === "history" && !document.hidden) loadLogs(); }, 5000);
}

/* ------------------------------------------------------------------ wiring */
function onTabShown(name) {
  currentTab = name;
  if (name === "personalities") loadPersonalities();
  else if (name === "ai") loadAiSettings();
  else if (name === "scheduler") loadLimits();
  else if (name === "history") { loadSessions(); loadConnLogs(); loadLogs(); }
}

function initPanels() {
  $("btn-stop").addEventListener("click", () => stopBot(false));
  $("btn-restart").addEventListener("click", () => stopBot(true));
  $("ai-form").addEventListener("submit", saveAiSettings);
  $("btn-ai-test").addEventListener("click", testAi);
  $("btn-sessions").addEventListener("click", loadSessions);
  $("btn-conn").addEventListener("click", loadConnLogs);
  $("btn-logs").addEventListener("click", loadLogs);
  $("log-level").addEventListener("change", loadLogs);
  $("log-auto").addEventListener("change", setLogTimer);
}

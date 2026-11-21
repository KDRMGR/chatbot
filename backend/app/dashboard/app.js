"use strict";
/* StealthBot control panel. Plain JavaScript, no build step.
 *
 * SECURITY RULE: chat messages and names come from strangers. Everything from the API is put on the page with
 * textContent (via el()), never innerHTML, so a viewer cannot inject markup or script. A test in the suite
 * fails if this file ever uses innerHTML, outerHTML, insertAdjacentHTML, document.write or eval.
 */

const $ = (id) => document.getElementById(id);
const KEY_STORE = "stealthbot.apikey"; // sessionStorage: gone when the tab is closed
let apiKey = null;
let models = null; // last /api/models answer
let lastFeedSig = "";
let switching = false;
let schedDirty = false; // the streamer is editing the timing form: the 2-second refresh must not overwrite it
const ACTIVITIES = ["general", "gaming_question", "trivia"];

/* ------------------------------------------------------------------ helpers */
function el(tag, props, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const child of children || []) node.append(child);
  return node;
}

function setText(id, text) { $(id).textContent = text; }

function fmtTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`;
}

function humanize(key) { return key.replace(/_/g, " "); }

function scalar(value) {
  if (value === null || value === undefined) return "-";
  if (typeof value === "object") return JSON.stringify(value).slice(0, 160);
  return String(value);
}

/* ------------------------------------------------------------------ API */
class AuthError extends Error {}

async function api(path, options) {
  const opts = Object.assign({ method: "GET" }, options || {});
  opts.headers = Object.assign({ "X-API-Key": apiKey }, opts.headers || {});
  if (opts.body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  if (resp.status === 401) throw new AuthError("unauthorised");
  let data = null;
  try { data = await resp.json(); } catch (_) { /* non-JSON error body */ }
  return { ok: resp.ok, status: resp.status, data };
}

function showSignIn(message) {
  apiKey = null;
  try { sessionStorage.removeItem(KEY_STORE); } catch (_) { /* storage blocked: fine */ }
  $("app").hidden = true;
  $("signin").hidden = false;
  setText("signin-msg", message || "");
}

function showApp() {
  $("signin").hidden = true;
  $("app").hidden = false;
}

/* ------------------------------------------------------------------ rendering */
function pill(text, level) {
  return el("span", { class: `pill ${level || ""}`.trim(), text });
}

function renderPills(status) {
  const box = $("pills");
  box.replaceChildren();
  const paused = status.controls.outgoing_paused;
  box.append(pill(paused ? "Replies paused" : "Replies live", paused ? "bad" : "ok"));

  const ai = status.ai;
  if (ai.ollama_up === false) box.append(pill("AI offline (rules only)", "bad"));
  else if (!status.controls.ai_enabled) box.append(pill("AI off (rules only)", "warn"));
  else if (ai.primary_degraded) box.append(pill(`Backup model: ${ai.active_model}`, "warn"));
  else box.append(pill(`AI: ${ai.active_model || "?"}`, ai.ollama_up ? "ok" : "warn"));

  if (!status.controls.proactive_enabled) box.append(pill("Proactive chat off", "warn"));
  if (typeof pendingApprovals === "number" && pendingApprovals > 0) box.append(pill(`${pendingApprovals} waiting for approval`, "warn"));
  else if (status.controls.approval_required) box.append(pill("Approval mode on", "warn"));

  for (const [name, a] of Object.entries(status.adapters)) {
    if (name === "simulator") continue;
    const level = a.state === "connected" ? "ok" : a.state === "error" ? "bad" : "warn";
    box.append(pill(`${name}: ${a.state}`, level));
  }
  box.append(pill(`Up ${fmtDuration(status.metrics.uptime_s)}`));
}

const toMinutes = (seconds) => Math.round((seconds / 60) * 100) / 100;

function renderScheduler(s) {
  $("chk-proactive").checked = s.enabled;
  const box = $("sched-status");
  box.replaceChildren();
  for (const [name, p] of Object.entries(s.platforms)) {
    let text;
    if (!p.connected) text = "not connected: timers start when it connects";
    else {
      const next = Object.entries(p.next_in_s).filter(([n]) => s.settings.activities[n] && s.settings.activities[n].enabled)
        .map(([n, secs]) => `${humanize(n)} in ${fmtDuration(secs)}`).join(", ");
      text = `chat quiet for ${fmtDuration(p.quiet_for_s || 0)}, ${p.may_speak_now ? "may speak now" : "waiting"}` +
        (next ? ` · next: ${next}` : "") + ` · sent ${p.sent}` + (p.last_outcome && p.failures ? ` · last problem: ${p.last_outcome}` : "");
    }
    box.append(el("div", { class: "platform" }, [el("h3", { text: name }), el("p", { class: "hint", text })]));
  }
  if (schedDirty || $("sched-form").contains(document.activeElement)) return;
  $("set-quiet").value = toMinutes(s.settings.quiet_after_s);
  $("set-gap").value = toMinutes(s.settings.min_gap_s);
  for (const n of ACTIVITIES) {
    const a = s.settings.activities[n];
    if (!a) continue;
    $(`act-${n}-on`).checked = a.enabled;
    $(`act-${n}-min`).value = toMinutes(a.min_interval_s);
    $(`act-${n}-max`).value = toMinutes(a.max_interval_s);
  }
}

function renderControls(status) {
  const paused = status.controls.outgoing_paused;
  const btn = $("btn-pause");
  btn.textContent = paused ? "Resume replies" : "Pause all replies";
  btn.className = paused ? "big go" : "big stop";
  setText("pause-hint", paused
    ? "Nothing is being sent to any chat. The bot still reads chat."
    : "Emergency stop: nothing is sent to any chat until you resume.");
  $("chk-ai").checked = status.controls.ai_enabled;
  $("chk-approval").checked = status.controls.approval_required;
}

function renderModelNow(status) {
  const ai = status.ai;
  const dl = $("model-now");
  dl.replaceChildren();
  const rows = [
    ["Primary", ai.primary_model],
    ["Backup", ai.fallback_model || "none"],
    ["In use now", ai.active_model || "-"],
    ["Ollama", ai.ollama_up ? `running ${ai.version || ""}`.trim() : ai.ollama_up === false ? "not reachable" : "checking"],
  ];
  for (const m of ai.loaded || []) {
    rows.push(["In memory", `${m.name} (${m.gpu_pct === null ? "?" : m.gpu_pct}% on GPU)`]);
  }
  if (ai.switching_to) rows.push(["Switching to", ai.switching_to]);
  for (const [k, v] of rows) dl.append(el("dt", { text: k }), el("dd", { text: scalar(v) }));
}

// What the user picked in each dropdown but has not applied yet (undefined = nothing picked). The 2-second refresh
// must never throw this away, otherwise a pick could vanish between choosing a model and pressing the button.
const picks = { primary: undefined, fallback: undefined };

function fillSelect(select, names, current, pick, noneLabel) {
  const wanted = (noneLabel ? [""] : []).concat(names);
  const same = select.options.length === wanted.length && wanted.every((n, i) => select.options[i].value === n);
  if (!same) {
    select.replaceChildren();
    for (const n of wanted) select.append(el("option", { value: n, text: n === "" ? noneLabel : n }));
  }
  select.value = pick !== undefined && wanted.includes(pick) ? pick : (current || "");
}

function renderModels() {
  if (!models) return;
  const installed = models.installed.slice();
  for (const m of [models.primary, models.fallback]) if (m && !installed.includes(m)) installed.push(m);
  if (picks.primary === models.primary) picks.primary = undefined; // already applied
  if (picks.fallback !== undefined && (picks.fallback || null) === (models.fallback || null)) picks.fallback = undefined;
  fillSelect($("sel-primary"), installed, models.primary, picks.primary);
  fillSelect($("sel-fallback"), installed.filter((n) => n !== models.primary), models.fallback, picks.fallback,
    "(none: rule replies instead)");
  $("btn-switch").disabled = switching || $("sel-primary").value === models.primary;
  $("btn-fallback").disabled = switching || ($("sel-fallback").value || null) === (models.fallback || null);
}

function renderFeed(items) {
  const sig = JSON.stringify(items.map((i) => [i.id, i.response && i.response.delivery_status]));
  if (sig === lastFeedSig) return;
  lastFeedSig = sig;
  const feed = $("feed");
  feed.replaceChildren();
  // A reply is shown under the message it answers, so its own stored copy as a "bot" row would appear twice.
  const answered = new Set(items.filter((i) => i.response).map((i) => i.response.text));
  items = items.filter((i) => !(i.is_bot_message && answered.has(i.message)));
  $("feed-empty").hidden = items.length > 0;
  for (const m of items.slice().reverse()) {
    const blocked = Boolean(m.filter_reason);
    const meta = el("div", { class: "meta" }, [
      el("span", { class: "who", text: m.username || "?" }),
      el("span", { text: m.platform }),
      el("span", { text: fmtTime(m.timestamp) }),
    ]);
    if (m.category) meta.append(el("span", { class: "tag", text: m.category }));
    if (blocked) meta.append(el("span", { class: "tag warn", text: `not answered: ${m.filter_reason}` }));
    const row = el("li", { class: `msg${blocked ? " blocked" : ""}${m.is_bot_message ? " bot" : ""}` }, [
      meta, el("div", { class: "body", text: m.message }),
    ]);
    if (m.response) {
      const r = m.response;
      const failed = r.delivery_status && r.delivery_status !== "sent" && r.delivery_status !== "pending";
      const rmeta = el("div", { class: "meta" }, [
        el("span", { class: "who", text: r.personality || "bot" }),
        el("span", { text: r.source === "ai" ? `AI · ${r.model || ""}` : r.source }),
      ]);
      if (r.latency_ms) rmeta.append(el("span", { text: `${r.latency_ms} ms` }));
      rmeta.append(el("span", { class: failed ? "tag bad" : "tag", text: r.delivery_status || "" }));
      row.append(el("div", { class: "reply" }, [rmeta, el("div", { class: "body", text: r.text })]));
    }
    feed.append(row);
  }
}

function renderPlatforms(status) {
  const box = $("platforms");
  box.replaceChildren();
  for (const [name, a] of Object.entries(status.adapters)) {
    const level = a.state === "connected" ? "ok" : a.state === "error" ? "bad" : "warn";
    const dl = el("dl", { class: "kv" });
    for (const [k, v] of Object.entries(a)) {
      if (k === "state" || k === "detail" || k === "quota") continue;
      dl.append(el("dt", { text: humanize(k) }), el("dd", { text: scalar(v) }));
    }
    if (a.quota) {
      dl.append(el("dt", { text: "quota used" }), el("dd", { text: `${a.quota.used} / ${a.quota.limit}` }));
      dl.append(el("dt", { text: "quota resets in" }), el("dd", { text: fmtDuration(a.quota.resets_in_s) }));
      if (a.quota.exhausted) dl.append(el("dt", { text: "quota" }), el("dd", { text: "EXHAUSTED" }));
    }
    box.append(el("div", { class: "platform" }, [
      el("h3", {}, [document.createTextNode(name), pill(a.state, level)]),
      el("p", { class: "hint", text: a.detail || "" }),
      dl,
    ]));
  }
}

function renderStats(status) {
  const dl = $("stats");
  dl.replaceChildren();
  const lat = status.metrics.ai_latency_ms;
  const rows = [
    ["Queue", `${status.queue_size} waiting, ${status.queue_dropped} dropped`],
    ["AI replies", status.ai.counters.generations],
    ["AI failures", status.ai.counters.failures],
    ["Backup used", status.ai.counters.fallback_used],
    ["AI speed (median)", lat.median === null ? "-" : `${lat.median} ms`],
    ["AI speed (p95)", lat.p95 === null ? "-" : `${lat.p95} ms`],
  ];
  for (const [k, v] of Object.entries(status.metrics.counters).sort()) rows.push([humanize(k), v]);
  for (const [k, v] of rows) dl.append(el("dt", { text: k }), el("dd", { text: scalar(v) }));
}

/* ------------------------------------------------------------------ actions */
async function togglePause() {
  const paused = $("btn-pause").textContent.startsWith("Resume");
  const r = await api(`/api/control/${paused ? "resume" : "pause"}`, { method: "POST" });
  if (r.ok) await refresh();
}

async function toggleAi() {
  const r = await api("/api/control/ai", { method: "POST", body: { enabled: $("chk-ai").checked } });
  if (!r.ok) $("chk-ai").checked = !$("chk-ai").checked;
  await refresh();
}

async function toggleProactive() {
  const r = await api("/api/control/proactive", { method: "POST", body: { enabled: $("chk-proactive").checked } });
  if (!r.ok) $("chk-proactive").checked = !$("chk-proactive").checked;
  await refresh();
}

function formatProblems(detail) {
  if (!Array.isArray(detail)) return String(detail || "Could not save");
  return detail.slice(0, 3).map((e) => `${(e.loc || []).join(" > ")}: ${e.msg}`).join("; ");
}

async function saveScheduler(event) {
  event.preventDefault();
  const minutes = (id) => Number($(id).value) * 60;
  const activities = {};
  for (const n of ACTIVITIES) {
    activities[n] = { enabled: $(`act-${n}-on`).checked, min_interval_s: minutes(`act-${n}-min`), max_interval_s: minutes(`act-${n}-max`) };
  }
  const body = { quiet_after_s: minutes("set-quiet"), min_gap_s: minutes("set-gap"), activities };
  const r = await api("/api/scheduler/settings", { method: "POST", body });
  const msg = $("sched-msg");
  msg.textContent = r.ok ? "Saved. New timings apply from now." : `Not saved: ${formatProblems(r.data && r.data.detail)}`;
  msg.className = `hint ${r.ok ? "ok" : "error"}`;
  if (r.ok) schedDirty = false;
  await refresh();
}

function modelMessage(text, level) {
  const p = $("model-msg");
  p.textContent = text;
  p.className = `hint ${level || ""}`.trim();
}

async function switchPrimary() {
  const target = $("sel-primary").value;
  if (!target || target === models.primary) return;
  if (!confirm(`Switch the AI to ${target}?\n\nChat replies pause for a few seconds while it loads and is tested. ` +
    "If the test fails, the current model stays.")) return;
  switching = true;
  renderModels();
  modelMessage(`Loading and testing ${target}... this can take up to a minute on a cold start.`);
  try {
    const r = await api("/api/control/model", { method: "POST", body: { model: target, role: "primary" } });
    if (r.ok && r.data.changed) {
      const speed = r.data.tokens_per_s ? `, ${r.data.tokens_per_s} tokens/s` : "";
      modelMessage(`Now using ${r.data.primary} (test reply in ${r.data.latency_ms} ms${speed}). Sample: "${r.data.sample}"`, "ok");
      if (r.data.thinking_leaked) modelMessage(`Now using ${r.data.primary}, but it printed thinking text during the test; replies may be slow or odd.`, "error");
    } else {
      modelMessage((r.data && r.data.detail) || `Could not switch (HTTP ${r.status})`, r.ok ? "" : "error");
    }
  } finally {
    switching = false;
    picks.primary = undefined; // success: now the current model; failure: the old model is still the current one
    await loadModels();
    await refresh();
  }
}

async function setFallback() {
  const target = $("sel-fallback").value || null;
  const r = await api("/api/control/model", { method: "POST", body: { model: target, role: "fallback" } });
  modelMessage(r.ok ? `Backup model: ${target || "none (rule replies instead)"}` : (r.data && r.data.detail) || "Could not change", r.ok ? "ok" : "error");
  picks.fallback = undefined;
  await loadModels();
}

async function toggleApproval() {
  const r = await api("/api/control/approval", { method: "POST", body: { enabled: $("chk-approval").checked } });
  if (!r.ok) $("chk-approval").checked = !$("chk-approval").checked;
  await refresh();
}

async function sendTest(event) {
  event.preventDefault();
  const user = $("test-user").value.trim() || "tester";
  const message = $("test-msg").value.trim();
  if (!message) return;
  setText("test-result", "Sending...");
  const r = await api("/api/simulate/message", { method: "POST", body: { username: user, message } });
  if (!r.ok) {
    setText("test-result", `Failed: ${JSON.stringify((r.data && r.data.detail) || r.status).slice(0, 200)}`);
    return;
  }
  const d = r.data;
  setText("test-result", d.action === "held"
    ? `${d.personality || "The bot"} wrote a reply and is waiting for your approval above: ${d.reply}`
    : d.reply
      ? `${d.personality} replied (${d.source}, ${d.latency_ms || 0} ms): ${d.reply}`
      : `No reply: ${d.action}${d.reason ? " (" + d.reason + ")" : ""}`);
  $("test-msg").value = "";
  await refresh();
}

/* ------------------------------------------------------------------ data loop */
async function loadModels() {
  const r = await api("/api/models");
  if (r.ok) { models = r.data; renderModels(); }
}

async function refresh() {
  const [status, feed, sched] = await Promise.all([api("/api/status"), api("/api/messages?limit=40"), api("/api/scheduler")]);
  if (typeof refreshLive === "function") await refreshLive(status.ok ? status.data : null); // approvals, PC load, emergency buttons (panels.js)
  if (sched.ok) renderScheduler(sched.data);
  if (status.ok) { renderPills(status.data); renderControls(status.data); renderModelNow(status.data); renderPlatforms(status.data); renderStats(status.data); }
  if (feed.ok) renderFeed(feed.data);
  if (models && status.ok) { models.primary = status.data.ai.primary_model; models.fallback = status.data.ai.fallback_model; renderModels(); }
}

/* ------------------------------------------------------------------ tabs */
const TAB_STORE = "stealthbot.tab";
const TABS = ["live", "personalities", "ai", "scheduler", "history"];

function showTab(name) {
  if (!TABS.includes(name)) name = "live";
  for (const t of TABS) {
    $(`panel-${t}`).hidden = t !== name;
    $(`tab-${t}`).setAttribute("aria-selected", String(t === name));
  }
  try { sessionStorage.setItem(TAB_STORE, name); } catch (_) { /* fine */ }
  if (typeof onTabShown === "function") onTabShown(name); // panels.js loads what that tab needs
}

let timer = null;
async function loop() {
  try {
    await refresh();
  } catch (err) {
    if (err instanceof AuthError) { showSignIn("The key was rejected. Sign in again."); return; }
    // Bot not reachable (restarting?): keep trying quietly.
    $("pills").replaceChildren(pill("Cannot reach the bot", "bad"));
  }
  timer = setTimeout(loop, document.hidden ? 10000 : 2000);
}

async function start() {
  showApp();
  let tab = "live";
  try { tab = sessionStorage.getItem(TAB_STORE) || "live"; } catch (_) { /* fine */ }
  showTab(tab);
  try { await loadModels(); } catch (err) { if (err instanceof AuthError) { showSignIn("The key was rejected."); return; } }
  loop();
}

/* ------------------------------------------------------------------ boot: sign in with a ticket, a saved key, or a pasted key */
async function redeemTicket(ticket) {
  const resp = await fetch("/api/auth/redeem", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticket }),
  });
  if (!resp.ok) return null;
  return (await resp.json()).api_key || null;
}

async function boot() {
  for (const t of TABS) $(`tab-${t}`).addEventListener("click", () => showTab(t));
  if (typeof initPanels === "function") initPanels(); // panels.js registers its own buttons and forms
  $("btn-pause").addEventListener("click", togglePause);
  $("chk-ai").addEventListener("change", toggleAi);
  $("chk-approval").addEventListener("change", toggleApproval);
  $("chk-proactive").addEventListener("change", toggleProactive);
  $("sched-form").addEventListener("submit", saveScheduler);
  $("sched-form").addEventListener("input", () => { schedDirty = true; });
  $("btn-switch").addEventListener("click", switchPrimary);
  $("btn-fallback").addEventListener("click", setFallback);
  $("btn-reload-models").addEventListener("click", () => loadModels().then(() => modelMessage("Model list reloaded.")));
  $("sel-primary").addEventListener("change", () => { picks.primary = $("sel-primary").value; renderModels(); });
  $("sel-fallback").addEventListener("change", () => { picks.fallback = $("sel-fallback").value; renderModels(); });
  $("test-form").addEventListener("submit", sendTest);
  $("signin-form").addEventListener("submit", (e) => {
    e.preventDefault();
    apiKey = $("signin-key").value.trim();
    $("signin-key").value = "";
    try { sessionStorage.setItem(KEY_STORE, apiKey); } catch (_) { /* fine */ }
    start();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && timer) { clearTimeout(timer); loop(); } });

  const ticket = new URLSearchParams(location.hash.slice(1)).get("ticket");
  if (ticket) history.replaceState(null, "", location.pathname); // the ticket must not stay in the address bar
  if (ticket) {
    try { apiKey = await redeemTicket(ticket); } catch (_) { apiKey = null; }
    if (apiKey) { try { sessionStorage.setItem(KEY_STORE, apiKey); } catch (_) { /* fine */ } }
  }
  if (!apiKey) { try { apiKey = sessionStorage.getItem(KEY_STORE); } catch (_) { apiKey = null; } }
  if (apiKey) start();
  else showSignIn(ticket ? "That sign-in link expired. Run scripts\\open_dashboard.bat again." : "");
}

// Both scripts (this one and panels.js) have run by the time the page is ready, so boot() can use functions from either.
document.addEventListener("DOMContentLoaded", boot);

"use strict";
/* The online panel. No libraries: it talks to Supabase's documented REST endpoints (Auth + PostgREST) with fetch.
 * Rules this file follows: text from the database goes on the page with textContent only (never as markup); the only secrets
 * it holds are the signed-in user's own tokens, kept in sessionStorage (this tab only); the key in config.js is the PUBLIC key.
 * What a user may do is decided by the database policies (supabase/schema.sql); the buttons hidden here are a convenience. */

const CFG = window.STEALTHBOT_CONFIG || {};
const BASE = String(CFG.url || "").replace(/\/$/, "");
const SESSION_KEY = "stealthbot.session";
const POLL_MS = 3000;
const ONLINE_WITHIN_S = 20;
const $ = (id) => document.getElementById(id);

let session = null;   // { access_token, refresh_token, expires_at (unix s), user: { id, email } }
let role = null;      // "admin" | "streamer"
let latest = null;    // { data, updated_at } from bot_status
let pollTimer = null;
const commandLog = [];
const dirty = new Set(); // form fields the user has edited and not saved: the 3-second refresh must not overwrite them

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
function say(id, text, level) { const p = $(id); p.textContent = text; p.className = `hint ${level || ""}`.trim(); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function ago(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
function clock(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function show(view) {
  for (const id of ["view-login", "view-password", "view-app"]) $(id).hidden = id !== view;
  $("top-right").hidden = view === "view-login";
}

/* ------------------------------------------------------------------ Supabase calls (documented REST endpoints) */
async function call(path, { method = "GET", body, token, params, extra } = {}) {
  const url = new URL(BASE + path);
  for (const [k, v] of Object.entries(params || {})) url.searchParams.set(k, v);
  const headers = { apikey: CFG.key, Accept: "application/json", ...(extra || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let resp;
  try { resp = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) }); }
  catch (_) { return { ok: false, status: 0, data: null }; }
  let data = null;
  try { data = await resp.json(); } catch (_) { /* empty body */ }
  return { ok: resp.ok, status: resp.status, data };
}

function sessionFrom(data, user) {
  return {
    access_token: data.access_token, refresh_token: data.refresh_token || (session && session.refresh_token),
    expires_at: Math.floor(Date.now() / 1000) + Number(data.expires_in || 3600),
    user: user || (data.user ? { id: data.user.id, email: data.user.email } : session && session.user),
  };
}
function keep(s) {
  session = s;
  try { s ? sessionStorage.setItem(SESSION_KEY, JSON.stringify(s)) : sessionStorage.removeItem(SESSION_KEY); } catch (_) { /* private mode */ }
}

async function validToken() {
  if (!session) throw new Error("signed out");
  if (session.expires_at - 60 > Date.now() / 1000) return session.access_token;
  const r = await call("/auth/v1/token", { method: "POST", params: { grant_type: "refresh_token" }, body: { refresh_token: session.refresh_token } });
  if (!r.ok || !r.data || !r.data.access_token) { await signOut("Your session ended. Please sign in again."); throw new Error("session ended"); }
  keep(sessionFrom(r.data));
  return session.access_token;
}

async function rest(method, path, body, extra) {
  const token = await validToken();
  return call(`/rest/v1/${path}`, { method, body, token, extra });
}

/* ------------------------------------------------------------------ sign in / out, password */
async function signIn(event) {
  event.preventDefault();
  say("login-msg", "Signing in...", "");
  const r = await call("/auth/v1/token", { method: "POST", params: { grant_type: "password" },
    body: { email: $("login-email").value.trim(), password: $("login-password").value } });
  $("login-password").value = "";
  if (!r.ok || !r.data || !r.data.access_token) {
    say("login-msg", r.status === 0 ? "Could not reach the server." : "Wrong email or password.", "error");
    return;
  }
  keep(sessionFrom(r.data));
  await enterApp();
}

async function forgot() {
  const email = $("login-email").value.trim();
  if (!email) { say("login-msg", "Type your email above first.", "error"); return; }
  await call("/auth/v1/recover", { method: "POST", body: { email }, params: { redirect_to: location.origin + location.pathname } });
  say("login-msg", "If that address has an account, an email with a link to choose a new password is on its way.", "ok");
}

async function setPassword(event) {
  event.preventDefault();
  const a = $("new-password").value, b = $("new-password2").value;
  if (a !== b) { say("password-msg", "The two passwords are not the same.", "error"); return; }
  const r = await call("/auth/v1/user", { method: "PUT", token: await validToken(), body: { password: a } });
  if (!r.ok) { say("password-msg", (r.data && (r.data.msg || r.data.message)) || "The password was not accepted (at least 12 characters).", "error"); return; }
  $("new-password").value = ""; $("new-password2").value = "";
  say("password-msg", "Password saved.", "ok");
  await enterApp();
}

async function signOut(message) {
  if (session) { try { await call("/auth/v1/logout", { method: "POST", token: session.access_token }); } catch (_) { /* best effort */ } }
  keep(null); role = null; latest = null;
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  show("view-login");
  say("login-msg", message || "", message ? "error" : "");
}

/* ------------------------------------------------------------------ entering the panel */
async function enterApp() {
  const r = await rest("GET", `profiles?select=role&user_id=eq.${encodeURIComponent(session.user.id)}`);
  role = r.ok && Array.isArray(r.data) && r.data[0] ? r.data[0].role : null;
  if (role !== "admin" && role !== "streamer") { await signOut("This account has no access to the panel."); return; }
  $("who").textContent = `${session.user.email} (${role})`;
  for (const card of document.querySelectorAll("[data-roles]")) card.hidden = !card.dataset.roles.split(" ").includes(role);
  show("view-app");
  poll();
}

async function poll() {
  if (!session) return;
  try {
    const r = await rest("GET", "bot_status?select=data,updated_at&id=eq.1");
    if (r.ok && Array.isArray(r.data) && r.data[0]) latest = r.data[0];
    render();
  } catch (_) { /* signed out meanwhile */ }
  if (session) pollTimer = setTimeout(poll, POLL_MS);
}

/* ------------------------------------------------------------------ commands */
async function send(kind, payload) {
  const r = await rest("POST", "bot_commands", { kind, payload: payload || {} }, { Prefer: "return=representation" });
  let entry;
  if (!r.ok || !Array.isArray(r.data) || !r.data[0]) {
    entry = { time: new Date(), kind, ok: false, text: r.status === 403 ? "Your role may not do that." : "Could not send the command." };
  } else {
    const id = r.data[0].id;
    entry = { time: new Date(), kind, ok: false, text: "The bot did not answer in time. Is it online?" };
    for (let i = 0; i < 20; i++) {
      await sleep(1000);
      const s = await rest("GET", `bot_commands?select=status,result&id=eq.${id}`);
      const row = s.ok && Array.isArray(s.data) ? s.data[0] : null;
      if (row && row.status !== "pending" && row.status !== "running") {
        const err = row.result && row.result.error ? String(row.result.error).slice(0, 200) : "";
        entry = { time: new Date(), kind, ok: row.status === "done", text: row.status === "done" ? "Done." : (err || row.status) };
        break;
      }
    }
  }
  commandLog.unshift(entry);
  commandLog.length = Math.min(commandLog.length, 8);
  renderLog();
  render();
  return entry;
}

/* ------------------------------------------------------------------ rendering */
function render() {
  const st = latest && latest.data ? latest.data : null;
  const ageS = latest ? (Date.now() - Date.parse(latest.updated_at)) / 1000 : Infinity;
  const online = ageS < ONLINE_WITHIN_S;
  const pill = $("pill-bot");
  pill.className = `pill ${online ? "ok" : "bad"}`;
  pill.textContent = latest ? (online ? "Bot: online" : `Bot: offline (seen ${ago(ageS)} ago)`) : "Bot: no data yet";
  renderStatus(st, online);
  if (role === "admin") { renderControls(st); renderApprovals(st); renderStreams(st); renderTopic(st); }
  renderFeed(st);
  renderTalk(st, online);
}

function renderStatus(st, online) {
  const body = $("status-body");
  if (!st) { body.replaceChildren(el("p", { class: "hint", text: "Waiting for the bot to publish its status." })); return; }
  const kick = (st.status && st.status.adapters && st.status.adapters.kick) || null;
  const yt = (st.status && st.status.adapters && st.status.adapters.youtube) || null;
  const ai = (st.status && st.status.ai) || {};
  const c = (st.status && st.status.controls) || {};
  const rows = [
    ["Replies", c.outgoing_paused ? "PAUSED" : c.approval_required ? "on, waiting for approval" : "on, automatic"],
    ["Kick", kick ? `${kick.state}${kick.channel ? ` in ${kick.channel}` : ""}${kick.is_live ? " (live)" : ""}` : "not enabled"],
    ["YouTube", yt ? yt.state : "not enabled"],
    ["AI", `${ai.active_model || "?"}${ai.primary_degraded ? " (backup model)" : ""}`],
    ["Stream", (st.topic && st.topic.game) || "?"],
    ["Queue", String((st.status && st.status.queue_size) ?? "?")],
  ];
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of rows) dl.append(el("dt", { text: k }), el("dd", { text: v }));
  body.replaceChildren(dl);
}

function renderControls(st) {
  const c = (st && st.status && st.status.controls) || {};
  const set = (id, value) => { if (!dirty.has(id)) $(id).checked = Boolean(value); };
  set("chk-approval", c.approval_required); set("chk-ai", c.ai_enabled); set("chk-proactive", c.proactive_enabled);
}

function renderApprovals(st) {
  const body = $("approvals-body");
  const items = (st && st.approvals && st.approvals.items) || [];
  if (body.contains(document.activeElement) && document.activeElement.tagName === "TEXTAREA") return; // someone is editing a reply
  if (!items.length) { body.replaceChildren(el("p", { class: "hint", text: st && st.approvals && st.approvals.enabled ? "Nothing is waiting." : "Approval is off, so replies are sent automatically." })); return; }
  body.replaceChildren(...items.map((it) => {
    const text = el("textarea", { rows: "2", maxlength: "500", "aria-label": "Reply text" });
    text.value = it.text || "";
    const approve = el("button", { type: "button", text: "Approve and send" });
    approve.addEventListener("click", async () => {
      approve.disabled = true;
      const payload = { id: it.id };
      if (text.value.trim() && text.value !== it.text) payload.text = text.value;
      await send("approve", payload);
    });
    const reject = el("button", { type: "button", class: "quiet", text: "Reject" });
    reject.addEventListener("click", async () => { reject.disabled = true; await send("reject", { id: it.id }); });
    return el("div", { class: "item" }, [
      el("div", { class: "meta", text: `${it.platform} · ${it.viewer}: ${it.viewer_message} · expires in ${Math.round(it.expires_in_s)}s` }),
      el("div", { class: "reply" }, [text]), el("div", { class: "actions" }, [approve, reject]),
    ]);
  }));
}

function renderFeed(st) {
  const feed = (st && st.feed) || [];
  const body = $("feed-body");
  if (!feed.length) { body.replaceChildren(el("p", { class: "hint", text: "No chat yet." })); return; }
  body.replaceChildren(...feed.map((m) => {
    const line = el("div", { class: `line${m.bot ? " bot" : ""}` }, [
      el("span", { class: "t", text: clock(m.time) }), el("b", { text: `${m.user}: ` }), document.createTextNode(m.text || ""),
    ]);
    if (m.reply) line.append(el("div", { class: "meta", text: `reply (${m.delivery || "?"}): ${m.reply}` }));
    return line;
  }));
}

function renderStreams(st) {
  const body = $("streams-body");
  const targets = (st && st.streams && st.streams.targets) || [];
  if (body.contains(document.activeElement)) return;
  if (!targets.length) { body.replaceChildren(el("p", { class: "hint", text: "No streams on the list." })); return; }
  body.replaceChildren(...targets.map((t) => {
    const dot = el("span", { class: `dot ${t.live === true ? "live" : t.live === false ? "offline" : ""}`, title: t.live === true ? "live" : t.live === false ? "offline" : "unknown" });
    const name = el("div", {}, [dot, el("b", { text: t.label || t.id }), document.createTextNode(`${t.active ? "  · bot is here" : ""}${t.selected ? "  · selected" : ""}`)]);
    const choose = el("button", { type: "button", text: t.selected ? "Unselect" : "Use this stream" });
    choose.disabled = !t.enabled;
    choose.addEventListener("click", async () => { choose.disabled = true; await send("select_stream", { target_id: t.selected ? null : t.id }); });
    const toggle = el("button", { type: "button", class: "quiet", text: t.enabled ? "Switch off" : "Switch on" });
    toggle.addEventListener("click", async () => { toggle.disabled = true; await send("update_stream", { id: t.id, enabled: !t.enabled }); });
    const actions = el("div", { class: "actions" }, [choose, toggle]);
    if (t.kind !== "own") {
      const remove = el("button", { type: "button", class: "quiet", text: "Remove" });
      remove.addEventListener("click", async () => { remove.disabled = true; await send("remove_stream", { id: t.id }); });
      actions.append(remove);
    }
    return el("div", { class: `item${t.active ? " active" : ""}` }, [name, el("div", { class: "meta", text: `${t.platform} · ${t.kind}${t.title ? ` · ${t.title}` : ""}` }), actions]);
  }));
}

function renderTopic(st) {
  const t = (st && st.topic) || null;
  if (!t) return;
  const fill = (id, value) => { if (!dirty.has(id) && document.activeElement !== $(id)) $(id).value = value || ""; };
  fill("topic-game", t.game); fill("topic-hint", t.hint); fill("topic-notes", t.notes);
  const status = t.error ? `Lookup problem: ${t.error}` : t.notes ? (t.source === "manual" ? "The bot uses your own notes." : `Matched Wikipedia page: ${t.title}. Is that the right one?`) : "The bot has no background note yet.";
  if (!$("topic-msg").dataset.busy) say("topic-msg", status, t.error ? "error" : "");
}

function renderLog() {
  const body = $("log-body");
  if (!commandLog.length) { body.replaceChildren(el("p", { class: "hint", text: "Nothing sent yet." })); return; }
  body.replaceChildren(...commandLog.map((e) => el("div", { class: "item" }, [
    el("div", { class: "meta", text: `${e.time.toLocaleTimeString()} · ${e.kind}` }), el("div", { class: `hint ${e.ok ? "ok" : "error"}`, text: e.text }),
  ])));
}

/* ------------------------------------------------------------------ talk (hold to speak) */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, holding = false, heard = "";

function sayEnabled() { return Boolean(latest && latest.data && latest.data.relay && latest.data.relay.say_enabled); }

function renderTalk(st, online) {
  const enabled = sayEnabled() && online;
  $("btn-send-say").disabled = !enabled || !$("talk-text").value.trim();
  $("btn-hold").disabled = !enabled || !SR;
  let note = "";
  if (!sayEnabled()) note = "Voice replies are not switched on yet: the bot would refuse them. You can already test the microphone, but Send is off.";
  else if (!online) note = "The bot is offline.";
  else if (!SR) note = "This browser has no speech recognition: type the message instead (Chrome or Edge work).";
  else note = "Hold the button while you speak, let go when you are done, check the text, then Send. The browser sends your audio to its vendor's speech service.";
  $("talk-note").textContent = note;
  if (!sayEnabled() && SR) $("btn-hold").disabled = false; // allow a microphone test
}

function startHold() {
  if (!SR || holding) return;
  holding = true; heard = "";
  $("btn-hold").classList.add("live");
  rec = new SR();
  rec.lang = $("talk-lang").value; rec.interimResults = true; rec.continuous = true;
  rec.onresult = (e) => {
    let final = "", interim = "";
    for (let i = 0; i < e.results.length; i++) (e.results[i].isFinal ? (final += e.results[i][0].transcript) : (interim += e.results[i][0].transcript));
    heard = final;
    $("talk-text").value = (final + interim).trim().slice(0, 500);
    renderTalk(null, true);
  };
  rec.onerror = (e) => say("talk-msg", e.error === "not-allowed" ? "The microphone is blocked: allow it for this site in the browser." : `Speech recognition problem: ${e.error}`, "error");
  rec.onend = async () => {
    $("btn-hold").classList.remove("live");
    holding = false;
    if ($("chk-autosend").checked && $("talk-text").value.trim() && sayEnabled()) await sendSay();
  };
  try { rec.start(); } catch (_) { holding = false; $("btn-hold").classList.remove("live"); }
}
function endHold() { if (holding && rec) { try { rec.stop(); } catch (_) { /* already stopped */ } } }

async function sendSay() {
  const text = $("talk-text").value.trim();
  if (!text) return;
  say("talk-msg", "Sending...", "");
  const entry = await send("say", { text, lang: $("talk-lang").value });
  say("talk-msg", entry.ok ? "Sent." : entry.text, entry.ok ? "ok" : "error");
  if (entry.ok) $("talk-text").value = "";
  renderTalk(null, true);
}

/* ------------------------------------------------------------------ forms and buttons */
function wire() {
  $("form-login").addEventListener("submit", signIn);
  $("btn-forgot").addEventListener("click", forgot);
  $("form-password").addEventListener("submit", setPassword);
  $("btn-signout").addEventListener("click", () => signOut());
  $("btn-pause").addEventListener("click", () => send("pause"));
  $("btn-resume").addEventListener("click", () => send("resume"));
  for (const [id, kind] of [["chk-approval", "set_approval"], ["chk-ai", "set_ai"], ["chk-proactive", "set_proactive"]]) {
    $(id).addEventListener("change", async () => {
      dirty.add(id);
      const entry = await send(kind, { enabled: $(id).checked });
      dirty.delete(id);
      say("controls-msg", entry.ok ? "Done." : entry.text, entry.ok ? "ok" : "error");
      render();
    });
  }
  $("form-add-stream").addEventListener("submit", async (event) => {
    event.preventDefault();
    const entry = await send("add_stream", { platform: "youtube", ref: $("stream-ref").value });
    say("streams-msg", entry.ok ? "Added." : entry.text, entry.ok ? "ok" : "error");
    if (entry.ok) $("stream-ref").value = "";
  });
  $("form-topic").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("topic-msg").dataset.busy = "1"; say("topic-msg", "Looking it up...", "");
    const entry = await send("set_topic", { game: $("topic-game").value, hint: $("topic-hint").value, lookup: true });
    for (const id of ["topic-game", "topic-hint", "topic-notes"]) dirty.delete(id);
    delete $("topic-msg").dataset.busy;
    say("topic-msg", entry.ok ? "Saved." : entry.text, entry.ok ? "ok" : "error");
  });
  $("btn-topic-notes").addEventListener("click", async () => {
    const entry = await send("set_topic", { notes: $("topic-notes").value });
    dirty.delete("topic-notes");
    say("topic-msg", entry.ok ? "Saved your notes." : entry.text, entry.ok ? "ok" : "error");
  });
  for (const id of ["topic-game", "topic-hint", "topic-notes"]) $(id).addEventListener("input", () => dirty.add(id));
  const hold = $("btn-hold");
  hold.addEventListener("pointerdown", (e) => { e.preventDefault(); hold.setPointerCapture(e.pointerId); startHold(); });
  for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) hold.addEventListener(ev, endHold);
  hold.addEventListener("keydown", (e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); startHold(); } });
  hold.addEventListener("keyup", (e) => { if (e.key === " " || e.key === "Enter") endHold(); });
  window.addEventListener("blur", endHold);
  $("btn-send-say").addEventListener("click", sendSay);
  $("btn-clear-say").addEventListener("click", () => { $("talk-text").value = ""; renderTalk(null, true); });
  $("talk-text").addEventListener("input", () => renderTalk(null, true));
}

/* ------------------------------------------------------------------ start */
async function boot() {
  wire();
  renderLog();
  if (!BASE || !CFG.key || CFG.key.includes("GOES_HERE")) { show("view-login"); say("login-msg", "This page is not configured yet (config.js).", "error"); return; }
  const hash = new URLSearchParams(location.hash.slice(1));
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);  // the link's tokens must not stay in the address bar
  if (hash.get("error_description")) { show("view-login"); say("login-msg", hash.get("error_description").replace(/\+/g, " "), "error"); return; }
  if (hash.get("access_token")) {  // an invite or password-reset link from the email
    const data = { access_token: hash.get("access_token"), refresh_token: hash.get("refresh_token"), expires_in: hash.get("expires_in") };
    const me = await call("/auth/v1/user", { token: data.access_token });
    if (me.ok && me.data) {
      keep(sessionFrom(data, { id: me.data.id, email: me.data.email }));
      if (["recovery", "invite", "signup"].includes(hash.get("type"))) { show("view-password"); return; }
      await enterApp(); return;
    }
  }
  try { session = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null"); } catch (_) { session = null; }
  if (session) { try { await enterApp(); return; } catch (_) { /* fall through to the sign-in page */ } }
  show("view-login");
}

// A link from an email opened in a tab that already shows the panel only changes the hash: start over with a full load.
window.addEventListener("hashchange", () => { if (location.hash.length > 1) location.reload(); });
boot();

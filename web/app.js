"use strict";
/* The online panel. No libraries: it talks to Supabase's documented REST endpoints (Auth + PostgREST) with fetch.
 * Three pages share this file (document.body.dataset.page): "login" (/), "admin" (/admin) and "streamer" (/streamer).
 * After signing in you are sent to the page for your role; opening the other role's page sends you back. That routing is a
 * convenience only: what a user may do is decided by the database policies (supabase/schema.sql), not by this file.
 * Rules this file follows: text from the database goes on the page with textContent only (never as markup); the only secrets
 * it holds are the signed-in user's own tokens, kept in sessionStorage (this tab only); the key in config.js is the PUBLIC key. */

const CFG = window.STEALTHBOT_CONFIG || {};
const BASE = String(CFG.url || "").replace(/\/$/, "");
const PAGE = document.body.dataset.page || "login";
const SESSION_KEY = "stealthbot.session";
const FLASH_KEY = "stealthbot.flash";
const POLL_MS = 3000;
const ONLINE_WITHIN_S = 20;
// A page only contains the parts of its own role; code for a part that is not on this page finds a harmless detached element.
const $ = (id) => document.getElementById(id) || document.createElement("div");

let session = null;   // { access_token, refresh_token, expires_at (unix s), user: { id, email } }
let role = null;      // "admin" | "streamer"
let latest = null;    // { data, updated_at } from bot_status
let pollTimer = null;
const commandLog = [];
const conversation = []; // the streamer's page: what was said and what the bot answered
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
const pageFor = (r) => (r === "admin" ? "/admin" : "/streamer");
function go(path) { location.replace(path); }
function showLoginView(view) {
  for (const id of ["view-login", "view-password"]) { const n = document.getElementById(id); if (n) n.hidden = id !== view; }
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

/* ------------------------------------------------------------------ sign in / out, password, routing by role */
async function loadRole() {
  const r = await rest("GET", `profiles?select=role&user_id=eq.${encodeURIComponent(session.user.id)}`);
  const found = r.ok && Array.isArray(r.data) && r.data[0] ? r.data[0].role : null;
  return found === "admin" || found === "streamer" ? found : null;
}

async function routeByRole() {
  role = await loadRole();
  if (!role) { await signOut("This account has no access to the panel."); return; }
  go(pageFor(role));
}

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
  await routeByRole();
}

async function forgot() {
  const email = $("login-email").value.trim();
  if (!email) { say("login-msg", "Type your email above first.", "error"); return; }
  await call("/auth/v1/recover", { method: "POST", body: { email }, params: { redirect_to: location.origin + "/" } });
  say("login-msg", "If that address has an account, an email with a link to choose a new password is on its way.", "ok");
}

async function setPassword(event) {
  event.preventDefault();
  const a = $("new-password").value, b = $("new-password2").value;
  if (a !== b) { say("password-msg", "The two passwords are not the same.", "error"); return; }
  const r = await call("/auth/v1/user", { method: "PUT", token: await validToken(), body: { password: a } });
  if (!r.ok) { say("password-msg", (r.data && (r.data.msg || r.data.message)) || "The password was not accepted.", "error"); return; }
  $("new-password").value = ""; $("new-password2").value = "";
  say("password-msg", "Password saved.", "ok");
  await routeByRole();
}

async function signOut(message) {
  if (session) { try { await call("/auth/v1/logout", { method: "POST", token: session.access_token }); } catch (_) { /* best effort */ } }
  keep(null); role = null; latest = null;
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  if (PAGE !== "login") {
    try { if (message) sessionStorage.setItem(FLASH_KEY, message); } catch (_) { /* fine */ }
    go("/");
    return;
  }
  showLoginView("view-login");
  say("login-msg", message || "", message ? "error" : "");
}

/* ------------------------------------------------------------------ the admin and streamer pages */
function startPage() {
  $("who").textContent = `${session.user.email} (${role})`;
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
        entry = { time: new Date(), kind, ok: row.status === "done", text: row.status === "done" ? "Done." : (err || row.status), result: row.result };
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
  if (PAGE === "admin") { renderNow(st); renderControls(st); renderApprovals(st); renderStreams(st); renderTopic(st); renderPeople(st); }
  renderFeed(st);
  renderTalk(st, online);
}

// Rebuilding a list every 3 seconds would swallow a click that is in progress, so a list is rebuilt only when what it shows changed.
const shown = {};
function changed(key, value) {
  const sig = JSON.stringify(value);
  if (shown[key] === sig) return false;
  shown[key] = sig;
  return true;
}
const streamsOf = (st) => (st && st.streams) || {};
const topicOf = (st) => (st && st.topic) || {};
const focusOf = (st) => streamsOf(st).focus || null;
function languageLabel(st) {
  const l = st && st.language;
  if (!l) return "?";
  const o = (l.options || []).find((x) => x.id === l.language);
  return o ? o.label : String(l.language);
}
function liveWord(t) { return t.live === true ? "LIVE" : t.live === false ? "offline" : "not checked"; }
function liveClass(t) { return t.live === true ? "live" : t.live === false ? "offline" : ""; }

function renderStatus(st, online) {
  const body = $("status-body");
  if (!st) { body.replaceChildren(el("p", { class: "hint", text: "Waiting for the bot to publish its status." })); return; }
  const kick = (st.status && st.status.adapters && st.status.adapters.kick) || null;
  const yt = (st.status && st.status.adapters && st.status.adapters.youtube) || null;
  const ai = (st.status && st.status.ai) || {};
  const c = (st.status && st.status.controls) || {};
  const focus = focusOf(st);
  const rows = [
    ["Replies", c.outgoing_paused ? "PAUSED" : c.approval_required ? "on, waiting for approval" : "on, automatic"],
    ["Stream", focus ? focus.label : "none yet"],
    ["Topic", topicOf(st).game || "not set yet"],
    ["Language", languageLabel(st)],
    ["Kick", kick ? `${kick.state}${kick.channel ? ` in ${kick.channel}` : ""}${kick.is_live ? " (live)" : ""}` : "not enabled"],
    ["YouTube", yt ? yt.state : "not enabled"],
    ["AI", `${ai.active_model || "?"}${ai.primary_degraded ? " (backup model)" : ""}`],
    ["Queue", String((st.status && st.status.queue_size) ?? "?")],
  ];
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of rows) dl.append(el("dt", { text: k }), el("dd", { text: v }));
  body.replaceChildren(dl);
}

/* The header of the admin page: where the bot is, what it believes the stream is about, and the language. */
function renderNow(st) {
  const box = $("now-stream");
  if (!st) { box.textContent = "Waiting for the bot..."; $("now-topic").textContent = ""; return; }
  const focus = focusOf(st);
  const t = focus ? (streamsOf(st).targets || []).find((x) => x.id === focus.id) : null;
  if (focus && t) box.replaceChildren(el("span", { class: `dot ${liveClass(t)}` }), document.createTextNode(focus.label + " "), el("span", { class: `tag ${liveClass(t)}`, text: liveWord(t) }));
  else {
    const now = streamsOf(st).now || {};
    const why = Object.values(now).map((d) => d.reason).filter(Boolean)[0];
    box.textContent = why ? `Not in a stream: ${why}` : "Not in a stream yet";
  }
  const topic = topicOf(st);
  const bits = [topic.is_set ? `Topic: ${topic.game}` : "Topic: not set yet. The bot will not guess what this stream is about."];
  if ((topic.headlines || []).length) bits.push(`${topic.headlines.length} recent headlines`);
  if (topic.notes) bits.push(topic.source === "manual" ? "your own note" : "background from Wikipedia");
  $("now-topic").textContent = bits.join("  ·  ");
  const sel = $("sel-language");
  const options = (st.language && st.language.options) || [];
  if (changed("language-options", options)) sel.replaceChildren(...options.map((o) => el("option", { value: o.id, text: o.label })));
  if (st.language && document.activeElement !== sel) sel.value = st.language.language;
}

function renderPeople(st) {
  const people = (st && st.personalities) || [];
  if (!changed("people", people)) return;
  const body = $("people-body");
  if (!people.length) { body.replaceChildren(el("p", { class: "hint", text: "Waiting for the bot." })); return; }
  body.replaceChildren(...people.map((p) => el("div", { class: "item" }, [
    el("b", { text: p.name }), document.createTextNode(" "), el("span", { class: `tag ${p.enabled ? "live" : ""}`, text: p.enabled ? "on" : "off" }),
    el("div", { class: "meta", text: p.description || "" }),
  ])));
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
    if (m.reply) line.append(el("div", { class: "meta", text: `reply${m.personality ? ` by ${m.personality}` : ""} (${m.delivery || "?"}): ${m.reply}` }));
    return line;
  }));
}

/* One row per stream, plus "Automatic". A click on "Use this stream" is all it takes; the bot's topic follows the stream. */
async function chooseStream(targetId, name) {
  say("streams-msg", `Switching to ${name}...`, "");
  const entry = await send("select_stream", { target_id: targetId });
  say("streams-msg", entry.ok ? (targetId ? `Selected ${name}. The bot moves there as soon as it sees it live.` : "Automatic: the bot follows whichever listed stream is live.") : entry.text, entry.ok ? "ok" : "error");
}

function renderStreams(st) {
  const s = streamsOf(st);
  const targets = s.targets || [];
  const topics = topicOf(st).streams || {};
  const focus = focusOf(st);
  const view = { targets, selected: s.selected, now: s.now, game: topicOf(st).game, topics, focus: focus && focus.id };
  if (!changed("streams", view)) return;
  const body = $("streams-body");
  if (!targets.length) { body.replaceChildren(el("p", { class: "hint", text: "No streams on the list yet. Paste a link below." })); return; }

  const auto = el("div", { class: `choice${s.selected ? "" : " selected"}` }, [
    el("div", { class: "choice-main" }, [el("b", { text: "Automatic" }),
      el("div", { class: "meta", text: "The bot follows whichever stream on the list is live (the fail-safe)." })]),
    el("div", { class: "actions" }, [(() => {
      const b = el("button", { type: "button", text: s.selected ? "Use automatic" : "Selected" });
      b.disabled = !s.selected;
      b.addEventListener("click", () => chooseStream(null, "automatic"));
      return b;
    })()]),
  ]);

  const rows = targets.map((t) => {
    const game = focus && focus.id === t.id ? topicOf(st).game : topics[t.id];
    const state = t.active ? "The bot is here" : t.selected ? "Selected: waiting for it to go live" : !t.enabled ? "Switched off" : "";
    const main = el("div", { class: "choice-main" }, [
      el("div", {}, [el("span", { class: `dot ${liveClass(t)}` }), el("b", { text: t.label || t.id }), document.createTextNode(" "),
        el("span", { class: `tag ${liveClass(t)}`, text: t.enabled ? liveWord(t) : "off" }), document.createTextNode(" "),
        el("span", { class: "tag", text: t.platform })]),
      el("div", { class: "meta", text: [game ? `Topic: ${game}` : "Topic: not set yet", t.title ? `Title: ${t.title}` : "", state, t.error ? `Problem: ${t.error}` : ""].filter(Boolean).join("  ·  ") }),
    ]);
    const use = el("button", { type: "button", text: t.selected ? "Selected" : "Use this stream" });
    use.disabled = !t.enabled || t.selected;
    use.addEventListener("click", () => chooseStream(t.id, t.label || t.id));
    const edit = el("button", { type: "button", class: "quiet", text: "Edit topic" });
    edit.addEventListener("click", () => { topicTarget = t.id; delete shown["topic-select"]; dirty.clear(); render(); $("card-topic").scrollIntoView({ behavior: "smooth", block: "start" }); });
    const toggle = el("button", { type: "button", class: "quiet", text: t.enabled ? "Switch off" : "Switch on" });
    toggle.addEventListener("click", async () => { toggle.disabled = true; await send("update_stream", { id: t.id, enabled: !t.enabled }); });
    const actions = el("div", { class: "actions" }, [use, edit, toggle]);
    if (t.kind !== "own") {
      const remove = el("button", { type: "button", class: "quiet", text: "Remove" });
      remove.addEventListener("click", async () => { remove.disabled = true; await send("remove_stream", { id: t.id }); });
      actions.append(remove);
    }
    return el("div", { class: `choice${t.selected ? " selected" : ""}${t.active ? " here" : ""}${t.enabled ? "" : " off"}` }, [main, actions]);
  });
  const why = Object.entries(s.now || {}).map(([p, d]) => `${p}: ${d.reason}`).join("  ·  ");
  body.replaceChildren(auto, ...rows, ...(why ? [el("p", { class: "hint", text: why })] : []));
}

/* The topic form edits ONE stream: the one the bot is in, or another one picked in the list (saved now, looked up when the bot gets there). */
let topicTarget = null;

function topicStream(st) {
  const targets = streamsOf(st).targets || [];
  const focus = focusOf(st);
  const wanted = topicTarget && targets.some((t) => t.id === topicTarget) ? topicTarget : null;
  return { id: wanted || (focus ? focus.id : (targets[0] ? targets[0].id : null)), focusId: focus ? focus.id : null, targets };
}

function renderTopic(st) {
  const t = topicOf(st);
  if (!st) return;
  const { id, focusId, targets } = topicStream(st);
  const options = targets.map((x) => [x.id, `${x.label}${x.id === focusId ? "  (the bot is here)" : ""}`]);
  const sel = $("sel-topic-stream");
  if (changed("topic-select", options)) sel.replaceChildren(...options.map(([v, label]) => el("option", { value: v, text: label })));
  if (id && document.activeElement !== sel) sel.value = id;
  const here = id !== null && id === focusId;           // the stream the bot is in: everything can be edited and looked up
  const game = here ? t.game : ((t.streams || {})[id] || "");
  const fill = (key, value) => { const f = $(key); if (!dirty.has(key) && document.activeElement !== f) f.value = value || ""; };
  fill("topic-game", game); fill("topic-hint", here ? t.hint : ""); fill("topic-notes", here ? t.notes : "");
  $("topic-know").hidden = !here;
  $("btn-news").disabled = !here || !(t.sources || []).some((s) => s !== "wikipedia");
  const state = $("topic-state");
  if (!id) state.textContent = "Add a stream first.";
  else if (!here) state.textContent = "The bot is not in this stream right now: what you save is kept and looked up on the internet when the bot moves there.";
  else if (!t.is_set) state.textContent = "This stream has no topic yet. Type what it is about below: the bot will not guess.";
  else state.textContent = t.error ? `Lookup problem: ${t.error}` : "";
  state.className = `hint${t.error && here ? " error" : ""}`;
  if (!here) return;
  const age = (seconds) => (seconds === null || seconds === undefined ? "not yet" : `${ago(seconds)} ago`);
  const chips = [];
  const wiki = (t.sources || []).includes("wikipedia");
  chips.push(el("span", { class: `chip ${t.notes ? "on" : ""}`, text: !wiki ? "Wikipedia: off" : t.source === "manual" ? "Your own note" : t.notes ? `Wikipedia: ${t.title}` : "Wikipedia: nothing yet" }));
  const news = (t.sources || []).filter((s) => s !== "wikipedia");
  chips.push(el("span", { class: `chip ${(t.headlines || []).length ? "on" : ""}`, text: !news.length ? "News: off (set STEALTHBOT_TOPIC_SOURCES)" : (t.headlines || []).length ? `News: ${t.news_source || news.join("+")} (${age(t.news_age_s)})` : `News: nothing yet${t.news_error ? ` (${t.news_error})` : ""}` }));
  if (changed("topic-chips", [t.title, t.notes, t.source, t.sources, t.news_source, t.headlines, t.news_error, Math.floor((t.news_age_s || 0) / 60)])) {
    $("topic-sources").replaceChildren(...chips);
    const list = $("topic-headlines");
    list.replaceChildren(...((t.headlines || []).length ? [el("p", { class: "hint", text: "Recent headlines the bot can use (unconfirmed news):" }), el("ul", { class: "plain" }, t.headlines.map((h) => el("li", { text: h })))] : []));
  }
  // The refresh must not wipe a message about something the user just did.
  if (!$("topic-msg").dataset.busy && Date.now() > Number($("topic-msg").dataset.hold || 0)) {
    say("topic-msg", t.notes && t.source !== "manual" ? "Check the Wikipedia match above: if it is the wrong page, add search words and save again." : "", "");
  }
}

function sayAndHold(id, text, level, ms = 8000) {
  say(id, text, level);
  $(id).dataset.hold = String(Date.now() + ms);
}

function renderLog() {
  const body = $("log-body");
  if (!commandLog.length) { body.replaceChildren(el("p", { class: "hint", text: "Nothing sent yet." })); return; }
  body.replaceChildren(...commandLog.map((e) => el("div", { class: "item" }, [
    el("div", { class: "meta", text: `${e.time.toLocaleTimeString()} · ${e.kind}` }), el("div", { class: `hint ${e.ok ? "ok" : "error"}`, text: e.text }),
  ])));
}

function renderConversation() {
  const body = $("convo-body");
  if (!conversation.length) { body.replaceChildren(el("p", { class: "hint", text: "Nothing said yet. Hold the button and speak." })); return; }
  body.replaceChildren(...conversation.map((c) => el("div", { class: "item" }, [
    el("div", { class: "meta", text: c.time.toLocaleTimeString() }),
    el("div", {}, [el("b", { text: "You: " }), document.createTextNode(c.said)]),
    el("div", { class: `reply ${c.ok ? "" : "hint error"}` }, [el("b", { text: "Bot: " }), document.createTextNode(c.answer)]),
  ])));
}

/* ------------------------------------------------------------------ talk (hold to speak) */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, holding = false;

function sayEnabled() { return Boolean(latest && latest.data && latest.data.relay && latest.data.relay.say_enabled); }

function renderTalk(st, online) {
  const enabled = sayEnabled() && online;
  $("btn-send-say").disabled = !enabled || !$("talk-text").value.trim();
  $("btn-hold").disabled = !enabled || !SR;
  let note = "";
  if (!sayEnabled()) note = "Voice replies are not switched on yet: the bot would refuse them. You can already test the microphone, but Send is off.";
  else if (!online) note = "The bot is offline.";
  else if (!SR) note = "This browser has no speech recognition: type the message instead (Chrome or Edge work).";
  else note = "Hold the button while you speak and let go when you are done. The bot answers in chat as if replying to what you said (your words are not posted). The browser sends your audio to its vendor's speech service.";
  $("talk-note").textContent = note;
  if (!sayEnabled() && SR) $("btn-hold").disabled = false; // allow a microphone test
}

function startHold() {
  if (!SR || holding) return;
  holding = true;
  $("btn-hold").classList.add("live");
  rec = new SR();
  rec.lang = $("talk-lang").value; rec.interimResults = true; rec.continuous = true;
  rec.onresult = (e) => {
    let final = "", interim = "";
    for (let i = 0; i < e.results.length; i++) (e.results[i].isFinal ? (final += e.results[i][0].transcript) : (interim += e.results[i][0].transcript));
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

/* What happened to something the streamer said: the bot answers it in chat as part of the conversation. */
function describeSay(result, fallback) {
  const r = result || {};
  const reply = r.reply ? `"${String(r.reply).slice(0, 160)}"` : "";
  switch (r.action) {
    case "replied": return `The bot answered in chat: ${reply}`;
    case "held": return `The bot wrote an answer and it is waiting for approval: ${reply}`;
    case "paused": return "Replies are paused, so the bot did not answer.";
    case "rate_limited": return "The bot is pacing itself right now. Try again in a moment.";
    case "ignored": return "The bot chose not to answer that one.";
    case "no_stream": return "No stream is connected, so the bot has nowhere to answer.";
    case "timeout": return "The bot took too long to answer.";
    case "no_reply": return "The bot had no answer for that.";
    default: return fallback;
  }
}

async function sendSay() {
  const text = $("talk-text").value.trim();
  if (!text) return;
  say("talk-msg", "Sending...", "");
  const entry = await send("say", { text, lang: $("talk-lang").value });
  const message = entry.ok ? describeSay(entry.result, "Sent.") : describeSay(entry.result, entry.text);
  say("talk-msg", message, entry.ok ? "ok" : "error");
  conversation.unshift({ time: new Date(), said: text, answer: message.replace(/^The bot answered in chat: /, ""), ok: entry.ok });
  conversation.length = Math.min(conversation.length, 10);
  renderConversation();
  if (entry.ok) $("talk-text").value = "";
  renderTalk(null, true);
}

/* ------------------------------------------------------------------ forms and buttons */
function on(id, event, handler) { document.getElementById(id)?.addEventListener(event, handler); }

function wire() {
  on("form-login", "submit", signIn);
  on("btn-forgot", "click", forgot);
  on("form-password", "submit", setPassword);
  on("btn-signout", "click", () => signOut());
  on("btn-pause", "click", () => send("pause"));
  on("btn-resume", "click", () => send("resume"));
  for (const [id, kind] of [["chk-approval", "set_approval"], ["chk-ai", "set_ai"], ["chk-proactive", "set_proactive"]]) {
    on(id, "change", async () => {
      dirty.add(id);
      const entry = await send(kind, { enabled: $(id).checked });
      dirty.delete(id);
      say("controls-msg", entry.ok ? "Done." : entry.text, entry.ok ? "ok" : "error");
      render();
    });
  }
  on("sel-language", "change", async () => {
    const entry = await send("set_language", { language: $("sel-language").value });
    say("controls-msg", entry.ok ? "Language changed: the next reply uses it." : entry.text, entry.ok ? "ok" : "error");
  });
  on("form-add-stream", "submit", async (event) => {
    event.preventDefault();
    const entry = await send("add_stream", { ref: $("stream-ref").value });  // the bot works out Kick or YouTube from the link
    say("streams-msg", entry.ok ? "Added. Click \"Use this stream\" to make the bot comment there." : entry.text, entry.ok ? "ok" : "error");
    if (entry.ok) $("stream-ref").value = "";
  });
  // The topic form edits one stream: the one the bot is in, or another one chosen in the list above the form.
  const editing = () => {
    const st = latest && latest.data;
    const s = st ? topicStream(st) : { id: null, focusId: null };
    return { id: s.id, here: s.id !== null && s.id === s.focusId };
  };
  on("sel-topic-stream", "change", () => { topicTarget = $("sel-topic-stream").value; dirty.clear(); render(); });
  on("form-topic", "submit", async (event) => {
    event.preventDefault();
    const { id, here } = editing();
    const payload = { game: $("topic-game").value, hint: $("topic-hint").value };
    if (here) payload.lookup = true; else payload.target_id = id;
    $("topic-msg").dataset.busy = "1"; sayAndHold("topic-msg", here ? "Looking it up on the internet..." : "Saving...", "", 60000);
    const entry = await send("set_topic", payload);
    for (const key of ["topic-game", "topic-hint", "topic-notes"]) dirty.delete(key);
    delete $("topic-msg").dataset.busy;
    sayAndHold("topic-msg", entry.ok ? (here ? "Saved and looked up." : "Saved. It is looked up when the bot moves to that stream.") : entry.text, entry.ok ? "ok" : "error");
  });
  on("btn-news", "click", async () => {
    $("topic-msg").dataset.busy = "1"; sayAndHold("topic-msg", "Looking for the latest headlines...", "", 60000);
    const entry = await send("set_topic", { news: true });
    delete $("topic-msg").dataset.busy;
    sayAndHold("topic-msg", entry.ok ? "Headlines refreshed." : entry.text, entry.ok ? "ok" : "error");
  });
  on("btn-topic-notes", "click", async () => {
    const { id, here } = editing();
    const payload = { notes: $("topic-notes").value };
    if (!here) payload.target_id = id;
    const entry = await send("set_topic", payload);
    dirty.delete("topic-notes");
    sayAndHold("topic-msg", entry.ok ? "Saved your note." : entry.text, entry.ok ? "ok" : "error");
  });
  for (const id of ["topic-game", "topic-hint", "topic-notes"]) on(id, "input", () => dirty.add(id));
  const hold = document.getElementById("btn-hold");
  if (hold) {
    hold.addEventListener("pointerdown", (e) => { e.preventDefault(); hold.setPointerCapture(e.pointerId); startHold(); });
    for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) hold.addEventListener(ev, endHold);
    hold.addEventListener("keydown", (e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); startHold(); } });
    hold.addEventListener("keyup", (e) => { if (e.key === " " || e.key === "Enter") endHold(); });
  }
  window.addEventListener("blur", endHold);
  on("btn-send-say", "click", sendSay);
  on("btn-clear-say", "click", () => { $("talk-text").value = ""; renderTalk(null, true); });
  on("talk-text", "input", () => renderTalk(null, true));
}

/* ------------------------------------------------------------------ start */
async function boot() {
  wire();
  if (PAGE !== "login") { renderLog(); renderConversation(); }
  const configured = BASE && CFG.key && !String(CFG.key).includes("GOES_HERE");
  if (PAGE === "login") {
    showLoginView("view-login");
    try { const flash = sessionStorage.getItem(FLASH_KEY); if (flash) { sessionStorage.removeItem(FLASH_KEY); say("login-msg", flash, "error"); } } catch (_) { /* fine */ }
    if (!configured) { say("login-msg", "This page is not configured yet (config.js).", "error"); return; }
    const hash = new URLSearchParams(location.hash.slice(1));
    if (location.hash) history.replaceState(null, "", location.pathname + location.search);  // the link's tokens must not stay in the address bar
    if (hash.get("error_description")) { say("login-msg", hash.get("error_description").replace(/\+/g, " "), "error"); return; }
    if (hash.get("access_token")) {  // an invite or password-reset link from the email
      const data = { access_token: hash.get("access_token"), refresh_token: hash.get("refresh_token"), expires_in: hash.get("expires_in") };
      const me = await call("/auth/v1/user", { token: data.access_token });
      if (me.ok && me.data) {
        keep(sessionFrom(data, { id: me.data.id, email: me.data.email }));
        if (["recovery", "invite", "signup"].includes(hash.get("type"))) { showLoginView("view-password"); return; }
        await routeByRole(); return;
      }
    }
  } else if (!configured) { go("/"); return; }

  try { session = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null"); } catch (_) { session = null; }
  if (!session) { if (PAGE !== "login") go("/"); return; }
  try { role = await loadRole(); } catch (_) { return; }  // validToken already sent the user back to sign in
  if (!role) { await signOut("This account has no access to the panel."); return; }
  if (PAGE === "login") { go(pageFor(role)); return; }
  if (PAGE === "admin" && role !== "admin") { go(pageFor(role)); return; }   // a streamer cannot use the admin page
  startPage();                                                            // an admin may also open the streamer's talk screen
}

// A link from an email opened in a tab that already shows a page only changes the hash: start over with a full load.
window.addEventListener("hashchange", () => { if (location.hash.length > 1) location.reload(); });
boot();

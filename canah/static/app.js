/* CANAH console: sign-in, then the topology, the controllers, the chat with its approvals, the audit,
   documents and memory. Plain JavaScript, no build step; it talks to the CANAH service only. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d = null) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private window */ } },
  del(k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } },
};
const fmtTime = (t) => new Date(t * 1000).toLocaleString();
const ago = (t) => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; };

const state = {
  token: store.get("canah.token", ""), user: "observer", level: "observer",
  info: null, network: null, topology: null, view: store.get("canah.view", "topology"),
  selected: null, chat: { thread: "", busy: false, bubble: null },
};

function toast(text, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`; t.textContent = text;
  $("#toasts").append(t); setTimeout(() => t.remove(), 5200);
}

async function api(path, opts = {}) {
  const headers = { ...(opts.body ? { "Content-Type": "application/json" } : {}), ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}) };
  const r = await fetch(path, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (r.status === 401 && path !== "/session" && state.token) { state.token = ""; state.level = "observer"; store.del("canah.token"); toast("Your session ended; sign in again.", "warn"); showLogin(); throw new Error("signed out"); }
  if (r.status === 401 && path !== "/session") throw new Error("signed out");
  if (!r.ok) { let m = `${r.status}`; try { m = (await r.json()).detail || m; } catch (e) { /* not json */ } throw new Error(m); }
  return opts.raw ? r : r.json();
}

/* ================= theme ================= */
function toggleTheme() {
  const light = document.documentElement.dataset.theme === "light";
  if (light) delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = "light";
  store.set("canah.theme", light ? "dark" : "light");
  if (state.topology) renderTopology();
}
$$("[data-act=theme]").forEach((b) => b.addEventListener("click", toggleTheme));

/* ================= sign-in ================= */
function showLogin(msg = "") {
  $("#shell").hidden = true; $("#login").hidden = false;
  $("#li-err").textContent = msg;
  setTimeout(() => $("#li-user").focus(), 50);
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#li-err").textContent = ""; $("#li-go").disabled = true;
  try {
    const r = await api("/session", { method: "POST", body: { username: $("#li-user").value.trim(), password: $("#li-pass").value } });
    state.token = r.token; store.set("canah.token", r.token); $("#li-pass").value = "";
    store.del("canah.observer");
    await enter();
  } catch (err) {
    $("#li-err").textContent = /401/.test(err.message) || /password|unknown|wrong/i.test(err.message) ? "That username and password do not match an account." : err.message;
  } finally { $("#li-go").disabled = false; }
});
$("#li-observer").addEventListener("click", async () => { store.set("canah.observer", true); state.token = ""; store.del("canah.token"); await enter(); });
async function signOut() {
  try { if (state.token) await api("/session", { method: "DELETE" }); } catch (e) { /* already gone */ }
  state.token = ""; store.del("canah.token"); store.del("canah.observer");
  state.chat.thread = ""; showLogin();
}
$("#signout").addEventListener("click", signOut);
$("#id-out").addEventListener("click", signOut);
$("#id-switch").addEventListener("click", () => { store.del("canah.observer"); showLogin(); });
$("#st-who").addEventListener("click", () => go("identity"));

async function boot() {
  if (state.token) {
    try { const s = await api("/session"); if (s.signed_in) return enter(s); } catch (e) { /* fall through */ }
    state.token = ""; store.del("canah.token");
  }
  if (store.get("canah.observer")) return enter();
  showLogin();
}

async function enter(session) {
  const s = session || await api("/session").catch(() => ({ user: "observer", level: "observer" }));
  state.user = s.user; state.level = s.level;
  $("#login").hidden = true; $("#shell").hidden = false;
  renderWho();
  loadThreads();
  go(state.view, true);
  refreshStatus();
  loadTopology();
  refreshTwinPill(); refreshIncidents();
}

/* ================= status strip ================= */
function renderWho() {
  const t = state.level === "observer" ? "observer (read only)" : `${state.user} · ${state.level}`;
  $("#st-who-text").textContent = t;
  $("#st-who .dot").className = `dot ${state.level === "root" ? "warn" : state.level === "admin" ? "ok" : ""}`;
  $("#signout").textContent = state.level === "observer" ? "Sign in" : "Sign out";
}
async function refreshStatus() {
  try { await api("/health"); $("#st-service .dot").className = "dot ok"; } catch (e) { $("#st-service .dot").className = "dot bad"; }
  try {
    state.info = await api("/info");
    const m = state.info.model?.model || "?";
    $("#st-model span:last-child").textContent = m.replace(/^ollama:/, "");
    $("#st-model .dot").className = "dot ok";
    if (state.topology) { renderKpis(); buildGraph(state.topology); if (state.view === "topology") renderTopology(); }
    $("#credit-info").textContent = `${state.info.tools} tools · ${(state.info.subagents || []).length} subagents · sandbox: ${(state.info.sandbox || "none").split(",")[0]}`;
  } catch (e) { $("#st-model .dot").className = "dot bad"; }
}
setInterval(() => { $("#st-clock").textContent = new Date().toLocaleTimeString(); }, 1000);
$("#st-ctl").addEventListener("click", () => go("controllers"));

/* ================= router ================= */
const VIEWS = ["incidents", "model", "topology", "controllers", "chat", "ops", "twins", "knowledge", "identity", "about"];
function go(view, first = false) {
  if (!VIEWS.includes(view)) view = "topology";
  state.view = view; store.set("canah.view", view);
  document.body.classList.remove("nav-open");
  $$(".nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "topology") { if (state.topology) { renderTopology(); if (first || !topo.fitted) fitTopology(false); } }
  if (view === "controllers") renderControllers();
  if (view === "chat") {
    // Coming back to the conversation on the page keeps it as it is: reloading it would cut off an answer still
    // streaming in, and the approval card it ends with.
    const want = state.chat.thread || (threads()[0] || {}).id || "";
    if (state.chat.busy || (state.chat.rendered === want && col().children.length)) loadThreads(); else openThread(want);
    setTimeout(() => $("#composer").focus(), 30);
  }
  if (view === "topology") pollActivity();
  if (view === "ops") renderOps();
  if (view === "twins") renderTwins();
  if (view === "knowledge") renderKnowledge();
  if (view === "identity") renderIdentity();
  if (view === "about") renderAbout();
  if (view === "model") renderModel();
  if (view === "incidents") renderIncidents();
}
$$(".nav button[data-view]").forEach((b) => b.addEventListener("click", () => go(b.dataset.view)));
const setPanel = (which, hidden) => { document.body.classList.toggle(`${which}-hidden`, hidden); store.set(`canah.${which}-hidden`, hidden); if (state.view === "topology") setTimeout(() => fitTopology(false), 30); };
const narrow = () => window.matchMedia("(max-width: 1100px)").matches;
$("#nav-toggle").addEventListener("click", () => {
  if (narrow()) { document.body.classList.toggle("nav-open"); return; }
  setPanel("nav", !document.body.classList.contains("nav-hidden"));
});
document.addEventListener("click", (e) => {     // a tap outside the overlay menu closes it
  if (document.body.classList.contains("nav-open") && !e.target.closest("#nav, #nav-toggle")) document.body.classList.remove("nav-open");
});
$("#side-toggle").addEventListener("click", () => {
  if (narrow() && state.view === "chat") { $("#lg").classList.toggle("flow-open"); return; }
  setPanel("side", !document.body.classList.contains("side-hidden"));
});
if (store.get("canah.nav-hidden")) document.body.classList.add("nav-hidden");
if (store.get("canah.side-hidden")) document.body.classList.add("side-hidden");

/* ================= topology ================= */
const NS = "http://www.w3.org/2000/svg";
const S = (tag, attrs = {}, text) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); if (text !== undefined) n.textContent = text; return n; };
const topo = { pos: {}, pins: store.get("canah.topo.pins", {}), open: store.get("canah.topo.open", {}), view: { x: 0, y: 0, k: 1 },
  labels: true, live: true, fitted: false, nodes: [], links: [], timer: null };

const domainClass = (d) => { d = String(d || "").toLowerCase(); return /packet|ip|l2|l3|sdn/.test(d) ? "packet" : /optic|wdm|photon/.test(d) ? "optical" : /data|dc|fabric|centre|center/.test(d) ? "dc" : "other"; };
const DOMAIN_COLOR = { packet: "var(--d-packet)", optical: "var(--d-optical)", dc: "var(--d-dc)", other: "var(--d-other)" };

/* CANAH's agents (librarian, general-purpose, one agent per controller) are not the controllers: the agentic MCP
   holds the controllers, and a controller's agent works it through the agentic MCP. */
const agentsDetail = () => state.info?.subagent_detail || (state.info?.subagents || []).map((n) => ({ name: n }));
const agentFor = (ctlKey) => (agentsDetail().find((a) => a.controller === ctlKey) || {}).name;
const ctlOfAgent = (name) => (agentsDetail().find((a) => a.name === name) || {}).controller;
const AGENT_KIND = { controller: "controller agent", knowledge: "knowledge", general: "general", custom: "agent",
                     qot: "quality of transmission", config: "configuration" };

// A row that scrolls sideways: arrows when it overflows, faded edges, the wheel scrolls it, nothing hidden.
function hscroll(el, extra = "") {
  if (!el || el._hs) return; el._hs = true;
  const wrap = document.createElement("div"); wrap.className = `hs-wrap ${extra}`.trim();
  el.parentNode.insertBefore(wrap, el); wrap.append(el); el.classList.add("hs");
  const mk = (dir) => {
    const b = document.createElement("button"); b.type = "button"; b.className = `hs-btn ${dir}`;
    b.setAttribute("aria-label", dir === "l" ? "Scroll left" : "Scroll right"); b.tabIndex = -1;
    b.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="${dir === "l" ? "M15 18l-6-6 6-6" : "M9 18l6-6-6-6"}"/></svg>`;
    b.addEventListener("click", () => el.scrollBy({ left: (dir === "l" ? -1 : 1) * Math.max(120, el.clientWidth * 0.7), behavior: "smooth" }));
    wrap.append(b);
  };
  mk("l"); mk("r");
  const upd = () => { const max = el.scrollWidth - el.clientWidth - 1;
    wrap.classList.toggle("overflows", max > 0); wrap.classList.toggle("at-start", el.scrollLeft <= 1); wrap.classList.toggle("at-end", el.scrollLeft >= max); };
  el.addEventListener("scroll", upd, { passive: true });
  el.addEventListener("wheel", (e) => { if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && el.scrollWidth > el.clientWidth + 1) { e.preventDefault(); el.scrollLeft += e.deltaY; } }, { passive: false });
  new ResizeObserver(upd).observe(el); new MutationObserver(upd).observe(el, { childList: true, subtree: true });
  upd();
}
hscroll($("#kpis"));

async function loadTopology(refresh = false) {
  try {
    const t = await api(`/topology${refresh ? "?refresh=1" : ""}`);
    state.topology = t; buildGraph(t);
    if (!state.mcp || Date.now() - (state.mcpAt || 0) > 300000) { state.mcpAt = Date.now(); api("/network/connection").then((m) => { state.mcp = m; }).catch(() => {}); }
    $("#topo-updated").textContent = `read ${new Date(t.at * 1000).toLocaleTimeString()}${t.url ? " from " + t.url : ""}`;
    const ctls = t.controllers || [];
    const okN = ctls.filter((c) => c.ok).length;
    $("#st-ctl span:last-child").textContent = `controllers ${okN}/${ctls.length}`;
    $("#st-ctl .dot").className = `dot ${!t.ok ? "bad" : okN === ctls.length && ctls.length ? "ok" : okN ? "warn" : "bad"}`;
    renderKpis();
    if (state.view === "topology") { renderTopology(); if (!topo.fitted) fitTopology(false); }
    if (state.selected) renderDetail(state.selected);
  } catch (e) {
    $("#st-ctl .dot").className = "dot bad";
    if (state.view === "topology") $("#detail-body").innerHTML = `<div class="banner bad">The topology could not be read: ${esc(e.message)}</div>`;
  }
  clearTimeout(topo.timer);
  topo.timer = setTimeout(() => { if (topo.live && !$("#shell").hidden) loadTopology(); }, 30000);
}
$("#topo-refresh").addEventListener("click", () => loadTopology(true));
$("#topo-live").addEventListener("change", (e) => { topo.live = e.target.checked; if (topo.live) loadTopology(); });
$("#topo-labels").addEventListener("change", (e) => { topo.labels = e.target.checked; renderTopology(); });

function buildGraph(t) {
  const ctlsAll = t.controllers || [];
  const nodes = [{ id: "canah", kind: "hub", label: "CANAH", ok: true },
                 { id: "mcp", kind: "mcp", label: "Agentic MCP", ok: t.ok !== false, url: t.url, n: ctlsAll.length, okN: ctlsAll.filter((c) => c.ok).length }];
  const links = [{ kind: "uplink", source: "canah", target: "mcp", ok: t.ok !== false }];
  // The digital twin CANAH tries changes in first (NetTwin, or another twin chosen on the Digital twins page).
  if (state.twin) {
    const w = state.twin;
    nodes.push({ id: "twin", kind: "twin", label: twinLabel(w), ok: !!w.ok, w });
    links.push({ kind: "twinlink", source: "canah", target: "twin", ok: !!w.ok });
    if ((w.extras || []).some((x) => x.name === "mirror")) links.push({ kind: "mirror", source: "twin", target: "mcp", ok: !!w.ok && t.ok !== false });
  }
  for (const c of ctlsAll) {
    const cid = `ctl:${c.key}`;
    nodes.push({ id: cid, kind: "controller", label: c.label || c.key, key: c.key, domain: c.domain, ok: c.ok, c,
      sub: `${c.domain || "?"} · ${c.ok ? (c.devices_total > c.devices.length ? `${c.devices.length} of ${c.devices_total} devices` : `${c.devices.length} devices`) : "unreadable"}` });
    links.push({ kind: "held", source: "mcp", target: cid, ok: c.ok });
    for (const d of c.devices || []) nodes.push({ id: `dev:${c.key}:${d.id}`, kind: "device", label: d.label || d.id, parent: cid, domain: c.domain, ok: d.status !== "down", d, ctl: c.key });
    for (const l of c.links || []) links.push({ kind: "link", source: `dev:${c.key}:${l.a}`, target: `dev:${c.key}:${l.z}`, ok: l.state !== "down", l, ctl: c.key });
  }
  // Relations between objects of two controllers ("optical:LP-1 carries packet:L-ab"): one arc between the two.
  const keys = new Set((t.controllers || []).map((c) => c.key));
  const cross = new Map();
  for (const r of t.relations || []) {
    const a = String(r.from || "").split(":")[0], b = String(r.to || "").split(":")[0];
    if (!keys.has(a) || !keys.has(b) || a === b) continue;
    const k = [a, b].sort().join("|");
    if (!cross.has(k)) cross.set(k, { kind: "cross", source: `ctl:${a}`, target: `ctl:${b}`, ok: true, rels: [] });
    cross.get(k).rels.push(r);
  }
  // CANAH's agents: shown when CANAH is opened, at work or not. A controller's agent reaches its controller
  // through the agentic MCP, never directly.
  for (const sd of agentsDetail()) {
    const sid = `sub:${sd.name}`;
    nodes.push({ id: sid, kind: "sub", label: sd.name, parent: "canah", ok: true, sd });
    links.push({ kind: "subdeleg", source: "canah", target: sid, ok: true, sub: sd.name });
    if (sd.controller && keys.has(sd.controller)) links.push({ kind: "drives", source: sid, target: "mcp", ok: true, sub: sd.name, ctl: sd.controller });
    if ((sd.controllers || []).length) links.push({ kind: "drives", source: sid, target: "mcp", ok: true, sub: sd.name, ctl: sd.controllers.join(", ") });
    if (sd.kind === "qot" && state.twin) links.push({ kind: "drives", source: sid, target: "twin", ok: true, sub: sd.name, ctl: "the digital twin" });
  }
  topo.nodes = nodes; topo.links = [...links, ...cross.values()];
  layout();
}

function layout() {
  // The agentic MCP in the middle with its controllers on an arc to the right; CANAH to its left, with its
  // agents on an arc further left when it is opened.
  const ctls = topo.nodes.filter((n) => n.kind === "controller");
  const devsOf = (cid) => topo.nodes.filter((n) => n.parent === cid);
  const clusterR = (cid) => topo.open[cid] ? 60 * Math.sqrt(Math.max(1, devsOf(cid).length)) + 60 : 0;
  const biggest = Math.max(0, ...ctls.map((c) => clusterR(c.id)));
  const subs = topo.nodes.filter((n) => n.kind === "sub");
  const span = ctls.length > 1 ? Math.min(Math.PI * 1.25, 0.55 * ctls.length) : 0;
  const R = Math.max(260, ctls.length > 1 ? (ctls.length * (180 + biggest * 1.1)) / (span || 1) : 260);
  const rs = Math.max(170, (subs.length * 64) / Math.PI);
  const D = 300 + (topo.open.canah ? 40 : 0);
  const pos = { mcp: { x: 0, y: 0 }, canah: { x: -D, y: 0 }, twin: { x: -D / 2, y: -Math.max(230, R * 0.62) } };
  subs.forEach((n, i) => {
    const a = Math.PI / 2 + (Math.PI * (i + 0.5)) / Math.max(1, subs.length);    // the left half around CANAH
    pos[n.id] = { x: -D + Math.cos(a) * rs, y: Math.sin(a) * rs };
  });
  ctls.forEach((c, i) => {
    const a = ctls.length > 1 ? -span / 2 + (span * i) / (ctls.length - 1) : 0;
    pos[c.id] = { x: Math.cos(a) * R, y: Math.sin(a) * R, a };
    if (topo.open[c.id]) {
      const devs = devsOf(c.id), cr = clusterR(c.id);
      const cx = pos[c.id].x + Math.cos(a) * (cr + 70), cy = pos[c.id].y + Math.sin(a) * (cr + 70);
      devs.forEach((d, j) => {   // a sunflower: even spacing for any count
        const r = 60 * Math.sqrt(j + 0.5), t = j * 2.39996;
        pos[d.id] = { x: cx + r * Math.cos(t), y: cy + r * Math.sin(t) };
      });
    }
  });
  for (const [id, p] of Object.entries(topo.pins)) if (pos[id]) pos[id] = { ...pos[id], x: p.x, y: p.y };
  topo.pos = pos;
}

const visible = (n) => (n.kind !== "device" && n.kind !== "sub") || !!topo.open[n.parent];
const activeAgents = () => new Set([...(state.activity?.active || []), ...(state.localActive || [])]);
function renderTopology() {
  const svg = $("#topo"); if (!svg || !state.topology) return;
  let vp = $("#topo-vp");
  if (!vp) { vp = S("g", { id: "topo-vp" }); svg.replaceChildren(vp); }
  const gl = S("g"), gn = S("g");
  vp.replaceChildren(gl, gn);
  const pos = topo.pos;
  const vis = new Set(topo.nodes.filter(visible).map((n) => n.id));
  const fewLinks = topo.links.filter((x) => x.kind === "link").length <= 14;

  for (const l of topo.links) {
    if (!vis.has(l.source) || !vis.has(l.target)) continue;
    const a = pos[l.source], b = pos[l.target]; if (!a || !b) continue;
    if (l.kind === "cross") {
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, k = 0.35;
      const d = `M${a.x},${a.y} Q${mx * (1 - k)},${my * (1 - k)} ${b.x},${b.y}`;
      const tip = `across domains: ${l.rels.length} relation(s)\n` + l.rels.slice(0, 12).map((r) => `${r.from} ${r.relation} ${r.to}`).join("\n");
      gl.append(S("path", { d, class: "lk cross halo" }));
      const p = S("path", { d, class: "lk cross" }); p.append(S("title", {}, tip)); gl.append(p);
      gl.append(S("path", { d, class: "lk cross run" }));
      if (topo.labels) {
        const txt = `${l.rels.length} relation${l.rels.length > 1 ? "s" : ""}`, w = txt.length * 6.2 + 14;
        const g = S("g", { class: "plate" }); const px = mx * (1 - k / 2), py = my * (1 - k / 2);
        g.append(S("rect", { x: px - w / 2, y: py - 11, width: w, height: 17, rx: 8.5 })); g.append(S("text", { x: px, y: py + 1.5, "text-anchor": "middle" }, txt));
        g.append(S("title", {}, tip)); gl.append(g);
      }
      continue;
    }
    let d, cls;
    if (l.kind === "subdeleg" || l.kind === "drives") {
      const on = activeAgents().has(l.sub);
      if (l.kind === "drives" && !on) continue;        // drawn only while the agent works its controller
      const p = S("path", { d: `M${a.x},${a.y} L${b.x},${b.y}`, class: `lk ${l.kind}${on ? " on" : ""}` });
      p.append(S("title", {}, l.kind === "drives" ? `${l.sub} is working ${l.ctl} through the agentic MCP` : `CANAH delegates to ${l.sub}`));
      gl.append(p); continue;
    }
    if (l.kind === "twinlink" || l.kind === "mirror") {
      const on = activeAgents().has("twin");
      const p2 = S("path", { d: `M${a.x},${a.y} L${b.x},${b.y}`, class: `lk ${l.kind}${l.ok ? "" : " bad"}${on ? " on" : ""}` });
      p2.append(S("title", {}, l.kind === "twinlink" ? `CANAH tries changes and failures in the digital twin first${on ? " (asking it now)" : ""}` : "the twin mirrors the live networks through the agentic MCP"));
      gl.append(p2); continue;
    }
    if (l.kind === "uplink") { d = `M${a.x},${a.y} L${b.x},${b.y}`; cls = `lk uplink${l.ok ? "" : " bad"}`; }
    else if (l.kind === "held") {
      const ag = agentFor(l.target.slice(4)), act = activeAgents(), on = (ag && act.has(ag)) || act.has(l.target) || act.has("mcp");
      d = `M${a.x},${a.y} L${b.x},${b.y}`; cls = `lk delegation${l.ok ? "" : " bad"}${on ? " on" : ""}`;
    }
    else {
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
      d = `M${a.x},${a.y} Q${mx - dy / len * 14},${my + dx / len * 14} ${b.x},${b.y}`; cls = `lk ${l.ok ? "ok" : "bad"}`;
    }
    const p = S("path", { d, class: cls });
    p.append(S("title", {}, l.kind === "uplink" ? `CANAH reaches every network through the agentic MCP${l.ok ? "" : " (not answering)"}`
      : l.kind === "held" ? `the agentic MCP holds ${l.target.slice(4)}${l.ok ? "" : " (cannot be read)"}` : `${l.l.id}: ${l.l.a} ⇄ ${l.l.z} · ${l.l.state}`));
    gl.append(p);
    if (l.kind === "link" && topo.labels && (state.selected === l.source || state.selected === l.target || fewLinks))
      gl.append(S("text", { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 6, class: "lbl", "text-anchor": "middle" }, l.l.id));
  }

  const light = document.documentElement.dataset.theme === "light";
  for (const n of topo.nodes) {
    if (!vis.has(n.id)) continue;
    const p = pos[n.id]; if (!p) continue;
    const g = S("g", { class: `nd ${n.ok ? "alive" : "down"}${state.selected === n.id ? " sel" : ""}`, transform: `translate(${p.x},${p.y})`, "data-id": n.id });
    if (n.kind === "hub") {
      const busy = state.chat.busy;
      g.append(S("circle", { r: 62, class: "beat", fill: "none", stroke: "var(--accent)" }));
      g.append(S("circle", { r: 62, fill: "var(--panel)", stroke: "var(--accent)" }));
      if (busy) g.append(S("circle", { r: 72, class: "hub-ring" }));
      g.append(S("image", { href: light ? "/static/logo-light.png" : "/static/logo-dark.png", x: -30, y: -46, width: 60, height: 60 }));
      g.append(S("text", { y: 30, "text-anchor": "middle", "font-weight": 700, "letter-spacing": "1.5" }, "CANAH"));
      g.append(S("text", { y: 46, "text-anchor": "middle", class: "sub" }, (state.info?.model?.model || "").replace(/^ollama:/, "").split(":")[0]));
      const nsub = topo.nodes.filter((x) => x.kind === "sub").length;
      if (nsub) g.append(S("text", { y: 80, "text-anchor": "middle", class: "grp" }, topo.open.canah ? `▾ ${nsub} agents` : `▸ ${nsub} agents`));
    } else if (n.kind === "twin") {
      const on = activeAgents().has("twin"), col = n.ok ? "#2fd3c1" : "var(--bad)", w = 196, h = 64;
      g.setAttribute("class", `nd twin ${n.ok ? "alive" : "down"}${state.selected === n.id ? " sel" : ""}${on ? " on" : ""}`);
      g.append(S("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 16, class: "beat", fill: "none", stroke: col }));
      g.append(S("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 16, fill: "var(--panel)", stroke: col, "stroke-width": 1.6, "stroke-dasharray": on ? "" : "5 3" }));
      g.append(S("image", { href: "/static/nettwin-icon.png", x: -w / 2 + 10, y: -22, width: 44, height: 44 }));
      g.append(S("text", { x: 24, y: -3, "text-anchor": "middle", "font-weight": 700 }, n.label));
      g.append(S("text", { x: 24, y: 15, "text-anchor": "middle", class: "sub" }, on ? "rehearsing a change…" : `digital twin · ${(n.w.domains || []).length} domains`));
      g.append(S("title", {}, `${n.label}: the digital twin CANAH tries a change or a failure in before the network sees it (${(n.w.domains || []).map((d) => d.title || d.domain).join(", ")}).`));
    } else if (n.kind === "mcp") {
      const col = n.ok ? "#22a9b0" : "var(--bad)", w = 188, h = 64, busy = [...activeAgents()].some((x) => x === "mcp" || String(x).startsWith("ctl:"));
      g.setAttribute("class", `nd mcp ${n.ok ? "alive" : "down"}${state.selected === n.id ? " sel" : ""}${busy ? " on" : ""}`);
      g.append(S("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 16, class: "beat", fill: "none", stroke: col }));
      g.append(S("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 16, fill: "var(--panel)", stroke: col, "stroke-width": 1.6 }));
      g.append(S("image", { href: "/static/agentic-mcp-icon.png", x: -w / 2 + 10, y: -22, width: 44, height: 44 }));
      g.append(S("text", { x: 22, y: -3, "text-anchor": "middle", "font-weight": 700 }, "Agentic MCP"));
      g.append(S("text", { x: 22, y: 15, "text-anchor": "middle", class: "sub" }, n.ok ? `${n.okN}/${n.n} controllers` : "not answering"));
      g.append(S("title", {}, `The agentic MCP${n.url ? " at " + n.url : ""}: it holds the SDN controllers and checks each against the common contract. CANAH reaches them only through it.`));
    } else if (n.kind === "sub") {
      const on = activeAgents().has(n.label), w = 136, h = 30;
      const ctlKey = n.sd?.controller, ctlRow = (state.topology?.controllers || []).find((c) => c.key === ctlKey);
      const col = ctlRow ? DOMAIN_COLOR[domainClass(ctlRow.domain)] : n.sd?.kind === "config" ? DOMAIN_COLOR[domainClass(n.sd.domain)]
        : n.sd?.kind === "qot" ? "#2fd3c1" : n.sd?.kind === "knowledge" ? "var(--accent2)" : "var(--faint)";
      g.setAttribute("class", `nd sub${on ? " on" : ""}${state.selected === n.id ? " sel" : ""}`);
      g.append(S("rect", { x: -w / 2 - 4, y: -h / 2 - 4, width: w + 8, height: h + 8, rx: 19, class: "halo" }));
      g.append(S("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 15, fill: "var(--panel)", stroke: on ? "var(--accent)" : col, class: "body" }));
      g.append(S("circle", { cx: -w / 2 + 13, cy: 0, r: 4, fill: on ? "var(--accent)" : "var(--faint)", class: on ? "led" : "" }));
      const used = state.activity?.used?.[n.label];
      g.append(S("text", { x: 6, y: 4.5, "text-anchor": "middle" }, `${n.label.length > 16 ? n.label.slice(0, 15) + "…" : n.label}${n.sd?.tools != null ? ` · ${n.sd.tools}` : ""}`));
      g.append(S("title", {}, `${n.label} (${AGENT_KIND[n.sd?.kind] || "agent"}): ${on ? "at work now" : "idle"}${used ? ` · ${used.calls} calls, last ${ago(used.last)}` : ""}${ctlKey ? `\nworks ${ctlRow?.label || ctlKey} through the agentic MCP` : ""}\n${n.sd?.description || ""}`));
    } else if (n.kind === "controller") {
      const col = n.ok ? DOMAIN_COLOR[domainClass(n.domain)] : "var(--bad)", w = 178, h = 54;
      g.append(S("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 10, class: "beat", fill: "none", stroke: col }));
      g.append(S("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 10, fill: "var(--panel)", stroke: col }));
      g.append(shapeFor(domainClass(n.domain), -w / 2 + 16, -h / 2 + 16, 7, col));
      g.append(S("circle", { cx: w / 2 - 12, cy: -h / 2 + 12, r: 4, class: n.ok ? "led" : "", fill: n.ok ? "var(--ok)" : "var(--bad)" }));
      g.append(S("text", { y: -2, "text-anchor": "middle", "font-weight": 600 }, n.label.length > 20 ? n.label.slice(0, 19) + "…" : n.label));
      g.append(S("text", { y: 15, "text-anchor": "middle", class: "sub" }, n.sub));
      const kids = topo.nodes.filter((x) => x.parent === n.id).length;
      if (kids) g.append(S("text", { y: h / 2 + 15, "text-anchor": "middle", class: "grp" }, topo.open[n.id] ? `▾ ${kids} open` : `▸ ${kids} folded`));
    } else {
      const col = n.ok ? DOMAIN_COLOR[domainClass(n.domain)] : "var(--bad)", w = 118, h = 32;
      g.append(S("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 8, fill: "var(--panel)", stroke: col }));
      g.append(S("circle", { cx: -w / 2 + 10, cy: 0, r: 3.5, class: n.ok ? "led" : "", fill: n.ok ? "var(--ok)" : "var(--bad)" }));
      const lab = n.label.length > 15 ? n.label.slice(0, 14) + "…" : n.label;
      g.append(S("text", { x: 4, y: 4.5, "text-anchor": "middle", style: "font-size:12px" }, lab));
      g.append(S("title", {}, `${n.label}\n${n.d.kind} · ${n.d.status} · held by ${n.ctl}`));
    }
    gn.append(g);
  }
  applyView();
}

function shapeFor(kind, x, y, r, col) {
  if (kind === "optical") return S("polygon", { points: `${x},${y - r} ${x + r},${y + r * 0.8} ${x - r},${y + r * 0.8}`, fill: col, stroke: "none" });
  if (kind === "dc") { const p = [0, 1, 2, 3, 4, 5].map((i) => `${x + r * Math.cos(Math.PI / 3 * i)},${y + r * Math.sin(Math.PI / 3 * i)}`).join(" "); return S("polygon", { points: p, fill: col, stroke: "none" }); }
  if (kind === "packet") return S("circle", { cx: x, cy: y, r: r - 1, fill: col, stroke: "none" });
  return S("rect", { x: x - r + 1, y: y - r + 1, width: 2 * r - 2, height: 2 * r - 2, rx: 2.5, fill: col, stroke: "none" });
}

function applyView() {
  const vp = $("#topo-vp"); if (!vp) return;
  vp.setAttribute("transform", `translate(${topo.view.x},${topo.view.y}) scale(${topo.view.k})`);
  $("#topo-zoom").textContent = `${Math.round(topo.view.k * 100)}%`;
}
function fitTopology() {
  const svg = $("#topo"); const r = svg.getBoundingClientRect(); if (!r.width) return;
  const ps = topo.nodes.filter(visible).map((n) => topo.pos[n.id]).filter(Boolean);
  if (!ps.length) return;
  const x0 = Math.min(...ps.map((p) => p.x)) - 110, x1 = Math.max(...ps.map((p) => p.x)) + 110;
  const y0 = Math.min(...ps.map((p) => p.y)) - 150, y1 = Math.max(...ps.map((p) => p.y)) + 80;   // room under the toolbar
  const k = Math.min(1.6, Math.max(0.15, Math.min(r.width / (x1 - x0), r.height / (y1 - y0))));
  topo.view = { k, x: r.width / 2 - ((x0 + x1) / 2) * k, y: r.height / 2 - ((y0 + y1) / 2) * k };
  topo.fitted = true; applyView();
}
const zoomAt = (f, cx, cy) => { const k = Math.min(4, Math.max(0.1, topo.view.k * f)); topo.view.x = cx - (cx - topo.view.x) * (k / topo.view.k); topo.view.y = cy - (cy - topo.view.y) * (k / topo.view.k); topo.view.k = k; applyView(); };
const center = () => { const r = $("#topo").getBoundingClientRect(); return [r.width / 2, r.height / 2]; };
$("#topo-in").addEventListener("click", () => zoomAt(1.2, ...center()));
$("#topo-out").addEventListener("click", () => zoomAt(1 / 1.2, ...center()));
$("#topo-fit").addEventListener("click", () => fitTopology());
$("#topo-reset").addEventListener("click", () => { topo.pins = {}; store.set("canah.topo.pins", {}); layout(); renderTopology(); fitTopology(); });
$("#topo-all").addEventListener("click", () => toggleAll());
function toggleAll() {
  const ctls = topo.nodes.filter((n) => n.kind === "controller");
  const anyClosed = ctls.some((c) => !topo.open[c.id]);
  for (const c of ctls) topo.open[c.id] = anyClosed;
  store.set("canah.topo.open", topo.open); $("#topo-all").textContent = anyClosed ? "Fold all" : "Open all";
  layout(); renderTopology(); fitTopology();
}

(function wireCanvas() {
  const svg = $("#topo");
  let drag = null;
  const pt = (e) => { const r = svg.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  svg.addEventListener("wheel", (e) => { e.preventDefault(); const p = pt(e); zoomAt(e.deltaY < 0 ? 1.12 : 1 / 1.12, p.x, p.y); }, { passive: false });
  svg.addEventListener("pointerdown", (e) => {
    const nd = e.target.closest(".nd"); const p = pt(e);
    drag = nd ? { id: nd.dataset.id, start: p, moved: false, orig: { ...topo.pos[nd.dataset.id] } } : { pan: true, start: p, v: { ...topo.view }, moved: false };
    if (drag.pan) svg.classList.add("panning");
    svg.setPointerCapture(e.pointerId);
  });
  svg.addEventListener("pointermove", (e) => {
    if (!drag) return; const p = pt(e), dx = p.x - drag.start.x, dy = p.y - drag.start.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.pan) { topo.view.x = drag.v.x + dx; topo.view.y = drag.v.y + dy; applyView(); return; }
    if (!drag.moved) return;
    const np = { x: drag.orig.x + dx / topo.view.k, y: drag.orig.y + dy / topo.view.k };
    const shift = { x: np.x - topo.pos[drag.id].x, y: np.y - topo.pos[drag.id].y };
    topo.pos[drag.id] = { ...topo.pos[drag.id], ...np };
    // A controller carries its open devices with it.
    for (const n of topo.nodes) if (n.parent === drag.id && topo.open[drag.id] && topo.pos[n.id]) topo.pos[n.id] = { x: topo.pos[n.id].x + shift.x, y: topo.pos[n.id].y + shift.y };
    if (!topo.frame) topo.frame = requestAnimationFrame(() => { topo.frame = 0; renderTopology(); });
  });
  svg.addEventListener("pointerup", () => {
    if (!drag) return;
    svg.classList.remove("panning");
    if (!drag.pan && drag.moved) {
      topo.pins[drag.id] = { x: topo.pos[drag.id].x, y: topo.pos[drag.id].y };
      for (const n of topo.nodes) if (n.parent === drag.id && topo.pins[n.id] === undefined && topo.open[drag.id]) { /* devices follow, unpinned */ }
      store.set("canah.topo.pins", topo.pins);
    } else if (!drag.pan) {
      // A click on a controller opens or folds what it holds, and selects it.
      const n = topo.nodes.find((x) => x.id === drag.id);
      if (n && (n.kind === "controller" || n.kind === "hub") && topo.nodes.some((x) => x.parent === n.id)) {
        topo.open[n.id] = !topo.open[n.id]; store.set("canah.topo.open", topo.open); layout();
      }
      state.selected = drag.id; renderDetail(drag.id); renderTopology();
    } else if (!drag.moved) { state.selected = null; renderDetail(null); renderTopology(); }
    drag = null;
  });
  svg.addEventListener("dblclick", (e) => { if (!e.target.closest(".nd")) fitTopology(); });
  new ResizeObserver(() => { if (state.view === "topology" && !topo.fitted) fitTopology(); }).observe(svg);
})();

function renderKpis() {
  const t = state.topology || { controllers: [] };
  const ctls = t.controllers || [];
  const okN = ctls.filter((c) => c.ok).length;
  const devs = ctls.reduce((s, c) => s + (c.devices || []).length, 0);
  const links = ctls.reduce((s, c) => s + (c.links || []).length, 0);
  const down = ctls.reduce((s, c) => s + (c.links || []).filter((l) => l.state === "down").length, 0);
  const doms = [...new Set(ctls.map((c) => c.domain).filter(Boolean))];
  const k = (label, value, sub, dot = "", small = false) => `<div class="kpi"><div class="label">${dot ? `<span class="dot ${dot}"></span>` : ""}${label}</div><div class="value${small ? " small" : ""}">${value}</div><div class="sub">${sub}</div></div>`;
  $("#kpis").innerHTML = [
    k("Controllers", `${okN}/${ctls.length}`, ctls.length ? "readable / held by the agentic MCP" : (t.error || "none held"), !t.ok ? "bad" : okN === ctls.length && ctls.length ? "ok" : okN ? "warn" : "bad"),
    k("Domains", doms.length, doms.join(" · ") || "–"),
    k("Devices", devs, "read through the contract"),
    k("Links", links, down ? `${down} down` : "all up", down ? "warn" : links ? "ok" : ""),
    k("Relations", (t.relations || []).length, "across domains"),
    k("Model", esc((state.info?.model?.model || "–").replace(/^ollama:/, "")), `${state.info?.tools ?? "–"} tools · ${(state.info?.subagents || []).length} agents`, "ok", true),
    k("Agentic MCP", t.ok === false ? "down" : "up", esc(t.url || "–"), t.ok === false ? "bad" : "ok", true),
  ].join("");
}

function hubAgents() {
  const act = activeAgents(), used = state.activity?.used || {};
  const subs = agentsDetail();
  const runs = state.activity?.running || [];
  const row = (sd) => { const on = act.has(sd.name), u = used[sd.name];
      const ctl = sd.controller ? (state.topology?.controllers || []).find((c) => c.key === sd.controller) : null;
      return `<div class="act-row"><span class="dot ${on ? "busy" : u ? "ok" : ""}"></span><div><b>${esc(sd.name)}</b>${sd.tools != null ? ` <span class="faint">${sd.tools} tools</span>` : ""}
        <div class="faint">${sd.kind === "config" ? `changes ${esc((sd.controllers || []).join(", "))} (${esc(sd.domain)}) through the agentic MCP` : ctl || sd.controller ? `works ${esc(ctl?.label || sd.controller)} through the agentic MCP` : esc((sd.description || "").slice(0, 110))}</div></div><span class="faint">${on ? "<b style='color:var(--accent)'>at work</b>" : u ? `${u.calls} calls · ${ago(u.last)}` : "idle"}</span></div>`; };
  const group = (title, list) => list.length ? `<div class="act-group">${title}</div>` + list.map(row).join("") : "";
  return group("Quality of transmission · works the digital twin", subs.filter((x) => x.kind === "qot"))
    + group("Configuration · one per network domain", subs.filter((x) => x.kind === "config"))
    + group("Knowledge", subs.filter((x) => x.kind === "knowledge"))
    + group("General", subs.filter((x) => !["controller", "knowledge", "qot", "config"].includes(x.kind)))
    + `<h4>Running now</h4>${runs.length ? runs.map((r) => `<div class="act-row"><span class="dot busy"></span><div>${esc(r.user || "a request")} · <span class="mono">${esc(r.step || "thinking")}</span><div class="faint">${esc(r.agents.join(", "))}</div></div><span class="faint">${Math.round(Date.now() / 1000 - r.since)} s</span></div>`).join("") : `<div class="faint" style="font-size:12px">nothing</div>`}`
    + ((state.activity?.recent || []).length ? `<h4>Last steps</h4>${state.activity.recent.slice(-8).reverse().map((e) => `<div class="act-row"><span class="dot"></span><div><span class="mono">${esc(e.tool)}</span> <span class="faint">by ${esc(e.agent)}</span></div><span class="faint">${ago(e.at)}</span></div>`).join("")}` : "");
}
async function pollActivity() {
  clearTimeout(state.activityTimer);
  if ($("#shell").hidden) return;
  try {
    const a = await api("/activity");
    const before = JSON.stringify([state.activity?.active, state.activity?.running?.length]);
    state.activity = a;
    if (state.view === "topology" && JSON.stringify([a.active, a.running.length]) !== before) renderTopology();
    if (state.view === "topology" && state.selected === "canah" && $("#hub-agents")) $("#hub-agents").innerHTML = hubAgents();
  } catch (e) { /* the next poll tries again */ }
  if (state.view === "topology") state.activityTimer = setTimeout(pollActivity, 2500);
}

function renderDetail(id) {
  const title = $("#detail-title"), body = $("#detail-body");
  if (!id) { title.textContent = "Details"; body.className = "body muted"; body.innerHTML = "Click CANAH, a controller or a device to see its state. A controller marked <b>▸</b> holds devices folded inside it; click it to open them."; return; }
  const n = topo.nodes.find((x) => x.id === id); if (!n) return;
  body.className = "body";
  const ask = (q) => `<button class="btn sm" data-ask="${esc(q)}">Ask CANAH</button>`;
  if (n.kind === "hub") {
    const i = state.info || {};
    title.textContent = "CANAH";
    body.innerHTML = `<div class="kv"><div>model</div><div>${esc(i.model?.model || "–")}</div><div>tools</div><div>${i.tools ?? "–"} (${i.main_agent_tools ?? "–"} on the main agent)</div>
      <div>agents</div><div>${esc((i.subagents || []).join(", ") || "none")}</div><div>sandbox</div><div>${esc(i.sandbox || "none")}</div>
      <div>middleware</div><div>${esc((i.middleware || []).join(", ") || "–")}</div><div>approvals</div><div>${(i.interrupt_on || []).length} tools pause for an operator</div>
      <div>agentic MCP</div><div class="mono">${esc(state.topology?.url || "–")}</div></div>
      <h4>CANAH's agents ${topo.open.canah ? "" : "<span class='faint' style='text-transform:none;letter-spacing:0'>(click CANAH to show them)</span>"}</h4>
      <div id="hub-agents">${hubAgents()}</div>
      ${(state.topology?.relations || []).length ? `<h4>Across domains</h4><table class="tbl"><tbody>${state.topology.relations.slice(0, 40).map((r) => `<tr><td class="mono">${esc(r.from)}</td><td>${esc(r.relation)}</td><td class="mono">${esc(r.to)}</td></tr>`).join("")}</tbody></table>` : ""}
      <div class="row" style="margin-top:12px">${ask("Which controllers do you hold, and are they all healthy?")}</div>`;
  } else if (n.kind === "twin") {
    const w = n.w || {};
    title.textContent = `${n.label} · digital twin`;
    body.innerHTML = `${/nettwin/i.test(n.label) ? `<img class="dt-logo" src="/static/nettwin-logo.png" alt="NetTwin: rehearse the change before you make it">` : ""}
      ${w.ok ? "" : `<div class="banner bad">The twin does not answer: ${esc(w.error || "")}</div>`}
      <p class="muted" style="margin:0 0 10px;font-size:12.5px">${esc(w.about || "")}</p>
      <div class="kv"><div>role</div><div>CANAH tries a change or a failure here first; a write to a controller runs only when its check here lets it (or an operator overrides it)</div>
      <div>version</div><div>${esc(w.version || "–")}</div><div>checks writes on</div><div>${esc((w.controller_domains || []).join(", ") || "none")}</div>
      <div>mirrors</div><div>${(w.extras || []).some((x) => x.name === "mirror") ? "the live networks, through the agentic MCP" : "no"}</div></div>
      <h4>Domains</h4><div class="chips">${(w.domains || []).map((d) => `<span class="chip">${esc(d.title || d.domain)}</span>`).join("")}</div>
      <div class="row" style="margin-top:12px"><button class="btn sm" id="dt-twins">Digital twins page</button>
        <button class="btn sm" data-ask="${esc(`What happens if the busiest link fails? Ask ${n.label} before answering.`)}">Ask CANAH</button></div>`;
    $("#dt-twins").onclick = () => go("twins");
  } else if (n.kind === "mcp") {
    const m = state.mcp || {};
    const ctls = state.topology?.controllers || [];
    title.textContent = "Agentic MCP";
    body.innerHTML = `${n.ok ? "" : `<div class="banner bad">The agentic MCP is not answering${state.topology?.error ? ": " + esc(state.topology.error) : ""}.</div>`}
      <p class="muted" style="margin:0 0 10px;font-size:12.5px">It holds the SDN controllers and checks each against the common contract. CANAH works every controller through it, with one tool per contract verb.</p>
      <div class="kv"><div>address</div><div class="mono">${esc(n.url || "–")}</div>
      <div>token</div><div>${m.token_set ? (m.token_accepted === false ? `<span class="tag bad">set, refused</span>` : `<span class="tag ok">set</span>`) : `<span class="tag">none</span>`}</div>
      <div>controllers</div><div>${n.okN} of ${n.n} readable</div></div>
      ${ctls.length ? `<h4>Controllers it holds</h4><table class="tbl"><tbody>${ctls.map((c) => `<tr><td><span class="dshape ${domainClass(c.domain)}"></span>${esc(c.label || c.key)}</td><td>${esc(c.verdict || "–")}</td></tr>`).join("")}</tbody></table>` : ""}
      <div class="row" style="margin-top:12px"><button class="btn sm" id="dt-ctl-page">Manage the agentic MCP</button></div>`;
    $("#dt-ctl-page").onclick = () => go("controllers");
  } else if (n.kind === "controller") {
    const c = n.c;
    title.textContent = c.label || c.key;
    const verbs = Object.entries(c.verbs || {});
    body.innerHTML = `${c.ok ? "" : `<div class="banner bad">Cannot be read: ${esc(c.error || "unknown")}</div>`}
      <div class="kv"><div>key</div><div class="mono">${esc(c.key)}</div><div>domain</div><div><span class="dshape ${domainClass(c.domain)}"></span>${esc(c.domain || "–")}</div>
      <div>family</div><div>${esc(c.family || "–")}</div><div>tools</div><div>${c.tools ?? "–"}</div><div>contract</div><div>${esc(c.verdict || "–")}</div>
      <div>held by</div><div>the agentic MCP</div>
      <div>devices</div><div>${c.devices.length}${c.devices_total > c.devices.length ? ` shown of ${c.devices_total} (the map draws at most ${c.devices.length})` : ""}</div><div>links</div><div>${c.links.length} (${c.links.filter((l) => l.state === "down").length} down)</div></div>
      ${(c.gaps || []).length ? `<h4>Gaps</h4><div class="chips">${c.gaps.map((g) => `<span class="chip">${esc(g)}</span>`).join("")}</div>` : ""}
      ${verbs.length ? `<h4>Contract verbs it serves</h4><table class="tbl"><tbody>${verbs.map(([v, t]) => `<tr><td>${esc(v.replace(/_/g, " "))}</td><td class="mono">${esc(t)}</td></tr>`).join("")}</tbody></table>` : ""}
      <div class="row" style="margin-top:12px">${ask(`Give me an overview of ${c.label || c.key}: its devices, links and anything unhealthy.`)}
        <button class="btn sm" id="dt-fold">${topo.open[n.id] ? "Fold" : "Open"} devices</button></div>`;
    $("#dt-fold").onclick = () => { topo.open[n.id] = !topo.open[n.id]; store.set("canah.topo.open", topo.open); layout(); renderTopology(); renderDetail(id); fitTopology(); };
  } else if (n.kind === "sub") {
    const sd = n.sd || {}, u = state.activity?.used?.[n.label], on = activeAgents().has(n.label);
    const ctl = sd.controller ? (state.topology?.controllers || []).find((c) => c.key === sd.controller) : null;
    title.textContent = `${n.label} · ${AGENT_KIND[sd.kind] || "agent"}`;
    body.innerHTML = `<div class="kv"><div>status</div><div>${on ? `<span class="tag info">at work now</span>` : `<span class="tag">idle</span>`}</div>
      <div>tools</div><div>${sd.tools ?? "the main agent's"}</div><div>calls</div><div>${u ? `${u.calls} since the service started, last ${ago(u.last)}` : "none yet"}</div>
      ${sd.controller ? `<div>works</div><div>${esc(ctl?.label || sd.controller)}${ctl ? ` (${esc(ctl.domain)})` : ""}, through the agentic MCP</div>` : ""}</div>
      <h4>What it does</h4><p class="muted" style="margin:0;font-size:12.5px">${esc(sd.description || "–")}</p>
      <div class="row" style="margin-top:12px"><button class="btn sm" data-ask="${esc(`Ask ${n.label}: what can you do, and with which tools?`)}">Ask CANAH</button></div>`;
  } else {
    const d = n.d, ls = topo.links.filter((l) => l.kind === "link" && (l.source === id || l.target === id));
    title.textContent = d.label || d.id;
    body.innerHTML = `<div class="kv"><div>id</div><div class="mono">${esc(d.id)}</div><div>kind</div><div>${esc(d.kind)}</div><div>status</div><div><span class="tag ${d.status === "down" ? "bad" : "ok"}">${esc(d.status)}</span></div>
      <div>controller</div><div>${esc(n.ctl)}</div><div>links</div><div>${ls.length}</div></div>
      ${ls.length ? `<h4>Links</h4><table class="tbl"><tbody>${ls.map((l) => `<tr><td class="mono">${esc(l.l.id)}</td><td>${esc(l.l.a)} ⇄ ${esc(l.l.z)}</td><td><span class="tag ${l.ok ? "ok" : "bad"}">${esc(l.l.state)}</span></td></tr>`).join("")}</tbody></table>` : ""}
      <div class="row" style="margin-top:12px">${ask(`On ${n.ctl}, what is the state of ${d.id}, its ports and what it carries?`)}</div>`;
  }
  $$("[data-ask]", body).forEach((b) => b.addEventListener("click", () => { go("chat"); $("#composer").value = b.dataset.ask; $("#composer").focus(); }));
}

/* ================= controllers ================= */
async function renderControllers(refresh = false) {
  loadKinds(); onboardAllowed(); renderConnection();
  if (state.ctlTab === "contract") renderContract();
  try {
    const n = state.network = await api("/network");
    $("#ctl-src").textContent = n.url ? `agentic MCP at ${n.url}` : "";
    $("#ctl-banner").innerHTML = n.ok ? "" : `<div class="banner bad">The agentic MCP did not answer: ${esc(n.error)}</div>`;
    const topoBy = Object.fromEntries((state.topology?.controllers || []).map((c) => [c.key, c]));
    const rows = n.controllers || [];
    $("#ctl-count").textContent = rows.length;
    const verdictTag = (v) => `<span class="tag ${/not/.test(v || "") ? "bad" : /gap/.test(v || "") ? "warn" : v ? "ok" : ""}">${esc(v || "–")}</span>`;
    $("#ctl-table").innerHTML = rows.length ? `<table class="tbl ctl-tbl"><thead><tr><th>controller</th><th>domain</th><th class="num">tools</th><th>contract</th><th>readable</th></tr></thead><tbody>
      ${rows.map((c) => { const t = topoBy[c.key]; const readable = t ? t.ok : null; const ag = agentFor(c.key); return `<tr class="click${state.ctlSel === c.key ? " sel" : ""}" data-key="${esc(c.key)}"><td><b>${esc(c.label || c.key)}</b><div class="faint mono cell-sub">${esc(c.key)}${c.base_url ? " · " + esc(c.base_url) : ""}</div></td>
        <td><span class="dshape ${domainClass(c.domain)}"></span>${esc(c.domain || "–")}<div class="faint cell-sub">${esc(c.family || "")}</div></td><td class="num">${c.tools ?? "–"}</td>
        <td>${verdictTag(c.conformance?.verdict)}</td>
        <td>${c.error ? `<span class="tag bad">error</span>` : readable === null ? `<span class="tag">–</span>` : readable ? `<span class="tag ok">${t.devices.length} devices</span>` : `<span class="tag bad" title="${esc(t.error || "")}">unreadable</span>`}</td></tr>`; }).join("")}</tbody></table>` : `<div class="empty">No controllers. Add one under <b>Add controllers</b>.</div>`;
    $$("#ctl-table tr.click").forEach((tr) => tr.addEventListener("click", () => { state.ctlSel = tr.dataset.key; $$("#ctl-table tr.sel").forEach((x) => x.classList.remove("sel")); tr.classList.add("sel"); controllerDetail(tr.dataset.key); }));
  } catch (e) { $("#ctl-banner").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
}

/* ---------- tabs ---------- */
function ctlTab(name) {
  state.ctlTab = name; store.set("canah.ctlTab", name);
  $$("#ctl-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$("#view-controllers .ctl-pane").forEach((p) => p.hidden = p.dataset.pane !== name);
  if (name === "contract") renderContract();
}
$$("#ctl-tabs .tab").forEach((b) => b.addEventListener("click", () => ctlTab(b.dataset.tab)));
ctlTab(store.get("canah.ctlTab", "held"));

/* ---------- the agentic MCP: address, token ---------- */
async function renderConnection(fresh) {
  const root = levelRank(state.level) >= 2;
  $$("#mcp-form input, #mcp-form button").forEach((x) => { if (x.id !== "mcp-test") x.disabled = !root; });
  try {
    const m = state.mcp = fresh || await api("/network/connection");
    if (document.activeElement !== $("#mcp-url")) $("#mcp-url").value = m.url || "";
    $("#mcp-token").placeholder = m.token_set ? "•••••••••• set: type a new one to replace it" : "the agentic MCP's AGENTIC_MCP_TOKEN";
    const stat = (label, value, cls = "") => `<div class="mcp-stat"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`;
    $("#mcp-stats").innerHTML = [
      stat("Status", m.reachable ? "answering" : "not answering", m.reachable ? "ok" : "bad"),
      stat("Controllers", m.controllers ?? "–"),
      stat("Token", !m.token_set ? "none" : m.token_accepted === false ? "refused" : m.token_accepted ? "accepted" : "set",
           !m.token_set ? "" : m.token_accepted === false ? "bad" : "ok"),
      stat("Contract API", m.contract_api === false ? "not offered" : m.contract_api ? "yes" : "–", m.contract_api === false ? "warn" : ""),
    ].join("");
    $("#mcp-msg").textContent = root ? (m.error ? m.error : "") : "Sign in as root to change the address or token.";
  } catch (e) { $("#mcp-stats").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
}
$("#mcp-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {}; const url = $("#mcp-url").value.trim(), token = $("#mcp-token").value.trim();
  if (url && url !== state.mcp?.url) body.url = url;
  if (token) body.token = token;
  if (!Object.keys(body).length) { $("#mcp-msg").textContent = "Nothing changed."; return; }
  $("#mcp-save").disabled = true; $("#mcp-msg").textContent = "Connecting…";
  try {
    const r = await api("/network/connection", { method: "PUT", body });
    $("#mcp-token").value = ""; await renderConnection(r);
    $("#mcp-msg").innerHTML = r.token_accepted === false ? `<span class="tag bad">token refused</span> the agentic MCP did not accept it.`
      : `<span class="tag ok">saved</span> ${r.reachable ? "connected." : "saved, but the agentic MCP is not answering."} <button class="btn sm" type="button" id="mcp-reload">Reload CANAH</button>`;
    $("#mcp-reload")?.addEventListener("click", reloadAgent);
    renderControllers(); loadTopology(true);
  } catch (err) { $("#mcp-msg").innerHTML = `<span class="tag bad">failed</span> ${esc(err.message)}`; }
  finally { $("#mcp-save").disabled = levelRank(state.level) < 2; }
});
$("#mcp-clear").addEventListener("click", async () => {
  if (!confirm("Remove the agentic MCP token from CANAH? Writes and anything else that needs it will be refused until a token is set again.")) return;
  try { const r = await api("/network/connection", { method: "PUT", body: { clear_token: true } }); await renderConnection(r); toast("Token removed.", "ok"); }
  catch (e) { toast(e.message, "bad"); }
});
$("#mcp-test").addEventListener("click", async () => { $("#mcp-msg").textContent = "Testing…"; await renderConnection(); if (!$("#mcp-msg").textContent) $("#mcp-msg").innerHTML = `<span class="tag ${state.mcp?.reachable ? "ok" : "bad"}">${state.mcp?.reachable ? "answering" : "not answering"}</span>`; });

/* ---------- the common contract ---------- */
async function renderContract() {
  const root = levelRank(state.level) >= 2, adm = levelRank(state.level) >= 1;
  $("#contract-apply").disabled = !root; $("#contract-check").disabled = !adm;
  try {
    const r = await api("/network/contract");
    const k = state.contract = r.contract || {};
    $("#contract-ver").textContent = k.version != null ? `v${k.version}` : "";
    $("#contract-file").textContent = r.file || "";
    if (!$("#contract-edit").value || !state.contractEdited) $("#contract-edit").value = JSON.stringify(k, null, 1);
    const doms = k.domains || [];
    $("#contract-view").innerHTML = `<p class="muted" style="margin:0 0 10px;font-size:12.5px">${esc(k.about || "")}</p>
      <div class="kv" style="margin-bottom:10px"><div>version</div><div>${esc(String(k.version ?? "–"))}</div><div>accepted when</div><div>${esc(k.accept || "–")}</div></div>
      <table class="tbl"><thead><tr><th>part of the contract</th><th>required</th><th>verbs</th></tr></thead><tbody>
      ${doms.map((d) => `<tr><td><b>${esc(d.name)}</b><div class="faint cell-sub">${esc(d.why || "")}</div></td><td>${d.required ? `<span class="tag info">required</span>` : `<span class="tag">optional</span>`}</td>
        <td style="font-size:12px">${Object.entries(d.verbs || {}).map(([v, rule]) => `<span class="verb" title="${esc(rule.why || "")}">${esc(v.replace(/_/g, " "))}${rule.writes ? " ✎" : ""}</span>`).join(" ")}</td></tr>`).join("")}</tbody></table>
      ${(r.controllers || []).length ? `<h4>Controllers under it</h4>${verdictTable(r.controllers)}` : ""}`;
  } catch (e) {
    $("#contract-view").innerHTML = `<div class="banner warn">The contract could not be read: ${esc(e.message)}. An agentic MCP from before the contract API (GET /contract) cannot show or change it here.</div>`;
  }
}
function verdictTable(rows, before) {
  const was = Object.fromEntries((before || []).map((x) => [x.key, x.verdict]));
  return `<table class="tbl"><thead><tr><th>controller</th><th>verdict</th>${before ? "<th>now</th>" : ""}<th>gaps</th></tr></thead><tbody>${rows.map((x) => {
    const tag = (v) => `<span class="tag ${/not/.test(v || "") ? "bad" : /gap/.test(v || "") ? "warn" : v ? "ok" : ""}">${esc(v || x.error || "–")}</span>`;
    const moved = before && was[x.key] !== x.verdict;
    return `<tr${moved ? ' class="moved"' : ""}><td><b>${esc(x.key)}</b></td><td>${tag(x.verdict)}</td>${before ? `<td>${tag(was[x.key])}</td>` : ""}<td class="faint" style="font-size:11.5px">${esc((x.gaps || []).slice(0, 4).join(", "))}${(x.gaps || []).length > 4 ? ` +${x.gaps.length - 4}` : ""}</td></tr>`; }).join("")}</tbody></table>`;
}
function editedContract() {
  try { return JSON.parse($("#contract-edit").value); }
  catch (e) { $("#contract-out").innerHTML = `<div class="banner bad">Not valid JSON: ${esc(e.message)}</div>`; return null; }
}
$("#contract-edit").addEventListener("input", () => { state.contractEdited = true; });
$("#contract-check").addEventListener("click", async () => {
  const k = editedContract(); if (!k) return;
  $("#contract-out").innerHTML = `<span class="muted">Checking…</span>`;
  try {
    const [r, cur] = await Promise.all([api("/network/contract/check", { method: "POST", body: { contract: k } }), api("/network/contract")]);
    $("#contract-out").innerHTML = r.ok ? `<div class="banner ok">The contract is valid. What each controller's verdict would be (current verdict beside it; changes highlighted):</div>${verdictTable(r.controllers, cur.controllers)}`
      : `<div class="banner bad"><b>${r.problems.length} problem${r.problems.length > 1 ? "s" : ""}</b><ul style="margin:6px 0 0 18px;padding:0">${r.problems.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>`;
  } catch (e) { $("#contract-out").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
});
$("#contract-apply").addEventListener("click", async () => {
  const k = editedContract(); if (!k) return;
  if (!confirm(`Make this the common contract (version ${k.version})? Every controller is checked against it from now on; the agentic MCP keeps the current one as a backup.`)) return;
  try {
    const r = await api("/network/contract", { method: "PUT", body: { contract: k } });
    state.contractEdited = false;
    $("#contract-out").innerHTML = `<div class="banner ok">Applied: version ${esc(String(r.version))}, ${r.domains} parts, ${r.verbs} verbs${(r.added || []).length ? `; added ${esc(r.added.join(", "))}` : ""}${(r.removed || []).length ? `; removed ${esc(r.removed.join(", "))}` : ""}. Backup: <span class="mono">${esc(r.backup || "")}</span></div>${verdictTable(r.controllers || [])}`;
    renderContract(); renderControllers(); loadTopology(true);
  } catch (e) { $("#contract-out").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
});
$("#contract-reset").addEventListener("click", () => { state.contractEdited = false; $("#contract-edit").value = JSON.stringify(state.contract || {}, null, 1); $("#contract-out").innerHTML = ""; });
$("#contract-download").addEventListener("click", () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([$("#contract-edit").value], { type: "application/json" })); a.download = "contract.json"; a.click(); });
$("#contract-file-in").addEventListener("change", async (e) => { const f = e.target.files[0]; if (!f) return; e.target.value = ""; $("#contract-edit").value = await f.text(); state.contractEdited = true; $("#contract-out").innerHTML = `<span class="muted">Loaded ${esc(f.name)}: Check it before you apply it.</span>`; });

/* ---------- several controllers from one file ---------- */
const SAMPLE = $("#onboard-card .sample")?.textContent || "";
$("#ob-sample").addEventListener("click", () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([SAMPLE + "\n"], { type: "text/yaml" })); a.download = "controllers.yaml"; a.click(); });
$("#ob-batch").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return; e.target.value = "";
  $("#ob-batch-out").innerHTML = `<span class="working"><span class="bars"><i></i><i></i><i></i></span>onboarding the controllers in ${esc(f.name)}…</span>`;
  try {
    const r = await api("/network/controllers/batch", { method: "POST", body: { name: f.name, content_base64: await fileB64(f) } });
    $("#ob-batch-out").innerHTML = `<table class="tbl"><thead><tr><th>controller</th><th>result</th></tr></thead><tbody>${r.results.map((x) => `<tr><td class="mono">${esc(x.key)}</td><td>${x.ok ? `<span class="tag ok">onboarded</span> ${esc(x.verdict || "")}${x.gaps ? `, ${x.gaps} gaps` : ""}` : `<span class="tag bad">failed</span> <span class="faint">${esc(x.error)}</span>`}</td></tr>`).join("")}</tbody></table>
      ${r.reload_needed ? `<div class="row" style="margin-top:8px"><button class="btn sm primary" type="button" id="ob-batch-reload">Reload CANAH to give them their agents</button></div>` : ""}`;
    $("#ob-batch-reload")?.addEventListener("click", reloadAgent);
    renderControllers(); loadTopology(true);
  } catch (err) { $("#ob-batch-out").innerHTML = `<div class="banner bad">${esc(err.message)}</div>`; }
});
$("#ctl-refresh").addEventListener("click", () => { renderControllers(true); loadTopology(true); });

let kindsAt = 0;
async function loadKinds() {
  if (Date.now() - kindsAt < 60000) return;
  kindsAt = Date.now();
  try {
    const k = await api("/network/kinds");
    const sel = $("#ob-kind"), cur = sel.value;
    sel.innerHTML = `<option value="">recognise it from its API</option>` + (k.kinds || []).map((x) => `<option value="${esc(x.kind)}" title="${esc(x.about || "")}">${esc(x.title || x.kind)} · ${esc(x.domain || "")}</option>`).join("");
    sel.value = cur;
    $("#ob-kinds").innerHTML = (k.kinds || []).length ? `<span class="faint" style="font-size:12px;margin-right:4px">Kinds it knows:</span>` + k.kinds.map((x) => `<span class="chip" title="${esc(x.about || "")}">${esc(x.title || x.kind)}</span>`).join("") : "";
  } catch (e) { /* the list stays as it is */ }
}
function onboardAllowed() {
  const ok = levelRank(state.level) >= 1;
  $$("#onboard-form input, #onboard-form select, #onboard-form button, #onboard-card label.btn input, #ob-guide-kind").forEach((x) => x.disabled = !ok);
  $$("#onboard-card label.btn").forEach((x) => x.classList.toggle("disabled", !ok));
  $("#ob-msg").textContent = ok ? "" : "Sign in as admin to onboard.";
  $("#ctl-reload").disabled = !ok;
}
$("#onboard-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { key: $("#ob-key").value.trim(), label: $("#ob-label").value.trim(), url: $("#ob-url").value.trim(), kind: $("#ob-kind").value,
    spec: $("#ob-spec").value.trim(), user_env: $("#ob-user").value.trim(), password_env: $("#ob-pass").value.trim() };
  $("#ob-go").disabled = true; $("#ob-msg").innerHTML = `<span class="working"><span class="bars"><i></i><i></i><i></i></span>the agentic MCP is learning ${esc(body.key)}…</span>`;
  try {
    const r = await api("/network/controllers", { method: "POST", body });
    const m = r.onboarded || {}, conf = m.conformance || {};
    const problem = (m.api || {}).problem || m.kept;
    $("#ob-msg").innerHTML = problem ? `<span class="tag warn">seated, with a problem</span> ${esc(problem)}`
      : `<span class="tag ok">onboarded</span> ${esc(m.label || body.key)}: contract ${esc(conf.verdict || "checked")}${(conf.gaps || []).length ? `, ${conf.gaps.length} gaps` : ""}. <button class="btn sm primary" type="button" id="ob-reload">Reload CANAH to give it its agent</button>`;
    $("#ob-reload")?.addEventListener("click", reloadAgent);
    renderControllers(); loadTopology(true);
  } catch (err) { $("#ob-msg").innerHTML = `<span class="tag bad">failed</span> ${esc(err.message)}`; }
  finally { $("#ob-go").disabled = false; }
});
async function reloadAgent() {
  toast("Rebuilding the agent…");
  try {
    const r = await api("/admin/reload", { method: "POST" });
    toast(`CANAH reloaded: ${r.tools} tools; agents ${(r.subagents || []).join(", ")}.`, "ok");
    await refreshStatus(); renderControllers(); loadTopology(true);
  } catch (e) { toast(`Reload refused: ${e.message}`, "bad"); }
}
$("#ctl-reload").addEventListener("click", reloadAgent);
async function fileB64(file) { return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(file); }); }
$("#ob-manifest").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return; e.target.value = "";
  try { const r = await api("/network/manifests", { method: "POST", body: { name: f.name, content_base64: await fileB64(f) } }); toast(`Manifest installed: ${JSON.stringify(r.installed || r).slice(0, 160)}`, "ok"); loadKinds(); }
  catch (err) { toast(err.message, "bad"); }
});
$("#ob-guide").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return; e.target.value = "";
  const kind = $("#ob-guide-kind").value.trim(); if (!kind) { toast("Say which kind of controller the guide is for.", "warn"); return; }
  toast(`Drafting a manifest for ${kind} from ${f.name}…`);
  try { await api("/network/guides", { method: "POST", body: { kind, name: f.name, content_base64: await fileB64(f) } }); toast(`A manifest for ${kind} was drafted; pick it under Kind.`, "ok"); loadKinds(); }
  catch (err) { toast(err.message, "bad"); }
});
function controllerDetail(key) {
  const c = (state.network?.controllers || []).find((x) => x.key === key); if (!c) return;
  const t = (state.topology?.controllers || []).find((x) => x.key === key);
  $("#ctl-detail-title").textContent = c.label || key;
  const doms = c.conformance?.domains || [];
  $("#ctl-detail").innerHTML = `${t && !t.ok ? `<div class="banner bad">Cannot be read: ${esc(t.error || "")}</div>` : ""}
    <div class="kv"><div>domain</div><div>${esc(c.domain || "–")}</div><div>verdict</div><div>${esc(c.conformance?.verdict || "–")}</div>
    <div>lacking</div><div>${esc((c.conformance?.lacking || []).join(", ") || "nothing")}</div></div>
    ${doms.length ? `<table class="tbl" style="margin-top:10px"><thead><tr><th>part of the contract</th><th>status</th><th>verbs</th></tr></thead><tbody>${doms.map((d) => `<tr><td>${esc(d.domain)}</td><td><span class="tag ${d.status === "covered" ? "ok" : d.status === "missing" ? "bad" : "warn"}">${esc(d.status)}</span></td><td style="font-size:11.5px">${Object.entries(d.features || {}).map(([v, h]) => `${esc(v.replace(/_/g, " "))}: <span class="mono">${esc(String(h).replace(/^tool /, ""))}</span>`).join("<br>")}</td></tr>`).join("")}</tbody></table>` : ""}
    <div class="row" style="margin-top:12px"><button class="btn sm" id="cd-topo">Show on the topology</button><button class="btn sm" id="cd-ask">Ask CANAH about it</button>
      <div style="flex:1"></div><button class="btn sm danger" id="cd-remove" ${levelRank(state.level) >= 2 ? "" : "disabled title='root only'"}>Remove</button></div>`;
  $("#cd-remove").onclick = async () => {
    if (!confirm(`Remove ${c.label || key} from the agentic MCP? Its manifest and seat go; the controller itself is not touched.`)) return;
    try { await api(`/network/controllers/${encodeURIComponent(key)}`, { method: "DELETE" }); toast(`${key} removed. Reload CANAH to drop its agent.`, "ok"); renderControllers(); loadTopology(true); }
    catch (e) { toast(e.message, "bad"); }
  };
  $("#cd-topo").onclick = () => { const id = `ctl:${key}`; topo.open[id] = true; store.set("canah.topo.open", topo.open); layout(); state.selected = id; go("topology"); renderDetail(id); fitTopology(); };
  $("#cd-ask").onclick = () => { go("chat"); $("#composer").value = `Give me an overview of ${c.label || key}: its devices, links and anything unhealthy.`; $("#composer").focus(); };
}

/* ================= chat (LangGraph agent-chat-ui style) ================= */
const threadsKey = () => `canah.threads.${state.user}`;
const threads = () => store.get(threadsKey(), []);
function saveThread(id, title) {
  const old = threads().find((t) => t.id === id);
  const list = threads().filter((t) => t.id !== id);
  const keep = old && old.title && old.title !== "new thread" && old.title !== "attached files";
  list.unshift({ id, title: ((keep ? old.title : title) || "new thread").slice(0, 80), at: Date.now() / 1000 });
  store.set(threadsKey(), list.slice(0, 60)); loadThreads();
}
function loadThreads() {
  const box = $("#thread-list"); if (!box) return;
  const list = threads();
  box.innerHTML = list.length ? list.map((t) => `<button class="lg-thread${t.id === state.chat.thread ? " on" : ""}" data-id="${esc(t.id)}">
      <span class="t">${esc(t.title)}</span><span class="x" data-x="${esc(t.id)}" title="Forget this thread here">×</span><span class="w">${esc(ago(t.at))}</span></button>`).join("")
    : `<div class="empty" style="font-size:12.5px">No threads yet. Ask something to start one.</div>`;
  $$(".lg-thread", box).forEach((b) => b.addEventListener("click", (e) => {
    if (e.target.dataset.x) { store.set(threadsKey(), threads().filter((t) => t.id !== e.target.dataset.x)); if (state.chat.thread === e.target.dataset.x) openThread(""); else loadThreads(); return; }
    openThread(b.dataset.id);
  }));
  const cur = list.find((t) => t.id === state.chat.thread);
  $("#lg-title").textContent = cur ? cur.title : "new thread";
}
$("#thread-new").addEventListener("click", () => openThread(""));
$("#threads-toggle").addEventListener("click", () => {
  if (narrow()) { $("#lg").classList.toggle("threads-open"); return; }
  $("#lg").classList.toggle("no-threads"); store.set("canah.threads-hidden", $("#lg").classList.contains("no-threads"));
});
if (store.get("canah.threads-hidden")) $("#lg").classList.add("no-threads");

// The chat in one view: drag the border of the thread list or of the call flow to widen or narrow it (double-click
// a border to put it back); the conversation keeps at least 420 px. Widths are remembered.
(function wireSplits() {
  const lg = $("#lg"); if (!lg) return;
  const W = { tw: [180, 480, 260], fw: [260, 760, 380] };
  const set = (k, v) => { const [lo, hi] = W[k]; v = Math.round(Math.min(hi, Math.max(lo, v))); lg.style.setProperty(`--${k}`, `${v}px`); return v; };
  for (const k of ["tw", "fw"]) { const v = store.get(`canah.chat.${k}`); if (v) set(k, v); }
  const mk = (k) => {
    const h = document.createElement("div"); h.className = `lg-split ${k}`; h.title = "Drag to resize; double-click to reset";
    h.setAttribute("role", "separator"); h.setAttribute("aria-orientation", "vertical"); lg.append(h);
    h.addEventListener("dblclick", () => { lg.style.removeProperty(`--${k}`); store.del(`canah.chat.${k}`); });
    h.addEventListener("pointerdown", (e) => {
      e.preventDefault(); h.setPointerCapture(e.pointerId); document.body.classList.add("resizing");
      const r = lg.getBoundingClientRect();
      const move = (ev) => {
        const other = parseFloat(getComputedStyle(lg).getPropertyValue(k === "tw" ? "--fw" : "--tw")) || W[k === "tw" ? "fw" : "tw"][2];
        const room = r.width - 420 - (k === "tw" ? (document.body.classList.contains("side-hidden") ? 0 : other) : (lg.classList.contains("no-threads") ? 0 : other));
        const want = k === "tw" ? ev.clientX - r.left : r.right - ev.clientX;
        store.set(`canah.chat.${k}`, set(k, Math.min(want, room)));
      };
      const up = () => { h.removeEventListener("pointermove", move); h.removeEventListener("pointerup", up); document.body.classList.remove("resizing"); };
      h.addEventListener("pointermove", move); h.addEventListener("pointerup", up);
    });
  };
  mk("tw"); mk("fw");
})();
$("#hide-tools").addEventListener("change", (e) => { document.body.classList.toggle("hide-tools", e.target.checked); store.set("canah.hide-tools", e.target.checked); });
if (store.get("canah.hide-tools")) { $("#hide-tools").checked = true; document.body.classList.add("hide-tools"); }

const SUGGEST = ["Which controllers do you hold, and are they healthy?", "Show me the topology of each network.", "Are any links down right now?",
  "What services are provisioned, and where?", "What can each controller do, by the common contract?"];
const logoSrc = () => document.documentElement.dataset.theme === "light" ? "/static/logo-light.png" : "/static/logo-dark.png";
const col = () => $("#messages");
// The chat follows the answer as it is written only while the reader is at the bottom: scroll up to read
// something earlier and it stays there, with a button back to the latest.
const chatScroll = { stick: true };
const scrollDown = (force = false) => {
  const s = $("#lg-scroll"); if (!s) return;
  if (force) chatScroll.stick = true;
  if (chatScroll.stick) { s.scrollTop = s.scrollHeight; $("#jump-latest")?.classList.remove("show"); }
  else $("#jump-latest")?.classList.add("show");
};
(function wireChatScroll() {
  const s = $("#lg-scroll"); if (!s) return;
  const btn = document.createElement("button"); btn.type = "button"; btn.id = "jump-latest"; btn.className = "jump-latest";
  btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12l7 7 7-7"/></svg>Jump to latest`;
  btn.addEventListener("click", () => scrollDown(true));
  s.parentElement.append(btn);
  s.addEventListener("scroll", () => {
    const near = s.scrollHeight - s.scrollTop - s.clientHeight < 80;
    chatScroll.stick = near; if (near) btn.classList.remove("show");
  }, { passive: true });
  // Copy a code block, or switch a diagram to its code and back (delegated: answers are re-drawn as they stream).
  $("#messages").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-code-act]"); if (!b) return;
    const box = b.closest(".code-box"), code = box?.querySelector("code")?.textContent || box?.dataset.src || "";
    const act = b.dataset.codeAct;
    if (act === "copy") { try { await navigator.clipboard.writeText(code); } catch (err) { const t = document.createElement("textarea"); t.value = code; document.body.append(t); t.select(); document.execCommand("copy"); t.remove(); }
      b.textContent = "Copied"; setTimeout(() => { b.textContent = "Copy"; }, 1400); }
    if (act === "toggle") { box.classList.toggle("show-code"); b.textContent = box.classList.contains("show-code") ? (box.classList.contains("folded") ? "Hide code" : "Diagram") : "Code"; }
    if (act === "svg" || act === "png") downloadDiagram(box, act);
  });
})();
function welcome() {
  return `<div class="welcome"><img src="${logoSrc()}" alt=""><h3>Controller Agnostic Network Agent Harness</h3>
    <p>Ask about any network the agentic MCP holds, attach a file for it to read, or ask for a change: it waits for your approval${state.level === "observer" ? " (sign in as admin or root to approve)" : ""}.</p></div>`;
}
async function openThread(id) {
  if (state.chat.busy && id !== state.chat.thread) { toast("Wait for the answer to finish before switching threads.", "warn"); return; }
  state.chat.thread = id; state.chat.rendered = id; state.chat.files = []; renderFiles(); loadThreads();
  $("#lg").classList.remove("threads-open");
  const box = col(); box.innerHTML = "";
  $("#suggest").innerHTML = SUGGEST.map((s) => `<button type="button">${esc(s)}</button>`).join("");
  $$("#suggest button").forEach((b) => b.addEventListener("click", () => { $("#composer").value = b.textContent; send(); }));
  renderAgentStrip();
  if (!id) { box.innerHTML = welcome(); showUsage(null); refreshGalleryButton(); return; }
  try {
    const h = await api(`/threads/${id}/history`);
    showUsage(h.context ? { context: h.context } : null);
    if (!h.messages.length) box.innerHTML = welcome();
    let turn = null, asked = "";
    const finish = (t) => { if (!t) return; t.flow.done = true; t.flow.ms = null; t.done(false); };
    for (const m of h.messages) {
      if (m.role === "user") { finish(turn); addUser(m.text); asked = String(m.text).split(ATT)[0]; turn = null; continue; }
      turn = turn || addAgent(asked);
      if (m.role === "tool") {
        const c = turn.tool(m.name, m.args, false); if (m.result != null) turn.result(c, m.result, m.ms);
        const st = flowCall(turn.flow, m.name, m.args || {}); st.ms = m.ms ?? null; turn.flow.open.pop(); st.result = m.result;
        if (st.to) turn.flow.delegating.pop();
      } else { turn.text(m.text); turn.flow.reply = m.text; }
    }
    finish(turn);
    if (turn) showFlow(turn);
    const p = await api(`/threads/${id}/pending`);
    if (p.pending?.length) { const t = turn || addAgent(); showPending(t, p.pending); t.done(false); }
  } catch (e) { box.innerHTML = welcome(); }
  scrollDown(true);
}
function renderAgentStrip(active = new Set()) {
  const subs = agentsDetail();
  $("#agents-strip").innerHTML = [`<span class="agent-chip${active.has("main") ? " active" : ""}" style="--c:var(--accent)" title="CANAH, the main agent"><span class="dot"></span>CANAH</span>`,
    ...subs.map((s) => `<span class="agent-chip${active.has(s.name) ? " active" : ""}" style="--c:${agentColor(s.name)}" title="${esc(`${s.name} · ${AGENT_KIND[s.kind] || "agent"}${s.controller ? `: works ${s.label || s.controller} through the agentic MCP` : ""}`)}"><span class="dot"></span>${esc(s.name)}</span>`)].join("");
}

const ATT = "\n\n[attachments]\n";
function addUser(text) {
  const box = col(); $(".welcome", box)?.remove();
  const [words, att] = String(text).split(ATT);
  const files = att ? [...att.matchAll(/^- (\/uploads\/\S+)/gm)].map((m) => m[1].split("/").pop()) : [];
  const m = document.createElement("div"); m.className = "lg-user";
  m.innerHTML = `${files.length ? `<div class="lg-files">${files.map((f) => `<span class="fchip"><span class="nm"><i class="ficon"></i>${esc(f)}</span></span>`).join("")}</div>` : ""}<div class="b"></div>`;
  $(".b", m).textContent = words; if (!words.trim()) $(".b", m).remove();
  box.append(m); scrollDown(true);
}
function kvTable(obj) {
  const rows = Object.entries(obj || {});
  if (!rows.length) return `<table class="lg-kv"><tr><td class="faint" colspan="2">no arguments</td></tr></table>`;
  return `<table class="lg-kv">${rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(typeof v === "string" ? v : JSON.stringify(v))}</td></tr>`).join("")}</table>`;
}
/* ---------- the agent call flow, on the right ---------- */
const AGENT_COLORS = ["#3d8bff", "#9b51c0", "#3dc46b", "#f6a52c", "#ff6b6b", "#89ddff", "#c792ea", "#f78c6c"];
function agentColor(a) {
  if (a === "CANAH") return "var(--accent)";
  const ck = ctlOfAgent(a), ctl = ck && (state.topology?.controllers || []).find((c) => c.key === ck);
  if (ctl) return DOMAIN_COLOR[domainClass(ctl.domain)];
  if (ck) return DOMAIN_COLOR[domainClass(agentsDetail().find((x) => x.name === a)?.domain || "other")];
  const sd = agentsDetail().find((x) => x.name === a);
  if (sd?.kind === "config") return DOMAIN_COLOR[domainClass(sd.domain)];
  if (sd?.kind === "qot") return "#2fd3c1";
  const subs = state.info?.subagents || [];
  return AGENT_COLORS[(subs.indexOf(a) + 1) % AGENT_COLORS.length];
}
function newFlow(question) { return { question, steps: [], open: [], delegating: [], t0: performance.now(), done: false }; }
// Who took a step: a subagent when its tool carries the subagent's prefix (onos__…), or while a delegation to it
// (task) is still open; CANAH otherwise.
function flowCall(flow, name, args) {
  const prefix = String(name).split("__")[0];
  let agent = "CANAH";
  if (String(name).includes("__") && agentFor(prefix)) agent = agentFor(prefix);
  else if (flow.delegating.length && name !== "task") agent = flow.delegating[flow.delegating.length - 1];
  const step = { kind: name === "task" ? "delegate" : "call", agent, name, args, t: performance.now(), ms: null, result: null,
                 to: name === "task" ? (args?.subagent_type || "general-purpose") : null };
  if (step.to) flow.delegating.push(step.to);
  flow.steps.push(step); flow.open.push(step);
  return step;
}
function flowResult(flow, name, content, ms) {
  const k = flow.open.findIndex((o) => o.name === name);
  const step = k >= 0 ? flow.open.splice(k, 1)[0] : null;
  if (!step) return;
  step.ms = ms != null ? ms : performance.now() - step.t; step.result = content;
  if (step.to) { const i = flow.delegating.lastIndexOf(step.to); if (i >= 0) flow.delegating.splice(i, 1); }
}
const fmtDur = (ms) => ms == null ? "" : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
const brief = (v, n = 160) => { const t = typeof v === "string" ? v : JSON.stringify(v); return t.length > n ? t.slice(0, n) + "…" : t; };
function renderFlow(turn) {
  const body = $("#flow-body"); if (!body || !turn) return;
  if (state.flowTurn !== turn) return;
  const f = turn.flow;
  $$(".lg-ai.traced").forEach((x) => x.classList.remove("traced")); turn.el.classList.add("traced");
  $("#flow-of").textContent = f.question ? `“${f.question.slice(0, 60)}”` : "";
  const counts = {}; for (const s2 of f.steps) counts[s2.agent] = (counts[s2.agent] || 0) + 1;
  const total = f.done ? (f.ms != null ? fmtDur(f.ms) : "") : "running…";
  const step = (cls, color, who, what, t, detail, extra = "") =>
    `<div class="fl-step ${cls}" style="--c:${color}"><span class="fl-dot"></span><div class="fl-box"><div class="fl-h"><span class="fl-who">${esc(who)}</span><span class="fl-what">${esc(what)}</span><span class="fl-t">${esc(t)}</span></div>${detail ? `<div class="fl-d">${esc(detail)}</div>` : ""}${extra}</div></div>`;
  let html = `<div class="fl-sum"><span>${f.steps.length} step${f.steps.length === 1 ? "" : "s"}</span>${Object.entries(counts).map(([a, n]) => `<span style="color:${agentColor(a)}">● ${esc(a)} ${n}</span>`).join("")}<span>${esc(total)}</span></div><div class="fl-turn">`;
  html += step("ask", "var(--faint)", "you", "asked", "", f.question || "");
  for (const s2 of f.steps) {
    const det = `<details><summary>arguments${s2.result != null ? " and result" : ""}</summary><pre>${esc(JSON.stringify(s2.args || {}, null, 1))}</pre>${s2.result != null ? `<pre>${esc(String(s2.result).slice(0, 3000))}</pre>` : ""}</details>`;
    const failed = s2.result != null && /^(error|not sent)/i.test(String(s2.result));
    const running = s2.ms == null && !f.done;
    if (s2.kind === "delegate")
      html += step(running ? "run" : "", agentColor(s2.agent), s2.agent, `→ ${s2.to}`, fmtDur(s2.ms), s2.args?.description ? brief(s2.args.description, 200) : "", det);
    else
      html += step(running ? "run" : "", failed ? "var(--bad)" : agentColor(s2.agent), s2.agent, s2.name, running ? "…" : fmtDur(s2.ms),
                   s2.result != null ? brief(s2.result) : brief(s2.args || {}, 120), det);
  }
  if (f.pending?.length) html += step("", "var(--warn)", "operator", `approval: ${f.pending.map((x) => x.name).join(", ")}`, "", "waiting for a decision");
  if (f.done) html += step("reply", "var(--accent)", "CANAH", f.error ? "failed" : "answered", "", f.error || brief(f.reply || "", 220));
  html += "</div>";
  body.innerHTML = html;
}
function showFlow(turn) { state.flowTurn = turn; renderFlow(turn); }

// The context window and the KV cache, after each reply (canah/window.py).
function showUsage(u) {
  const el = $("#ctx-meter"); if (!el) return;
  const c = u?.context, k = u?.kv_cache;
  if (!c && !k) { el.hidden = true; return; }
  const ctx = state.info?.context || {}, at = Math.round((ctx.compact_at || 0.85) * 100);
  const pct = c?.percent;
  el.hidden = false;
  el.className = "ctx-meter" + (pct == null ? "" : pct >= at ? " full" : pct >= at * 0.75 ? " warn" : "");
  const n = (x) => x == null ? "–" : Number(x).toLocaleString();
  el.innerHTML = (pct != null ? `<span class="ctx-bar"><i style="width:${Math.min(100, pct)}%"></i><b style="left:${at}%"></b></span><span>context ${pct}%</span>` : "")
    + (k ? `<span class="ctx-sep ctx-kv">·</span><span class="ctx-kv">KV cache ${k.exact ? "" : "≈"}${k.percent}%</span>` : "");
  el.title = (c ? `Context window: ${n(c.used)} of ${n(c.window)} tokens (${pct}%). At ${at}% the older part of the conversation is summarised automatically; ask to compact (or type /compact) to do it sooner.` : "")
    + (k ? `\nKV cache this turn: ${n(k.cached_tokens)} of ${n(k.prompt_tokens)} prompt tokens reused rather than read again${k.exact ? "" : " (estimated from Ollama's reading time)"}.` : "");
  if (u?.compacted) toast("The conversation was compacted: the older part is summarised, the recent part kept as it is.", "info");
}

// What CANAH is doing, in words, while it works: the tool it is waiting on, or a thought between steps.
const THINKING = ["Thinking", "Reading the question", "Working out the next step", "Weighing what it found", "Putting the answer together"];
function doingWhat(name, args) {
  const n = String(name || ""), sub = n === "task" ? args?.subagent_type : agentFor(n.split("__")[0]);
  if (n === "task") return `Asking ${sub || "a subagent"}`;
  if (/^search_knowledge|^research_knowledge/.test(n)) return "Searching the documents";
  if (/^twin_predict/.test(n)) return `Rehearsing it in ${twinLabel(state.twin) || "the digital twin"}`;
  if (/^twin_check_write/.test(n)) return `Checking the change in ${twinLabel(state.twin) || "the digital twin"} before it is sent`;
  if (/^twin_/.test(n)) return `Asking ${twinLabel(state.twin) || "the digital twin"} (digital twin)`;
  if (/^net_/.test(n)) { const v = n.slice(4).replace(/_/g, " "); return args?.controller ? `${v[0].toUpperCase() + v.slice(1)} on ${args.controller}, through the agentic MCP` : `${v[0].toUpperCase() + v.slice(1)} on every controller that serves it`; }
  if (n === "controller_read" || n === "controller_write") return `${n === "controller_write" ? "Changing" : "Reading"} ${args?.controller || "a controller"} (${args?.tool || "a tool"}) through the agentic MCP`;
  if (n === "network_tools") return "Looking up the controllers' tools";
  if (/^network_|follow_relations/.test(n)) return "Reading the networks through the agentic MCP";
  if (n.includes("__")) return `${sub || n.split("__")[0]} is working ${n.split("__")[0]} through the agentic MCP`;
  if (n === "execute") return "Running a command in the sandbox";
  return `Running ${n}`;
}
function addAgent(question = "") {
  const box = col(); $(".welcome", box)?.remove();
  const m = document.createElement("div"); m.className = "lg-ai";
  m.innerHTML = `<div class="av"><img src="${logoSrc()}" alt=""></div><div class="segs"></div>`;
  box.append(m);
  const segs = $(".segs", m);
  let last = null;                                     // the text segment the tokens go into
  const working = () => $(".working", segs);
  const t0 = performance.now();
  const timing = { t0, first: null, end: null, tools: 0 };
  const running = new Set();                           // tool cards whose result has not come back
  let phrase = 0, since = t0, label = "";
  // One status line at the end: what CANAH is doing and for how long, live.
  const think = (what) => {
    working()?.remove();
    const w = document.createElement("div"); w.className = "working";
    label = what || ""; since = performance.now(); phrase = 0;
    w.innerHTML = `<span class="bars"><i></i><i></i><i></i></span><span class="w-what"></span><span class="w-t"></span>`;
    segs.append(w); paint(); scrollDown();
  };
  const paint = () => {
    const w = working(); const now = performance.now();
    if (w) {
      const txt = label || THINKING[Math.floor((now - since) / 2600) % THINKING.length];
      $(".w-what", w).textContent = `${txt}…`;
      $(".w-t", w).textContent = fmtDur(now - since);
    }
    for (const c of running) { const l = $(".lat", c); if (l) l.textContent = fmtDur(now - c._t0); }
  };
  const tick = setInterval(paint, 100);
  think("");
  const turn = {
    el: m, segs, raw: "", flow: newFlow(question), timing,
    token(t) { if (timing.first == null) timing.first = performance.now() - t0; working()?.remove(); if (!last) { last = document.createElement("div"); last.className = "reply-rich"; last._raw = ""; segs.append(last); } last._raw += t; last.innerHTML = md(last._raw); scrollDown(); },
    text(t) { if (!t) return; working()?.remove(); last = document.createElement("div"); last.className = "reply-rich"; last._raw = t; last.innerHTML = md(t); segs.append(last); drawDiagrams(last); },
    final(t) {     // the reply as the agent ended it: replaces what streamed after the last tool card
      working()?.remove();
      if (!t) return;
      if (last && last.parentNode === segs && segs.lastElementChild === last) { last._raw = t; last.innerHTML = md(t); }
      else turn.text(t);
      drawDiagrams(segs);
    },
    tool(name, args, live = true) {
      working()?.remove(); last = null;
      const write = (state.info?.interrupt_on || []).includes(name);
      const c = document.createElement("div"); c.className = `lg-tool${write ? " write" : ""}`;
      const by = name === "task" ? "" : args?.controller ? `on ${args.controller}` : (String(name).includes("__") ? agentFor(String(name).split("__")[0]) : "");
      c.innerHTML = `<div class="lg-tool-h"><span class="n">${esc(name)}</span><span class="k">${write ? `change${by ? " " + esc(by) : ""}` : name === "task" ? `delegated to ${esc(args?.subagent_type || "an agent")}` : by ? esc(by.startsWith("on ") ? by : `by ${by}`) : "tool call"}</span><span class="lat" title="how long it took"></span><span class="st">${live ? `<span class="dot busy"></span>` : ""}</span></div>${kvTable(args)}`;
      segs.append(c);
      if (live) { c._t0 = performance.now(); running.add(c); timing.tools += 1; think(doingWhat(name, args)); }
      scrollDown(); return c;
    },
    result(c, content, ms) {
      if (!c) return;
      if ($(".n", c)?.textContent === "draw_diagram") {
        try { const j = JSON.parse(content); if (j.ok && DG_ID.test(j.id)) (turn.diagrams = turn.diagrams || []).push({ id: j.id, title: j.title || "" }); } catch (e) { /* not a picture */ }
      }
      if (running.has(c)) { running.delete(c); c._ms = ms != null ? ms : performance.now() - c._t0; }
      else if (ms != null) c._ms = ms;
      const lat = $(".lat", c); if (lat) { lat.textContent = c._ms != null ? fmtDur(c._ms) : ""; lat.classList.toggle("slow", (c._ms || 0) > 5000); }
      $(".st", c).innerHTML = /^error/i.test(String(content)) ? `<span class="tag bad">error</span>` : `<span class="tag ok">done</span>`;
      const r = document.createElement("div"); r.className = "lg-res";
      let body; try { const j = JSON.parse(content); body = j && typeof j === "object" && !Array.isArray(j) && Object.keys(j).length <= 14 ? kvTable(j) : `<pre>${esc(JSON.stringify(j, null, 2))}</pre>`; }
      catch (e) { body = `<pre>${esc(content)}</pre>`; }
      const long = String(content).length > 500;
      r.innerHTML = `<div class="h">tool result${long ? `<button type="button">Expand</button>` : ""}</div>${body}`;
      if (long) $("button", r).addEventListener("click", (e) => { r.classList.toggle("open"); e.target.textContent = r.classList.contains("open") ? "Collapse" : "Expand"; });
      c.append(r);
      if (c._t0 != null && !running.size) think(`Reading what ${$(".n", c)?.textContent || "the tool"} returned`);
    },
    done(live = true) {
      clearInterval(tick); working()?.remove();
      const missing = (turn.diagrams || []).filter((d) => !segs.querySelector(`.dg-fig[data-dg="${d.id}"]`));
      if (missing.length) {
        const div = document.createElement("div"); div.className = "reply-rich"; div.innerHTML = missing.map((d) => dgFigure(d.id, d.title)).join("");
        segs.append(div); drawDiagrams(segs);
      }
      refreshGalleryButton();
      if (live && timing.end == null) timing.end = performance.now() - t0;
      if (!$(".acts", segs)) {
        const a = document.createElement("div"); a.className = "acts";
        const lat = live && timing.end != null ? `<span class="lat-final" title="from your message to the end of the answer">answered in ${fmtDur(timing.end)}${timing.first != null ? ` · first words after ${fmtDur(timing.first)}` : ""}${timing.tools ? ` · ${timing.tools} tool call${timing.tools > 1 ? "s" : ""}` : ""}</span>` : "";
        a.innerHTML = `<button type="button" title="Copy the answer">Copy</button>${lat}`;
        $("button", a).addEventListener("click", () => { navigator.clipboard?.writeText($$(".reply-rich", segs).map((x) => x._raw || x.textContent).join("\n\n")); toast("Copied.", "ok"); });
        segs.append(a);
      }
    },
  };
  m.addEventListener("click", (e) => { if (!e.target.closest("button, a, textarea, input, summary")) showFlow(turn); });
  showFlow(turn);
  return turn;
}

async function stream(path, body, turn) {
  setBusy(true);
  const active = new Set(["main"]); renderAgentStrip(active);
  const open = [];                                     // tool cards waiting for their result, in order
  try {
    const r = await api(path, { method: "POST", body, raw: true });
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        const ev = (chunk.match(/^event: (.*)$/m) || [])[1]; const data = (chunk.match(/^data: (.*)$/m) || [])[1];
        if (!ev || !data) continue;
        const d = JSON.parse(data);
        if (ev === "token") turn.token(d.text);
        else if (ev === "tool_call") {
          const sub = d.name === "task" ? d.args?.subagent_type : agentFor(String(d.name).split("__")[0]);
          if (/^net_|^controller_|^network_|follow_relations/.test(d.name)) active.add(d.args?.controller ? `ctl:${d.args.controller}` : "mcp");
          if (/^twin_/.test(d.name)) active.add("twin");
          if (sub && (state.info?.subagents || []).includes(sub)) active.add(sub);
          renderAgentStrip(active); noteActivity(active);
          open.push({ name: d.name, card: turn.tool(d.name, d.args || {}) });
          flowCall(turn.flow, d.name, d.args || {}); renderFlow(turn);
        } else if (ev === "tool_result") {
          const k = open.findIndex((o) => o.name === d.name); const o = k >= 0 ? open.splice(k, 1)[0] : open.shift();
          turn.result(o?.card, d.content, d.ms);
          flowResult(turn.flow, d.name, d.content, d.ms); renderFlow(turn);
        } else if (ev === "reply") {
          if (d.thread_id && !state.chat.thread) state.chat.thread = d.thread_id;
          turn.final(d.text || "");
          showUsage(d.usage);
          turn.flow.reply = d.text || ""; turn.flow.pending = d.pending || [];
          if (d.pending?.length) showPending(turn, d.pending);
        } else if (ev === "error") { turn.text(`**Error:** ${d.error}`); turn.flow.error = d.error; }
      }
    }
  } catch (e) { turn.text(`**Error:** ${e.message}`); turn.flow.error = e.message; }
  finally { turn.flow.done = true; turn.flow.ms = performance.now() - turn.flow.t0; renderFlow(turn); turn.done(); setBusy(false); renderAgentStrip(); noteActivity(new Set()); refreshPendingBadge(); loadThreads(); }
}
function setBusy(b) { state.chat.busy = b; $("#st-busy").hidden = !b; $("#send").disabled = b; if (state.view === "topology") renderTopology(); }
function noteActivity(active) { state.localActive = active; if (state.view === "topology") renderTopology(); }

async function ensureThread() {
  if (!state.chat.thread) { state.chat.thread = (await api("/threads", { method: "POST" })).thread_id; state.chat.rendered = state.chat.thread; saveThread(state.chat.thread, "new thread"); }
  return state.chat.thread;
}
async function send() {
  const ta = $("#composer"); const words = ta.value.trim();
  if ((!words && !state.chat.files.length) || state.chat.busy) return;
  if (state.chat.files.some((f) => f.busy)) { toast("Wait for the uploads to finish.", "warn"); return; }
  const id = await ensureThread();
  let text = words || "Read the attached file(s) and tell me what they contain.";
  if (state.chat.files.length) {
    let budget = 10000;
    text += ATT + state.chat.files.map((f) => {
      const head = `- ${f.path} (${fmtBytes(f.bytes)}, ${f.read_as}${f.sandbox_path ? `; also ${f.sandbox_path} in the sandbox` : ""})`;
      if (!f.preview) return head + ": read it with read_file";
      const take = f.preview.slice(0, Math.max(0, Math.min(4000, budget))); budget -= take.length;
      return `${head}: read it with read_file; its beginning:\n--- ${f.name} ---\n${take}${f.text_chars > take.length ? "\n[… more in the file]" : ""}\n--- end of ${f.name} ---`;
    }).join("\n");
  }
  ta.value = ""; ta.style.height = ""; state.chat.files = []; renderFiles();
  saveThread(id, words || "attached files");
  addUser(text);
  await stream(`/threads/${id}/stream`, { text }, addAgent(words || "attached files"));
}
$("#composer-form").addEventListener("submit", (e) => { e.preventDefault(); send(); });
$("#composer").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
$("#composer").addEventListener("input", (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(200, e.target.scrollHeight) + "px"; });

/* attachments */
const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : n < 1073741824 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1073741824).toFixed(1)} GB`;
state.chat.files = [];
function renderFiles() {
  const box = $("#files"); if (!box) return;
  box.innerHTML = state.chat.files.map((f, i) => `<span class="fchip${f.busy ? " busy" : ""}" title="${esc(f.path || "uploading")}"><span class="nm"><i class="ficon"></i>${esc(f.name)}</span><span class="sz">${f.busy ? "uploading…" : fmtBytes(f.bytes)}</span><button type="button" data-i="${i}" title="Remove">×</button></span>`).join("");
  $$("button[data-i]", box).forEach((b) => b.addEventListener("click", () => { state.chat.files.splice(+b.dataset.i, 1); renderFiles(); }));
}
async function attach(files) {
  const id = await ensureThread();
  for (const file of files) {
    if (file.size > 25 * 1024 * 1024) { toast(`${file.name} is over 25 MB.`, "bad"); continue; }
    const f = { name: file.name, bytes: file.size, busy: true }; state.chat.files.push(f); renderFiles();
    try {
      const b64 = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(file); });
      Object.assign(f, await api(`/threads/${id}/files`, { method: "POST", body: { name: file.name, content_base64: b64 } }), { busy: false });
      if (!f.text_chars) toast(`${file.name}: kept as bytes; the model cannot read it as text.`, "warn");
    } catch (e) { state.chat.files.splice(state.chat.files.indexOf(f), 1); toast(`${file.name}: ${e.message}`, "bad"); }
    renderFiles();
  }
}
$("#attach").addEventListener("change", (e) => { attach([...e.target.files]); e.target.value = ""; });
$("#lg-scroll").addEventListener("dragover", (e) => e.preventDefault());
$("#lg-scroll").addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer.files.length) attach([...e.dataTransfer.files]); });
$("#composer").addEventListener("paste", (e) => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); attach(fs); } });

/* an interrupt: accept, edit or reject each waiting change */
function showPending(turn, pending) {
  const may = pending.every((p) => p.you_may_approve !== false && levelRank(state.level) >= levelRank(p.needs || "admin"));
  const card = document.createElement("div"); card.className = "lg-interrupt";
  card.innerHTML = `<div class="h"><span class="k">interrupt</span>Approval needed · ${pending.length} change${pending.length > 1 ? "s" : ""}</div>
    ${pending.map((p, i) => `<div class="chg" data-i="${i}"><div class="row" style="gap:8px"><b class="mono">${esc(p.name)}</b><span class="tag ${p.needs === "root" ? "bad" : "warn"}">needs ${esc(p.needs || "admin")}</span></div>
      ${p.description ? `<div class="muted" style="font-size:12px">${esc(p.description)}</div>` : ""}<div class="args">${kvTable(p.args)}</div></div>`).join("")}
    <div class="acts">${may ? `<button class="btn ok" data-d="approve">Accept</button><button class="btn" data-d="edit">Edit</button>`
      : `<span class="muted" style="font-size:12px">${state.level === "observer" ? "Sign in as admin or root to accept." : "This needs root to accept."}</span>`}
      <button class="btn danger" data-d="reject">Reject</button></div>`;
  turn.segs.append(card); scrollDown();
  const finish = async (decisions, label) => {
    $$("button", card).forEach((x) => x.disabled = true); card.classList.add("done");
    $(".h", card).innerHTML = `<span class="k">interrupt</span>${esc(label)}`;
    await stream(`/threads/${state.chat.thread}/decide`, { decisions, stream: true }, addAgent(`decision: ${label}`));
  };
  $$("button[data-d]", card).forEach((b) => b.addEventListener("click", async () => {
    if (b.dataset.d === "approve") return finish(pending.map(() => ({ type: "approve" })), "Accepted");
    if (b.dataset.d === "reject") { const why = prompt("Why is it rejected? (the agent is told)", "not now"); if (why === null) return; return finish(pending.map(() => ({ type: "reject", message: why || "rejected" })), "Rejected"); }
    if (!card.classList.contains("editing")) {            // first click: the arguments become editable
      card.classList.add("editing"); b.textContent = "Send edited";
      $$(".chg", card).forEach((c) => { const p = pending[+c.dataset.i]; $(".args", c).innerHTML = `<textarea spellcheck="false">${esc(JSON.stringify(p.args || {}, null, 2))}</textarea>`; });
      return;
    }
    try {
      const decisions = $$(".chg", card).map((c) => ({ type: "edit", name: pending[+c.dataset.i].name, args: JSON.parse($("textarea", c).value) }));
      finish(decisions, "Edited and accepted");
    } catch (e) { toast(`The arguments are not valid JSON: ${e.message}`, "bad"); }
  }));
}
const levelRank = (l) => ({ observer: 0, admin: 1, root: 2 }[l] ?? 0);

/* A small markdown reader for replies: headings, lists, code, tables, bold, links. */
function md(src) {
  const lines = String(src).replace(/\r/g, "").split("\n"); const out = []; let i = 0;
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*(.+?)\*\*(?!\*)/g, "<b>$1</b>").replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, "$1<i>$2</i>")
    .replace(/!?\[([^\]]*)\]\(\/diagrams\/([0-9a-f]{16})\.(?:png|svg)\)/g, (m0, t, id) => dgFigure(id, t.replace(/\s*\((?:PNG|SVG)\)\s*$/i, "")))
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*```/.test(l)) {
      const lang = (l.trim().slice(3).trim().split(/\s+/)[0] || "").toLowerCase();
      const buf = []; i++; while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      const closed = i < lines.length; i++;
      const code = buf.join("\n");
      out.push(`<div class="code-box${lang === "mermaid" ? " mermaid-box" : ""}"${closed ? "" : ' data-open="1"'} data-lang="${esc(lang)}"><div class="code-h"><span class="lang">${esc(lang || "code")}</span><span class="sp"></span>${lang === "mermaid" ? `<button type="button" data-code-act="toggle" hidden>Code</button><button type="button" data-code-act="svg" hidden>SVG</button><button type="button" data-code-act="png" hidden>PNG</button>` : ""}<button type="button" data-code-act="copy">Copy</button></div><pre><code>${esc(code)}</code></pre></div>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(l) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      const row = (r) => r.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
      const head = row(l); i += 2; const body = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) body.push(row(lines[i++]));
      out.push(`<table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`); continue;
    }
    const h = l.match(/^(#{1,4})\s+(.*)$/); if (h) { out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`); i++; continue; }
    if (/^\s*[-*•]\s+/.test(l)) { const it = []; while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) it.push(lines[i++].replace(/^\s*[-*•]\s+/, "")); out.push(`<ul>${it.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`); continue; }
    if (/^\s*\d+[.)]\s+/.test(l)) {
      // One list across blank lines and indented lines under an item; it starts at the number written.
      const start = parseInt(l.trim(), 10) || 1; const it = [];
      while (i < lines.length) {
        if (/^\s*\d+[.)]\s+/.test(lines[i])) { it.push([lines[i++].replace(/^\s*\d+[.)]\s+/, "")]); continue; }
        if (it.length && /^\s{2,}\S/.test(lines[i])) { it[it.length - 1].push(lines[i++].trim().replace(/^[-*•]\s+/, "• ")); continue; }
        if (!lines[i].trim() && /^\s*\d+[.)]\s+/.test(lines.slice(i + 1).find((x) => x.trim()) || "")) { i++; continue; }
        break;
      }
      out.push(`<ol${start !== 1 ? ` start="${start}"` : ""}>${it.map((x) => `<li>${x.map(inline).join("<br>")}</li>`).join("")}</ol>`); continue;
    }
    if (!l.trim()) { i++; continue; }
    const p = []; while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*[-*•]\s|\s*\d+[.)]\s|\s*\|)/.test(lines[i])) p.push(lines[i++]);
    if (!p.length) p.push(lines[i++]);
    out.push(`<p>${p.map(inline).join("<br>")}</p>`);
  }
  return out.join("");
}

/* ---------- the pictures CANAH draws (draw_diagram): in the answer, in the thread's gallery, open to change ---------- */
const DG_ID = /^[0-9a-f]{16}$/;
function dgFigure(id, title) {
  if (!DG_ID.test(id)) return "";
  const t = esc(title || "Diagram");
  return `<figure class="dg-fig" data-dg="${id}" data-title="${t}"><button type="button" class="dg-img" data-dg-act="open" title="Open"><img src="/diagrams/${id}.png" alt="${t}" loading="lazy"></button>
    <figcaption><span class="t">${t}</span><span class="sp"></span><button type="button" data-dg-act="open">Open</button><a href="/diagrams/${id}.png" download="${id}.png">PNG</a><a href="/diagrams/${id}.svg" download="${id}.svg">SVG</a><button type="button" class="chg" data-dg-act="change">Suggest a change</button></figcaption></figure>`;
}
function threadDiagrams() {
  const seen = new Map();
  $$("#messages .dg-fig").forEach((f) => { if (!seen.has(f.dataset.dg)) seen.set(f.dataset.dg, f.dataset.title || "Diagram"); });
  return [...seen].map(([id, title]) => ({ id, title }));
}
function refreshGalleryButton() {
  const n = threadDiagrams().length, b = $("#dg-gallery-btn"); if (!b) return;
  b.hidden = !n; $(".n", b).textContent = n;
}
function suggestChange(id, title) {
  go("chat"); closeLightbox();
  const c = $("#composer"); c.value = `Change diagram ${id} ("${title}"): `; c.focus(); c.setSelectionRange(c.value.length, c.value.length);
  c.dispatchEvent(new Event("input"));
}
function lightbox(id, title) {
  let lb = $("#dg-lightbox");
  if (!lb) {
    lb = document.createElement("div"); lb.id = "dg-lightbox"; lb.className = "dg-lightbox"; lb.hidden = true;
    lb.innerHTML = `<div class="dg-lb-card" role="dialog" aria-modal="true"><div class="dg-lb-h"><b class="t"></b><span class="faint mono id"></span><span class="sp"></span>
      <a class="btn sm png" download>PNG</a><a class="btn sm svg" download>SVG</a><button type="button" class="btn sm primary chg">Suggest a change</button><button type="button" class="btn sm ghost x" aria-label="Close">✕</button></div>
      <div class="dg-lb-body"><img alt=""></div><div class="dg-lb-strip"></div></div>`;
    document.body.append(lb);
    lb.addEventListener("click", (e) => { if (e.target === lb || e.target.closest(".x")) closeLightbox(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !lb.hidden) closeLightbox(); });
    $(".chg", lb).addEventListener("click", () => suggestChange(lb.dataset.id, lb.dataset.title));
  }
  const all = threadDiagrams();
  lb.dataset.id = id; lb.dataset.title = title;
  $(".t", lb).textContent = title || "Diagram"; $(".id", lb).textContent = id;
  $("img", lb).src = `/diagrams/${id}.png`; $("img", lb).alt = title || "";
  $(".png", lb).href = `/diagrams/${id}.png`; $(".svg", lb).href = `/diagrams/${id}.svg`;
  $(".dg-lb-strip", lb).innerHTML = all.length > 1 ? all.map((d) => `<button type="button" class="${d.id === id ? "on" : ""}" data-id="${d.id}" data-title="${esc(d.title)}" title="${esc(d.title)}"><img src="/diagrams/${d.id}.png" alt=""></button>`).join("") : "";
  $$(".dg-lb-strip button", lb).forEach((b) => b.addEventListener("click", () => lightbox(b.dataset.id, b.dataset.title)));
  lb.hidden = false;
}
function closeLightbox() { const lb = $("#dg-lightbox"); if (lb) lb.hidden = true; }
(function wireDiagrams() {
  $("#messages").addEventListener("click", (e) => {
    const b = e.target.closest("[data-dg-act]"); if (!b) return;
    const f = b.closest(".dg-fig"); if (!f) return;
    if (b.dataset.dgAct === "open") lightbox(f.dataset.dg, f.dataset.title);
    if (b.dataset.dgAct === "change") suggestChange(f.dataset.dg, f.dataset.title);
  });
  const head = $(".lg-head"); if (!head) return;
  const btn = document.createElement("button"); btn.type = "button"; btn.id = "dg-gallery-btn"; btn.className = "btn sm ghost dg-gallery-btn"; btn.hidden = true;
  btn.title = "The pictures in this conversation";
  btn.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>Images <span class="n">0</span>`;
  btn.addEventListener("click", () => { const all = threadDiagrams(); if (all.length) lightbox(all[all.length - 1].id, all[all.length - 1].title); });
  head.append(btn);
})();

/* ---------- diagrams: a ```mermaid block is drawn (mermaid.js, served by CANAH itself) ---------- */
let mermaidReady = null;
function loadMermaid() {
  if (mermaidReady) return mermaidReady;
  mermaidReady = new Promise((res, rej) => {
    const sc = document.createElement("script"); sc.src = "/static/vendor/mermaid.min.js";
    sc.onload = () => { try { window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "default", fontFamily: "inherit", themeVariables: { fontSize: "14px" } }); res(window.mermaid); } catch (e) { rej(e); } };
    sc.onerror = () => { mermaidReady = null; rej(new Error("mermaid.js is not installed (start.sh fetches it into canah/static/vendor)")); };
    document.head.append(sc);
  });
  return mermaidReady;
}
let mermaidN = 0;
async function drawDiagrams(root) {
  refreshGalleryButton();
  const boxes = [...(root || document).querySelectorAll(".mermaid-box:not([data-open]):not(.drawn)")];
  if (!boxes.length) return;
  if ((root || document).querySelector(".dg-fig")) {
    // The drawn picture is in the answer: the text stays as code, folded, not drawn a second time.
    boxes.forEach((box) => { box.classList.add("drawn", "folded"); const t = box.querySelector("[data-code-act=toggle]"); if (t) { t.hidden = false; t.textContent = "Code"; } });
    return;
  }
  let mm; try { mm = await loadMermaid(); } catch (e) { boxes.forEach((b) => b.classList.add("drawn")); return; }
  for (const box of boxes) {
    box.classList.add("drawn");
    const src = box.querySelector("code").textContent; box.dataset.src = src;
    try {
      const { svg } = await mm.render(`mmd-${Date.now()}-${mermaidN++}`, src);
      const fig = document.createElement("div"); fig.className = "diagram"; fig.innerHTML = svg;
      box.querySelector(".code-h").after(fig);
      box.querySelectorAll("[data-code-act=toggle], [data-code-act=svg], [data-code-act=png]").forEach((b) => b.hidden = false);
    } catch (e) {
      const err = document.createElement("div"); err.className = "diagram-err"; err.textContent = `The diagram could not be drawn: ${String(e.message || e).split("\n")[0]}`;
      box.querySelector(".code-h").after(err);
    }
  }
}
function downloadDiagram(box, kind) {
  const svg = box.querySelector(".diagram svg"); if (!svg) return;
  const xml = new XMLSerializer().serializeToString(svg);
  const name = `canah-diagram-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}`;
  const save = (href, ext) => { const a = document.createElement("a"); a.href = href; a.download = `${name}.${ext}`; a.click(); };
  if (kind === "svg") { save(URL.createObjectURL(new Blob([xml], { type: "image/svg+xml" })), "svg"); return; }
  const img = new Image(), r = svg.getBoundingClientRect(), scale = 2;
  img.onload = () => {
    const c = document.createElement("canvas"); c.width = Math.ceil(r.width * scale); c.height = Math.ceil(r.height * scale);
    const g = c.getContext("2d"); g.fillStyle = getComputedStyle(document.body).getPropertyValue("--panel") || "#fff"; g.fillRect(0, 0, c.width, c.height);
    g.drawImage(img, 0, 0, c.width, c.height); save(c.toDataURL("image/png"), "png");
  };
  img.src = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(xml)));
}

/* ================= incidents ================= */
const SEV_CLASS = { critical: "bad", major: "bad", minor: "warn", warning: "warn", info: "" };
const ago2 = (t) => { const s = Math.max(0, Date.now() / 1000 - t); return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`; };
async function refreshIncidents() {
  try {
    const r = await api("/incidents?status=active&limit=1");
    const n = r.counts?.active || 0, worst = r.counts?.worst;
    $("#inc-badge").hidden = !n; $("#inc-badge").textContent = n;
    $("#st-inc").hidden = false; $("#st-inc span:last-child").textContent = n ? `incidents ${n}` : "no incidents";
    $("#st-inc .dot").className = `dot ${!n ? "ok" : worst === "critical" || worst === "major" ? "bad" : "warn"}`;
  } catch (e) { /* the pill stays as it was */ }
}
$("#st-inc").addEventListener("click", () => go("incidents"));
setInterval(() => { if (!$("#shell").hidden) refreshIncidents(); }, 30000);
function incTab(t) {
  state.incTab = t;
  $$("#inc-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  $$("#view-incidents .inc-pane").forEach((p) => p.hidden = p.dataset.pane !== (t === "active" || t === "all" ? "list" : t));
  if (t === "changes") renderHistory(); else if (t === "setup") renderSetup(); else if (t === "health") renderHealth(); else renderIncidents();
}
$$("#inc-tabs .tab").forEach((b) => b.addEventListener("click", () => incTab(b.dataset.tab)));
async function renderIncidents() {
  const tab = state.incTab || "active";
  if (tab === "changes" || tab === "setup" || tab === "health") return;
  let r; try { r = await api(`/incidents?status=${tab === "all" ? "all" : "active"}`); } catch (e) { $("#inc-table").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  refreshIncidents();
  const c = r.counts || {}, by = c.by_severity || {};
  const k = (label, value, sub, dot = "") => `<div class="kpi"><div class="label">${dot ? `<span class="dot ${dot}"></span>` : ""}${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  $("#inc-kpis").innerHTML = [k("Active", c.active || 0, "open or acknowledged", c.active ? "bad" : "ok"), k("Critical", by.critical || 0, "", by.critical ? "bad" : ""),
    k("Major", by.major || 0, "", by.major ? "bad" : ""), k("Minor / warning", (by.minor || 0) + (by.warning || 0), "", ""),
    k("Watcher", r.watching ? "on" : "off", r.watch?.at ? `last read ${ago2(r.watch.at)} ago` : "not read yet", r.watching ? "ok" : "warn")].join("");
  $("#inc-n").textContent = c.active || "";
  $("#inc-watch").textContent = r.watch?.at ? `${r.watch.findings} findings at the last read` : "";
  const rows = r.incidents || [];
  $("#inc-table").innerHTML = rows.length ? `<table class="tbl inc-tbl"><thead><tr><th>severity</th><th>what</th><th>controller</th><th>since</th><th>seen</th><th>status</th><th>runbook</th></tr></thead><tbody>
    ${rows.map((i) => `<tr class="click${state.incSel === i.id ? " sel" : ""}" data-id="${i.id}"><td><span class="tag ${SEV_CLASS[i.severity] || ""}">${esc(i.severity)}</span></td>
      <td><b>${esc(i.title)}</b><div class="faint cell-sub">${esc(i.kind.replace(/_/g, " "))} · #${i.id}</div></td><td class="mono" style="font-size:12px">${esc(i.controller)}</td>
      <td>${ago2(i.first_seen)}</td><td class="num">${i.count}</td><td><span class="tag ${i.status === "resolved" ? "ok" : i.status === "acked" ? "info" : "warn"}">${esc(i.status)}</span></td>
      <td>${i.runbook ? `<span class="faint">${esc(i.runbook)}</span> <span class="tag">${esc(i.mode || "")}</span>` : "<span class='faint'>–</span>"}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">${tab === "all" ? "No incidents yet." : "Nothing is wrong that the watcher can see."}</div>`;
  $$("#inc-table tr.click").forEach((tr) => tr.addEventListener("click", () => showIncident(Number(tr.dataset.id))));
  if (state.incSel) showIncident(state.incSel, true);
}
async function showIncident(id, quiet = false) {
  state.incSel = id;
  $$("#inc-table tr.sel").forEach((x) => x.classList.remove("sel")); $(`#inc-table tr[data-id="${id}"]`)?.classList.add("sel");
  let r; try { r = await api(`/incidents/${id}`); } catch (e) { if (!quiet) toast(e.message, "bad"); return; }
  const i = r.incident, adm = levelRank(state.level) >= 1;
  $("#inc-detail-title").textContent = `#${i.id} · ${i.title}`;
  let details = i.details || ""; try { details = JSON.stringify(JSON.parse(details), null, 1); } catch (e) { /* as it is */ }
  const tools = (r.messages || []).filter((m) => m.role === "tool");
  $("#inc-detail").innerHTML = `<div class="kv"><div>severity</div><div><span class="tag ${SEV_CLASS[i.severity] || ""}">${esc(i.severity)}</span></div>
      <div>status</div><div>${esc(i.status)}${i.acked_by ? ` · by ${esc(i.acked_by)}` : ""}</div><div>controller</div><div class="mono">${esc(i.controller)}</div>
      <div>first seen</div><div>${new Date(i.first_seen * 1000).toLocaleString()} (${ago2(i.first_seen)} ago)</div><div>last seen</div><div>${new Date(i.last_seen * 1000).toLocaleString()} · ${i.count} times</div>
      ${i.resolved_at ? `<div>resolved</div><div>${new Date(i.resolved_at * 1000).toLocaleString()}</div>` : ""}
      <div>runbook</div><div>${i.runbook ? `${esc(i.runbook)} (${esc(i.mode || "")})` : "none matched"}</div>${i.note ? `<div>note</div><div>${esc(i.note)}</div>` : ""}</div>
    ${details ? `<details style="margin-top:8px"><summary class="muted">what the watcher saw</summary><pre class="inc-pre">${esc(details)}</pre></details>` : ""}
    ${(r.pending || []).length ? `<h4>A fix waits for approval</h4>${r.pending.map((p, n) => `<div class="inc-fix"><b class="mono">${esc(p.name)}</b> <span class="faint">needs ${esc(p.needs || "admin")}</span>${kvTable(p.args || {})}
        <div class="row" style="margin-top:6px"><button class="btn sm primary" data-dec="approve" ${levelRank(state.level) >= levelRank(p.needs || "admin") ? "" : "disabled"}>Approve</button><button class="btn sm danger" data-dec="reject" ${adm ? "" : "disabled"}>Reject</button></div></div>`).join("")}` : ""}
    <h4>CANAH's investigation</h4>
    ${i.report ? `<div class="reply-rich inc-report">${md(i.report)}</div>` : i.thread ? `<div class="working"><span class="bars"><i></i><i></i><i></i></span>CANAH is looking into it…</div>` : `<div class="faint" style="font-size:12.5px">Not investigated${i.runbook ? "" : ": no runbook asks for it"}.</div>`}
    ${tools.length ? `<details style="margin-top:8px"><summary class="muted">${tools.length} tool call${tools.length > 1 ? "s" : ""}</summary>${tools.map((t) => `<div class="inc-step"><span class="mono">${esc(t.name)}</span> <span class="faint">${t.ms != null ? fmtDur(t.ms) : ""}</span><div class="faint">${esc(brief(t.result || "", 220))}</div></div>`).join("")}</details>` : ""}
    <div class="row" style="margin-top:12px;flex-wrap:wrap">
      ${i.status !== "resolved" ? `<button class="btn sm" data-act="ack" ${adm && i.status === "open" ? "" : "disabled"}>Acknowledge</button><button class="btn sm" data-act="resolve" ${adm ? "" : "disabled"}>Resolve</button>` : ""}
      <button class="btn sm" data-act="investigate" ${adm ? "" : "disabled title='admin'"}>Investigate now</button>
      <button class="btn sm" data-act="propose" ${adm ? "" : "disabled title='admin'"}>Propose a fix</button>
      <button class="btn sm" data-ask="${esc(`About incident #${i.id} (${i.title}, on ${i.controller}): what happened, what does it affect, and what changed just before it?`)}">Ask CANAH</button></div>`;
  $$("#inc-detail [data-act]").forEach((b) => b.addEventListener("click", async () => {
    const act = b.dataset.act, body = act === "investigate" ? { mode: "investigate" } : act === "propose" ? { mode: "propose" } : {};
    try { await api(`/incidents/${id}/${act === "propose" ? "investigate" : act}`, { method: "POST", body }); toast(act === "ack" ? "Acknowledged." : act === "resolve" ? "Resolved." : "CANAH is looking into it.", "ok"); renderIncidents(); setTimeout(() => showIncident(id, true), 4000); }
    catch (e) { toast(e.message, "bad"); }
  }));
  $$("#inc-detail [data-dec]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/incidents/${id}/decide`, { method: "POST", body: { decisions: (r.pending || []).map(() => ({ type: b.dataset.dec })) } }); toast(b.dataset.dec === "approve" ? "Approved: the fix was sent." : "Rejected.", "ok"); showIncident(id); }
    catch (e) { toast(e.message, "bad"); }
  }));
  $$("#inc-detail [data-ask]").forEach((b) => b.addEventListener("click", () => { go("chat"); $("#composer").value = b.dataset.ask; $("#composer").focus(); }));
  if (i.thread && !i.report) { clearTimeout(state.incTimer); state.incTimer = setTimeout(() => { if (state.view === "incidents" && state.incSel === id) showIncident(id, true); }, 5000); }
}
// A small chart per controller: what is down (devices, links, services) and the incidents open, over time.
function spark(points, keys, w = 320, h = 70) {
  if (!points.length) return "";
  const t0 = points[0].at, t1 = points[points.length - 1].at || t0 + 1, max = Math.max(1, ...points.flatMap((p) => keys.map((k) => p[k.f])));
  const x = (t) => 4 + ((t - t0) / Math.max(1, t1 - t0)) * (w - 8), y = (v) => h - 6 - (v / max) * (h - 14);
  return `<svg viewBox="0 0 ${w} ${h}" class="spark" preserveAspectRatio="none"><line x1="4" x2="${w - 4}" y1="${h - 6}" y2="${h - 6}" class="axis"/>${keys.map((k) =>
    `<polyline fill="none" stroke="${k.c}" stroke-width="1.8" points="${points.map((p) => `${x(p.at).toFixed(1)},${y(p[k.f]).toFixed(1)}`).join(" ")}"><title>${k.l}</title></polyline>`).join("")}
    <text x="${w - 4}" y="11" text-anchor="end" class="mx">${max}</text></svg>`;
}
async function renderHealth() {
  let r; try { r = await api(`/health/series?hours=${$("#hl-hours").value}`); } catch (e) { $("#hl-body").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  const keys = [{ f: "devices_down", l: "devices down", c: "#ff6b6b" }, { f: "links_down", l: "links down", c: "#f6a52c" },
                { f: "services_down", l: "services down", c: "#c792ea" }, { f: "incidents", l: "incidents open", c: "#3d8bff" }];
  const rows = Object.entries(r.series || {});
  $("#hl-body").innerHTML = rows.length ? rows.map(([k, v]) => { const last = v[v.length - 1], reach = Math.round(100 * v.filter((p) => p.reachable).length / v.length);
    return `<div class="card hl-card"><header><h3>${esc(k)}</h3><div class="spacer"></div><span class="tag ${last.reachable ? "ok" : "bad"}">${last.reachable ? "reachable" : "not answering"}</span></header>
      <div class="body">${spark(v, keys)}<div class="hl-legend">${keys.map((x) => `<span><i style="background:${x.c}"></i>${x.l} <b>${last[x.f]}</b></span>`).join("")}</div>
      <div class="faint" style="font-size:11.5px;margin-top:4px">${last.devices} devices · ${last.links} links · ${last.services} services · reachable ${reach}% of ${v.length} readings</div></div></div>`; }).join("")
    : `<div class="empty">No readings yet: the watcher records one each minute while CANAH runs.</div>`;
}
$("#hl-hours").addEventListener("change", renderHealth);

async function renderHistory() {
  const ctl = $("#hist-ctl").value, hours = $("#hist-hours").value;
  let r; try { r = await api(`/history?hours=${hours}${ctl ? `&controller=${encodeURIComponent(ctl)}` : ""}`); } catch (e) { $("#hist-body").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  const sel = $("#hist-ctl"), keep = sel.value;
  sel.innerHTML = `<option value="">every controller</option>` + (r.controllers || []).map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join(""); sel.value = keep;
  const rows = (r.changes || []).flatMap((c) => c.changes.map((x) => ({ ...x, at: c.at, controller: c.controller })));
  $("#hist-body").innerHTML = rows.length ? `<table class="tbl"><thead><tr><th>when</th><th>controller</th><th>what</th><th>change</th></tr></thead><tbody>${rows.slice(0, 400).map((x) => `<tr><td style="white-space:nowrap">${new Date(x.at * 1000).toLocaleString()}</td>
      <td class="mono" style="font-size:12px">${esc(x.controller)}</td><td>${esc(x.kind)} <b class="mono">${esc(x.id)}</b></td>
      <td><span class="tag ${x.change === "added" ? "ok" : x.change === "removed" ? "bad" : "warn"}">${esc(x.change)}</span> ${x.fields ? Object.entries(x.fields).map(([f, v]) => `<span class="faint">${esc(f)}:</span> ${esc(String(v[0]))} → <b>${esc(String(v[1]))}</b>`).join(" · ") : ""}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">No change in this window. The history starts when the watcher first reads each network, and keeps a snapshot whenever something changes.</div>`;
}
$("#hist-ctl").addEventListener("change", renderHistory); $("#hist-hours").addEventListener("change", renderHistory);
async function renderSetup() {
  try {
    const [rb, nt] = await Promise.all([api("/runbooks"), api("/notify")]);
    $("#rb-body").innerHTML = `<table class="tbl"><thead><tr><th>runbook</th><th>when</th><th>does</th></tr></thead><tbody>${(rb.runbooks || []).map((x) => `<tr><td><b>${esc(x.name)}</b><div class="faint cell-sub">${x.builtin ? "built in" : "this site's"}</div></td>
      <td class="mono" style="font-size:11.5px">${esc(JSON.stringify(x.when || {}))}</td><td><span class="tag ${x.mode === "propose" ? "info" : ""}">${esc(x.mode)}</span><div class="faint cell-sub">${esc(String(x.instructions || "").split("\n")[0].slice(0, 120))}</div></td></tr>`).join("")}</tbody></table>`;
    $("#nt-body").innerHTML = (nt.channels || []).length ? `<table class="tbl"><thead><tr><th>channel</th><th>events</th><th>from</th><th>ready</th></tr></thead><tbody>${nt.channels.map((c) => `<tr><td><b>${esc(c.kind)}</b><div class="faint cell-sub">${esc(c.name)}${c.to ? " · " + esc([].concat(c.to).join(", ")) : ""}</div></td>
        <td style="font-size:11.5px">${esc(c.events.join(", ").replace(/_/g, " "))}</td><td>${esc(c.min_severity)}</td><td>${c.ready ? `<span class="tag ok">yes</span>` : `<span class="tag warn">not set</span>`}</td></tr>`).join("")}</tbody></table>`
      : `<p class="muted" style="margin:0;font-size:12.5px">No channel yet. Add them to <span class="mono">notify_channels</span> in canah.yaml, e.g.<br><span class="mono">- {kind: slack, url_env: SLACK_WEBHOOK_URL, min_severity: major}</span><br>(webhook, slack, teams, or email with CANAH_SMTP_HOST).</p>`;
    $("#nt-body").innerHTML += (nt.sent || []).length ? `<h4>Last sent</h4>${nt.sent.slice(0, 8).map((x) => `<div class="act-row"><span class="dot ${x.results.every((y) => y.ok) ? "ok" : "bad"}"></span><div>${esc(x.title)}<div class="faint">${esc(x.event.replace(/_/g, " "))}</div></div><span class="faint">${ago2(x.at)}</span></div>`).join("")}` : "";
    $("#nt-test").disabled = levelRank(state.level) < 1 || !(nt.channels || []).length;
  } catch (e) { $("#rb-body").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
}
$("#nt-test").addEventListener("click", async () => { try { const r = await api("/notify/test", { method: "POST" }); toast(r.results.map((x) => `${x.channel}: ${x.ok ? "sent" : x.error}`).join(" · ") || "No channel took it.", r.results.every((x) => x.ok) ? "ok" : "warn"); renderSetup(); } catch (e) { toast(e.message, "bad"); } });
$("#inc-refresh").addEventListener("click", () => incTab(state.incTab || "active"));
$("#inc-poll").addEventListener("click", async () => { try { const r = await api("/watch/poll", { method: "POST" }); toast(`Read every controller: ${r.findings} findings, ${r.opened} new, ${r.resolved} resolved.`, "ok"); renderIncidents(); } catch (e) { toast(e.message, "bad"); } });
window.addEventListener("hashchange", () => { const m = location.hash.match(/^#incident-(\d+)/); if (m) { go("incidents"); incTab("all"); showIncident(Number(m[1])); } });

/* ================= accounts ================= */
async function renderAccounts() {
  const root = levelRank(state.level) >= 2;
  $("#acc-card").hidden = levelRank(state.level) < 1;
  if ($("#acc-card").hidden) return;
  let r; try { r = await api("/users"); } catch (e) { $("#acc-table").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  const ctls = (state.topology?.controllers || []).map((c) => c.key);
  $("#acc-table").innerHTML = `<table class="tbl"><thead><tr><th>account</th><th>level</th><th>controllers</th><th></th></tr></thead><tbody>${r.users.map((u) => `<tr><td><b>${esc(u.user)}</b><div class="faint cell-sub">${esc(u.from)}${u.default_password ? " · <span style='color:var(--warn)'>default password</span>" : ""}</div></td>
    <td><span class="tag ${u.level === "root" ? "warn" : "ok"}">${esc(u.level)}</span></td>
    <td>${u.from === "users.json" ? `<div class="acc-scope" data-user="${esc(u.user)}">${ctls.map((k) => `<label class="chip-check"><input type="checkbox" value="${esc(k)}" ${u.controllers.includes(k) ? "checked" : ""} ${root ? "" : "disabled"}>${esc(k)}</label>`).join("")}<span class="faint" style="font-size:11px">${u.controllers.length ? "" : "none ticked: every controller"}</span></div>` : `<span class="faint">every controller</span>`}</td>
    <td style="text-align:right">${u.from === "users.json" && root && u.user !== state.user ? `<button class="btn sm danger" data-drop-user="${esc(u.user)}">Remove</button>` : ""}</td></tr>`).join("")}</tbody></table>`;
  $$("#acc-table .acc-scope input").forEach((x) => x.addEventListener("change", async () => {
    const box = x.closest(".acc-scope"), controllers = $$("input:checked", box).map((y) => y.value);
    try { await api(`/users/${encodeURIComponent(box.dataset.user)}/scope`, { method: "PUT", body: { controllers } }); toast(`${box.dataset.user}: ${controllers.length ? controllers.join(", ") : "every controller"} (from the next sign-in).`, "ok"); renderAccounts(); } catch (e) { toast(e.message, "bad"); }
  }));
  $$("[data-drop-user]").forEach((b) => b.addEventListener("click", async () => { if (!confirm(`Remove the account ${b.dataset.dropUser}?`)) return; try { await api(`/users/${encodeURIComponent(b.dataset.dropUser)}`, { method: "DELETE" }); renderAccounts(); } catch (e) { toast(e.message, "bad"); } }));
  $("#acc-scope").innerHTML = ctls.map((k) => `<label class="chip-check"><input type="checkbox" value="${esc(k)}">${esc(k)}</label>`).join("") + `<span class="faint" style="font-size:11px">none ticked: every controller</span>`;
  $$("#acc-form input, #acc-form select, #acc-form button").forEach((x) => x.disabled = !root);
  $("#acc-msg").textContent = root ? "" : "Sign in as root to add accounts.";
}
$("#acc-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { user: $("#acc-user").value.trim(), level: $("#acc-level").value, password: $("#acc-pass").value, controllers: $$("#acc-scope input:checked").map((x) => x.value) };
  try { await api("/users", { method: "POST", body }); $("#acc-pass").value = ""; $("#acc-user").value = ""; toast(`${body.user} added.`, "ok"); renderAccounts(); } catch (err) { $("#acc-msg").textContent = err.message; }
});

/* ================= the model ================= */
async function renderModel() {
  const root = levelRank(state.level) >= 2;
  let m;
  try { m = state.modelInfo = await api("/admin/model"); }
  catch (e) { $("#md-banner").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  $("#md-banner").innerHTML = m.env_override ? `<div class="banner warn">CANAH_MODEL is set in the environment (<span class="mono">${esc(m.env_override)}</span>): it wins over the choice made here.</div>` : "";
  const cur = m.current?.model || "–", [prov, ...rest] = cur.split(":"), name = rest.join(":");
  $("#md-current").innerHTML = `<div class="md-cur"><div class="md-ico">${prov === "ollama" ? "◉" : "☁"}</div><div><div class="eyebrow">CANAH runs on</div>
      <h3 class="mono">${esc(name || cur)}</h3><div class="faint">${esc(prov)}${m.context?.window ? ` · context window ${Number(m.context.window).toLocaleString()} tokens, compacted at ${Math.round((m.context.compact_at || 0.85) * 100)}%` : ""}${m.choice ? " · chosen on this page" : " · from canah/models.py"}</div></div>
      <div class="spacer"></div>${m.choice ? `<button class="btn sm" id="md-reset" ${root ? "" : "disabled title='root'"}>Back to the default in canah/models.py</button>` : ""}</div>`;
  $("#md-reset")?.addEventListener("click", async () => {
    if (!confirm("Go back to the model in canah/models.py?")) return;
    try { await api("/admin/model", { method: "PUT", body: { reset: true } }); toast("Back to the default model.", "ok"); await refreshStatus(); renderModel(); } catch (e) { toast(e.message, "bad"); }
  });
  // Ollama's models
  $("#md-ollama-url").textContent = m.ollama?.url || "";
  const om = (m.ollama?.models || []).filter((x) => !x.embedding);
  $("#md-ollama").innerHTML = m.ollama?.error ? `<div class="banner bad" style="margin:12px">Ollama did not answer: ${esc(m.ollama.error)}</div>`
    : om.length ? `<table class="tbl"><thead><tr><th>model</th><th>size</th><th>state</th><th></th></tr></thead><tbody>${om.map((x) => {
      const using = prov === "ollama" && name === x.name;
      return `<tr${using ? ' class="sel"' : ""}><td><b class="mono">${esc(x.name)}</b><div class="faint cell-sub">${esc([x.family, x.params, x.quant].filter(Boolean).join(" · "))}</div></td>
        <td class="num">${x.size_gb} GB</td><td>${x.loaded ? `<span class="tag ok">in memory</span>` : `<span class="tag">on disk</span>`}</td>
        <td style="text-align:right">${using ? `<span class="tag info">in use</span>` : `<button class="btn sm" data-ollama="${esc(x.name)}" ${root ? "" : "disabled title='root'"}>Use</button>`}</td></tr>`; }).join("")}</tbody></table>`
    : `<div class="empty">Ollama has no chat models: <span class="mono">ollama pull qwen3:30b-a3b-instruct-2507-q4_K_M</span></div>`;
  $$("[data-ollama]").forEach((b) => b.addEventListener("click", () => useModel({ provider: "ollama", model: b.dataset.ollama, num_ctx: Number($("#md-ctx").value) }, b)));
  // other providers
  const sel = $("#md-provider"), keep = sel.value;
  sel.innerHTML = m.providers.filter((x) => x.id !== "ollama").map((x) => `<option value="${esc(x.id)}">${esc(x.title)}${x.installed ? "" : " (not installed)"}</option>`).join("");
  if (keep) sel.value = keep; else if (prov !== "ollama" && m.providers.some((x) => x.id === prov)) sel.value = prov;
  providerChanged();
  $$("#md-form input, #md-form select, #md-form button, #md-ctx").forEach((x) => x.disabled = !root);
  if (!root) $("#md-msg").textContent = "Sign in as root to change the model.";
}
function providerChanged() {
  const m = state.modelInfo; if (!m) return;
  const pv = m.providers.find((x) => x.id === $("#md-provider").value) || {};
  $("#md-examples").innerHTML = (pv.examples || []).map((x) => `<option value="${esc(x)}">`).join("");
  $("#md-model").placeholder = pv.examples?.[0] ? `e.g. ${pv.examples[0]}` : "the model's name";
  $$(".md-base").forEach((x) => x.hidden = !pv.base_url);
  $("#md-key").placeholder = pv.key_set ? "•••••••••• set: paste a new one to replace it" : "paste the key";
  $("#md-key-note").innerHTML = !pv.installed ? `Not installed here: <span class="mono">.venv/bin/pip install ${esc(pv.pip)}</span>, then restart CANAH.`
    : `The key is kept on CANAH's host, readable by CANAH's user only, never shown again${pv.key_env ? ` (it is used as <span class="mono">${esc(pv.key_env)}</span>)` : ""}.`;
  $("#md-key-drop").hidden = !pv.key_on_page;
}
$("#md-provider").addEventListener("change", providerChanged);
$("#md-refresh").addEventListener("click", renderModel);
async function useModel(body, btn) {
  const was = btn?.textContent; if (btn) { btn.disabled = true; btn.textContent = "Testing…"; }
  $("#md-msg").textContent = `Trying ${body.model}…`;
  try {
    const r = await api("/admin/model", { method: "PUT", body });
    toast(`CANAH now runs on ${r.current?.model} (it answered in ${r.test?.seconds} s).`, "ok");
    $("#md-msg").innerHTML = `<span class="tag ok">in use</span> answered in ${r.test?.seconds} s`; $("#md-key").value = "";
    await refreshStatus(); renderModel();
  } catch (e) { $("#md-msg").innerHTML = `<span class="tag bad">not changed</span> ${esc(e.message)}`; toast(e.message, "bad"); }
  finally { if (btn) { btn.disabled = false; btn.textContent = was; } }
}
$("#md-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const body = { provider: $("#md-provider").value, model: $("#md-model").value.trim(), base_url: $("#md-base").value.trim() };
  if ($("#md-key").value.trim()) body.api_key = $("#md-key").value.trim();
  useModel(body, $("#md-go"));
});
$("#md-key-drop").addEventListener("click", async () => {
  const pv = $("#md-provider").value;
  if (!confirm(`Remove the stored API key for ${pv}?`)) return;
  try { await api(`/admin/keys/${encodeURIComponent(pv)}`, { method: "DELETE" }); toast("Key removed.", "ok"); renderModel(); } catch (e) { toast(e.message, "bad"); }
});

/* ================= approvals & audit ================= */
async function refreshPendingBadge() {
  let n = 0;
  for (const t of threads().slice(0, 15)) { try { n += (await api(`/threads/${t.id}/pending`)).pending.length; } catch (e) { /* gone */ } }
  $("#pending-badge").hidden = !n; $("#pending-badge").textContent = n;
  return n;
}
async function renderOps() {
  const rows = [];
  for (const t of threads().slice(0, 25)) { try { const p = await api(`/threads/${t.id}/pending`); for (const x of p.pending) rows.push({ t, x }); } catch (e) { /* gone */ } }
  $("#pending-badge").hidden = !rows.length; $("#pending-badge").textContent = rows.length;
  $("#ops-pending").innerHTML = rows.length ? `<table class="tbl"><thead><tr><th>conversation</th><th>change</th><th>needs</th><th>arguments</th><th></th></tr></thead><tbody>${rows.map(({ t, x }) => `<tr><td>${esc(t.title)}</td><td class="mono">${esc(x.name)}</td><td><span class="tag ${x.needs === "root" ? "bad" : "warn"}">${esc(x.needs)}</span></td><td class="mono" style="font-size:11px">${esc(JSON.stringify(x.args || {})).slice(0, 220)}</td><td><button class="btn sm" data-open="${esc(t.id)}">Open</button></td></tr>`).join("")}</tbody></table>` : `<div class="empty">Nothing waits for approval in your conversations.</div>`;
  $$("[data-open]").forEach((b) => b.addEventListener("click", () => { state.chat.thread = b.dataset.open; go("chat"); }));
  const k = (label, value, sub, dot = "") => `<div class="kpi"><div class="label">${dot ? `<span class="dot ${dot}"></span>` : ""}${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  if (levelRank(state.level) < 1) {
    $("#ops-audit").innerHTML = `<div class="empty">The audit log and the metrics are for admin and root.</div>`;
    $("#ops-kpis").innerHTML = k("Waiting", rows.length, "changes in your conversations", rows.length ? "warn" : "ok");
    return;
  }
  try {
    const [a, m] = await Promise.all([api("/audit?limit=300"), api("/metrics?hours=24")]);
    $("#ops-kpis").innerHTML = [k("Waiting", rows.length, "changes in your conversations", rows.length ? "warn" : "ok"),
      k("Requests · 24 h", m.requests ?? 0, m.requests ? `median ${m.median_seconds} s · p95 ${m.p95_seconds} s` : "none yet"),
      k("Tool calls · 24 h", m.tool_calls ?? 0, `${m.model_calls ?? 0} model calls`),
      k("KV cache · 24 h", m.kv_cache_percent != null ? `${m.kv_cache_percent}%` : "–", "of prompt tokens reused, not read again"),
      k("Context window", m.context_percent_median != null ? `${m.context_percent_median}%` : "–", m.context_percent_max != null ? `median filled · max ${m.context_percent_max}%` : "filled, after each request"),
      k("Sent back · 24 h", Object.values(m.sent_back || {}).reduce((a, b) => a + b, 0), Object.entries(m.sent_back || {}).map(([a, b]) => `${a} ${b}`).join(" · ") || "by the checks"),
      k("Audit entries", a.audit.length, "latest shown")].join("");
    $("#ops-audit").innerHTML = a.audit.length ? `<table class="tbl"><thead><tr><th>when</th><th>event</th><th>who</th><th>tool</th><th>arguments / result</th></tr></thead><tbody>${a.audit.map((r) => `<tr><td style="white-space:nowrap">${esc(fmtTime(r.at))}</td><td><span class="tag ${r.event === "executed" ? "ok" : r.event === "decided" ? (r.decision === "reject" ? "bad" : "info") : r.event === "proposed" ? "warn" : ""}">${esc(r.event)}${r.decision ? " · " + esc(r.decision) : ""}</span></td><td>${esc(r.user)} <span class="faint">${esc(r.level)}</span></td><td class="mono">${esc(r.tool)}</td><td class="mono" style="font-size:11px;max-width:520px;overflow-wrap:anywhere">${esc(typeof r.args === "string" ? r.args : JSON.stringify(r.args)).slice(0, 260)}${r.result ? `<div class="faint">${esc(String(r.result).slice(0, 220))}</div>` : ""}${r.event === "executed" ? `<div style="margin-top:4px"><button class="btn sm ghost" data-undo="${esc(JSON.stringify({ at: r.at, tool: r.tool, args: r.args, user: r.user }))}" title="Ask CANAH to propose the change that undoes this one">Propose undo</button></div>` : ""}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">The audit log is empty.</div>`;
    $$("#ops-audit [data-undo]").forEach((b) => b.addEventListener("click", () => {
      const u = JSON.parse(b.dataset.undo);
      go("chat");
      $("#composer").value = `Propose the change that undoes this one, executed ${fmtTime(u.at)} by ${u.user}: ${u.tool} with ${typeof u.args === "string" ? u.args : JSON.stringify(u.args)}. Read the current state first, rehearse the undo in the digital twin, then propose it for approval.`;
      $("#composer").focus();
    }));
  } catch (e) { $("#ops-audit").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
$("#ops-refresh").addEventListener("click", renderOps);

/* ================= digital twins ================= */
const twinLabel = (w) => !w ? "" : /^net-?t?wins?$/i.test(w.name) ? "NetTwin" : w.name;
async function refreshTwinPill() {
  try {
    const t = await api("/twins");
    const act = (t.twins || []).find((x) => x.active);
    const before = JSON.stringify(state.twin?.name || null);
    state.twin = act || null;
    if (state.topology && JSON.stringify(act?.name || null) !== before) { buildGraph(state.topology); if (state.view === "topology") renderTopology(); }
    $("#st-twin span:last-child").textContent = act ? `twin: ${twinLabel(act)}` : "no twin";
    $("#st-twin .dot").className = `dot ${act ? (act.ok ? "ok" : "bad") : ""}`;
    $("#st-twin").hidden = false;
    return t;
  } catch (e) { $("#st-twin").hidden = true; return null; }
}
$("#st-twin").addEventListener("click", () => go("twins"));
async function renderTwins() {
  const t = await refreshTwinPill();
  if (!t) { $("#tw-active").innerHTML = `<div class="banner warn">This agent was built without digital twins: add one under <span class="mono">twins:</span> in canah.yaml.</div>`; $("#tw-cards").innerHTML = ""; $("#tw-domains").innerHTML = ""; return; }
  $("#tw-contract").textContent = `contract ${t.contract}`;
  const admin = levelRank(state.level) >= 1, root = levelRank(state.level) >= 2;
  const twins = t.twins || [], cur = twins.find((w) => w.active);
  const caps = (w) => (w.capabilities || []).map((c) => `<span class="chip">${esc(c.replace(/_/g, " "))}</span>`).join("");
  // 1. the twin in use
  $("#tw-active").innerHTML = cur ? `<div class="card tw-hero${/^net-?t?wins?$/i.test(cur.name) ? " with-logo" : ""}">${/^net-?t?wins?$/i.test(cur.name) ? `<img class="tw-logo" src="/static/nettwin-logo.png" alt="NetTwin: rehearse the change before you make it">` : ""}
      <div class="tw-hero-h"><span class="dot ${cur.ok ? "ok" : "bad"}"></span><div><div class="eyebrow">In use · the digital twin CANAH rehearses changes in</div><h3>${esc(twinLabel(cur))}${twinLabel(cur) !== cur.name ? ` <span class="mono faint" style="font-size:12px;font-weight:400">${esc(cur.name)}</span>` : ""}</h3></div>
        <div class="tags"><span class="tag">${esc(cur.kind)}</span>${cur.version ? `<span class="tag">v${esc(cur.version)}</span>` : ""}${cur.declared ? `<span class="tag">canah.yaml</span>` : ""}</div>
        <div class="spacer"></div><button class="btn sm" data-test="${esc(cur.name)}">Test</button></div>
      ${cur.ok ? `<p class="muted tw-about">${esc(cur.about || "")}</p>
      <div class="tw-facts">
        <div><div class="label">Where</div><div class="mono">${esc(cur.url || cur.spec || "–")}${cur.path ? `<span class="faint"> · ${esc(cur.path)}</span>` : ""}</div></div>
        <div><div class="label">Domains</div><div>${(cur.domains || []).length}</div></div>
        <div><div class="label">Checks writes on</div><div>${esc((cur.controller_domains || []).join(", ") || "none")}</div></div>
        <div><div class="label">Extras</div><div>${esc((cur.extras || []).map((x) => x.name).join(", ") || "none")}</div></div>
      </div><div class="chips">${caps(cur)}</div>`
      : `<div class="banner bad" style="margin:0">${esc(cur.error || "does not answer")}</div>`}</div>`
    : `<div class="card tw-hero none"><div class="tw-hero-h"><span class="dot"></span><div><div class="eyebrow">In use</div><h3>No twin</h3></div></div>
       <p class="muted tw-about">Answers come from the controllers alone, and writes go through approval alone, with no prediction first. Choose a twin below.</p></div>`;
  // 2. its domains, as equal boxes
  const doms = cur?.ok ? cur.domains || [] : [];
  $("#tw-dom-of").textContent = cur ? `of ${cur.name}` : "";
  $("#tw-domains").innerHTML = doms.length ? doms.map((d) => `<div class="tw-dom-card">
      <div class="h"><b>${esc(d.title || d.domain)}</b><span class="mono faint">${esc(d.domain)}</span></div>
      <p>${esc(d.answers || "")}</p>
      <div class="label">KPIs</div><div class="chips">${(d.kpis || []).map((k) => `<span class="chip mono">${esc(k)}</span>`).join("")}</div>
      ${d.fidelity ? `<div class="label">Fidelity</div><p class="faint">${esc(d.fidelity)}</p>` : ""}
      ${d.not_modelled ? `<div class="label">Not modelled</div><p class="faint">${esc(d.not_modelled)}</p>` : ""}</div>`).join("")
    : `<div class="empty">${cur ? "The twin in use does not answer." : "No twin in use."}</div>`;
  // 3. every twin, as aligned rows, "no twin" among them
  const row = (w) => `<div class="tw-row${w.active ? " active" : ""}">
      <div class="c-name"><span class="dot ${w.ok ? "ok" : "bad"}"></span><div><b>${esc(w.name)}</b>${w.active ? ` <span class="tag info">in use</span>` : ""}
        <div class="faint mono cell-sub">${esc(w.url || w.spec || "")}${w.token_env ? ` · token $${esc(w.token_env)}` : ""}</div></div></div>
      <div class="c-kind"><span class="tag">${esc(w.kind)}</span>${w.version ? ` <span class="faint">v${esc(w.version)}</span>` : ""}</div>
      <div class="c-doms">${w.ok ? (w.domains || []).map((d) => `<span class="chip">${esc(d.title || d.domain)}</span>`).join("") : `<span class="tag bad">${esc((w.error || "does not answer").slice(0, 80))}</span>`}</div>
      <div class="c-acts">${!w.active ? `<button class="btn sm primary" data-use="${esc(w.name)}" ${admin ? "" : "disabled title='admin'"}>Use</button>` : ""}
        <button class="btn sm" data-test="${esc(w.name)}">Test</button>
        ${!w.declared ? `<button class="btn sm danger" data-drop="${esc(w.name)}" ${root ? "" : "disabled title='root'"}>Remove</button>` : `<span class="faint cell-sub" title="declared in canah.yaml">canah.yaml</span>`}</div></div>`;
  $("#tw-cards").innerHTML = twins.map(row).join("") + `<div class="tw-row none${t.active ? "" : " active"}">
      <div class="c-name"><span class="dot"></span><div><b>No twin</b>${!t.active ? ` <span class="tag info">in use</span>` : ""}<div class="faint cell-sub">no prediction before a write</div></div></div>
      <div class="c-kind"></div><div class="c-doms faint" style="font-size:12px">answers from the controllers alone; writes through approval alone</div>
      <div class="c-acts">${t.active ? `<button class="btn sm" data-use="none" ${admin ? "" : "disabled"}>Use no twin</button>` : ""}</div></div>`;
  $$("[data-use]").forEach((b) => b.addEventListener("click", async () => {
    try { await api("/twins/active", { method: "PUT", body: { name: b.dataset.use } }); toast(b.dataset.use === "none" ? "No twin in use." : `Using ${b.dataset.use} from the next question.`, "ok"); renderTwins(); }
    catch (e) { toast(e.message, "bad"); }
  }));
  $$("[data-test]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { const r = await api(`/twins/${encodeURIComponent(b.dataset.test)}/test`, { method: "POST" });
      toast(r.ok ? `${r.name} answers (${r.contract}): ${r.domains.join(", ")}` : `It does not answer: ${r.error}`, r.ok ? "ok" : "bad"); }
    catch (e) { toast(e.message, "bad"); } finally { b.disabled = false; }
  }));
  $$("[data-drop]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Remove the twin ${b.dataset.drop}?`)) return;
    try { await api(`/twins/${encodeURIComponent(b.dataset.drop)}`, { method: "DELETE" }); toast("Removed.", "ok"); renderTwins(); } catch (e) { toast(e.message, "bad"); }
  }));
  $$("#tw-form input, #tw-form select, #tw-form button").forEach((x) => x.disabled = !admin);
  if (!admin) $("#tw-msg").textContent = "Sign in as admin to add a twin.";
}
$("#tw-refresh").addEventListener("click", renderTwins);
$("#tw-kind").addEventListener("change", (e) => { const py = e.target.value === "python"; $$(".tw-http").forEach((x) => x.hidden = py); $$(".tw-py").forEach((x) => x.hidden = !py); });
$("#tw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const kind = $("#tw-kind").value;
  const body = kind === "http" ? { name: $("#tw-name").value.trim(), kind, url: $("#tw-url").value.trim(), token_env: $("#tw-token").value.trim() }
                               : { name: $("#tw-name").value.trim(), kind, spec: $("#tw-spec").value.trim(), path: $("#tw-path").value.trim() };
  $("#tw-msg").textContent = "adding and testing…";
  try {
    const r = await api("/twins", { method: "POST", body });
    $("#tw-msg").innerHTML = r.status?.ok ? `<span class="tag ok">added</span> it answers in the contract: ${esc((r.status.domains || []).map((d) => d.domain).join(", "))}. Choose “Use this twin” to use it.`
      : `<span class="tag warn">added, but it does not answer</span> ${esc(r.status?.error || "")}`;
    renderTwins();
  } catch (err) { $("#tw-msg").innerHTML = `<span class="tag bad">not added</span> ${esc(err.message)}`; }
});

/* ================= knowledge ================= */
const mesh = { data: null, pos: {}, sel: null, view: { k: 1, x: 0, y: 0 } };
const MESH_COLOURS = ["#1f7cf2", "#3dc46b", "#f6a52c", "#9b51c0", "#ff6b6b", "#89ddff", "#f78c6c", "#c3e88d", "#b2a6ff"];
async function loadMesh(rebuild = false) {
  $("#mesh-cost").textContent = rebuild ? "rebuilding…" : "reading…";
  try { mesh.data = await api(`/knowledge/mesh${rebuild ? "?rebuild=1" : ""}`); } catch (e) { $("#mesh-cost").textContent = e.message; return; }
  const d = mesh.data;
  if (!d.available || !(d.nodes || []).length) { $("#mesh").replaceChildren(); $("#mesh-cost").textContent = d.reason || d.empty || "nothing indexed yet"; return; }
  const c = d.cost || {};
  $("#mesh-cost").textContent = `${d.nodes.length} documents · ${d.edges.length} links · ${d.clusters.length} clusters · ${d.from_cache ? "from cache" : `built in ${c.total_seconds}s from ${c.chunks_read} chunks`}`;
  $("#mesh-means").textContent = d.means || "";
  layoutMesh(); renderMesh(); fitMesh();
}
function layoutMesh() {      // Fruchterman-Reingold, run once, seeded by cluster (as edgeLM's)
  const d = mesh.data, nodes = d.nodes, n = nodes.length, W = 1100, H = 440;
  const index = new Map(nodes.map((x, i) => [x.id, i]));
  const px = new Float64Array(n), py = new Float64Array(n);
  const clusters = [...new Set(nodes.map((x) => x.cluster))];
  nodes.forEach((x, i) => { const a = (clusters.indexOf(x.cluster) / clusters.length) * Math.PI * 2;
    px[i] = W / 2 + Math.cos(a) * 200 + ((Math.sin(i * 12.9898) * 43758.5453) % 1) * 60; py[i] = H / 2 + Math.sin(a) * 150 + ((Math.sin(i * 78.233) * 43758.5453) % 1) * 60; });
  const ticks = Math.min(320, Math.max(120, Math.round(9000 / Math.max(1, n)))), k = Math.sqrt((W * H) / Math.max(1, n)) * 1.5;
  for (let t = 0; t < ticks; t++) {
    const temp = (1 - t / ticks) * 28 + 0.4, fx = new Float64Array(n), fy = new Float64Array(n);
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) { let dx = px[i] - px[j], dy = py[i] - py[j]; const dist = Math.hypot(dx, dy) || 0.01; if (dist > k * 4) continue;
      const rep = (k * k) / dist; dx /= dist; dy /= dist; fx[i] += dx * rep; fy[i] += dy * rep; fx[j] -= dx * rep; fy[j] -= dy * rep; }
    for (const e of d.edges) { const i = index.get(e.source), j = index.get(e.target); if (i == null || j == null) continue;
      let dx = px[i] - px[j], dy = py[i] - py[j]; const dist = Math.hypot(dx, dy) || 0.01; const att = (dist * dist) / k * (0.4 + e.similarity);
      dx /= dist; dy /= dist; fx[i] -= dx * att; fy[i] -= dy * att; fx[j] += dx * att; fy[j] += dy * att; }
    for (let i = 0; i < n; i++) { const len = Math.hypot(fx[i], fy[i]) || 1, step = Math.min(len, temp);
      px[i] += (fx[i] / len) * step + (W / 2 - px[i]) * 0.004; py[i] += (fy[i] / len) * step + (H / 2 - py[i]) * 0.004; }
  }
  mesh.pos = {}; nodes.forEach((x, i) => { mesh.pos[x.id] = { x: px[i], y: py[i] }; });
}
function renderMesh() {
  const d = mesh.data, svg = $("#mesh"); if (!d || !svg) return;
  const vp = S("g", { id: "mesh-vp" }); svg.replaceChildren(vp);
  const near = new Set(mesh.sel ? d.edges.filter((e) => e.source === mesh.sel || e.target === mesh.sel).flatMap((e) => [e.source, e.target]) : []);
  for (const e of d.edges) { const a = mesh.pos[e.source], b = mesh.pos[e.target]; if (!a || !b) continue;
    const on = mesh.sel && (e.source === mesh.sel || e.target === mesh.sel);
    const l = S("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: `mlk${on ? " on" : ""}`, "stroke-width": 0.6 + e.similarity * 2 });
    l.append(S("title", {}, `similarity ${e.similarity}`)); vp.append(l); }
  for (const nd of d.nodes) { const p = mesh.pos[nd.id]; const r = 6 + Math.sqrt(nd.chunks) * 1.1, c = MESH_COLOURS[nd.cluster % MESH_COLOURS.length];
    const g = S("g", { class: `mnd${mesh.sel === nd.id ? " sel" : ""}`, transform: `translate(${p.x},${p.y})`, "data-id": nd.id });
    g.append(S("circle", { r, fill: c, "fill-opacity": mesh.sel && !near.has(nd.id) && mesh.sel !== nd.id ? 0.25 : 0.7, stroke: c }));
    g.append(S("text", { y: r + 13, "text-anchor": "middle", class: "mlbl" }, nd.title.length > 34 ? nd.title.slice(0, 33) + "…" : nd.title));
    g.append(S("title", {}, `${nd.title}\n${nd.chunks} chunks · ${nd.links} neighbours`)); vp.append(g); }
  applyMesh();
}
function applyMesh() { const vp = $("#mesh-vp"); if (vp) vp.setAttribute("transform", `translate(${mesh.view.x},${mesh.view.y}) scale(${mesh.view.k})`); $("#mesh-zoom").textContent = `${Math.round(mesh.view.k * 100)}%`; }
function fitMesh() {
  const r = $("#mesh").getBoundingClientRect(), ps = Object.values(mesh.pos); if (!r.width || !ps.length) return;
  const x0 = Math.min(...ps.map((p) => p.x)) - 70, x1 = Math.max(...ps.map((p) => p.x)) + 70, y0 = Math.min(...ps.map((p) => p.y)) - 50, y1 = Math.max(...ps.map((p) => p.y)) + 50;
  const k = Math.min(2, Math.max(0.2, Math.min(r.width / (x1 - x0), r.height / (y1 - y0))));
  mesh.view = { k, x: r.width / 2 - ((x0 + x1) / 2) * k, y: r.height / 2 - ((y0 + y1) / 2) * k }; applyMesh();
}
function meshDetail() {
  const d = mesh.data, nd = d.nodes.find((x) => x.id === mesh.sel);
  if (!nd) { $("#mesh-detail").innerHTML = `<b>Clusters</b>${d.clusters.map((c) => `<div class="act-row"><span class="dot" style="background:${MESH_COLOURS[c.id % MESH_COLOURS.length]}"></span><div>${esc(c.name)}</div><span class="faint">${c.size}</span></div>`).join("")}`; return; }
  const nb = d.edges.filter((e) => e.source === nd.id || e.target === nd.id).map((e) => ({ id: e.source === nd.id ? e.target : e.source, s: e.similarity })).sort((a, b) => b.s - a.s);
  $("#mesh-detail").innerHTML = `<b>${esc(nd.title)}</b><div class="kv" style="margin-top:8px"><div>file</div><div class="mono">${esc(nd.id)}</div><div>chunks</div><div>${nd.chunks}</div>
    <div>cluster</div><div>${esc((d.clusters.find((c) => c.id === nd.cluster) || {}).name || nd.cluster)}</div></div>
    <h4 style="margin:12px 0 6px;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--faint)">About similar things</h4>
    ${nb.length ? nb.map((x) => `<div class="act-row" data-go="${esc(x.id)}" style="cursor:pointer"><span class="dot ok"></span><div>${esc((d.nodes.find((y) => y.id === x.id) || {}).title || x.id)}</div><span class="faint">${x.s}</span></div>`).join("") : `<div class="faint">no close neighbour: apart from the rest</div>`}`;
  $$("[data-go]", $("#mesh-detail")).forEach((r) => r.addEventListener("click", () => { mesh.sel = r.dataset.go; renderMesh(); meshDetail(); }));
}
(function wireMesh() {
  const svg = $("#mesh"); let drag = null;
  const pt = (e) => { const r = svg.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  const zoom = (f, x, y) => { const k = Math.min(5, Math.max(0.15, mesh.view.k * f)); mesh.view.x = x - (x - mesh.view.x) * (k / mesh.view.k); mesh.view.y = y - (y - mesh.view.y) * (k / mesh.view.k); mesh.view.k = k; applyMesh(); };
  svg.addEventListener("wheel", (e) => { e.preventDefault(); const p = pt(e); zoom(e.deltaY < 0 ? 1.12 : 1 / 1.12, p.x, p.y); }, { passive: false });
  svg.addEventListener("pointerdown", (e) => { drag = { start: pt(e), v: { ...mesh.view }, moved: false, nd: e.target.closest(".mnd")?.dataset.id }; svg.setPointerCapture(e.pointerId); });
  svg.addEventListener("pointermove", (e) => { if (!drag) return; const p = pt(e), dx = p.x - drag.start.x, dy = p.y - drag.start.y; if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.moved) { svg.classList.add("panning"); mesh.view.x = drag.v.x + dx; mesh.view.y = drag.v.y + dy; applyMesh(); } });
  svg.addEventListener("pointerup", () => { svg.classList.remove("panning"); if (drag && !drag.moved && mesh.data) { mesh.sel = drag.nd || null; renderMesh(); meshDetail(); } drag = null; });
  svg.addEventListener("dblclick", fitMesh);
  $("#mesh-in").addEventListener("click", () => { const r = svg.getBoundingClientRect(); zoom(1.2, r.width / 2, r.height / 2); });
  $("#mesh-out").addEventListener("click", () => { const r = svg.getBoundingClientRect(); zoom(1 / 1.2, r.width / 2, r.height / 2); });
  $("#mesh-fit").addEventListener("click", fitMesh);
  $("#mesh-rebuild").addEventListener("click", () => loadMesh(true));
})();

const agoTs = (t) => !t ? "never" : (Date.now() / 1000 - t) < 86400 * 2 ? ago(t) : new Date(t * 1000).toLocaleDateString();
const kn = { tab: "map", docs: null, mems: [], memSel: null, poll: null };
function knTab(t) {
  kn.tab = t;
  $$("#kn-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  $$("#view-knowledge .kn-pane").forEach((p) => p.hidden = p.dataset.pane !== t);
  if (t === "map") loadMesh().then(() => mesh.data?.nodes?.length && meshDetail());
  if (t === "mem") renderMemories();
  if (t === "search") setTimeout(() => $("#kn-q").focus(), 30);
}
$$("#kn-tabs .tab").forEach((b) => b.addEventListener("click", () => knTab(b.dataset.tab)));

async function renderKnowledge() {
  knTab(kn.tab);
  await renderDocuments();
  if (levelRank(state.level) >= 1) {
    try { kn.mems = (await api("/memories")).memories || []; } catch (e) { kn.mems = []; }
  }
  $("#kn-count-mem").textContent = levelRank(state.level) >= 1 ? kn.mems.length : "";
  knKpis();
}
function knKpis() {
  const d = kn.docs; if (!d) return;
  const count = (st) => d.documents.filter((x) => x.status === st).length;
  const pending = count("not indexed") + count("changed");
  const kpi = (label, value, sub, dot = "") => `<div class="kpi"><div class="label">${dot ? `<span class="dot ${dot}"></span>` : ""}${label}</div><div class="value${String(value).length > 7 ? " small" : ""}">${value}</div><div class="sub">${sub}</div></div>`;
  $("#kn-kpis").innerHTML = [
    kpi("Documents", d.documents.length, `${fmtBytes(d.bytes)} · ${[...new Set(d.documents.map((x) => x.type))].join(", ") || "none"}`),
    kpi("Passages indexed", d.passages.toLocaleString(), "what search ranks and the agent reads"),
    kpi("Index", pending ? `${pending} to index` : "up to date", d.reindex?.running ? "indexing now…" : `last indexed ${agoTs(d.index_updated)}`, d.reindex?.running ? "warn" : pending ? "warn" : "ok"),
    kpi("Relevance bar", d.settings.min_relevance, `re-ranker ${String(d.settings.reranker || "").split("/").pop() || "?"}`),
    kpi("Embedder", String(d.settings.embedder || "?").split("/").pop(), "meaning search, on the GPU when there is one"),
    kpi("Memories", levelRank(state.level) >= 1 ? kn.mems.length : "—", levelRank(state.level) >= 1 ? `${new Set(kn.mems.map((m) => m.user)).size} users` : "admin and root"),
  ].join("");
}

async function renderDocuments() {
  let d;
  try { d = kn.docs = await api("/documents"); }
  catch (e) { $("#doc-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  $("#kn-folder").textContent = d.folder || "";
  $("#kn-count-docs").textContent = d.documents.length;
  $("#doc-types").textContent = (d.settings.supported || []).join(" ");
  const admin = levelRank(state.level) >= 1;
  $("#doc-reindex").hidden = $("#doc-reindex-all").hidden = !admin;
  const job = d.reindex || {};
  $("#doc-job").innerHTML = job.running ? `<div class="banner warn" style="margin:10px">Indexing${job.force ? " every document" : " what changed"}… started ${ago(job.started)} by ${esc(job.by || "")}. Search keeps working on the index as it was.</div>`
    : job.error ? `<div class="banner bad" style="margin:10px">The last indexing failed: ${esc(job.error)}</div>`
    : job.finished && Date.now() / 1000 - job.finished < 600 ? `<div class="banner ok" style="margin:10px">Indexed ${ago(job.finished)}: ${(job.added || []).length} added, ${(job.changed || []).length} changed, ${(job.removed || []).length} removed${(job.failed || []).length ? `, ${job.failed.length} failed` : ""}.</div>` : "";
  if (d.removed_since_index?.length) $("#doc-job").innerHTML += `<div class="banner warn" style="margin:10px">${d.removed_since_index.length} indexed document(s) are no longer in the folder: index changes to drop them.</div>`;
  const most = Math.sqrt(Math.max(1, ...d.documents.map((x) => x.passages || 0)));
  const f = ($("#doc-filter").value || "").toLowerCase();
  const rows = d.documents.filter((x) => !f || x.name.toLowerCase().includes(f) || x.type.includes(f));
  const statusOf = (x) => x.status === "indexed" ? `<span class="dstat"><span class="dot ok"></span>indexed</span>`
    : x.status === "changed" ? `<span class="dstat"><span class="dot warn"></span>changed since</span>`
    : x.status === "not indexed" ? `<span class="dstat"><span class="dot warn"></span>not indexed</span>` : `<span class="dstat"><span class="dot"></span>${esc(x.status)}</span>`;
  $("#doc-list").innerHTML = rows.length ? `<table class="tbl"><thead><tr><th>document</th><th class="num hide-sm">size</th><th class="hide-sm">passages</th><th class="hide-sm">indexed</th><th></th></tr></thead><tbody>${rows.map((x) => `
    <tr data-name="${esc(x.name)}"><td><div class="doc-name"><span class="ftype ${esc(x.type)}">${esc(x.type)}</span><span class="nm" title="${esc(x.name)}">${esc(x.name)}</span></div>
        <div class="faint only-sm" style="font-size:11px;margin-top:3px">${fmtBytes(x.bytes)} · ${(x.passages || 0).toLocaleString()} passages · ${esc(x.status)}</div></td>
      <td class="num faint hide-sm">${fmtBytes(x.bytes)}</td>
      <td class="hide-sm"><div style="display:flex;gap:8px;align-items:center"><div class="bar" style="width:70px"><i style="width:${Math.max(2, 100 * Math.sqrt(x.passages || 0) / most)}%"></i></div><span class="mono faint" style="font-size:11.5px">${(x.passages || 0).toLocaleString()}</span></div></td>
      <td class="hide-sm">${statusOf(x)}<div class="faint" style="font-size:11px">${x.indexed_at ? agoTs(x.indexed_at) : ""}</div></td>
      <td class="doc-acts"><button class="btn sm ghost" data-act="ask" title="Search within the documents">Search</button>
        <a class="btn sm ghost" href="/documents/file?name=${encodeURIComponent(x.name)}" data-act="get" title="Download" aria-label="Download">↓</a>
        ${admin ? `<button class="btn sm ghost del" data-act="del" title="Remove from the corpus and the index" aria-label="Remove">✕</button>` : ""}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">${d.documents.length ? "Nothing matches the filter." : "No documents yet: drop some below."}</div>`;
  $$("#doc-list tr[data-name]").forEach((tr) => {
    const name = tr.dataset.name;
    tr.querySelector('[data-act="get"]').addEventListener("click", async (e) => {
      e.preventDefault();                    // with the session token, not a bare link
      try { const r = await fetch(`/documents/file?name=${encodeURIComponent(name)}`, { headers: { Authorization: `Bearer ${state.token}` } });
        if (!r.ok) throw new Error(`${r.status}`); const blob = await r.blob(); const a = document.createElement("a");
        a.href = URL.createObjectURL(blob); a.download = name.split("/").pop(); a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      } catch (err) { toast(`Download failed: ${err.message}`, "bad"); }
    });
    tr.querySelector('[data-act="ask"]').addEventListener("click", () => { knTab("search"); $("#kn-q").value = `${name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ")}: `; $("#kn-q").focus(); });
    tr.querySelector('[data-act="del"]')?.addEventListener("click", async () => {
      if (!confirm(`Remove ${name} from the corpus and its passages from the index?`)) return;
      try { const r = await api(`/documents?name=${encodeURIComponent(name)}`, { method: "DELETE" }); toast(`Removed ${name} (${r.passages} passages).`, "ok"); renderKnowledge(); }
      catch (e) { toast(e.message, "bad"); }
    });
  });
  clearTimeout(kn.poll);
  if (job.running) kn.poll = setTimeout(() => state.view === "knowledge" && renderDocuments().then(knKpis), 4000);
}
$("#doc-filter").addEventListener("input", () => kn.docs && renderDocuments());
async function reindex(force) {
  if (force && !confirm("Re-chunk and re-embed every document? With a large corpus this takes minutes; search keeps working meanwhile.")) return;
  try { await api("/documents/reindex", { method: "POST", body: { force } }); toast(force ? "Rebuilding the index…" : "Indexing changes…"); renderDocuments().then(knKpis); }
  catch (e) { toast(e.message, "bad"); }
}
$("#doc-reindex").addEventListener("click", () => reindex(false));
$("#doc-reindex-all").addEventListener("click", () => reindex(true));

function cleanPassage(t) {
  return String(t || "").replace(/^\s*\[[^\]\n]{1,120}\]\s*\n/, "").replace(/-\n(?=[a-z])/g, "")
    .replace(/([^\n.:;!?])\n(?=[^\n•\-*\d#])/g, "$1 ").replace(/\n{3,}/g, "\n\n").trim();
}
$("#kn-search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#kn-q").value.trim(); if (!q) return;
  $("#kn-results").innerHTML = `<div class="muted">Searching…</div>`;
  let r; try { r = await api(`/knowledge/search?q=${encodeURIComponent(q)}`); } catch (err) { $("#kn-results").innerHTML = `<div class="banner bad">${esc(err.message)}</div>`; return; }
  const bar = r.min_relevance ?? 0.1;
  const card = (h, miss) => `<div class="hit${miss ? " miss" : ""}"><header>
      <span class="relbar" title="relevance ${h.relevance ?? "?"} (bar ${bar})"><i style="width:${Math.round(100 * (h.relevance || 0))}%"></i><b style="left:${Math.round(100 * bar)}%"></b></span>
      <span class="rel">${h.relevance != null ? Number(h.relevance).toFixed(3) : "?"}</span>
      <span class="mono">${esc(h.source || "?")}${h.page ? ` · p.${esc(h.page)}` : ""}</span><div class="spacer"></div>
      <span class="faint" style="font-size:11px">${[h.keyword && "keyword", h.dense && "meaning"].filter(Boolean).join(" + ")}</span></header>
      <div class="txt">${esc(cleanPassage(h.text))}</div></div>`;
  $("#kn-results").innerHTML = `<div class="faint" style="font-size:12px;margin-bottom:8px">${r.ms} ms · best relevance ${r.best_relevance ?? "?"} against a bar of ${bar}${r.rewrites?.length ? ` · also tried: ${r.rewrites.map(esc).join("; ")}` : ""}</div>`
    + (r.abstained ? `<div class="banner warn" style="margin-bottom:10px">No passage clears the bar: the agent would say the documents do not cover this, and not answer it from them.</div>
        ${r.near_misses.length ? `<div class="faint" style="font-size:12px;margin:0 0 6px">Closest, judged not to answer it:</div>${r.near_misses.map((h) => card(h, true)).join("")}` : ""}`
      : r.hits.map((h) => card(h, false)).join(""));
  $$("#kn-results .hit").forEach((h) => h.addEventListener("click", () => h.classList.toggle("open")));
});

function renderMemories() {
  if (levelRank(state.level) < 1) { $("#mem-list").innerHTML = `<div class="empty">Memories are for admin and root.</div>`; return; }
  const f = ($("#mem-filter").value || "").toLowerCase();
  const rows = kn.mems.filter((m) => !f || `${m.user} ${m.path} ${m.preview}`.toLowerCase().includes(f));
  const users = [...new Set(rows.map((m) => m.user))].sort();
  const label = (u) => u === "~lessons" ? "Lessons · kept by the harness from what went wrong" : u;
  $("#mem-list").innerHTML = rows.length ? users.map((u) => `<div class="mem-user">${esc(label(u))}</div>` + rows.filter((m) => m.user === u).map((m) => `
      <div class="mem-row${kn.memSel && kn.memSel.user === m.user && kn.memSel.path === m.path ? " sel" : ""}" data-u="${esc(m.user)}" data-p="${esc(m.path)}">
        <div class="p">${esc(m.path)}</div><div class="pv">${esc(m.preview || "")}</div></div>`).join("")).join("")
    : `<div class="empty">${kn.mems.length ? "Nothing matches." : "No long-term memories yet."}</div>`;
  $$("#mem-list .mem-row").forEach((r) => r.addEventListener("click", () => openMemory(r.dataset.u, r.dataset.p)));
}
$("#mem-filter").addEventListener("input", renderMemories);
async function openMemory(user, path) {
  kn.memSel = { user, path }; renderMemories();
  let m; try { m = await api(`/memories/${encodeURIComponent(user)}/item?path=${encodeURIComponent(path)}`); }
  catch (e) { $("#mem-detail").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  $("#mem-title").textContent = path;
  $("#mem-meta").textContent = `${user} · ${m.content.length} characters · changed ${m.modified ? new Date(m.modified).toLocaleString() : "?"}`;
  $("#mem-detail").innerHTML = `<textarea id="mem-edit" spellcheck="false" maxlength="4000">${esc(m.content)}</textarea>
    <div class="row" style="margin-top:10px"><button class="btn primary" id="mem-save">Save</button><span class="faint" id="mem-chars" style="font-size:11.5px"></span>
      <div class="spacer"></div><button class="btn danger" id="mem-forget">Forget</button></div>`;
  const chars = () => $("#mem-chars").textContent = `${$("#mem-edit").value.length} / 4000`;
  chars(); $("#mem-edit").addEventListener("input", chars);
  $("#mem-save").addEventListener("click", async () => {
    try { await api(`/memories/${encodeURIComponent(user)}/item`, { method: "PUT", body: { path, content: $("#mem-edit").value } }); toast("Saved.", "ok"); renderKnowledge(); }
    catch (e) { toast(e.message, "bad"); }
  });
  $("#mem-forget").addEventListener("click", async () => {
    if (!confirm(`Forget ${path} for ${user}?`)) return;
    await api(`/memories/${encodeURIComponent(user)}?path=${encodeURIComponent(path)}`, { method: "DELETE" });
    toast("Forgotten.", "ok"); kn.memSel = null;
    $("#mem-detail").innerHTML = `<div class="empty">Forgotten.</div>`; $("#mem-title").textContent = "A memory"; $("#mem-meta").textContent = "";
    renderKnowledge();
  });
}

async function upload(files) {
  if (levelRank(state.level) < 1) { toast("Adding documents needs admin.", "warn"); return; }
  for (const file of [...files]) {
    const b64 = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.readAsDataURL(file); });
    toast(`Indexing ${file.name}…`);
    try { const r = await api("/documents", { method: "POST", body: { name: file.name, content_base64: b64 } }); toast(`${file.name} added${r.index?.failed?.length ? " (failed to index)" : ""}.`, r.index?.failed?.length ? "warn" : "ok"); }
    catch (e) { toast(`${file.name}: ${e.message}`, "bad"); }
  }
  renderKnowledge();
}
$("#doc-drop").addEventListener("click", () => $("#doc-file").click());
$("#doc-file").addEventListener("change", (e) => { if (e.target.files.length) upload(e.target.files); e.target.value = ""; });
$("#doc-drop").addEventListener("dragover", (e) => { e.preventDefault(); e.currentTarget.classList.add("over"); });
$("#doc-drop").addEventListener("dragleave", (e) => e.currentTarget.classList.remove("over"));
$("#doc-drop").addEventListener("drop", (e) => { e.preventDefault(); e.currentTarget.classList.remove("over"); if (e.dataTransfer.files.length) upload(e.dataTransfer.files); });

/* ================= profile, about ================= */
function renderIdentity() {
  setTimeout(renderAccounts, 0);
  $("#id-current").innerHTML = `<div>user</div><div>${esc(state.user)}</div><div>level</div><div><span class="tag ${state.level === "root" ? "warn" : state.level === "admin" ? "ok" : ""}">${esc(state.level)}</span></div><div>session</div><div>${state.token ? "signed in" : "observer, no session"}</div>`;
  const yes = (l) => levelRank(state.level) >= levelRank(l) ? "✓" : "";
  $("#id-levels").innerHTML = `<table class="tbl"><thead><tr><th>profile</th><th>may</th><th></th></tr></thead><tbody>
    <tr><td><b>observer</b></td><td>look at everything, ask anything; changes are refused</td><td>${yes("observer")}</td></tr>
    <tr><td><b>admin</b></td><td>approve changes; read the audit log, metrics and memories; add documents</td><td>${yes("admin")}</td></tr>
    <tr><td><b>root</b></td><td>everything admin may, and approve the destructive changes (delete, remove, restart, reboot)</td><td>${yes("root")}</td></tr></tbody></table>`;
}
function renderAbout() {
  const i = state.info || {};
  const light = document.documentElement.dataset.theme === "light";
  $("#about-body").innerHTML = `<div class="row" style="gap:14px;align-items:center;margin-bottom:12px"><img src="${light ? "/static/logo-light.png" : "/static/logo-dark.png"}" style="width:64px;height:64px;border-radius:16px" alt="">
    <div><b style="font-size:16px;letter-spacing:1.5px">CANAH</b><div class="muted">Controller Agnostic Network Agent Harness</div></div></div>
    <p class="muted" style="margin-top:0">An agent for networks of any controller and any domain. It reaches the controllers through the agentic MCP's common contract, grounds every figure in a tool result, and pauses every change for an operator of the right level.</p>
    <div class="kv"><div>model</div><div>${esc(i.model?.model || "–")}</div><div>tools</div><div>${i.tools ?? "–"}</div><div>subagents</div><div>${esc((i.subagents || []).join(", ") || "–")}</div>
    <div>skills</div><div>${esc((i.skills || []).join(", ") || "–")}</div><div>sandbox</div><div>${esc(i.sandbox || "none")}</div><div>licence</div><div>MIT</div></div>`;
}

/* ================= keyboard ================= */
let gPending = false;
document.addEventListener("keydown", (e) => {
  if ($("#shell").hidden || e.target.closest("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (gPending) { gPending = false; const v = { t: "topology", c: "controllers", h: "chat", a: "ops", w: "twins", k: "knowledge" }[e.key]; if (v) { go(v); e.preventDefault(); } return; }
  if (e.key === "g") { gPending = true; setTimeout(() => { gPending = false; }, 900); return; }
  if (e.key === "[") $("#nav-toggle").click();
  else if (e.key === "]") $("#side-toggle").click();
  else if (e.key === "/") { e.preventDefault(); go("chat"); }
  else if (e.key === "n") { go("chat"); openThread(""); }
  else if (state.view === "topology" && (e.key === "+" || e.key === "=")) zoomAt(1.2, ...center());
  else if (state.view === "topology" && (e.key === "-" || e.key === "−")) zoomAt(1 / 1.2, ...center());
  else if (state.view === "topology" && e.key === "0") fitTopology();
  else if (state.view === "topology" && e.key === "e") toggleAll();
  else if (e.key === "Escape") { state.selected = null; renderDetail(null); renderTopology(); }
});

boot();

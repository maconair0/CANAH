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
  refreshTwinPill();
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
const VIEWS = ["topology", "controllers", "chat", "ops", "twins", "knowledge", "identity", "about"];
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

async function loadTopology(refresh = false) {
  try {
    const t = await api(`/topology${refresh ? "?refresh=1" : ""}`);
    state.topology = t; buildGraph(t);
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
  const nodes = [{ id: "canah", kind: "hub", label: "CANAH", ok: true }];
  const links = [];
  for (const c of t.controllers || []) {
    const cid = `ctl:${c.key}`;
    nodes.push({ id: cid, kind: "controller", label: c.label || c.key, key: c.key, domain: c.domain, ok: c.ok, c,
      sub: `${c.domain || "?"} · ${c.ok ? (c.devices_total > c.devices.length ? `${c.devices.length} of ${c.devices_total} devices` : `${c.devices.length} devices`) : "unreadable"}` });
    links.push({ kind: "delegation", source: "canah", target: cid, ok: c.ok });
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
  // The agents CANAH delegates to: shown when CANAH is opened, at work or not.
  const subs = state.info?.subagent_detail || (state.info?.subagents || []).map((n) => ({ name: n }));
  for (const sd of subs) {
    const sid = `sub:${sd.name}`;
    nodes.push({ id: sid, kind: "sub", label: sd.name, parent: "canah", ok: true, sd });
    links.push({ kind: "subdeleg", source: "canah", target: sid, ok: true, sub: sd.name });
    if (keys.has(sd.name)) links.push({ kind: "drives", source: sid, target: `ctl:${sd.name}`, ok: true, sub: sd.name });
  }
  topo.nodes = nodes; topo.links = [...links, ...cross.values()];
  layout();
}

function layout() {
  const ctls = topo.nodes.filter((n) => n.kind === "controller");
  const devsOf = (cid) => topo.nodes.filter((n) => n.parent === cid);
  const clusterR = (cid) => topo.open[cid] ? 60 * Math.sqrt(Math.max(1, devsOf(cid).length)) + 60 : 0;
  const biggest = Math.max(0, ...ctls.map((c) => clusterR(c.id)));
  const subs = topo.nodes.filter((n) => n.kind === "sub");
  const rs = Math.max(150, (subs.length * 140) / (2 * Math.PI));
  let R = Math.max(250, ctls.length > 1 ? (ctls.length * (190 + biggest * 1.1)) / (2 * Math.PI) : 250);
  if (topo.open.canah) R = Math.max(R, rs + 200);
  const pos = { canah: { x: 0, y: 0 } };
  // A controller's subagent sits on the line to its controller; the others in the gaps between.
  const ctlAngle = {}; ctls.forEach((c, i) => { ctlAngle[c.key] = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(1, ctls.length); });
  const others = subs.filter((n) => ctlAngle[n.label] === undefined);
  const gaps = ctls.length ? ctls.map((c, i) => ctlAngle[c.key] + Math.PI / ctls.length) : [-Math.PI / 2];
  const perGap = {};
  others.forEach((n, i) => { const g = i % gaps.length; perGap[g] = (perGap[g] || 0) + 1; n._gap = g; n._nth = perGap[g] - 1; });
  subs.forEach((n) => {
    let a;
    if (ctlAngle[n.label] !== undefined) a = ctlAngle[n.label];
    else { const cnt = perGap[n._gap], span = (2 * Math.PI / Math.max(1, gaps.length)) * 0.55; a = gaps[n._gap] + (cnt > 1 ? (n._nth / (cnt - 1) - 0.5) * span : 0); }
    pos[n.id] = { x: Math.cos(a) * rs, y: Math.sin(a) * rs };
  });
  ctls.forEach((c, i) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(1, ctls.length);
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
      const p = S("path", { d: `M${a.x},${a.y} L${b.x},${b.y}`, class: `lk ${l.kind}${on ? " on" : ""}` });
      p.append(S("title", {}, l.kind === "drives" ? `${l.sub} works ${l.target.slice(4)} through its tools` : `CANAH delegates to ${l.sub}`));
      gl.append(p); continue;
    }
    if (l.kind === "delegation") { d = `M${a.x},${a.y} L${b.x},${b.y}`; cls = `lk delegation${l.ok ? "" : " bad"}`; }
    else {
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
      d = `M${a.x},${a.y} Q${mx - dy / len * 14},${my + dx / len * 14} ${b.x},${b.y}`; cls = `lk ${l.ok ? "ok" : "bad"}`;
    }
    const p = S("path", { d, class: cls });
    p.append(S("title", {}, l.kind === "delegation" ? `CANAH → ${l.target.slice(4)} through the agentic MCP${l.ok ? "" : " (cannot be read)"}` : `${l.l.id}: ${l.l.a} ⇄ ${l.l.z} · ${l.l.state}`));
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
      if (nsub) g.append(S("text", { y: 80, "text-anchor": "middle", class: "grp" }, topo.open.canah ? `▾ ${nsub} subagents` : `▸ ${nsub} subagents`));
    } else if (n.kind === "sub") {
      const on = activeAgents().has(n.label), w = 128, h = 30;
      const col = (state.topology?.controllers || []).some((c) => c.key === n.label)
        ? DOMAIN_COLOR[domainClass((state.topology.controllers.find((c) => c.key === n.label) || {}).domain)] : "var(--accent2)";
      g.setAttribute("class", `nd sub${on ? " on" : ""}${state.selected === n.id ? " sel" : ""}`);
      g.append(S("rect", { x: -w / 2 - 4, y: -h / 2 - 4, width: w + 8, height: h + 8, rx: 19, class: "halo" }));
      g.append(S("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 15, fill: "var(--panel)", stroke: on ? "var(--accent)" : col, class: "body" }));
      g.append(S("circle", { cx: -w / 2 + 13, cy: 0, r: 4, fill: on ? "var(--accent)" : "var(--faint)", class: on ? "led" : "" }));
      const used = state.activity?.used?.[n.label];
      g.append(S("text", { x: 6, y: 4.5, "text-anchor": "middle" }, `${n.label.length > 14 ? n.label.slice(0, 13) + "…" : n.label}${n.sd?.tools != null ? ` · ${n.sd.tools}` : ""}`));
      g.append(S("title", {}, `${n.label}: ${on ? "at work now" : "idle"}${used ? ` · ${used.calls} calls, last ${ago(used.last)}` : ""}\n${n.sd?.description || ""}`));
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
    k("Model", esc((state.info?.model?.model || "–").replace(/^ollama:/, "")), `${state.info?.tools ?? "–"} tools · ${(state.info?.subagents || []).length} subagents`, "ok", true),
  ].join("");
}

function hubAgents() {
  const act = activeAgents(), used = state.activity?.used || {};
  const subs = state.info?.subagent_detail || [];
  const runs = state.activity?.running || [];
  return subs.map((sd) => { const on = act.has(sd.name), u = used[sd.name];
      return `<div class="act-row"><span class="dot ${on ? "busy" : u ? "ok" : ""}"></span><div><b>${esc(sd.name)}</b>${sd.tools != null ? ` <span class="faint">${sd.tools} tools</span>` : ""}
        <div class="faint">${esc((sd.description || "").slice(0, 110))}</div></div><span class="faint">${on ? "<b style='color:var(--accent)'>at work</b>" : u ? `${u.calls} calls · ${ago(u.last)}` : "idle"}</span></div>`; }).join("")
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
      <div>subagents</div><div>${esc((i.subagents || []).join(", ") || "–")}</div><div>sandbox</div><div>${esc(i.sandbox || "none")}</div>
      <div>middleware</div><div>${esc((i.middleware || []).join(", ") || "–")}</div><div>approvals</div><div>${(i.interrupt_on || []).length} tools pause for an operator</div>
      <div>agentic MCP</div><div class="mono">${esc(state.topology?.url || "–")}</div></div>
      <h4>Subagents ${topo.open.canah ? "" : "<span class='faint' style='text-transform:none;letter-spacing:0'>(click CANAH to show them)</span>"}</h4>
      <div id="hub-agents">${hubAgents()}</div>
      ${(state.topology?.relations || []).length ? `<h4>Across domains</h4><table class="tbl"><tbody>${state.topology.relations.slice(0, 40).map((r) => `<tr><td class="mono">${esc(r.from)}</td><td>${esc(r.relation)}</td><td class="mono">${esc(r.to)}</td></tr>`).join("")}</tbody></table>` : ""}
      <div class="row" style="margin-top:12px">${ask("Which controllers do you hold, and are they all healthy?")}</div>`;
  } else if (n.kind === "controller") {
    const c = n.c;
    title.textContent = c.label || c.key;
    const verbs = Object.entries(c.verbs || {});
    body.innerHTML = `${c.ok ? "" : `<div class="banner bad">Cannot be read: ${esc(c.error || "unknown")}</div>`}
      <div class="kv"><div>key</div><div class="mono">${esc(c.key)}</div><div>domain</div><div><span class="dshape ${domainClass(c.domain)}"></span>${esc(c.domain || "–")}</div>
      <div>family</div><div>${esc(c.family || "–")}</div><div>tools</div><div>${c.tools ?? "–"}</div><div>contract</div><div>${esc(c.verdict || "–")}</div>
      <div>devices</div><div>${c.devices.length}${c.devices_total > c.devices.length ? ` shown of ${c.devices_total} (the map draws at most ${c.devices.length})` : ""}</div><div>links</div><div>${c.links.length} (${c.links.filter((l) => l.state === "down").length} down)</div></div>
      ${(c.gaps || []).length ? `<h4>Gaps</h4><div class="chips">${c.gaps.map((g) => `<span class="chip">${esc(g)}</span>`).join("")}</div>` : ""}
      ${verbs.length ? `<h4>Contract verbs it serves</h4><table class="tbl"><tbody>${verbs.map(([v, t]) => `<tr><td>${esc(v.replace(/_/g, " "))}</td><td class="mono">${esc(t)}</td></tr>`).join("")}</tbody></table>` : ""}
      <div class="row" style="margin-top:12px">${ask(`Give me an overview of ${c.label || c.key}: its devices, links and anything unhealthy.`)}
        <button class="btn sm" id="dt-fold">${topo.open[n.id] ? "Fold" : "Open"} devices</button></div>`;
    $("#dt-fold").onclick = () => { topo.open[n.id] = !topo.open[n.id]; store.set("canah.topo.open", topo.open); layout(); renderTopology(); renderDetail(id); fitTopology(); };
  } else if (n.kind === "sub") {
    const sd = n.sd || {}, u = state.activity?.used?.[n.label], on = activeAgents().has(n.label);
    const ctl = (state.topology?.controllers || []).find((c) => c.key === n.label);
    title.textContent = `${n.label} (subagent)`;
    body.innerHTML = `<div class="kv"><div>status</div><div>${on ? `<span class="tag info">at work now</span>` : `<span class="tag">idle</span>`}</div>
      <div>tools</div><div>${sd.tools ?? "the main agent's"}</div><div>calls</div><div>${u ? `${u.calls} since the service started, last ${ago(u.last)}` : "none yet"}</div>
      ${ctl ? `<div>controller</div><div>${esc(ctl.label)} (${esc(ctl.domain)})</div>` : ""}</div>
      <h4>What it does</h4><p class="muted" style="margin:0;font-size:12.5px">${esc(sd.description || "–")}</p>
      <div class="row" style="margin-top:12px"><button class="btn sm" data-ask="${esc(`Ask the ${n.label} subagent: what can you do, and with which tools?`)}">Ask CANAH</button></div>`;
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
  loadKinds(); onboardAllowed();
  try {
    const n = state.network = await api("/network");
    $("#ctl-src").textContent = n.url ? `agentic MCP at ${n.url}` : "";
    $("#ctl-banner").innerHTML = n.ok ? "" : `<div class="banner bad">The agentic MCP did not answer: ${esc(n.error)}</div>`;
    const topoBy = Object.fromEntries((state.topology?.controllers || []).map((c) => [c.key, c]));
    const rows = n.controllers || [];
    $("#ctl-table").innerHTML = rows.length ? `<table class="tbl"><thead><tr><th>controller</th><th>domain</th><th>family</th><th class="num">tools</th><th>contract</th><th>read</th></tr></thead><tbody>
      ${rows.map((c) => { const t = topoBy[c.key]; const readable = t ? t.ok : null; return `<tr class="click" data-key="${esc(c.key)}"><td><b>${esc(c.label || c.key)}</b><div class="faint mono" style="font-size:11px">${esc(c.key)}${c.base_url ? " · " + esc(c.base_url) : ""}</div></td>
        <td><span class="dshape ${domainClass(c.domain)}"></span>${esc(c.domain || "–")}</td><td>${esc(c.family || "–")}</td><td class="num">${c.tools ?? "–"}</td>
        <td><span class="tag ${/gap/.test(c.conformance?.verdict || "") ? "warn" : c.conformance?.verdict ? "ok" : ""}">${esc(c.conformance?.verdict || "–")}</span></td>
        <td>${c.error ? `<span class="tag bad">error</span>` : readable === null ? `<span class="tag">–</span>` : readable ? `<span class="tag ok">${t.devices.length} devices</span>` : `<span class="tag bad" title="${esc(t.error || "")}">unreadable</span>`}</td></tr>`; }).join("")}</tbody></table>` : `<div class="empty">No controllers.</div>`;
    $$("#ctl-table tr.click").forEach((tr) => tr.addEventListener("click", () => controllerDetail(tr.dataset.key)));
  } catch (e) { $("#ctl-banner").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
}
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
  } catch (e) { /* the list stays as it is */ }
}
function onboardAllowed() {
  const ok = levelRank(state.level) >= 1;
  $$("#onboard-form input, #onboard-form select, #onboard-form button, #onboard-card label.btn input, #ob-guide-kind").forEach((x) => x.disabled = !ok);
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
      : `<span class="tag ok">onboarded</span> ${esc(m.label || body.key)}: contract ${esc(conf.verdict || "checked")}${(conf.gaps || []).length ? `, ${conf.gaps.length} gaps` : ""}. <button class="btn sm primary" type="button" id="ob-reload">Reload CANAH to give it a subagent</button>`;
    $("#ob-reload")?.addEventListener("click", reloadAgent);
    renderControllers(); loadTopology(true);
  } catch (err) { $("#ob-msg").innerHTML = `<span class="tag bad">failed</span> ${esc(err.message)}`; }
  finally { $("#ob-go").disabled = false; }
});
async function reloadAgent() {
  toast("Rebuilding the agent…");
  try {
    const r = await api("/admin/reload", { method: "POST" });
    toast(`CANAH reloaded: ${r.tools} tools; subagents ${(r.subagents || []).join(", ")}.`, "ok");
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
    try { await api(`/network/controllers/${encodeURIComponent(key)}`, { method: "DELETE" }); toast(`${key} removed. Reload CANAH to drop its subagent.`, "ok"); renderControllers(); loadTopology(true); }
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
$("#hide-tools").addEventListener("change", (e) => { document.body.classList.toggle("hide-tools", e.target.checked); store.set("canah.hide-tools", e.target.checked); });
if (store.get("canah.hide-tools")) { $("#hide-tools").checked = true; document.body.classList.add("hide-tools"); }

const SUGGEST = ["Which controllers do you hold, and are they healthy?", "Show me the topology of each network.", "Are any links down right now?",
  "What services are provisioned, and where?", "What can each controller do, by the common contract?"];
const logoSrc = () => document.documentElement.dataset.theme === "light" ? "/static/logo-light.png" : "/static/logo-dark.png";
const col = () => $("#messages");
const scrollDown = () => { const s = $("#lg-scroll"); s.scrollTop = s.scrollHeight; };
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
  if (!id) { box.innerHTML = welcome(); return; }
  try {
    const h = await api(`/threads/${id}/history`);
    if (!h.messages.length) box.innerHTML = welcome();
    let turn = null, asked = "";
    const finish = (t) => { if (!t) return; t.flow.done = true; t.flow.ms = null; t.done(); };
    for (const m of h.messages) {
      if (m.role === "user") { finish(turn); addUser(m.text); asked = String(m.text).split(ATT)[0]; turn = null; continue; }
      turn = turn || addAgent(asked);
      if (m.role === "tool") {
        const c = turn.tool(m.name, m.args); if (m.result != null) turn.result(c, m.result);
        const st = flowCall(turn.flow, m.name, m.args || {}); st.ms = null; turn.flow.open.pop(); st.result = m.result;
        if (st.to) turn.flow.delegating.pop();
      } else { turn.text(m.text); turn.flow.reply = m.text; }
    }
    finish(turn);
    if (turn) showFlow(turn);
    const p = await api(`/threads/${id}/pending`);
    if (p.pending?.length) { const t = turn || addAgent(); showPending(t, p.pending); t.done(); }
  } catch (e) { box.innerHTML = welcome(); }
  scrollDown();
}
function renderAgentStrip(active = new Set()) {
  const subs = state.info?.subagents || [];
  const color = (s) => DOMAIN_COLOR[domainClass((state.topology?.controllers || []).find((c) => c.key === s)?.domain || "other")];
  $("#agents-strip").innerHTML = [`<span class="agent-chip${active.has("main") ? " active" : ""}" style="--c:var(--accent)"><span class="dot"></span>CANAH</span>`,
    ...subs.map((s) => `<span class="agent-chip${active.has(s) ? " active" : ""}" style="--c:${color(s)}"><span class="dot"></span>${esc(s)}</span>`)].join("");
}

const ATT = "\n\n[attachments]\n";
function addUser(text) {
  const box = col(); $(".welcome", box)?.remove();
  const [words, att] = String(text).split(ATT);
  const files = att ? [...att.matchAll(/^- (\/uploads\/\S+)/gm)].map((m) => m[1].split("/").pop()) : [];
  const m = document.createElement("div"); m.className = "lg-user";
  m.innerHTML = `${files.length ? `<div class="lg-files">${files.map((f) => `<span class="fchip"><span class="nm"><i class="ficon"></i>${esc(f)}</span></span>`).join("")}</div>` : ""}<div class="b"></div>`;
  $(".b", m).textContent = words; if (!words.trim()) $(".b", m).remove();
  box.append(m); scrollDown();
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
  const ctl = (state.topology?.controllers || []).find((c) => c.key === a);
  if (ctl) return DOMAIN_COLOR[domainClass(ctl.domain)];
  const subs = state.info?.subagents || [];
  return AGENT_COLORS[(subs.indexOf(a) + 1) % AGENT_COLORS.length];
}
function newFlow(question) { return { question, steps: [], open: [], delegating: [], t0: performance.now(), done: false }; }
// Who took a step: a subagent when its tool carries the subagent's prefix (onos__…), or while a delegation to it
// (task) is still open; CANAH otherwise.
function flowCall(flow, name, args) {
  const subs = state.info?.subagents || [];
  const prefix = String(name).split("__")[0];
  let agent = "CANAH";
  if (String(name).includes("__") && subs.includes(prefix)) agent = prefix;
  else if (flow.delegating.length && name !== "task") agent = flow.delegating[flow.delegating.length - 1];
  const step = { kind: name === "task" ? "delegate" : "call", agent, name, args, t: performance.now(), ms: null, result: null,
                 to: name === "task" ? (args?.subagent_type || "general-purpose") : null };
  if (step.to) flow.delegating.push(step.to);
  flow.steps.push(step); flow.open.push(step);
  return step;
}
function flowResult(flow, name, content) {
  const k = flow.open.findIndex((o) => o.name === name);
  const step = k >= 0 ? flow.open.splice(k, 1)[0] : null;
  if (!step) return;
  step.ms = performance.now() - step.t; step.result = content;
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

function addAgent(question = "") {
  const box = col(); $(".welcome", box)?.remove();
  const m = document.createElement("div"); m.className = "lg-ai";
  m.innerHTML = `<div class="av"><img src="${logoSrc()}" alt=""></div><div class="segs"><div class="working"><span class="bars"><i></i><i></i><i></i></span>reading the network…</div></div>`;
  box.append(m); scrollDown();
  const segs = $(".segs", m);
  let last = null;                                     // the text segment the tokens go into
  const working = () => $(".working", segs);
  const turn = {
    el: m, segs, raw: "", flow: newFlow(question),
    token(t) { working()?.remove(); if (!last) { last = document.createElement("div"); last.className = "reply-rich"; last._raw = ""; segs.append(last); } last._raw += t; last.innerHTML = md(last._raw); scrollDown(); },
    text(t) { if (!t) return; working()?.remove(); last = document.createElement("div"); last.className = "reply-rich"; last._raw = t; last.innerHTML = md(t); segs.append(last); },
    final(t) {     // the reply as the agent ended it: replaces what streamed after the last tool card
      working()?.remove();
      if (!t) return;
      if (last && last.parentNode === segs && segs.lastElementChild === last) { last._raw = t; last.innerHTML = md(t); }
      else turn.text(t);
    },
    tool(name, args) {
      working()?.remove(); last = null;
      const write = (state.info?.interrupt_on || []).includes(name);
      const c = document.createElement("div"); c.className = `lg-tool${write ? " write" : ""}`;
      c.innerHTML = `<div class="lg-tool-h"><span class="n">${esc(name)}</span><span class="k">${write ? "change" : "tool call"}</span><span class="st"><span class="dot busy"></span></span></div>${kvTable(args)}`;
      segs.append(c); scrollDown(); return c;
    },
    result(c, content) {
      if (!c) return;
      $(".st", c).innerHTML = /^error/i.test(String(content)) ? `<span class="tag bad">error</span>` : `<span class="tag ok">done</span>`;
      const r = document.createElement("div"); r.className = "lg-res";
      let body; try { const j = JSON.parse(content); body = j && typeof j === "object" && !Array.isArray(j) && Object.keys(j).length <= 14 ? kvTable(j) : `<pre>${esc(JSON.stringify(j, null, 2))}</pre>`; }
      catch (e) { body = `<pre>${esc(content)}</pre>`; }
      const long = String(content).length > 500;
      r.innerHTML = `<div class="h">tool result${long ? `<button type="button">Expand</button>` : ""}</div>${body}`;
      if (long) $("button", r).addEventListener("click", (e) => { r.classList.toggle("open"); e.target.textContent = r.classList.contains("open") ? "Collapse" : "Expand"; });
      c.append(r);
    },
    done() {
      working()?.remove();
      if (!$(".acts", segs)) {
        const a = document.createElement("div"); a.className = "acts";
        a.innerHTML = `<button type="button" title="Copy the answer">Copy</button>`;
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
          const sub = d.name === "task" ? d.args?.subagent_type : String(d.name).split("__")[0];
          if (sub && (state.info?.subagents || []).includes(sub)) active.add(sub);
          renderAgentStrip(active); noteActivity(active);
          open.push({ name: d.name, card: turn.tool(d.name, d.args || {}) });
          flowCall(turn.flow, d.name, d.args || {}); renderFlow(turn);
        } else if (ev === "tool_result") {
          const k = open.findIndex((o) => o.name === d.name); const o = k >= 0 ? open.splice(k, 1)[0] : open.shift();
          turn.result(o?.card, d.content);
          flowResult(turn.flow, d.name, d.content); renderFlow(turn);
        } else if (ev === "reply") {
          if (d.thread_id && !state.chat.thread) state.chat.thread = d.thread_id;
          turn.final(d.text || "");
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
const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;
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
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) { const buf = []; i++; while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]); i++; out.push(`<pre>${esc(buf.join("\n"))}</pre>`); continue; }
    if (/^\s*\|.*\|\s*$/.test(l) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      const row = (r) => r.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
      const head = row(l); i += 2; const body = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) body.push(row(lines[i++]));
      out.push(`<table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`); continue;
    }
    const h = l.match(/^(#{1,4})\s+(.*)$/); if (h) { out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`); i++; continue; }
    if (/^\s*[-*•]\s+/.test(l)) { const it = []; while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) it.push(lines[i++].replace(/^\s*[-*•]\s+/, "")); out.push(`<ul>${it.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`); continue; }
    if (/^\s*\d+[.)]\s+/.test(l)) { const it = []; while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) it.push(lines[i++].replace(/^\s*\d+[.)]\s+/, "")); out.push(`<ol>${it.map((x) => `<li>${inline(x)}</li>`).join("")}</ol>`); continue; }
    if (!l.trim()) { i++; continue; }
    const p = []; while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*[-*•]\s|\s*\d+[.)]\s|\s*\|)/.test(lines[i])) p.push(lines[i++]);
    if (!p.length) p.push(lines[i++]);
    out.push(`<p>${p.map(inline).join("<br>")}</p>`);
  }
  return out.join("");
}

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
      k("Sent back · 24 h", Object.values(m.sent_back || {}).reduce((a, b) => a + b, 0), Object.entries(m.sent_back || {}).map(([a, b]) => `${a} ${b}`).join(" · ") || "by the checks"),
      k("Audit entries", a.audit.length, "latest shown")].join("");
    $("#ops-audit").innerHTML = a.audit.length ? `<table class="tbl"><thead><tr><th>when</th><th>event</th><th>who</th><th>tool</th><th>arguments / result</th></tr></thead><tbody>${a.audit.map((r) => `<tr><td style="white-space:nowrap">${esc(fmtTime(r.at))}</td><td><span class="tag ${r.event === "executed" ? "ok" : r.event === "decided" ? (r.decision === "reject" ? "bad" : "info") : r.event === "proposed" ? "warn" : ""}">${esc(r.event)}${r.decision ? " · " + esc(r.decision) : ""}</span></td><td>${esc(r.user)} <span class="faint">${esc(r.level)}</span></td><td class="mono">${esc(r.tool)}</td><td class="mono" style="font-size:11px;max-width:520px;overflow-wrap:anywhere">${esc(typeof r.args === "string" ? r.args : JSON.stringify(r.args)).slice(0, 260)}${r.result ? `<div class="faint">${esc(String(r.result).slice(0, 220))}</div>` : ""}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">The audit log is empty.</div>`;
  } catch (e) { $("#ops-audit").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
$("#ops-refresh").addEventListener("click", renderOps);

/* ================= digital twins ================= */
async function refreshTwinPill() {
  try {
    const t = await api("/twins");
    const act = (t.twins || []).find((x) => x.active);
    $("#st-twin span:last-child").textContent = act ? `twin: ${act.name}` : "no twin";
    $("#st-twin .dot").className = `dot ${act ? (act.ok ? "ok" : "bad") : ""}`;
    $("#st-twin").hidden = false;
    return t;
  } catch (e) { $("#st-twin").hidden = true; return null; }
}
$("#st-twin").addEventListener("click", () => go("twins"));
async function renderTwins() {
  const t = await refreshTwinPill();
  if (!t) { $("#tw-cards").innerHTML = `<div class="banner warn">This agent was built without digital twins: add one under <span class="mono">twins:</span> in canah.yaml.</div>`; return; }
  $("#tw-contract").textContent = `contract ${t.contract}`;
  const admin = levelRank(state.level) >= 1, root = levelRank(state.level) >= 2;
  $("#tw-cards").innerHTML = (t.twins || []).map((w) => `<div class="tw-card${w.active ? " active" : ""}">
      <div class="h"><span class="dot ${w.ok ? "ok" : "bad"}"></span><b>${esc(w.name)}</b>${w.active ? `<span class="tag info">in use</span>` : ""}
        <span class="tag">${esc(w.kind)}</span>${w.version ? `<span class="faint" style="font-size:11.5px">v${esc(w.version)}</span>` : ""}<div class="spacer"></div>
        ${w.declared ? `<span class="faint" style="font-size:11px">canah.yaml</span>` : ""}</div>
      <div class="where">${esc(w.url || w.spec || "")}${w.path ? ` · ${esc(w.path)}` : ""}${w.token_env ? ` · token: $${esc(w.token_env)}` : ""}</div>
      ${w.ok ? `<div class="muted" style="font-size:12.5px">${esc(w.about || "")}</div>
        ${(w.domains || []).map((d) => `<div class="tw-dom"><b>${esc(d.title || d.domain)}</b> <span class="faint mono" style="font-size:11px">${esc(d.domain)}</span><div>${esc(d.answers || "")}</div><div class="k">KPIs: ${esc((d.kpis || []).join(", "))}</div></div>`).join("")}
        <div class="faint" style="font-size:11.5px">can: ${esc((w.capabilities || []).join(", "))}${(w.controller_domains || []).length ? ` · checks writes on ${esc(w.controller_domains.join(", "))} controllers` : ""}${(w.extras || []).length ? ` · extras: ${esc(w.extras.map((x) => x.name).join(", "))}` : ""}</div>`
      : `<div class="banner bad" style="margin:0">${esc(w.error || "does not answer")}</div>`}
      <div class="acts">${!w.active ? `<button class="btn sm primary" data-use="${esc(w.name)}" ${admin ? "" : "disabled title='admin'"}>Use this twin</button>` : ""}
        <button class="btn sm" data-test="${esc(w.name)}">Test</button>
        ${!w.declared ? `<button class="btn sm danger" data-drop="${esc(w.name)}" ${root ? "" : "disabled title='root'"}>Remove</button>` : ""}</div>
    </div>`).join("") + `<div class="tw-card${t.active ? "" : " active"}"><div class="h"><b>No twin</b><div class="spacer"></div>${!t.active ? `<span class="tag info">in use</span>` : ""}</div>
      <div class="muted" style="font-size:12.5px">Answers come from the controllers alone, and writes go through approval alone, with no prediction first.</div>
      <div class="acts">${t.active ? `<button class="btn sm" data-use="none" ${admin ? "" : "disabled"}>Use no twin</button>` : ""}</div></div>`;
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

async function renderKnowledge() {
  loadMesh().then(() => mesh.data?.nodes?.length && meshDetail());
  try {
    const d = await api("/documents");
    $("#doc-folder").textContent = d.folder || "";
    $("#doc-list").innerHTML = d.documents.length ? `<table class="tbl"><tbody>${d.documents.map((n) => `<tr><td class="mono">${esc(n)}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">No documents yet.</div>`;
  } catch (e) { $("#doc-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  if (levelRank(state.level) < 1) { $("#mem-list").innerHTML = `<div class="empty">Memories are for admin and root.</div>`; return; }
  try {
    const m = await api("/memories");
    $("#mem-list").innerHTML = m.memories.length ? `<table class="tbl"><thead><tr><th>user</th><th>memory</th><th></th></tr></thead><tbody>${m.memories.map((x) => `<tr><td>${esc(x.user)}</td><td><span class="mono">${esc(x.path)}</span><div class="faint" style="font-size:11.5px">${esc(String(x.content || x.preview || "").slice(0, 200))}</div></td><td><button class="btn sm danger" data-u="${esc(x.user)}" data-p="${esc(x.path)}">Forget</button></td></tr>`).join("")}</tbody></table>` : `<div class="empty">No long-term memories.</div>`;
    $$("#mem-list [data-u]").forEach((b) => b.addEventListener("click", async () => { await api(`/memories/${encodeURIComponent(b.dataset.u)}?path=${encodeURIComponent(b.dataset.p)}`, { method: "DELETE" }); toast("Forgotten.", "ok"); renderKnowledge(); }));
  } catch (e) { $("#mem-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
async function upload(file) {
  if (levelRank(state.level) < 1) { toast("Adding documents needs admin.", "warn"); return; }
  const b64 = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.readAsDataURL(file); });
  toast(`Indexing ${file.name}…`);
  try { await api("/documents", { method: "POST", body: { name: file.name, content_base64: b64 } }); toast(`${file.name} added.`, "ok"); renderKnowledge(); }
  catch (e) { toast(e.message, "bad"); }
}
$("#doc-drop").addEventListener("click", () => $("#doc-file").click());
$("#doc-file").addEventListener("change", (e) => { if (e.target.files[0]) upload(e.target.files[0]); });
$("#doc-drop").addEventListener("dragover", (e) => { e.preventDefault(); e.currentTarget.classList.add("over"); });
$("#doc-drop").addEventListener("dragleave", (e) => e.currentTarget.classList.remove("over"));
$("#doc-drop").addEventListener("drop", (e) => { e.preventDefault(); e.currentTarget.classList.remove("over"); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); });

/* ================= profile, about ================= */
function renderIdentity() {
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

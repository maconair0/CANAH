/* What happens inside the digital twin, as it happens: the network each step is about, drawn per domain (one band
   per domain, stacked), and the steps themselves. Used by the Digital twins page ("In the twin now") and by the
   chat's twin pop-up. The events come from GET /twins/activity. */

const TV_DOM = { ip: "packet", packet: "packet", optical: "optical", photonic: "optical", wdm: "optical",
  "data-centre": "dc", fabric: "dc", wifi: "other", ntn: "other" };
const TV_DOM_NAME = { ip: "IP / packet", packet: "IP / packet", optical: "Optical", wifi: "Wi-Fi", ntn: "Satellite (NTN)", "data-centre": "Data centre" };
const TV_PATH_COLOURS = ["var(--accent)", "var(--brand-orange)", "var(--brand-green)", "#e879a6", "#a3e635"];

function tvHash(s) { let h = 2166136261; for (const c of String(s)) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967295; }

/* A long chain of spans (a route of amplifier sites) keeps its ends and a few sites each side. */
function tvCompact(view) {
  const deg = {}; view.links.forEach((l) => { deg[l.a] = (deg[l.a] || 0) + 1; deg[l.z] = (deg[l.z] || 0) + 1; });
  const n = view.nodes.length;
  if (n <= 16 || !view.nodes.every((x) => (deg[x.id] || 0) <= 2)) return view;
  // walk the chain from an end
  const adj = {}; view.links.forEach((l) => { (adj[l.a] = adj[l.a] || []).push(l); (adj[l.z] = adj[l.z] || []).push(l); });
  const start = view.nodes.find((x) => (deg[x.id] || 0) === 1)?.id; if (!start) return view;
  const order = [start], seenL = new Set();
  let cur = start;
  for (;;) { const l = (adj[cur] || []).find((y) => !seenL.has(y.id)); if (!l) break; seenL.add(l.id); cur = l.a === cur ? l.z : l.a; order.push(cur); }
  if (order.length < n) return view;
  const keep = new Set([...order.slice(0, 5), ...order.slice(-5)]);
  const gap = `… ${n - 10} sites`;
  const nodes = view.nodes.filter((x) => keep.has(x.id)).concat([{ id: gap, kind: "gap" }]);
  const links = view.links.filter((l) => keep.has(l.a) && keep.has(l.z));
  links.push({ id: "gap-a", a: order[4], z: gap, label: "", kind: "gap" }, { id: "gap-z", a: gap, z: order[n - 5], label: "", kind: "gap" });
  return { ...view, nodes, links, chain: order.filter((x) => keep.has(x)).slice(0, 5).concat([gap], order.slice(-5)) };
}

/* Positions in [0,1]²: the scenario's own coordinates when it has them, a chain in a line, else a force layout. */
function tvLayout(view) {
  const nodes = view.nodes, pos = {};
  if (view.chain) { view.chain.forEach((id, i) => { pos[id] = [i / (view.chain.length - 1), 0.5 + (i % 2 ? 0.12 : -0.12)]; }); return pos; }
  const xy = nodes.filter((n) => n.x != null && n.y != null), geo = nodes.filter((n) => n.lat != null && n.lon != null);
  const fit = (pts, key) => {
    const xs = pts.map((p) => p[key[0]]), ys = pts.map((p) => p[key[1]]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    pts.forEach((p) => { pos[p.id] = [x1 > x0 ? (p[key[0]] - x0) / (x1 - x0) : 0.5, y1 > y0 ? 1 - (p[key[1]] - y0) / (y1 - y0) : 0.5]; });
  };
  if (xy.length === nodes.length && nodes.length) { fit(xy, ["x", "y"]); return pos; }
  if (geo.length >= 2) {
    fit(geo, ["lon", "lat"]);
    const rest = nodes.filter((n) => !pos[n.id]);       // a constellation, say: above the ground sites
    rest.forEach((n, i) => { pos[n.id] = [(i + 1) / (rest.length + 1), -0.05]; });
    Object.values(pos).forEach((p) => { p[1] = 0.15 + p[1] * 0.85; });
    return pos;
  }
  // force-directed, the same every time for the same network
  const ids = nodes.map((n) => n.id).sort(), N = ids.length, k = 1 / Math.sqrt(Math.max(N, 1));
  ids.forEach((id, i) => { const a = 2 * Math.PI * i / N + tvHash(id) * 0.3; pos[id] = [0.5 + 0.35 * Math.cos(a), 0.5 + 0.35 * Math.sin(a)]; });
  const E = view.links.filter((l) => pos[l.a] && pos[l.z] && l.a !== l.z);
  for (let it = 0, t = 0.08; it < 220; it++, t *= 0.985) {
    const d = {}; ids.forEach((id) => { d[id] = [0, 0]; });
    for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
      const a = pos[ids[i]], b = pos[ids[j]]; let dx = a[0] - b[0], dy = a[1] - b[1]; const r = Math.max(Math.hypot(dx, dy), 0.01);
      const f = k * k / r; dx /= r; dy /= r; d[ids[i]][0] += dx * f; d[ids[i]][1] += dy * f; d[ids[j]][0] -= dx * f; d[ids[j]][1] -= dy * f;
    }
    E.forEach((l) => {
      const a = pos[l.a], b = pos[l.z]; let dx = a[0] - b[0], dy = a[1] - b[1]; const r = Math.max(Math.hypot(dx, dy), 0.01);
      const f = r * r / k; dx /= r; dy /= r; d[l.a][0] -= dx * f; d[l.a][1] -= dy * f; d[l.z][0] += dx * f; d[l.z][1] += dy * f;
    });
    ids.forEach((id) => {
      const [dx, dy] = d[id], m = Math.max(Math.hypot(dx, dy), 1e-9), s = Math.min(m, t);
      pos[id][0] += dx / m * s + (0.5 - pos[id][0]) * 0.01; pos[id][1] += dy / m * s + (0.5 - pos[id][1]) * 0.01;
    });
  }
  const xs = ids.map((i) => pos[i][0]), ys = ids.map((i) => pos[i][1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  ids.forEach((i) => { pos[i] = [x1 > x0 ? (pos[i][0] - x0) / (x1 - x0) : 0.5, y1 > y0 ? (pos[i][1] - y0) / (y1 - y0) : 0.5]; });
  // no two nodes on top of each other: their names must stay readable
  const gap = Math.max(0.07, 0.5 / Math.sqrt(Math.max(N, 1)));
  for (let it = 0; it < 80; it++) {
    let moved = false;
    for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
      const a = pos[ids[i]], b = pos[ids[j]]; let dx = b[0] - a[0], dy = b[1] - a[1]; let r = Math.hypot(dx, dy);
      if (r >= gap) continue;
      if (r < 1e-6) { const t = tvHash(ids[i] + ids[j]) * 6.283; dx = Math.cos(t); dy = Math.sin(t); r = 1; }
      const push = (gap - Math.min(r, gap)) / 2; dx /= r; dy /= r;
      a[0] -= dx * push; a[1] -= dy * push; b[0] += dx * push; b[1] += dy * push; moved = true;
    }
    if (!moved) break;
  }
  ids.forEach((i) => { pos[i] = [Math.min(1, Math.max(0, pos[i][0])), Math.min(1, Math.max(0, pos[i][1]))]; });
  return pos;
}

function tvShape(kind, x, y, r) {
  const k = String(kind || "").toLowerCase();
  if (/roadm|optical/.test(k)) return `<rect x="${x - r}" y="${y - r}" width="${2 * r}" height="${2 * r}" rx="2" transform="rotate(45 ${x} ${y})"/>`;
  if (/xpdr|xponder|transp/.test(k)) return `<rect x="${x - r * 0.8}" y="${y - r * 0.8}" width="${1.6 * r}" height="${1.6 * r}" rx="2"/>`;
  if (/^ap$|access/.test(k)) return `<path d="M${x} ${y - r * 1.1} L${x + r} ${y + r * 0.8} L${x - r} ${y + r * 0.8} Z"/>`;
  if (/constellation|sat/.test(k)) return `<path d="M${x - r * 1.3} ${y} L${x} ${y - r} L${x + r * 1.3} ${y} L${x} ${y + r} Z"/>`;
  if (/gap/.test(k)) return "";
  return `<circle cx="${x}" cy="${y}" r="${/station|host|terminal/.test(k) ? r * 0.7 : r}"/>`;
}

/* One domain's band: its nodes, links, what fails, the lightpaths' routes, the demands, the change proposed. */
function tvBand(view, y0, W, H, opt) {
  const v = tvCompact(view), pos = tvLayout(v), padX = 70, padY = 34;
  const P = (id) => pos[id] && [padX + pos[id][0] * (W - 2 * padX), y0 + padY + pos[id][1] * (H - 2 * padY - 8)];
  const failed = new Set(v.failed || []), touched = new Set(v.touched || []);
  const pathOf = {}; (v.paths || []).forEach((p, i) => p.links.forEach((l) => { (pathOf[l] = pathOf[l] || []).push(i); }));
  const dom = TV_DOM[v.domain] || "other";
  let g = `<g class="tv-band d-${dom}"><rect class="tv-bg" x="2" y="${y0 + 2}" width="${W - 4}" height="${H - 4}" rx="8"/>
    <text class="tv-dom" x="16" y="${y0 + 22}">${esc(TV_DOM_NAME[v.domain] || v.domain)}${v.source === "controller" ? ` · ${esc(v.controller)} now` : ""}</text>`;
  // the lightpaths' routes, under the links
  v.links.forEach((l) => {
    const a = P(l.a), b = P(l.z); if (!a || !b || !pathOf[l.id]) return;
    pathOf[l.id].forEach((pi, j) => {
      const off = (j - (pathOf[l.id].length - 1) / 2) * 5, nx = -(b[1] - a[1]), ny = b[0] - a[0], m = Math.hypot(nx, ny) || 1;
      g += `<line class="tv-route${opt.running ? " run" : ""}" style="stroke:${TV_PATH_COLOURS[pi % TV_PATH_COLOURS.length]}" x1="${a[0] + nx / m * off}" y1="${a[1] + ny / m * off}" x2="${b[0] + nx / m * off}" y2="${b[1] + ny / m * off}"/>`;
    });
  });
  v.links.forEach((l) => {
    const a = P(l.a), b = P(l.z); if (!a || !b) return;
    const cls = ["tv-link", l.kind || "", failed.has(l.id) ? "failed" : "", touched.has(l.id) ? "touched" : "", l.down ? "failed" : ""].join(" ");
    g += `<line class="${cls}" x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}"><title>${esc(l.id)}${l.label ? ` · ${esc(l.label)}` : ""}</title></line>`;
    const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
    if (failed.has(l.id)) g += `<g class="tv-cut"><circle cx="${mx}" cy="${my}" r="8"/><path d="M${mx - 4} ${my - 4}L${mx + 4} ${my + 4}M${mx + 4} ${my - 4}L${mx - 4} ${my + 4}"/></g>`;
    if (opt.labels && l.kind !== "radio" && l.kind !== "gap" && v.nodes.length <= 24)
      g += `<text class="tv-ll" x="${mx}" y="${my - 7}">${esc(l.id)}${l.label ? ` · ${esc(l.label)}` : ""}</text>`;
  });
  (v.proposed || []).forEach((l) => {
    const a = P(l.a), b = P(l.z); if (!a || !b) return;
    const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2 - 22;
    g += `<path class="tv-proposed" d="M${a[0]} ${a[1]} Q${mx} ${my - 20} ${b[0]} ${b[1]}"/><text class="tv-ll strong" x="${mx}" y="${my - 6}">${esc(l.id)}${l.label ? ` · ${esc(l.label)}` : ""} (the change)</text>`;
  });
  // demands between two nodes: a dotted arc
  (v.demands || []).forEach((d, i) => {
    const a = P(d.src), b = P(d.dst); if (!a || !b || d.src === d.dst) return;
    const mx = (a[0] + b[0]) / 2, my = Math.min(a[1], b[1]) - 26 - i * 8;
    g += `<path class="tv-demand" d="M${a[0]} ${a[1]} Q${mx} ${my} ${b[0]} ${b[1]}"><title>${esc(d.id)} ${esc(d.label)}</title></path>`;
  });
  v.nodes.forEach((n) => {
    const p = P(n.id); if (!p) return;
    const cls = ["tv-node", touched.has(n.id) ? "touched" : "", failed.has(n.id) ? "failed" : "", n.kind === "gap" ? "gap" : ""].join(" ");
    const showLabel = n.kind === "gap" || v.nodes.length <= 30 || touched.has(n.id) || failed.has(n.id);
    g += `<g class="${cls}">${tvShape(n.kind, p[0], p[1], 7)}<title>${esc(n.id)} (${esc(n.kind)})</title>${showLabel ? `<text x="${p[0]}" y="${p[1] + 20}">${esc(n.id)}</text>` : ""}</g>`;
  });
  return g + "</g>";
}

/* The views (one per domain, the latest of each), stacked; with a legend of what is drawn. */
function twinGraph(views, opt = {}) {
  const vs = (views || []).filter((v) => v && v.nodes && v.nodes.length);
  if (!vs.length) return `<div class="tv-none">${esc(opt.none || "No network to draw for this step.")}</div>`;
  const W = opt.width || 900, H = opt.band || (vs.length > 1 ? 230 : 300);
  const svg = vs.map((v, i) => tvBand(v, i * H, W, H, opt)).join("");
  const items = [];
  if (vs.some((v) => v.source !== "controller" && (v.failed || []).length)) items.push(`<span><i class="lg-cut"></i>fails in the scenario</span>`);
  if (vs.some((v) => v.source === "controller" && (v.failed || []).length)) items.push(`<span><i class="lg-cut"></i>down now</span>`);
  if (vs.some((v) => (v.paths || []).length)) items.push(`<span><i class="lg-route"></i>a lightpath's route</span>`);
  if (vs.some((v) => (v.demands || []).length)) items.push(`<span><i class="lg-demand"></i>a demand</span>`);
  if (vs.some((v) => (v.proposed || []).length)) items.push(`<span><i class="lg-prop"></i>the change being tried</span>`);
  if (vs.some((v) => (v.touched || []).length)) items.push(`<span><i class="lg-touch"></i>what the change touches</span>`);
  return `<svg class="tv-svg${opt.running ? " running" : ""}" viewBox="0 0 ${W} ${H * vs.length}" role="img" aria-label="the network in the digital twin">${svg}</svg>
    ${opt.legend !== false && items.length ? `<div class="tv-legend">${items.join("")}</div>` : ""}`;
}

/* ---------- the feed, shared by the page and the pop-up ---------- */
const tw = { seq: 0, events: [], timer: null, sel: "", popTimer: null, popFor: "", popClosedAt: {}, popDone: 0 };
const TV_GAP_S = 120;

async function twinPoll() {
  try {
    const r = await api(`/twins/activity?since=${tw.seq}`);
    if (r.seq < tw.seq) tw.events = [];            // the service restarted: start again
    tw.seq = r.seq;
    if (r.events.length) {
      const byOp = new Map(tw.events.map((e) => [e.seq, e]));
      r.events.forEach((e) => byOp.set(e.seq, e));
      tw.events = [...byOp.values()].sort((a, b) => a.seq - b.seq).slice(-600);
    }
    tw.running = r.running || [];
    return r;
  } catch (e) { return null; }
}

/* Sessions: a thread's steps with no pause longer than TV_GAP_S; each step once (its last event). */
function twinSessions() {
  const steps = new Map(); tw.events.forEach((e) => steps.set(e.op, e));
  const firstAt = new Map(); tw.events.forEach((e) => { if (!firstAt.has(e.op)) firstAt.set(e.op, e.at); });
  const list = [...steps.values()].map((e) => ({ ...e, started: firstAt.get(e.op) })).sort((a, b) => a.started - b.started);
  const out = [];
  list.forEach((s) => {
    const key = s.thread || "(no thread)";
    let cur = out.filter((x) => x.thread === key).pop();
    if (!cur || s.started - cur.last > TV_GAP_S) { cur = { id: `${key}@${Math.round(s.started)}`, thread: key, user: s.user, steps: [], first: s.started, last: s.at, twin: s.twin }; out.push(cur); }
    cur.steps.push(s); cur.last = Math.max(cur.last, s.at); cur.twin = s.twin || cur.twin;
  });
  out.forEach((x) => {
    x.running = x.steps.some((s) => s.state === "running");
    const views = {}; x.steps.forEach((s) => { if (s.view) views[s.view.domain + (s.view.source === "controller" ? "@" + s.view.controller : "")] = s.view; });
    x.views = Object.values(views);
    x.final = [...x.steps].reverse().find((s) => s.state !== "running" && s.outcome && (s.outcome.decision || s.outcome.action));
  });
  return out.sort((a, b) => b.last - a.last);
}

function tvOutcome(o) {
  if (!o) return "";
  const bits = [];
  const say = (a) => ({ no_twin: "could not check it", not_modelled: "cannot model this change", propose: "supports it",
    send: "supports it", reject: "advises against it", ask_operator: "needs the operator's answer", gather_more_data: "gather more data",
    not_met: "not met", met: "met" }[a] || String(a).replace(/_/g, " "));
  if (o.action) bits.push(`<span class="dec ${esc(o.action)}">${esc(say(o.action))}</span>`);
  if (o.decision && o.decision !== o.action) bits.push(`<span class="dec ${esc(o.decision)}">${esc(say(o.decision))}</span>`);
  if (o.p != null) bits.push(`<span class="faint">p ${Number(o.p).toFixed(2)}</span>`);
  if (o.trust) bits.push(`<span class="faint">trust ${esc(o.trust)}</span>`);
  if ((o.problems || []).length) bits.push(`<span class="bad-t">${esc(o.problems[0])}</span>`);
  const why = (o.questions || [])[0] || (o.reasons || [])[0];
  if (why) bits.push(`<span class="tv-why">${esc(why)}</span>`);
  return bits.join(" ");
}

function tvSteps(s, max = 50) {
  const icon = (st) => st === "running" ? `<span class="tv-spin" aria-label="running"></span>` : st === "error" ? `<span class="dot bad"></span>` : `<span class="dot ok"></span>`;
  return `<ol class="tv-steps">${s.steps.slice(-max).map((x) => `<li class="${x.state}">${icon(x.state)}
      <div><div class="t">${esc(x.agent || "CANAH")} <span class="faint">${esc(x.title)}</span>${x.took_s != null ? ` <span class="faint">· ${x.took_s} s</span>` : ""}</div>
      ${x.detail ? `<div class="d">${esc(x.detail)}</div>` : ""}
      ${x.state !== "running" ? `<div class="o">${tvOutcome(x.outcome)}${x.error && !(x.outcome || {}).problems ? ` <span class="bad-t">${esc(x.error)}</span>` : ""}${x.outcome?.run_id ? ` <a href="#" data-run="${esc(x.outcome.run_id)}">the run</a>` : ""}</div>` : ""}</div></li>`).join("")}</ol>`;
}

/* A thread's name as people know it: the conversation's title when it is one of yours, the watcher's incident. */
function tvThreadName(t) {
  t = String(t || "");
  if (t.startsWith("canah-watch.")) return t.replace(/^canah-watch\./, "watcher · ").replace("incident-", "incident #");
  if (state.user && t.startsWith(`${state.user}.`)) {
    const id = t.slice(state.user.length + 1);
    const th = (typeof threads === "function" ? threads() : []).find((x) => x.id === id);
    return (th && th.title ? th.title : "your conversation").slice(0, 34);
  }
  return t.slice(0, 30);
}

/* ---------- the Digital twins page: In the twin now ---------- */
function drawTwinNow() {
  const host = $("#tw-now"); if (!host) return;
  const ss = twinSessions();
  if (!ss.length) {
    host.innerHTML = `<div class="card tv-card"><header><h3>In the twin now</h3><div class="spacer"></div><span class="faint" style="font-size:12px">idle</span></header>
      <div class="body"><div class="empty">Nothing has gone through the twin since CANAH started. Ask a what-if in the chat, or ask for a change: each step the twin takes shows here as it happens.</div></div></div>`;
    return;
  }
  const s = ss.find((x) => x.id === tw.sel) || ss.find((x) => x.running) || ss[0];
  const cur = [...s.steps].reverse().find((x) => x.state === "running");
  const status = s.running ? `<span class="tv-live"><span class="tv-spin"></span>${esc(cur ? cur.title : "working")}</span>` : `<span class="faint">finished ${ago(s.last)}</span>`;
  const q = s.views.find((v) => v.question)?.question || s.steps.find((x) => x.detail)?.detail || "";
  host.innerHTML = `<div class="card tv-card${s.running ? " is-running" : ""}"><header><h3>In the twin now</h3><div class="spacer"></div>${status}</header>
    <div class="body">
      <div class="tv-sessions">${ss.slice(0, 8).map((x) => `<button class="tv-sess${x.id === s.id ? " sel" : ""}${x.running ? " running" : ""}" data-s="${esc(x.id)}" title="${esc(x.thread)}">
        ${x.running ? `<span class="tv-spin"></span>` : `<span class="dot ${x.final ? (/(propose|met)$/.test(x.final.outcome.decision || x.final.outcome.action || "") && !/not_met/.test(x.final.outcome.decision || "") ? "ok" : "warn") : "ok"}"></span>`}
        ${esc(tvThreadName(x.thread))} <span class="faint">${ago(x.last)}</span></button>`).join("")}</div>
      ${q ? `<p class="tv-q">${esc(q)}</p>` : ""}
      <div class="tv-grid"><div class="tv-canvas">${twinGraph(s.views, { running: s.running, labels: true, none: "The twin has not been given a network yet in this session: it is reading its example or the live network." })}</div>
        <div class="tv-side"><div class="label">Steps · ${esc(s.twin || "the twin")}</div>${tvSteps(s)}
          ${String(s.thread).startsWith(`${state.user}.`) ? `<button class="btn sm" data-open-thread="${esc(s.thread.slice(state.user.length + 1))}">Open the conversation</button>` : ""}</div></div>
    </div></div>`;
  $$("#tw-now .tv-sess").forEach((b) => b.addEventListener("click", () => { tw.sel = b.dataset.s; drawTwinNow(); }));
  $$("#tw-now a[data-run]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); if (typeof showTwinRun === "function") { twr.sel = a.dataset.run; showTwinRun(a.dataset.run); $("#twr-detail").scrollIntoView({ behavior: "smooth", block: "start" }); } }));
  $$("#tw-now [data-open-thread]").forEach((b) => b.addEventListener("click", () => { go("chat"); if (typeof openThread === "function") openThread(b.dataset.openThread); }));
}

async function twinNowLoop() {
  clearTimeout(tw.timer);
  if (state.view !== "twins" && !(state.view === "chat")) return;
  const before = tw.seq;
  await twinPoll();
  if (state.view === "twins" && (tw.seq !== before || twinSessions().some((x) => x.running) || !$("#tw-now").innerHTML)) drawTwinNow();
  if (state.view === "chat") drawTwinPop();
  const fast = state.chat.busy || (tw.running || []).length || $("#tv-pop")?.classList.contains("show");
  tw.timer = setTimeout(twinNowLoop, fast ? 900 : state.view === "twins" ? 2500 : 4000);
}

/* ---------- the chat's pop-up: what the twin is doing for this conversation ---------- */
function drawTwinPop() {
  const pop = $("#tv-pop"); if (!pop) return;
  const thread = state.chat.thread;
  const s = thread ? twinSessions().find((x) => x.thread === thread || x.thread === `${state.user}.${thread}`) : null;
  const now = Date.now() / 1000;
  // Between two of the twin's steps the agent is still at it: the pop-up stays until the answer is in, then
  // shows the twin's verdict for a few seconds and closes.
  const busy = s && (s.running || (state.chat.busy && now - s.last < 300));
  const fresh = s && (busy || (now - s.last < 60 && now - Math.max(s.last, tw.popDone || 0) < 5));
  if (s && busy) tw.popDone = 0; else if (s && !tw.popDone) tw.popDone = now;
  if (!s || !fresh || (tw.popClosedAt[s.id] && tw.popClosedAt[s.id] >= s.last)) { pop.classList.remove("show"); return; }
  const cur = [...s.steps].reverse().find((x) => x.state === "running");
  const last = s.steps[s.steps.length - 1];
  const now_ = s.running ? `${cur?.agent || "CANAH"} ${cur?.title || "is working"}…`
    : busy ? `${last?.agent || "CANAH"} is working with the twin's answer…` : "";
  pop.innerHTML = `<div class="tv-pop-h">${busy ? `<span class="tv-spin"></span>` : `<span class="dot ok"></span>`}
      <b>Digital twin</b><span class="faint">${esc(s.twin || "")}</span><div class="spacer"></div>
      <button class="btn sm ghost" id="tv-pop-open" title="See it on the Digital twins page">Open</button>
      <button class="btn sm ghost" id="tv-pop-x" aria-label="Close">✕</button></div>
    <div class="tv-pop-now">${busy ? esc(now_) : `Done: ${tvOutcome(s.final?.outcome) || "the twin has answered"}`}</div>
    <div class="tv-pop-g">${twinGraph(s.views, { running: s.running, legend: false, width: 460, band: 230, none: "Reading the network…" })}</div>
    ${tvSteps(s, 3)}`;
  pop.classList.add("show");
  $("#tv-pop-x").onclick = () => { tw.popClosedAt[s.id] = Date.now() / 1000; pop.classList.remove("show"); };
  $("#tv-pop-open").onclick = () => { tw.sel = s.id; go("twins"); };
}

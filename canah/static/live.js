/* The twin's runs (Digital twins page) and the live network (Live network page). Loaded after app.js. */

/* ---------- what the twin is working on ---------- */
const twr = { runs: [], sel: null };
async function renderTwinRuns() {
  let r; try { r = await api("/twins/runs?limit=40"); } catch (e) { $("#twr-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  twr.runs = r.runs || [];
  const pill = (d) => `<span class="dec ${esc(d || "none")}">${esc(String(d || "?").replace(/_/g, " "))}</span>`;
  $("#twr-list").innerHTML = twr.runs.length ? twr.runs.map((x) => `
    <div class="twr-row${twr.sel === x.run_id ? " sel" : ""}" data-id="${esc(x.run_id)}">
      <div class="q">${esc(x.question || "(no question)")}</div>
      <div class="m"><span class="chip d-${esc(x.domain || "")}">${esc(x.domain || "?")}</span>${pill(x.decision)}${x.hypothetical ? `<span class="hypo" title="a route built for a question: nothing in it exists in a network">hypothetical</span>` : ""}
        <span class="faint">${x.at ? ago(x.at) : ""}${x.by ? ` · ${esc(x.by)}` : ""}</span></div></div>`).join("")
    : `<div class="empty">The twin has not run anything yet. Ask a what-if in the chat.</div>`;
  $$("#twr-list .twr-row").forEach((el) => el.addEventListener("click", () => showTwinRun(el.dataset.id)));
  if (!twr.sel && twr.runs.length) showTwinRun(twr.runs[0].run_id);
}

async function showTwinRun(id) {
  twr.sel = id;
  $$("#twr-list .twr-row").forEach((el) => el.classList.toggle("sel", el.dataset.id === id));
  let r; try { r = await api(`/twins/runs/${encodeURIComponent(id)}`); } catch (e) { $("#twr-detail").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  const sc = r.scenario || {}, res = r.result || {};
  const dec = (res.decision || {}).action, pr = res.probability || {};
  const head = `<div class="twr-head"><div class="q">${esc(sc.question || "")}</div>
      <div class="m"><span class="chip d-${esc(r.domain || sc.domain || "")}">${esc(r.domain || sc.domain || "")}</span><span class="dec ${esc(dec || "none")}">${esc(String(dec || "?").replace(/_/g, " "))}</span>
        ${pr.p != null ? `<span>p ${Number(pr.p).toFixed(3)}${pr.interval ? ` <span class="faint">[${pr.interval.map((v) => Number(v).toFixed(3)).join(", ")}]</span>` : ""}</span>` : ""}
        ${(res.trust || {}).tier ? `<span class="faint">trust tier ${esc(res.trust.tier)}</span>` : ""}
        <span class="faint">${r.at ? new Date(r.at * 1000).toLocaleString() : ""}${r.by ? ` · asked by ${esc(r.by)}` : ""}</span></div>
      ${r.hypothetical ? `<div class="banner warn" style="margin:8px 0 0">Hypothetical: a route built for a question, from distances. Nothing in it exists in the network, and no change follows from it.</div>` : ""}
      ${(res.decision || {}).reasons?.length ? `<div class="faint" style="font-size:12px;margin-top:6px">${res.decision.reasons.map(esc).join("<br>")}</div>` : ""}</div>`;
  $("#twr-detail").innerHTML = head + `<div class="twr-plot">${topoSvg(sc)}</div><div class="twr-figs">${figuresSvg(res.figures || [], sc.requirements || [])}</div>
    ${res.not_modelled ? `<details class="twr-nm"><summary>What the twin does not model</summary><div class="faint">${esc(res.not_modelled)}</div></details>` : ""}`;
}

/* the network the scenario describes: its links (a failed one dashed red, one on a lightpath's path in accent) */
function topoSvg(sc) {
  const topo = sc.topology || {}, nodes = (topo.nodes || []).map((n) => n.id ?? n), links = topo.links || [];
  if (!nodes.length) {
    const sats = sc.constellation || sc.satellites ? "a satellite scenario" : "no topology in this scenario";
    return `<div class="empty">${esc(sats)}</div>`;
  }
  const down = new Set(((sc.change || {}).events || []).filter((e) => /down|cut|fail/.test(e.type || "")).map((e) => e.target));
  const onPath = new Set(((sc.traffic || {}).lightpaths || []).flatMap((l) => l.path || []));
  const deg = {}; links.forEach((l) => { deg[l.a] = (deg[l.a] || 0) + 1; deg[l.z] = (deg[l.z] || 0) + 1; });
  const chain = nodes.length > 6 && nodes.every((n) => (deg[n] || 0) <= 2);
  const W = 640, H = chain ? 150 : 300, pos = {};
  let shown = nodes, note = "";
  if (chain) {                                   // a long span chain: the ends and a few spans each side
    const keep = nodes.length > 14 ? [...nodes.slice(0, 6), "…", ...nodes.slice(-6)] : nodes;
    if (keep.includes("…")) note = `${nodes.length - 12} more amplifier sites`;
    keep.forEach((n, i) => { pos[n] = [30 + i * (W - 60) / Math.max(1, keep.length - 1), H / 2]; });
    shown = keep;
  } else {
    nodes.forEach((n, i) => { const a = -Math.PI / 2 + 2 * Math.PI * i / nodes.length; pos[n] = [W / 2 + Math.cos(a) * (W / 2 - 90), H / 2 + Math.sin(a) * (H / 2 - 40)]; });
  }
  const seg = (l) => {
    const p = pos[l.a], q = pos[l.z]; if (!p || !q) return "";
    const cls = down.has(l.id) ? "down" : onPath.has(l.id) ? "path" : "";
    const mx = (p[0] + q[0]) / 2, my = (p[1] + q[1]) / 2;
    const lbl = [l.id, l.length_km != null ? `${l.length_km} km` : l.capacity_mbps != null ? `${l.capacity_mbps} Mb/s` : ""].filter(Boolean).join(" · ");
    return `<line class="tl ${cls}" x1="${p[0]}" y1="${p[1]}" x2="${q[0]}" y2="${q[1]}"/>${chain && shown.length > 8 ? "" : `<text class="tll" x="${mx}" y="${my - 6}">${esc(lbl)}</text>`}`;
  };
  let gap = "";
  if (shown.includes("…")) { const p = pos["…"]; gap = `<text class="tll" x="${p[0]}" y="${p[1] + 4}">…</text>`; }
  const nd = shown.filter((n) => n !== "…").map((n) => {
    const [x, y] = pos[n]; const end = !/^ILA-/.test(String(n));
    return `<g class="tn${end ? " end" : ""}"><circle cx="${x}" cy="${y}" r="${end ? 9 : 4}"/>${end || shown.length <= 10 ? `<text x="${x}" y="${y + (chain ? 24 : -14)}">${esc(n)}</text>` : ""}</g>`;
  }).join("");
  const legend = `<text class="tleg" x="8" y="${H - 6}">${down.size ? "red dashed: failed in the change · " : ""}${onPath.size ? "accent: on the lightpath's path · " : ""}${esc(note)}</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" class="twr-topo" role="img" aria-label="the scenario's network">${links.map(seg).join("")}${gap}${nd}${legend}</svg>`;
}

/* each figure: the baseline and, with the change, the 5-95 % band and the mean, against the requirement */
function figuresSvg(figs, reqs) {
  if (!figs.length) return `<div class="empty">No figures in this run.</div>`;
  const rows = figs.slice(0, 12).map((f) => {
    const c = f.changed || {}, b = f.baseline || {};
    const req = reqs.find((q) => q.kpi === f.kpi && (q.demand === f.entity || !q.demand));
    const vals = [c.p5, c.p95, c.mean, b.mean, req?.value].filter((v) => typeof v === "number");
    let lo = Math.min(...vals), hi = Math.max(...vals); if (lo === hi) { lo -= 1; hi += 1; } const pad = (hi - lo) * .08; lo -= pad; hi += pad;
    const X = (v) => 170 + (v - lo) / (hi - lo) * 400;
    const ok = req ? (req.op?.startsWith(">") ? c.mean >= req.value : c.mean <= req.value) : null;
    return `<g>
      <text class="fl" x="0" y="16">${esc(f.entity)} · ${esc(f.kpi)}</text>
      <line class="axis" x1="170" y1="12" x2="570" y2="12"/>
      ${typeof c.p5 === "number" && typeof c.p95 === "number" ? `<rect class="band ${ok === false ? "bad" : "ok"}" x="${X(c.p5)}" y="5" width="${Math.max(2, X(c.p95) - X(c.p5))}" height="14" rx="3"/>` : ""}
      ${typeof c.mean === "number" ? `<line class="mean" x1="${X(c.mean)}" y1="2" x2="${X(c.mean)}" y2="22"/>` : ""}
      ${typeof b.mean === "number" ? `<circle class="base" cx="${X(b.mean)}" cy="12" r="4"><title>without the change: ${b.mean}</title></circle>` : ""}
      ${req ? `<line class="req" x1="${X(req.value)}" y1="0" x2="${X(req.value)}" y2="24"/><text class="rl" x="${X(req.value) + 3}" y="33">${esc(req.op)} ${req.value}</text>` : ""}
      <text class="fv" x="578" y="16">${typeof c.mean === "number" ? Number(c.mean).toFixed(2) : "?"}</text></g>`;
  });
  return `<svg viewBox="0 0 640 ${rows.length * 42 + 22}" class="twr-fig" role="img" aria-label="the run's figures">
    ${rows.map((r, i) => `<g transform="translate(0 ${i * 42})">${r}</g>`).join("")}
    <text class="tleg" x="0" y="${rows.length * 42 + 16}">bar: with the change, 5–95 %; line: its mean; dot: without the change; dashed: the requirement</text></svg>`;
}

/* ---------- the live network ---------- */
const lv = { data: null, timer: null, runs: [], perf: null, open: new Set() };
const TWIN_DOMAIN = { packet: "ip", optical: "optical", "data centre": "ip", "data-centre": "ip", wifi: "wifi", wireless: "wifi", ntn: "ntn" };
async function renderLive() {
  clearTimeout(lv.timer);
  try {
    const [d, r, pf] = await Promise.all([api("/live"), api("/twins/runs?limit=60").catch(() => ({ runs: [] })),
      api("/performance?hours=6&points=48").catch(() => null)]);
    lv.data = d; lv.runs = r.runs || []; lv.perf = pf;
  } catch (e) { $("#lv-domains").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  drawLive();
  if ($("#lv-auto").checked) lv.timer = setTimeout(() => state.view === "live" && renderLive(), 10000);
}
function drawLive() {
  const d = lv.data; if (!d) return;
  $("#lv-when").textContent = `read ${ago(d.at)}`;
  const ctls = Object.values(d.domains).flat();
  const svcs = ctls.flatMap((c) => c.services);
  const kpi = (label, value, sub, dot = "") => `<div class="kpi"><div class="label">${dot ? `<span class="dot ${dot}"></span>` : ""}${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  const up = ctls.filter((c) => c.ok).length, sDown = svcs.filter((s) => !s.up).length;
  const dDown = ctls.reduce((a, c) => a + c.devices_down, 0), lDown = ctls.reduce((a, c) => a + c.links_down, 0);
  const al = ctls.reduce((a, c) => a + c.alarms.length, 0), inc = ctls.reduce((a, c) => a + c.incidents, 0);
  $("#lv-kpis").innerHTML = [
    kpi("Controllers", `${up}/${ctls.length}`, "answering now", up === ctls.length ? "ok" : "bad"),
    kpi("Services", svcs.length, sDown ? `${sDown} not up` : "all up", sDown ? "bad" : "ok"),
    kpi("Devices down", dDown, `of ${ctls.reduce((a, c) => a + c.devices, 0)}`, dDown ? "bad" : "ok"),
    kpi("Links down", lDown, `of ${ctls.reduce((a, c) => a + c.links, 0)}`, lDown ? "bad" : "ok"),
    kpi("Alarms", al, "raised by the controllers", al ? "warn" : "ok"),
    kpi("Open incidents", inc, "the watcher's", inc ? "warn" : "ok"),
    (() => { const objs = (lv.perf?.controllers || []).flatMap((c) => c.objects);
      const bad = objs.filter((o) => o.worst === "bad").length, warn = objs.filter((o) => o.worst === "warn").length;
      return kpi("Out of limits", bad + warn, objs.length ? `${bad} bad · ${warn} warning, of ${objs.length} measured` : "nothing measured yet", bad ? "bad" : warn ? "warn" : "ok"); })(),
  ].join("");
  const f = ($("#lv-filter").value || "").toLowerCase(), only = $("#lv-problems").checked;
  const doms = Object.entries(d.domains).sort(([a], [b]) => a.localeCompare(b));
  $("#lv-domains").innerHTML = doms.length ? doms.map(([dom, list]) => {
    const tdom = TWIN_DOMAIN[dom.toLowerCase()] || dom.toLowerCase();
    const runs = lv.runs.filter((x) => x.domain === tdom && !x.hypothetical);
    const last = runs[0];
    const rows = list.flatMap((c) => c.services.map((s) => ({ ...s, ctl: c })))
      .filter((s) => (!only || !s.up) && (!f || `${s.id} ${s.a} ${s.z} ${s.status} ${s.ctl.key} ${s.rate}`.toLowerCase().includes(f)));
    const ctlChips = list.map((c) => `<span class="lv-ctl ${c.ok ? "ok" : "bad"}" title="${esc(c.error || "")}"><span class="dot ${c.ok ? "ok" : "bad"}"></span>${esc(c.label)}
        <span class="faint">${c.ok ? `${c.devices} devices${c.devices_down ? ` (${c.devices_down} down)` : ""} · ${c.links} links${c.links_down ? ` (${c.links_down} down)` : ""}` : "not answering"}</span>
        ${c.alarms.length ? `<span class="tag warn">${c.alarms.length} alarm${c.alarms.length > 1 ? "s" : ""}</span>` : ""}${c.incidents ? `<span class="tag bad">${c.incidents} incident${c.incidents > 1 ? "s" : ""}</span>` : ""}</span>`).join("");
    const twin = `<div class="lv-twin"><div class="label">In the digital twin</div>
        <div>${list.some((c) => c.twin_snapshot) ? `snapshot <span class="mono">${esc(list.find((c) => c.twin_snapshot).twin_snapshot)}</span>` : `<span class="faint">no snapshot of this domain (scenarios are built from live reads)</span>`}</div>
        <div>${last ? `last run ${ago(last.at)}: <span class="dec ${esc(last.decision || "none")}">${esc(String(last.decision || "?").replace(/_/g, " "))}</span> <a href="#" data-run="${esc(last.run_id)}">see it</a> · ${runs.length} run${runs.length === 1 ? "" : "s"}` : `<span class="faint">no run on ${esc(tdom)} yet</span>`}</div></div>`;
    return `<div class="card lv-dom d-${esc(dom.toLowerCase().replace(/\s+/g, "-"))}"><header><h3>${esc(dom)}</h3><div class="spacer"></div>
        <span class="faint">${rows.length} of ${list.reduce((a, c) => a + c.services.length, 0)} services</span></header>
      <div class="body"><div class="lv-top"><div class="lv-ctls">${ctlChips}</div>${twin}</div>
      ${rows.length ? `<div class="lv-table"><table class="tbl"><thead><tr><th>service</th><th>state</th><th>from</th><th>to</th><th>size</th><th class="hide-sm">controller</th></tr></thead><tbody>
        ${rows.slice(0, 400).map((s) => `<tr class="${s.up ? "" : "bad"}"><td class="mono">${esc(s.id)}</td><td><span class="dstat"><span class="dot ${s.up ? "ok" : "bad"}"></span>${esc(s.status)}</span></td>
          <td class="mono faint">${esc(s.a || "")}</td><td class="mono faint">${esc(s.z || "")}</td><td>${esc(s.rate || s.kind || "")}</td><td class="hide-sm faint">${esc(s.ctl.label)}</td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">${only ? "Every service here is up." : list.every((c) => !c.ok) ? "No controller of this domain answers." : "No services listed."}</div>`}
      ${perfSection(list, f, only)}</div></div>`;
  }).join("") : `<div class="empty">No controller is held by the agentic MCP.</div>`;
  $$("#lv-domains tr.pf-row").forEach((tr) => tr.addEventListener("click", () => {
    const k = tr.dataset.k; lv.open.has(k) ? lv.open.delete(k) : lv.open.add(k); drawLive();
  }));
  $$("#lv-domains .pf-chart[data-k]").forEach(loadPerfCharts);
  $$("#lv-domains a[data-run]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); twr.sel = a.dataset.run; go("twins"); }));
}
$("#lv-refresh").addEventListener("click", renderLive);
$("#lv-auto").addEventListener("change", () => { if ($("#lv-auto").checked) renderLive(); else clearTimeout(lv.timer); });
$("#lv-filter").addEventListener("input", drawLive);
$("#lv-problems").addEventListener("change", drawLive);


/* ---------- performance: what the controllers measure (read_metrics), against limits, with trends ---------- */
const KIND_ORDER = ["lightpath", "fibre", "link", "port", "interface", "router", "switch", "device"];
const KIND_NAME = { lightpath: "Lightpaths", fibre: "Fibres", link: "IP links", port: "Ports", interface: "Interfaces", router: "Routers", switch: "Switches", device: "Devices" };
function pfFmt(m, v) {
  const d = lv.perf?.metrics?.[m] || {};
  if (v === null || v === undefined) return "–";
  if (d.log) return v === 0 ? "0" : v.toExponential(1);
  const a = Math.abs(v);
  return a >= 100 ? String(Math.round(v)) : a >= 10 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : v.toPrecision(2);
}
function spark(vals, m, judge) {
  if (!vals || vals.length < 2) return "";
  const d = lv.perf?.metrics?.[m] || {}, tr = (x) => d.log ? Math.log10(Math.max(x, 1e-15)) : x;
  const ys = vals.map(tr), lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
  const W = 64, H = 18, pts = ys.map((y, i) => `${(i / (ys.length - 1) * W).toFixed(1)},${(H - 2 - (y - lo) / span * (H - 4)).toFixed(1)}`);
  return `<svg class="pf-spark ${judge || ""}" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true"><polyline points="${pts.join(" ")}"/></svg>`;
}
function perfSection(list, f, only) {
  const P = lv.perf; if (!P) return "";
  const ctls = list.map((c) => P.controllers.find((x) => x.key === c.key)).filter(Boolean);
  const none = list.filter((c) => !P.controllers.some((x) => x.key === c.key && x.published));
  if (!ctls.some((c) => c.published)) {
    return `<div class="pf"><div class="pf-h"><h4>Performance</h4></div><div class="empty">${list.every((c) => !c.ok) ? "Nothing to measure: no controller of this domain answers."
      : P.controllers.length ? `${esc(none.map((c) => c.label).join(", "))} publish${none.length === 1 ? "es" : ""} no measurements: the controller serves no <span class="mono">read_metrics</span>.`
      : "No measurements yet: the watcher reads them at its next pass."}</div></div>`;
  }
  const objs = ctls.flatMap((c) => c.objects.map((o) => ({ ...o, ctl: c })))
    .filter((o) => (!only || o.worst !== "ok") && (!f || `${o.object} ${o.kind} ${o.state}`.toLowerCase().includes(f)));
  const kinds = [...new Set(objs.map((o) => o.kind || "device"))].sort((a, b) => (KIND_ORDER.indexOf(a) + 99) % 99 - (KIND_ORDER.indexOf(b) + 99) % 99);
  const at = Math.max(...ctls.map((c) => c.at || 0));
  const blocks = kinds.map((k) => {
    const rows = objs.filter((o) => (o.kind || "device") === k);
    const metrics = [...new Set(rows.flatMap((o) => Object.keys(o.values)))];
    const head = metrics.map((m) => { const d = P.metrics[m] || {}; return `<th title="${esc(d.warn != null ? `warning ${d.worse === "low" ? "below" : "above"} ${d.warn}, bad ${d.worse === "low" ? "below" : "above"} ${d.bad}` : "measured, no limit")}">${esc(d.label || m)}${d.unit ? ` <span class="faint">${esc(d.unit)}</span>` : ""}</th>`; }).join("");
    const body = rows.map((o) => {
      const key = `${o.ctl.key}|${o.object}`, open = lv.open.has(key);
      const cells = metrics.map((m) => { const x = o.values[m]; return x ? `<td class="pf-v ${x.judge}"><span class="n">${pfFmt(m, x.v)}</span>${spark(x.trend, m, x.judge)}</td>` : `<td class="faint">–</td>`; }).join("");
      const st = o.state && !/^(up|in-service|ok|active)$/i.test(o.state);
      return `<tr class="pf-row click ${o.worst}${open ? " open" : ""}" data-k="${esc(key)}"><td class="mono"><span class="dot ${st ? "bad" : o.worst === "ok" ? "ok" : o.worst}"></span> ${esc(o.object)}${st ? ` <span class="tag bad">${esc(o.state)}</span>` : ""}</td>${cells}</tr>`
        + (open ? `<tr class="pf-more"><td colspan="${metrics.length + 1}"><div class="pf-chart" data-k="${esc(key)}" data-ctl="${esc(o.ctl.key)}" data-obj="${esc(o.object)}">reading 24 h…</div></td></tr>` : "");
    }).join("");
    return `<div class="pf-kind"><div class="pf-kh">${esc(KIND_NAME[k] || k)} <span class="faint">${rows.length}</span></div>
      <div class="lv-table"><table class="tbl pf-tbl"><thead><tr><th>${esc(k)}</th>${head}</tr></thead><tbody>${body}</tbody></table></div></div>`;
  }).join("");
  return `<div class="pf"><div class="pf-h"><h4>Performance</h4><span class="faint">measured by the controller${ctls.length > 1 ? "s" : ""} · read ${ago(at)} · trend: last ${P.hours} h · click a row for 24 h</span></div>
    ${blocks || `<div class="empty">${only ? "Everything measured here is within its limits." : "Nothing matches."}</div>`}
    ${none.length && ctls.some((c) => c.published) ? `<div class="faint pf-note">${esc(none.map((c) => c.label).join(", "))}: no measurements published (no read_metrics).</div>` : ""}</div>`;
}
const pfChartCache = {};
async function loadPerfCharts(el) {
  const { ctl, obj, k } = el.dataset;
  let s = pfChartCache[k];
  if (!s || Date.now() - s.t > 30000) {
    try { s = { t: Date.now(), data: (await api(`/performance/series?controller=${encodeURIComponent(ctl)}&object=${encodeURIComponent(obj)}&hours=24&points=240`)).series }; pfChartCache[k] = s; }
    catch (e) { el.innerHTML = `<span class="faint">${esc(e.message)}</span>`; return; }
  }
  const lim = (km) => { const d = lv.perf?.metrics?.[km.split("|")[1]] || {}; return d.warn != null ? 0 : 1; };
  const entries = Object.entries(s.data).sort(([a], [b]) => lim(a) - lim(b) || a.localeCompare(b));
  el.innerHTML = entries.length ? `<div class="pf-charts">${entries.map(([km, pts]) => bigChart(km.split("|")[1], pts)).join("")}</div>` : `<span class="faint">No history yet.</span>`;
}
function bigChart(m, pts) {
  const d = lv.perf?.metrics?.[m] || { label: m }, tr = (x) => d.log ? Math.log10(Math.max(x, 1e-15)) : x;
  const W = 300, H = 110, L = 44, B = 18;
  const ys = pts.map((p) => tr(p[1])), lims = [d.warn, d.bad].filter((x) => x != null).map(tr);
  let lo = Math.min(...ys, ...lims), hi = Math.max(...ys, ...lims); if (hi === lo) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const t0 = pts[0][0], t1 = pts[pts.length - 1][0] || t0 + 1;
  const X = (t) => L + (t - t0) / ((t1 - t0) || 1) * (W - L - 6), Y = (y) => 6 + (hi - y) / (hi - lo) * (H - B - 6);
  const line = pts.map((p) => `${X(p[0]).toFixed(1)},${Y(tr(p[1])).toFixed(1)}`).join(" ");
  const lab = (y) => d.log ? `1e${Math.round(y)}` : pfFmt(m, y);
  const limLine = (v, cls) => v == null ? "" : `<line class="lim ${cls}" x1="${L}" x2="${W - 6}" y1="${Y(tr(v))}" y2="${Y(tr(v))}"/>`;
  const last = pts[pts.length - 1][1];
  const when = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `<figure class="pf-big"><figcaption>${esc(d.label)} <b>${pfFmt(m, last)}</b>${d.unit ? ` ${esc(d.unit)}` : ""}</figcaption>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(d.label)} over the last 24 hours">
      <text class="ax" x="${L - 4}" y="${Y(hi - pad) + 4}" text-anchor="end">${lab(hi - pad)}</text>
      <text class="ax" x="${L - 4}" y="${Y(lo + pad) + 4}" text-anchor="end">${lab(lo + pad)}</text>
      <text class="ax" x="${L}" y="${H - 4}">${when(t0)}</text><text class="ax" x="${W - 6}" y="${H - 4}" text-anchor="end">${when(t1)}</text>
      ${limLine(d.warn, "warn")}${limLine(d.bad, "bad")}<polyline class="val" points="${line}"/></svg></figure>`;
}

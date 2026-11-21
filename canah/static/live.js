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
      <div class="m"><span class="chip">${esc(x.domain || "?")}</span>${pill(x.decision)}${x.hypothetical ? `<span class="hypo" title="a route built for a question: nothing in it exists in a network">hypothetical</span>` : ""}
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
      <div class="m"><span class="chip">${esc(r.domain || sc.domain || "")}</span><span class="dec ${esc(dec || "none")}">${esc(String(dec || "?").replace(/_/g, " "))}</span>
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
const lv = { data: null, timer: null, runs: [] };
const TWIN_DOMAIN = { packet: "ip", optical: "optical", "data centre": "ip", "data-centre": "ip", wifi: "wifi", wireless: "wifi", ntn: "ntn" };
async function renderLive() {
  clearTimeout(lv.timer);
  try {
    const [d, r] = await Promise.all([api("/live"), api("/twins/runs?limit=60").catch(() => ({ runs: [] }))]);
    lv.data = d; lv.runs = r.runs || [];
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
    return `<div class="card lv-dom"><header><h3>${esc(dom)}</h3><div class="spacer"></div>
        <span class="faint">${rows.length} of ${list.reduce((a, c) => a + c.services.length, 0)} services</span></header>
      <div class="body"><div class="lv-top"><div class="lv-ctls">${ctlChips}</div>${twin}</div>
      ${rows.length ? `<div class="lv-table"><table class="tbl"><thead><tr><th>service</th><th>state</th><th>from</th><th>to</th><th>size</th><th class="hide-sm">controller</th></tr></thead><tbody>
        ${rows.slice(0, 400).map((s) => `<tr class="${s.up ? "" : "bad"}"><td class="mono">${esc(s.id)}</td><td><span class="dstat"><span class="dot ${s.up ? "ok" : "bad"}"></span>${esc(s.status)}</span></td>
          <td class="mono faint">${esc(s.a || "")}</td><td class="mono faint">${esc(s.z || "")}</td><td>${esc(s.rate || s.kind || "")}</td><td class="hide-sm faint">${esc(s.ctl.label)}</td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">${only ? "Every service here is up." : list.every((c) => !c.ok) ? "No controller of this domain answers." : "No services listed."}</div>`}</div></div>`;
  }).join("") : `<div class="empty">No controller is held by the agentic MCP.</div>`;
  $$("#lv-domains a[data-run]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); twr.sel = a.dataset.run; go("twins"); }));
}
$("#lv-refresh").addEventListener("click", renderLive);
$("#lv-auto").addEventListener("change", () => { if ($("#lv-auto").checked) renderLive(); else clearTimeout(lv.timer); });
$("#lv-filter").addEventListener("input", drawLive);
$("#lv-problems").addEventListener("change", drawLive);

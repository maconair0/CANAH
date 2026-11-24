/* Tutorials & guides: the console's video tutorials (scripts/record_tutorials.py records them) with their steps,
   and the support documents (manifest format, template, examples, the controllers' manifests, the contract). */
const help = { tutorials: [], docs: [], sel: "", doc: null };

async function renderTutorials() {
  try {
    const [t, d] = await Promise.all([api("/help/tutorials"), api("/help/docs").catch(() => ({ docs: [] }))]);
    help.tutorials = t.tutorials || []; help.docs = d.docs || [];
  } catch (e) { $("#tut-list").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  const want = (location.hash.match(/^#tutorial-(\w+)/) || [])[1];
  if (!help.sel || !help.tutorials.some((x) => x.id === help.sel)) help.sel = want || help.tutorials[0]?.id || "";
  drawTutorialList(); showTutorial(help.sel, false); drawDocList();
}

const fmtSec = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

function drawTutorialList() {
  $("#tut-list").innerHTML = help.tutorials.length ? help.tutorials.map((t, i) => `
    <button class="tut-card${t.id === help.sel ? " sel" : ""}" data-t="${esc(t.id)}">
      <span class="tut-n">${i + 1}</span>
      <span class="tut-txt"><b>${esc(t.title)}</b><span class="muted">${esc(t.about)}</span>
        <span class="faint">${fmtSec(t.seconds || 0)} · ${(t.chapters || []).length} steps</span></span></button>`).join("")
    : `<div class="empty">No tutorial recorded yet: run <span class="mono">.venv/bin/python scripts/record_tutorials.py</span>.</div>`;
  $$("#tut-list .tut-card").forEach((b) => b.addEventListener("click", () => showTutorial(b.dataset.t, true)));
}

function showTutorial(id, play) {
  const t = help.tutorials.find((x) => x.id === id); if (!t) return;
  help.sel = id;
  $$("#tut-list .tut-card").forEach((b) => b.classList.toggle("sel", b.dataset.t === id));
  $("#tut-title").textContent = t.title;
  $("#tut-meta").textContent = `${fmtSec(t.seconds || 0)} · recorded ${t.recorded || ""}`;
  $("#tut-about").textContent = t.about;
  const v = $("#tut-video");
  if (!v.src.endsWith(t.url)) { v.src = t.url; v.load(); }
  // The video's own length (the recording's clock ran a little past its end).
  v.onloadedmetadata = () => { if (isFinite(v.duration)) $("#tut-meta").textContent = `${fmtSec(v.duration)} · recorded ${t.recorded || ""}`; };
  if (play) v.play().catch(() => {});
  const ch = t.chapters || [];
  $("#tut-chapters").innerHTML = ch.map((c, i) => `<li data-i="${i}"><button type="button" data-t="${c.t}">
      <span class="tut-at mono">${fmtSec(c.t)}</span><span>${c.text}</span></button></li>`).join("");
  $$("#tut-chapters button").forEach((b) => b.addEventListener("click", () => { v.currentTime = +b.dataset.t; v.play().catch(() => {}); }));
  v.ontimeupdate = () => {
    let cur = -1; ch.forEach((c, i) => { if (v.currentTime + 0.25 >= c.t) cur = i; });
    $$("#tut-chapters li").forEach((li) => li.classList.toggle("now", +li.dataset.i === cur));
  };
}

function drawDocList() {
  const groups = {};
  help.docs.forEach((d) => { (groups[d.group] = groups[d.group] || []).push(d); });
  $("#doc-list").innerHTML = Object.keys(groups).length ? Object.entries(groups).map(([g, ds]) => `<div class="mem-user">${esc(g)}</div>`
    + ds.map((d) => `<div class="mem-row doc-row${help.doc === d.id ? " sel" : ""}" data-d="${esc(d.id)}">
        <div class="p">${esc(d.title)} <span class="faint mono" style="font-size:11px">${esc(d.kind)}</span></div>
        ${d.about ? `<div class="pv">${esc(d.about)}</div>` : ""}</div>`).join("")).join("")
    : `<div class="empty">No documents: the agentic MCP is not deployed here (./start.sh deploys it).</div>`;
  $$("#doc-list .doc-row").forEach((r) => r.addEventListener("click", () => openDoc(r.dataset.d)));
}

async function openDoc(id) {
  help.doc = id; drawDocList();
  $("#doc-view").innerHTML = `<span class="muted">Reading…</span>`;
  let d; try { d = await api(`/help/docs/${encodeURIComponent(id)}`); } catch (e) { $("#doc-view").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  $("#doc-title").textContent = d.title;
  const pretty = d.kind === "json" ? (() => { try { return JSON.stringify(JSON.parse(d.text), null, 1); } catch (e) { return d.text; } })() : d.text;
  $("#doc-view").innerHTML = d.kind === "md" ? `<div class="reply-rich doc-md">${md(d.text)}</div>` : `<pre class="doc-code">${esc(pretty)}</pre>`;
  const btn = $("#doc-download"); btn.hidden = false;
  btn.onclick = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([d.text], { type: "text/plain" })); a.download = d.name; a.click(); };
  $("#doc-view").scrollTop = 0;
}

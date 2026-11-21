/* Skills (Documents & memory page): what the agent knows how to do, and the operators' own, written here. */
const sk = { list: [], sel: null, dirty: false };

async function renderSkills() {
  let r; try { r = await api("/skills"); } catch (e) { $("#sk-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  sk.list = r.skills || [];
  $("#kn-count-skills").textContent = sk.list.length;
  $("#sk-new").hidden = levelRank(state.level) < 1;
  drawSkillList();
  if (!sk.sel && sk.list.length) openSkill(sk.list[0].name);
}

function drawSkillList() {
  const f = ($("#sk-filter").value || "").toLowerCase();
  const rows = sk.list.filter((x) => !f || `${x.name} ${x.description}`.toLowerCase().includes(f));
  const group = (title, items) => items.length ? `<div class="mem-user">${esc(title)}</div>` + items.map((x) => `
      <div class="mem-row sk-row${sk.sel === x.name ? " sel" : ""}" data-name="${esc(x.name)}">
        <div class="p">${esc(x.name)}${x.overrides ? ` <span class="tag" title="yours, used instead of the built-in one">overrides</span>` : ""}</div>
        <div class="pv">${esc(x.description)}</div></div>`).join("") : "";
  $("#sk-list").innerHTML = rows.length
    ? group("Yours · written here", rows.filter((x) => x.editable)) + group("Built in", rows.filter((x) => !x.editable))
    : `<div class="empty">${sk.list.length ? "Nothing matches." : "No skills."}</div>`;
  $$("#sk-list .sk-row").forEach((el) => el.addEventListener("click", () => openSkill(el.dataset.name)));
}
$("#sk-filter").addEventListener("input", drawSkillList);

function skillEditor(s, isNew) {
  const admin = levelRank(state.level) >= 1;
  const edit = admin && (isNew || s.editable);
  const ro = edit ? "" : " readonly";
  $("#sk-title").textContent = isNew ? "New skill" : s.name;
  $("#sk-meta").textContent = isNew ? "saved to your skills; used from the next question"
    : s.editable ? "yours" : "built in · read only: copy it to change it";
  $("#sk-detail").innerHTML = `<form id="sk-form" class="sk-form" autocomplete="off">
      <label>Name<input id="sk-name" value="${esc(s.name || "")}" placeholder="fan-check" pattern="[a-z][a-z0-9-]{1,48}" required${isNew ? "" : " readonly"}
        title="lower-case letters, digits and -"></label>
      <label>When to use it <span class="faint">· the agent chooses a skill by this</span>
        <textarea id="sk-desc" rows="2" required minlength="20" maxlength="1024"${ro}
          placeholder="Check a site's fans and temperatures when an alarm names a fan or a device runs hot.">${esc(s.description || "")}</textarea></label>
      <div class="sk-two">
        <label>Instructions <span class="faint">· Markdown: steps, the tools to use, what to check, what to report</span>
          <textarea id="sk-body" rows="18" required${ro} spellcheck="false"
            placeholder="1. Read the site's alarms (net_read_alarms).&#10;2. ...&#10;3. Report: ...">${esc(s.body || "")}</textarea></label>
        <div class="sk-preview"><div class="label">Preview</div><div id="sk-prev" class="reply-rich"></div></div>
      </div>
      <div class="row sk-actions">
        ${edit ? `<button class="btn primary" type="submit">Save</button>` : ""}
        ${admin && !isNew && !s.editable ? `<button class="btn" type="button" id="sk-copy">Copy to my skills</button>` : ""}
        ${admin ? `<button class="btn" type="button" id="sk-import">Import a SKILL.md</button>` : ""}
        <span class="faint" id="sk-msg" style="font-size:12px"></span><div class="spacer"></div>
        ${edit && !isNew ? `<button class="btn danger" type="button" id="sk-del">Delete</button>` : ""}
      </div></form>`;
  const prev = () => { $("#sk-prev").innerHTML = md($("#sk-body").value || "_Nothing yet._"); };
  prev(); $("#sk-body").addEventListener("input", () => { prev(); sk.dirty = true; });
  $("#sk-desc").addEventListener("input", () => { sk.dirty = true; });
  $("#sk-form").addEventListener("submit", saveSkill);
  $("#sk-del")?.addEventListener("click", () => deleteSkill(s.name));
  $("#sk-copy")?.addEventListener("click", () => { skillEditor({ ...s, name: `${s.name}-local`, editable: true }, true); });
  $("#sk-import")?.addEventListener("click", () => $("#sk-file").click());
}

async function openSkill(name) {
  if (sk.dirty && !confirm("Leave without saving the skill you are writing?")) return;
  sk.sel = name; sk.dirty = false; drawSkillList();
  try { skillEditor(await api(`/skills/${encodeURIComponent(name)}`), false); }
  catch (e) { $("#sk-detail").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
}

$("#sk-new").addEventListener("click", () => {
  if (sk.dirty && !confirm("Leave without saving the skill you are writing?")) return;
  sk.sel = null; sk.dirty = false; drawSkillList(); skillEditor({ name: "", description: "", body: "" }, true); $("#sk-name").focus();
});

async function saveSkill(e) {
  e.preventDefault();
  const name = $("#sk-name").value.trim();
  $("#sk-msg").textContent = "Saving, and rebuilding the agent…";
  try {
    const r = await api(`/skills/${encodeURIComponent(name)}`, { method: "PUT", body: { description: $("#sk-desc").value, body: $("#sk-body").value } });
    sk.dirty = false; sk.sel = name;
    const re = r.reload;
    toast(re && re.reloaded === false ? `Saved. The agent was not rebuilt yet (${re.why}); it will use the skill after the next reload.` : `Saved: the agent uses "${name}" from the next question.`, re && re.reloaded === false ? "warn" : "ok");
    await renderSkills(); openSkill(name);
  } catch (err) { $("#sk-msg").textContent = ""; toast(err.message, "bad"); }
}

async function deleteSkill(name) {
  if (!confirm(`Delete your skill "${name}"? The agent stops using it from the next question.`)) return;
  try { await api(`/skills/${encodeURIComponent(name)}`, { method: "DELETE" }); toast("Skill deleted.", "ok"); sk.sel = null; sk.dirty = false; renderSkills(); }
  catch (err) { toast(err.message, "bad"); }
}

$("#sk-file").addEventListener("change", async (e) => {
  const f = e.target.files[0]; e.target.value = ""; if (!f) return;
  const text = await f.text();
  const m = text.match(/^\s*---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/);
  const head = {}; (m ? m[1] : "").split("\n").forEach((l) => { const i = l.indexOf(":"); if (i > 0) head[l.slice(0, i).trim().toLowerCase()] = l.slice(i + 1).trim().replace(/^["']|["']$/g, ""); });
  const base = (head.name || f.name.replace(/\.md$/i, "").replace(/^SKILL$/i, "imported")).toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
  skillEditor({ name: base, description: head.description || "", body: m ? m[2].trim() : text }, true);
  sk.dirty = true; toast(`Imported ${f.name}: check it, then save.`);
});

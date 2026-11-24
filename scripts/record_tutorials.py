"""
Record the console's video tutorials (canah/static/tutorials/): the console itself, driven step by step, with a
caption for each step, a cursor and the control it uses highlighted. Re-run after the console changes:

  .venv/bin/python scripts/record_tutorials.py            all of them
  .venv/bin/python scripts/record_tutorials.py contract   one (contract | controller | guide)

It records on a world of its own, never on the running system: an agentic MCP on :8402 with a fresh data
folder and no token, its example controller ("metro", :9100), and a CANAH on :8372 that knows only them. The
agentic MCP is the deployed one (scripts/agentic_mcp.py prepare first). Needs Playwright's Chromium.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "scripts"))
import agentic_mcp as amcp  # noqa: E402

OUT = HERE / "canah" / "static" / "tutorials"
CANAH_PORT, MCP_PORT, CTL_PORT = 8372, 8402, 9100
ROOT_PW = "tutorial-root"
CONTRACT = os.getenv("TUTORIAL_CONTRACT", "")      # a contract file to load in the contract tutorial

OVERLAY = r"""
(() => {
  if (document.getElementById('tut-cap')) return;
  const st = document.createElement('style');
  st.textContent = `
  #tut-cap{position:fixed;left:50%;top:66px;transform:translateX(-50%);max-width:880px;z-index:99999;
    background:rgba(8,16,22,.94);color:#eaf3f6;border:1px solid #36c2d8;border-radius:10px;padding:12px 18px;
    font:500 17px/1.45 Montserrat,system-ui,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.45);transition:opacity .25s}
  #tut-cap b{color:#36c2d8;font-weight:700;margin-right:8px}
  #tut-cur{position:fixed;width:22px;height:22px;border-radius:50%;background:rgba(54,194,216,.35);border:2px solid #36c2d8;
    z-index:99998;left:40px;top:40px;transition:left .7s ease,top .7s ease;pointer-events:none;transform:translate(-50%,-50%)}
  #tut-ring{position:fixed;border:3px solid #edb13e;border-radius:8px;z-index:99997;pointer-events:none;transition:all .35s;
    box-shadow:0 0 0 4px rgba(237,177,62,.25)}`;
  document.head.append(st);
  const c = document.createElement('div'); c.id = 'tut-cap'; c.style.opacity = 0; document.body.append(c);
  const k = document.createElement('div'); k.id = 'tut-cur'; document.body.append(k);
  const r = document.createElement('div'); r.id = 'tut-ring'; r.style.display = 'none'; document.body.append(r);
})()"""


class Recorder:
    def __init__(self, page, t0: float):
        self.page, self.t0, self.chapters, self.n = page, t0, [], 0

    def overlay(self):
        self.page.evaluate(OVERLAY)

    def caption(self, text: str, hold: float = 3.2):
        self.n += 1
        self.overlay()
        self.chapters.append({"t": round(time.time() - self.t0, 1), "step": self.n, "text": text})
        self.page.evaluate("([n, t]) => { const c = document.getElementById('tut-cap'); c.innerHTML = `<b>${n}</b>` + t; c.style.opacity = 1; }",
                           [self.n, text])
        self.page.wait_for_timeout(int(hold * 1000))

    def point(self, selector: str, ring: bool = True):
        self.overlay()
        el = self.page.locator(selector).first
        el.scroll_into_view_if_needed()
        b = el.bounding_box()
        if not b:
            return
        self.page.evaluate("""([x, y, w, h, ring]) => { const k = document.getElementById('tut-cur');
            k.style.left = (x + w / 2) + 'px'; k.style.top = (y + h / 2) + 'px';
            const r = document.getElementById('tut-ring');
            if (ring) { Object.assign(r.style, {display: 'block', left: (x - 6) + 'px', top: (y - 6) + 'px', width: (w + 12) + 'px', height: (h + 12) + 'px'}); }
            else r.style.display = 'none'; }""", [b["x"], b["y"], b["width"], b["height"], ring])
        self.page.wait_for_timeout(900)

    def unring(self):
        self.page.evaluate("() => { const r = document.getElementById('tut-ring'); if (r) r.style.display = 'none'; }")

    def click(self, selector: str):
        self.point(selector)
        self.page.locator(selector).first.click()
        self.page.wait_for_timeout(600)

    def type(self, selector: str, text: str):
        self.point(selector)
        el = self.page.locator(selector).first
        el.click()
        el.fill("")
        el.type(text, delay=45)
        self.page.wait_for_timeout(300)


# ---------- the world it records on ----------

def world():
    cfg = amcp.config()
    tmp = Path(tempfile.mkdtemp(prefix="canah-tutorial-"))
    data = tmp / "amcp-data"
    data.mkdir()
    shutil.copyfile(Path(cfg["dir"]) / "agentic_mcp" / "contract.json", data / "contract.json")
    env = {**os.environ, "AGENTIC_MCP_DATA": str(data), "AGENTIC_MCP_CONTRACT": str(data / "contract.json"),
           "AGENTIC_MCP_TOKEN": "", "AGENTIC_MCP_ENV_FILE": str(tmp / "none.env"),
           "AGENTIC_MCP_DRAFT_MODEL": os.getenv("AGENTIC_MCP_DRAFT_MODEL", "qwen2.5:3b-instruct-q4_K_M")}
    procs = [subprocess.Popen([amcp.py(cfg), "run_example_controller.py", str(CTL_PORT)], cwd=cfg["dir"], env=env,
                              stdout=open(tmp / "metro.log", "w"), stderr=subprocess.STDOUT),
             subprocess.Popen([amcp.py(cfg), "-m", "agentic_mcp", "serve", "--port", str(MCP_PORT)], cwd=cfg["dir"], env=env,
                              stdout=open(tmp / "amcp.log", "w"), stderr=subprocess.STDOUT)]
    home = tmp / "canah"
    home.mkdir()
    (home / "canah.yaml").write_text(json.dumps({"name": "canah", "providers": ["canah.network:provider"], "sandbox": "none",
                                                 "watch": False, "twins": []}))
    cenv = {**os.environ, "CANAH_HOME": str(home), "CANAH_CONFIG": str(home / "canah.yaml"),
            "AGENTIC_MCP_URL": f"http://127.0.0.1:{MCP_PORT}", "AGENTIC_MCP_TOKEN": "",
            "CANAH_ADMIN_PASSWORD": "tutorial-admin", "CANAH_ROOT_PASSWORD": ROOT_PW}
    procs.append(subprocess.Popen([str(HERE / ".venv" / "bin" / "python"), "-m", "canah", "serve", "--port", str(CANAH_PORT),
                                   "--no-sandbox"], cwd=str(HERE), env=cenv, stdout=open(tmp / "canah.log", "w"),
                                  stderr=subprocess.STDOUT))
    for url in (f"http://127.0.0.1:{MCP_PORT}/health", f"http://127.0.0.1:{CANAH_PORT}/health"):
        for _ in range(240):
            try:
                if requests.get(url, timeout=2).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
    return cfg, tmp, procs


# ---------- the tutorials ----------

def contract(rec: Recorder, cfg):
    pg = rec.page
    rec.caption("The <i>common contract</i> is the list of verbs (read devices, read links, provision…) every "
                "controller is checked against. It is on the <b>SDN controllers</b> page.")
    rec.click('.nav button[data-view="controllers"]')
    rec.click('#ctl-tabs .tab[data-tab="contract"]')
    rec.caption("On the left, the contract in use: its parts and their verbs, and the file the agentic MCP keeps it in.", 4)
    rec.point("#contract-view")
    pg.wait_for_timeout(1500)
    rec.caption("To change it, edit it on the right, or load a contract file (JSON).")
    src = CONTRACT or str(Path(cfg["dir"]) / "agentic_mcp" / "contract.json")
    rec.point('label.btn:has(#contract-file-in)')
    pg.set_input_files("#contract-file-in", src)
    pg.wait_for_timeout(1200)
    rec.caption("The file is loaded into the editor. Nothing has changed yet.")
    rec.caption("<b>Check</b> first: it shows what each controller's verdict would become under this contract, without changing anything.")
    rec.click("#contract-check")
    pg.wait_for_timeout(2500)
    rec.point("#contract-out", ring=True)
    rec.caption("Each controller's verdict under the new contract, beside the current one: changes are highlighted.", 4)
    rec.caption("<b>Apply</b> makes it the contract (root only). The agentic MCP keeps the previous one as a backup.")
    pg.once("dialog", lambda d: d.accept())
    rec.click("#contract-apply")
    pg.wait_for_timeout(2500)
    rec.point("#contract-out")
    rec.caption("Applied: its version, its parts and verbs, what was added or removed, and where the backup is.", 4)
    rec.unring()
    rec.caption("Every controller is checked against the new contract from now on. Done.", 3)


def controller(rec: Recorder, cfg):
    pg = rec.page
    rec.caption("A controller is added through the agentic MCP. For a kind it does not know yet, first give it the "
                "controller's <i>manifest</i>: a YAML file describing its API.")
    rec.click('.nav button[data-view="controllers"]')
    rec.click('#ctl-tabs .tab[data-tab="add"]')
    rec.point("#onboard-card .card:nth-child(3)")
    rec.caption("Under <b>A new kind of controller</b>: <b>Upload a manifest</b>. Here, metro.yaml, the example in the "
                "agentic MCP's repository (manifests/example).", 4.5)
    rec.point('label.btn:has(#ob-manifest)')
    pg.set_input_files("#ob-manifest", str(Path(cfg["dir"]) / "manifests" / "example" / "metro.yaml"))
    pg.wait_for_timeout(2500)
    rec.point("#ob-kinds")
    rec.caption("Installed: <b>Example metro controller</b> is now among the kinds it knows.")
    rec.caption("Then onboard the controller itself, under <b>Onboard one by its address</b>: a key, a label and its base URL.")
    rec.type("#ob-key", "metro")
    rec.type("#ob-label", "Metro (example)")
    rec.type("#ob-url", f"http://127.0.0.1:{CTL_PORT}/api")
    rec.point("#ob-kind")
    rec.caption("Kind: leave it on <b>recognise it from its API</b>: the agentic MCP reads the controller's API and "
                "matches it to the manifest just installed. With a password, give the <i>names</i> of the variables "
                "that hold the credentials (in controllers.env), never the secrets.", 5)
    rec.click("#ob-go")
    pg.wait_for_selector("#ob-msg .tag", timeout=120000)
    pg.wait_for_timeout(1200)
    rec.point("#ob-msg")
    rec.caption("Onboarded, and checked against the common contract: its verdict and any gaps.", 4)
    rec.caption("<b>Reload CANAH</b> gives it its agent: CANAH can now read it and work it.")
    btn = "#ob-reload" if pg.locator("#ob-reload").count() else "#ctl-reload"
    rec.click(btn)
    pg.wait_for_timeout(5000)
    rec.click('#ctl-tabs .tab[data-tab="held"]')
    pg.wait_for_timeout(1500)
    row = 'tr:has-text("metro")'
    if pg.locator(row).count():
        rec.click(row)
        pg.wait_for_timeout(1200)
        rec.point("#ctl-detail")
        rec.caption("Its detail: which parts of the contract it covers, with which of its own operations, and what it lacks.", 4.5)
    rec.unring()
    rec.caption("Done: ask CANAH about it in the chat, like any other controller.", 3)


def guide(rec: Recorder, cfg):
    pg = rec.page
    rec.caption("No manifest for a controller? The agentic MCP can draft one from the controller's <i>user guide</i> "
                "or API document (text, Markdown, HTML, OpenAPI or PDF).")
    rec.click('.nav button[data-view="controllers"]')
    rec.click('#ctl-tabs .tab[data-tab="add"]')
    rec.point("#onboard-card .card:nth-child(3)")
    rec.caption("Under <b>A new kind of controller</b>, name the kind the guide is for…")
    rec.type("#ob-guide-kind", "metro2")
    rec.caption("…then <b>Upload a document</b>: here the example's user guide (manifests/example/metro-user-guide.md).")
    rec.point('label.btn:has(#ob-guide)')
    pg.set_input_files("#ob-guide", str(Path(cfg["dir"]) / "manifests" / "example" / "metro-user-guide.md"))
    try:
        pg.wait_for_function("() => [...document.querySelectorAll('#ob-kinds .chip')].some(c => /metro2/i.test(c.textContent))",
                             timeout=300000)
    except Exception:  # noqa: BLE001 - the draft is the model's; the video says what happened
        pass
    pg.wait_for_timeout(1500)
    rec.point("#ob-kinds")
    rec.caption("A manifest for <b>metro2</b> was drafted: it is among the kinds it knows. A draft states no verb on its "
                "own: onboard with this kind, then check its verdict.", 5)
    rec.point("#ob-kind")
    rec.caption("Then onboard the controller as in the previous tutorial: the agentic MCP matches it to the drafted manifest.", 3.5)
    rec.unring()
    rec.caption("Done.", 2)


# In this order: the contract one then shows a controller's verdict under the new contract.
TUTORIALS = {
    "controller": ("Add a controller with its manifest", "Install the manifest of a new kind of controller, onboard a controller of that kind and give it its agent.", controller),
    "guide": ("Draft a manifest from a user guide", "When a controller has no manifest: let the agentic MCP draft one from its user guide or API document.", guide),
    "contract": ("Change the common contract", "Load a contract file, check what it changes for every controller, and apply it.", contract),
}
FFMPEG = os.getenv("TUTORIAL_FFMPEG", "") or shutil.which("ffmpeg") or ""


def squeeze(path: Path) -> None:
    """The recording, smaller for the page (VP9 at 12 frames a second; a screen changes little): when ffmpeg is there."""
    if not FFMPEG:
        return
    tmp = path.with_suffix(".small.webm")
    got = subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-i", str(path), "-r", "12", "-c:v", "libvpx-vp9", "-b:v", "0",
                          "-crf", "42", "-row-mt", "1", "-an", str(tmp)], capture_output=True, text=True)
    if got.returncode == 0 and tmp.exists() and tmp.stat().st_size < path.stat().st_size:
        tmp.replace(path)
    else:
        tmp.unlink(missing_ok=True)


def record(which):
    from playwright.sync_api import sync_playwright
    import glob
    cfg, tmp, procs = world()
    OUT.mkdir(parents=True, exist_ok=True)
    index_file = OUT / "tutorials.json"
    index = json.loads(index_file.read_text()) if index_file.exists() else {}
    try:
        exe = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))[-1]
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=exe)
            for name in which:
                title, about, fn = TUTORIALS[name]
                vids = tmp / f"video-{name}"
                ctx = b.new_context(viewport={"width": 1280, "height": 760}, record_video_dir=str(vids),
                                    record_video_size={"width": 1280, "height": 760})
                pg = ctx.new_page()
                t0 = time.time()          # the video starts with the page: step times are from here
                pg.goto(f"http://127.0.0.1:{CANAH_PORT}/")
                pg.wait_for_timeout(800)
                pg.fill("#li-user", "root")
                pg.fill("#li-pass", ROOT_PW)
                pg.click("#li-go")
                pg.wait_for_timeout(1800)
                rec = Recorder(pg, t0)
                fn(rec, cfg)
                pg.wait_for_timeout(800)
                ctx.close()
                got = next(vids.glob("*.webm"))
                shutil.move(str(got), OUT / f"{name}.webm")
                squeeze(OUT / f"{name}.webm")
                index[name] = {"title": title, "about": about, "file": f"{name}.webm",
                               "seconds": round(time.time() - t0, 1), "chapters": rec.chapters,
                               "recorded": time.strftime("%Y-%m-%d")}
                print(f"{name}: {OUT / (name + '.webm')} ({(OUT / (name + '.webm')).stat().st_size // 1024} KB, "
                      f"{index[name]['seconds']} s, {len(rec.chapters)} steps)", flush=True)
            b.close()
    finally:
        for pr in procs:
            pr.send_signal(signal.SIGTERM)
        for pr in procs:
            try:
                pr.wait(20)
            except subprocess.TimeoutExpired:
                pr.kill()
        index_file.write_text(json.dumps({k: index[k] for k in TUTORIALS if k in index}, indent=1))
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    record(sys.argv[1:] or list(TUTORIALS))

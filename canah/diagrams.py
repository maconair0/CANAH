"""
Diagrams: the agent draws a flow, a scenario or a topology as a picture, from Mermaid.

  draw_diagram(mermaid, title)   renders the Mermaid text with mermaid.js (served by CANAH itself, from
                                 static/vendor) in a headless Chromium, and keeps it as SVG and PNG under
                                 CANAH_HOME/diagrams. A syntax error comes back as the tool's answer, so the
                                 agent fixes the diagram before the operator sees it.

The web page also draws any ```mermaid block in an answer by itself; the tool is what checks the text first and
gives files to download (/diagrams/<id>.png, .svg). start.sh installs mermaid.js and the Chromium it needs.
"""
from __future__ import annotations

import concurrent.futures
import glob
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List

from langchain_core.tools import BaseTool, StructuredTool

logger = logging.getLogger("canah.diagrams")

MERMAID_JS = Path(__file__).resolve().parent / "static" / "vendor" / "mermaid.min.js"
NAME = re.compile(r"^[0-9a-f]{16}\.(png|svg)$")


def chromium() -> str:
    """Playwright's own Chromium when it is installed, else the newest one in its cache."""
    found = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    return found[-1] if found else ""


def render(mermaid: str, out_dir: Path, theme: str = "default") -> Dict[str, Any]:
    """{"ok", "id", "svg", "png"} or {"ok": False, "error"}: in a thread of its own, so it runs the same from
    inside an event loop or outside one."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_render, mermaid, out_dir, theme).result(timeout=90)


def _render(mermaid: str, out_dir: Path, theme: str) -> Dict[str, Any]:
    if not MERMAID_JS.exists():
        return {"ok": False, "error": "mermaid.js is not installed: run ./start.sh (it fetches canah/static/vendor/mermaid.min.js)"}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright is not installed (pip install -r requirements.txt)"}
    out_dir.mkdir(parents=True, exist_ok=True)
    did = uuid.uuid4().hex[:16]
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception:  # noqa: BLE001 - the cached Chromium when playwright's own is not installed
            exe = chromium()
            if not exe:
                return {"ok": False, "error": "no Chromium: run `python -m playwright install chromium`"}
            browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1200}, device_scale_factor=2)
            page.set_content('<html><body style="margin:0;background:#fff"><div id="out" style="display:inline-block;padding:16px"></div></body></html>')
            page.add_script_tag(path=str(MERMAID_JS))
            got = page.evaluate("""async ([src, theme]) => {
                mermaid.initialize({startOnLoad: false, securityLevel: 'strict', theme});
                try { await mermaid.parse(src); } catch (e) { return {error: String(e.message || e)}; }
                const {svg} = await mermaid.render('d', src);
                document.getElementById('out').innerHTML = svg;
                return {svg};
            }""", [mermaid, theme])
            if got.get("error"):
                return {"ok": False, "error": "not valid Mermaid: " + " / ".join(x.strip() for x in got["error"].split("\n")[:4] if x.strip())[:500]}
            (out_dir / f"{did}.svg").write_text(got["svg"])
            (out_dir / f"{did}.mmd").write_text(mermaid)
            page.locator("#out").screenshot(path=str(out_dir / f"{did}.png"))
        finally:
            browser.close()
    return {"ok": True, "id": did, "svg": f"/diagrams/{did}.svg", "png": f"/diagrams/{did}.png"}


class DiagramProvider:
    """draw_diagram and get_diagram, for every agent (a ToolProvider)."""

    def __init__(self, home: Any):
        self.dir = Path(home) / "diagrams"
        self._tools = [StructuredTool.from_function(self.draw_diagram), StructuredTool.from_function(self.get_diagram)]

    def get_diagram(self, diagram_id: str) -> str:
        """The Mermaid text and title of a diagram drawn before (its id: the 16 hex digits in /diagrams/<id>.png),
        to change it as the operator asks: edit the text, then draw_diagram again with based_on=<id>."""
        did = diagram_id.strip().split("/")[-1].split(".")[0]
        if not re.fullmatch(r"[0-9a-f]{16}", did) or not (self.dir / f"{did}.mmd").exists():
            return f"Error: no diagram {diagram_id!r}."
        meta = {}
        try:
            meta = json.loads((self.dir / f"{did}.json").read_text())
        except (OSError, ValueError):
            pass
        return json.dumps({"id": did, "title": meta.get("title", ""), "mermaid": (self.dir / f"{did}.mmd").read_text(),
                           "png": f"/diagrams/{did}.png", "based_on": meta.get("based_on")})

    def draw_diagram(self, mermaid: str, title: str = "", based_on: str = "") -> str:
        """Draw a diagram from Mermaid text (flowchart, sequenceDiagram, stateDiagram, ...), to explain a flow, a
        scenario, a topology or an issue with a picture. Gives back links to the picture (PNG and SVG), or the
        syntax error to fix. Then put the same Mermaid text in your answer in a ```mermaid block: the console draws
        it there. Keep labels short; quote labels with spaces or brackets: A["Agentic MCP (hub)"]. To change a
        diagram drawn before, get its text with get_diagram, edit it, and pass based_on=<its id>."""
        try:
            got = render(mermaid, self.dir)
        except Exception as e:  # noqa: BLE001 - said to the model
            return f"Error: the diagram could not be drawn: {type(e).__name__}: {e}"
        if got.get("ok"):
            got["title"] = title
            if based_on:
                got["based_on"] = based_on
            try:
                (self.dir / f"{got['id']}.json").write_text(json.dumps({"title": title, "based_on": based_on or None}))
            except OSError:
                pass
            got["note"] = ("Drawn. Put the same Mermaid text in your answer in a ```mermaid block (the console draws "
                           f"it), and give the picture as a link: [{title or 'diagram'} (PNG)]({got['png']}).")
        return json.dumps(got)

    def tools(self) -> List[BaseTool]:
        return list(self._tools)

    def writes(self) -> List[str]:
        return []

    def instructions(self) -> str:
        return ("Pictures only when the operator asks for one (draw, a diagram, a picture, an image, a chart, a "
                "sketch). Then call draw_diagram with Mermaid text, keep it small (at most about 20 boxes, short "
                "labels), and put the same text in a ```mermaid block in your answer (the console draws it) with the "
                "PNG link. Otherwise answer in words and lists: no diagram and no ```mermaid block, however much a "
                "picture might help. When the operator asks to change a diagram (they name it by its id, 16 hex "
                "digits), call get_diagram(id), change the text as asked, and draw it again with "
                "draw_diagram(..., based_on=id).")


# What asks for a picture. "Show me the topology" does not: a list of what is where answers it.
ASKS_PICTURE = re.compile(r"\b(draw\w*|diagrams?|pictures?|images?|sketch\w*|charts?|plot\w*|graphically|visuali[sz]\w*|"
                          r"illustrat\w*|flow ?charts?|mermaid|png|svg)\b|\b[0-9a-f]{16}\b", re.I)
_PICTURE_TOOLS = ("draw_diagram", "get_diagram")


def asks_picture(text: str) -> bool:
    return bool(ASKS_PICTURE.search(text or ""))


class PicturesOnRequest:
    """The drawing tools are offered to the model only when the question asks for a picture. Otherwise a model that
    reaches for one anyway writes a long Mermaid call (cut short once, retried twice: 220 s for "show me the
    topology"); not offered, it answers in words at once."""

    def __new__(cls):
        from langchain.agents.middleware import AgentMiddleware

        class _Pictures(AgentMiddleware):
            def _strip(self, request):
                from .grounding import current_question
                if asks_picture(current_question(list(getattr(request, "messages", []) or []))):
                    return request
                tools = [t for t in (request.tools or []) if getattr(t, "name", t.get("name") if isinstance(t, dict) else "")
                         not in _PICTURE_TOOLS]
                return request.override(tools=tools) if len(tools) != len(request.tools or []) else request

            def wrap_model_call(self, request, handler):
                return handler(self._strip(request))

            async def awrap_model_call(self, request, handler):
                return await handler(self._strip(request))
        _Pictures.__name__ = "PicturesOnRequest"
        return _Pictures()

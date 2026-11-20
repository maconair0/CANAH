"""The web page in a real browser (Playwright), against a running service on the demo network.

  .venv/bin/python -m tests_canah.browser_check [--port 8352]

Starts `canah serve` on the demo network (the configured model), then in headless Chromium:
loads the page as observer, asks a question and sees the streamed answer and its tool calls,
signs in as admin, asks for a change, sees the approval box, approves it, and sees the result.
Exits non-zero on the first step that does not happen. Not part of the unit tests (it needs a
model and a browser)."""
import glob
import os
import subprocess
import sys
import tempfile
import time

import requests


def chromium() -> str:
    found = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    return found[-1] if found else ""


def main(port: int = 8352) -> int:
    from playwright.sync_api import expect, sync_playwright
    home = tempfile.mkdtemp(prefix="canah-browser-")
    env = {**os.environ, "CANAH_HOME": home, "CANAH_PROVIDERS": "examples.demo_network:provider",
           "CANAH_CONFIG": "/nonexistent", "CANAH_ADMIN_PASSWORD": "browser-admin"}
    srv = subprocess.Popen([sys.executable, "-m", "canah", "serve", "--port", str(port), "--no-sandbox"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/"
    try:
        for _ in range(120):
            try:
                if requests.get(url + "health", timeout=2).ok:
                    break
            except requests.RequestException:
                time.sleep(1)
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=chromium() or None)
            page = b.new_page()
            page.goto(url)
            expect(page.locator("#who")).to_contain_text("observer")
            print("ok  page loads; observer by default")
            page.fill("#text", "Which devices are down?")
            page.click("#send")
            expect(page.locator(".step").first).to_contain_text("list_devices", timeout=120_000)
            expect(page.locator(".msg:not(.you)").last).to_contain_text("r2", timeout=120_000)
            print("ok  observer question: tool call shown, answer streamed")
            page.fill("#user", "admin")
            page.fill("#pass", "browser-admin")
            page.click("#login")
            expect(page.locator("#who")).to_contain_text("admin (admin)")
            print("ok  signed in as admin")
            page.fill("#text", "Restart device r2.")
            page.click("#send")
            box = page.locator(".pending").last
            expect(box).to_contain_text("restart_device", timeout=120_000)
            expect(box.locator("button.primary")).to_be_enabled()
            print("ok  change waits for approval; Approve offered to admin")
            box.locator("button.primary").click()
            expect(page.locator(".msg:not(.you)").last).to_contain_text("up", timeout=120_000)
            print("ok  approved in the page; the result comes back")
            b.close()
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {type(e).__name__}: {str(e)[:400]}")
        return 1
    finally:
        srv.terminate()
        srv.wait(20)


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8352
    sys.exit(main(port))

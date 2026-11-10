"""Sends a guest-post form with a real headless browser (Playwright), for forms without a CAPTCHA. It uses exactly the same
form-filling code as the owner's Chrome extension (extension/fill.js), so both fill a form the same way. The browser uses its
own default identity: nothing pretends to be a person, and a site that refuses automated browsers is left to the owner's click.
Outcomes: sent | unclear (it may have gone through: never resend) | captcha | not_filled | error | blocked | unavailable."""
import subprocess
import sys
from pathlib import Path

FILL_JS = Path(__file__).resolve().parents[2] / "extension" / "fill.js"
_ready = False


def ensure_browser() -> str | None:
    """Installs Playwright's Chromium on first use (a GitHub runner starts empty). Returns a problem, or None when ready."""
    global _ready
    if _ready:
        return None
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "the playwright package is not installed"
    for args in (["install", "--with-deps", "chromium"], ["install", "chromium"]):   # --with-deps needs root, which GitHub has
        done = subprocess.run([sys.executable, "-m", "playwright", *args], capture_output=True, text=True, timeout=900)
        if done.returncode == 0:
            _ready = True
            return None
    return "the browser could not be installed: " + (done.stderr or done.stdout)[-300:]


def submit(url: str, payload: dict, shot_path: Path | None = None, timeout_ms: int = 45000) -> dict:
    problem = ensure_browser()
    if problem:
        return {"outcome": "unavailable", "detail": problem}
    from playwright.sync_api import sync_playwright
    js = FILL_JS.read_text(encoding="utf-8")
    clicked = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if resp is not None and resp.status in (401, 403, 429, 503):
                    return {"outcome": "blocked", "detail": f"the site answered HTTP {resp.status} to the browser"}
                page.wait_for_timeout(2500)
                page.add_script_tag(content=js)
                filled = page.evaluate("p => blFill(p)", payload)
                if filled["missing"]:
                    return {"outcome": "not_filled", "detail": "could not fill: " + ", ".join(filled["missing"])}
                if filled["captcha"] != "none" or filled.get("captcha_present") or filled.get("human"):
                    return {"outcome": "captcha", "detail": "a CAPTCHA (visible or invisible) or a human-check question is on the live form"}
                stable = 0   # a website may reset its form a moment after loading (Contact Form 7): wait until the values have stayed put
                for _ in range(8):
                    page.wait_for_timeout(700)
                    stable = stable + 1 if not page.evaluate("p => blRefill(p)", payload) else 0
                    if stable >= 3:
                        break
                clicked = page.evaluate("p => blSubmit(p)", payload)
                if not clicked:
                    return {"outcome": "not_filled", "detail": "the form's Send button was not found"}
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:  # noqa: BLE001 -- a page that keeps loading is not a failure in itself
                    pass
                page.wait_for_timeout(3000)
                if not page.evaluate("typeof blResult === 'function'"):
                    page.add_script_tag(content=js)   # a redirect to a new page loses the injected code
                res = page.evaluate("p => blResult(p)", payload)
                shot = None
                if shot_path:
                    shot_path.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(shot_path))
                    shot = str(shot_path)
                outcome = "error" if res["error"] else "sent" if res["success"] else "unclear"
                return {"outcome": outcome, "detail": "; ".join(res["errors"]) or res["text"][:300], "final_url": page.url, "screenshot": shot}
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        # after the Send button was pressed the form may have gone through: report it as unclear, never as safe to resend
        return {"outcome": "unclear" if clicked else "blocked", "detail": f"{type(e).__name__}: {str(e)[:200]}"}

"""One long-lived Chromium that holds the Innoslate login.

Innoslate's REST key is refused on the free tier, and the login cookie is a session
cookie. So the server keeps a browser alive for its whole lifetime and sends every API
call as a same-origin fetch() from a page that is already logged in.

Two modes:
  * owned:  a persistent Chromium profile under ~/.innoslate-mcp/browser-profile (default)
  * attach: an existing Chromium started with --remote-debugging-port, via INNOSLATE_CDP_URL

Credentials, when the user put them in the environment or ~/.innoslate-mcp/.env, are typed
into the visible login form by this module. They never travel through a tool argument, a
tool result, a log line or the audit file.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from . import config
from .urls import Project

log = logging.getLogger("innoslate_mcp.browser")

_FETCH_JS = """
async ({method, url, body, form}) => {
  const opts = {method, credentials: 'same-origin', headers: {'Accept': 'application/json'}};
  if (body !== null && body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = body;
  }
  if (form) {
    const bin = Uint8Array.from(atob(form.b64), c => c.charCodeAt(0));
    const fd = new FormData();
    fd.append(form.field, new Blob([bin], {type: form.mime}), form.filename);
    opts.body = fd;
  }
  try {
    const r = await fetch(url, opts);
    const text = await r.text();
    return {status: r.status, text, url: r.url};
  } catch (e) {
    return {status: 0, text: String(e), url};
  }
}
"""

_MANY_JS = """
async ({urls, concurrency}) => {
  const out = new Array(urls.length);
  let next = 0;
  async function worker() {
    while (true) {
      const i = next++;
      if (i >= urls.length) return;
      try {
        const r = await fetch(urls[i], {credentials: 'same-origin', headers: {'Accept': 'application/json'}});
        out[i] = {status: r.status, text: await r.text()};
      } catch (e) { out[i] = {status: 0, text: String(e)}; }
    }
  }
  await Promise.all(Array.from({length: Math.min(concurrency, urls.length)}, worker));
  return out;
}
"""


def install_chromium() -> None:
    """One-time browser download (about 150 MB). Output goes to stderr: stdout is the MCP stream."""
    import subprocess
    import sys
    log.warning("Chromium is missing, installing it once with playwright (about 150 MB)...")
    r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       stdout=sys.stderr, stderr=sys.stderr, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise InnoslateError("Could not install Chromium. Run this once by hand: python -m playwright install chromium")


class InnoslateError(RuntimeError):
    pass


class NotLoggedIn(InnoslateError):
    pass


class Response:
    def __init__(self, status: int, text: str, url: str = ""):
        self.status, self.text, self.url = status, text, url

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        if not self.text:
            return None
        try:
            return json.loads(self.text)
        except ValueError as e:
            raise InnoslateError(f"Expected JSON from {self.url}, got: {self.text[:200]!r}") from e


class BrowserSession:
    """Owns Playwright, one browser context and one API page per Innoslate origin."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None      # only in attach mode
        self._context = None
        self._api_pages: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self.mode = ""

    # ------------------------------------------------------------------ lifecycle
    async def _ensure_context(self, headless: bool | None = None) -> Any:
        if self._context is not None:
            try:
                _ = self._context.pages  # raises if the browser died
                if self._browser is None or self._browser.is_connected():
                    return self._context
            except Exception:
                pass
            await self.close()
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        cdp = config.cdp_url()
        if cdp:
            self._browser = await self._pw.chromium.connect_over_cdp(cdp)
            self._context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            self.mode = f"attached to {cdp}"
        else:
            hl = config.headless() if headless is None else headless
            kw = dict(headless=hl, accept_downloads=True, viewport={"width": 1400, "height": 900},
                      args=["--disable-blink-features=AutomationControlled"])
            try:
                self._context = await self._pw.chromium.launch_persistent_context(str(config.profile_dir()), **kw)
            except Exception as e:
                if "Executable doesn't exist" not in str(e) and "playwright install" not in str(e):
                    raise
                await asyncio.to_thread(install_chromium)
                self._context = await self._pw.chromium.launch_persistent_context(str(config.profile_dir()), **kw)
            self.mode = "owned profile, " + ("headless" if hl else "headed window")
        self._context.on("close", lambda *_: self._forget())
        return self._context

    def _forget(self) -> None:
        self._context = None
        self._api_pages.clear()

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()       # in attach mode this only disconnects
            elif self._context is not None:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = None
        self._api_pages.clear()

    @property
    def running(self) -> bool:
        return self._context is not None

    async def context(self) -> Any:
        return await self._ensure_context()

    async def api_page(self, project: Project) -> Any:
        """A page sitting on the Innoslate origin, so fetch() carries the login cookie.
        Reuses one tab; only opens a new one when the old tab was closed."""
        ctx = await self._ensure_context()
        page = self._api_pages.get(project.origin)
        if page is not None and not page.is_closed():
            if not page.url.startswith(project.origin):
                await page.goto(project.home_url, wait_until="domcontentloaded")
            return page
        for p in ctx.pages:
            if not p.is_closed() and p.url.startswith(project.origin):
                self._api_pages[project.origin] = p
                return p
        page = ctx.pages[0] if (ctx.pages and ctx.pages[0].url in ("about:blank", "")) else await ctx.new_page()
        self._api_pages[project.origin] = page
        await page.goto(project.home_url, wait_until="domcontentloaded")
        return page

    # ----------------------------------------------------------------- requests
    async def request(self, project: Project, method: str, url: str, body: Any = None,
                      form: dict | None = None, retries: int = 2) -> Response:
        payload = None if body is None else json.dumps(body)
        last: Response | None = None
        for attempt in range(retries + 1):
            async with self._lock:
                page = await self.api_page(project)
                try:
                    r = await page.evaluate(_FETCH_JS, {"method": method, "url": url, "body": payload, "form": form})
                except Exception as e:  # page navigated or closed mid-call
                    self._api_pages.pop(project.origin, None)
                    r = {"status": 0, "text": f"browser error: {e}", "url": url}
            last = Response(r["status"], r["text"], r.get("url", url))
            if last.status in (0, 502, 503, 504) and attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            break
        assert last is not None
        if last.status in (401, 403):
            raise NotLoggedIn(f"Innoslate answered {last.status} for {method} {url}. "
                              "The login has expired or this account cannot open the project. Call innoslate_login.")
        return last

    async def get_many(self, project: Project, urls: list[str], concurrency: int = 8) -> list[Response]:
        if not urls:
            return []
        async with self._lock:
            page = await self.api_page(project)
            rows = await page.evaluate(_MANY_JS, {"urls": urls, "concurrency": concurrency})
        out = [Response(r["status"], r["text"], u) for r, u in zip(rows, urls)]
        if any(r.status in (401, 403) for r in out):
            raise NotLoggedIn("Innoslate refused the request. Call innoslate_login.")
        return out

    async def upload(self, project: Project, path: Path) -> Response:
        data = path.read_bytes()
        limit = config.max_upload_mb()
        if len(data) > limit * 1024 * 1024:
            raise InnoslateError(f"{path.name} is {len(data) / 1e6:.1f} MB. The limit is {limit} MB "
                                 "(INNOSLATE_MAX_UPLOAD_MB). Upload big files in the Innoslate UI.")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        form = {"field": "upload_file", "b64": base64.b64encode(data).decode(), "mime": mime, "filename": path.name}
        return await self.request(project, "POST", f"{project.papi}/files", form=form, retries=0)

    # -------------------------------------------------------------------- login
    async def is_logged_in(self, project: Project) -> tuple[bool, int]:
        """Probe the API with the browser's cookie jar, without touching any tab. A tab may be
        in the middle of a login on another origin (SSO) and must not be navigated away."""
        ctx = await self._ensure_context()
        try:
            r = await ctx.request.get(f"{project.papi}/schema", headers={"Accept": "application/json"},
                                      max_redirects=0, timeout=20000)
            return r.ok, r.status
        except Exception:
            return False, 0

    async def login(self, project: Project, wait_seconds: int = 300) -> dict:
        """Open the project, type stored credentials if there are any, wait for the API to answer 200."""
        ok, status = await self.is_logged_in(project)
        if ok:
            await self.api_page(project)
            return {"logged_in": True, "how": "already logged in", "status": status}
        page = await self.api_page(project)

        creds = config.credentials()
        tried_form = False
        deadline = time.monotonic() + max(5, wait_seconds)
        last_status = status
        while time.monotonic() < deadline:
            if creds and not tried_form:
                tried_form = await self._fill_login_form(page, creds)
            await asyncio.sleep(2)
            if not self.running:
                return {"logged_in": False, "status": 0, "how": "the browser window was closed. Call innoslate_login again."}
            ok, last_status = await self.is_logged_in(project)
            if ok:
                try:
                    if page.is_closed() or not page.url.startswith(project.origin):
                        self._api_pages.pop(project.origin, None)
                    await self.api_page(project)
                except Exception:
                    pass
                return {"logged_in": True, "status": last_status,
                        "how": "stored credentials typed into the login form" if tried_form else "user logged in in the browser window"}
        return {"logged_in": False, "status": last_status,
                "how": "timed out waiting for the login" + (" (stored credentials were typed but did not work)" if tried_form else "")}

    async def _fill_login_form(self, page: Any, creds: config.Credentials) -> bool:
        """Type username and password into whatever login form is visible. Handles one-page and
        two-step (username, Next, password) forms. Returns True once the password was submitted."""
        try:
            await page.wait_for_load_state("domcontentloaded")
            pw = page.locator("input[type=password]").filter(visible=True)
            user_sel = ("input[type=email], input[autocomplete=username], input[name*=user i], input[id*=user i], "
                        "input[name*=email i], input[id*=email i], input[name*=login i], input[type=text]")
            user = page.locator(user_sel).filter(visible=True)
            if await pw.count() == 0:
                if await user.count() == 0:
                    return False
                await user.first.fill(creds.username)
                await user.first.press("Enter")
                try:
                    await pw.first.wait_for(state="visible", timeout=15000)
                except Exception:
                    return False
            elif await user.count() > 0:
                await user.first.fill(creds.username)
            await pw.first.fill(creds.password)
            await pw.first.press("Enter")
            await page.wait_for_timeout(1500)
            return True
        except Exception as e:  # never include the exception text, it can echo field values
            log.warning("login form fill failed: %s", type(e).__name__)
            return False

    async def new_page(self) -> Any:
        ctx = await self._ensure_context()
        return await ctx.new_page()

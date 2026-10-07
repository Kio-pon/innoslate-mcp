from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_innoslate import PASSWORD, USER, FakeInnoslate  # noqa: E402

from innoslate_mcp.api import InnoslateAPI  # noqa: E402
from innoslate_mcp.browser import NotLoggedIn, Response  # noqa: E402
from innoslate_mcp.urls import parse_project_url  # noqa: E402


class DirectSession:
    """Same interface as BrowserSession, but plain HTTP with the session cookie.
    Lets the plan engine be tested quickly; the browser path has its own tests."""

    def __init__(self, base: str):
        self.base = base
        self.cookie = ""
        self.running = True
        self.mode = "direct"

    def login(self) -> None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        data = urllib.parse.urlencode({"username": USER, "password": PASSWORD, "next": "/"}).encode()
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(self.base + "/login", data)
        except urllib.error.HTTPError as e:
            self.cookie = e.headers["Set-Cookie"].split(";")[0]
        assert self.cookie

    async def request(self, project, method, url, body=None, form=None, retries=0):
        data, headers = None, {"Accept": "application/json", "Cookie": self.cookie}
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        if form is not None:
            import base64
            b = uuid.uuid4().hex
            raw = base64.b64decode(form["b64"])
            data = (f"--{b}\r\nContent-Disposition: form-data; name=\"{form['field']}\"; filename=\"{form['filename']}\"\r\n"
                    f"Content-Type: {form['mime']}\r\n\r\n").encode() + raw + f"\r\n--{b}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={b}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                resp = Response(r.status, r.read().decode(), url)
        except urllib.error.HTTPError as e:
            resp = Response(e.code, e.read().decode(), url)
        if resp.status in (401, 403):
            raise NotLoggedIn(f"{resp.status}")
        return resp

    async def get_many(self, project, urls, concurrency=8):
        return [await self.request(project, "GET", u) for u in urls]

    async def upload(self, project, path):
        import base64
        import mimetypes
        form = {"field": "upload_file", "b64": base64.b64encode(Path(path).read_bytes()).decode(),
                "mime": mimetypes.guess_type(str(path))[0] or "application/octet-stream", "filename": Path(path).name}
        return await self.request(project, "POST", f"{project.papi}/files", form=form)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("INNOSLATE_MCP_HOME", str(home))
    monkeypatch.setenv("INNOSLATE_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("INNOSLATE_HEADLESS", "1")
    monkeypatch.delenv("INNOSLATE_USERNAME", raising=False)
    monkeypatch.delenv("INNOSLATE_PASSWORD", raising=False)
    monkeypatch.delenv("INNOSLATE_CDP_URL", raising=False)
    return home


@pytest.fixture
def fake():
    with FakeInnoslate() as f:
        yield f


@pytest.fixture
def fake_nonum():
    """Search that does not index entity numbers, to exercise the fallback lookups."""
    with FakeInnoslate(search_numbers=False) as f:
        yield f


def direct_api(f: FakeInnoslate) -> InnoslateAPI:
    s = DirectSession(f.base)
    s.login()
    return InnoslateAPI(s, parse_project_url(f.project_url))


@pytest.fixture
def api(fake):
    return direct_api(fake)


@pytest.fixture
def api_nonum(fake_nonum):
    return direct_api(fake_nonum)

"""Turn an Innoslate project URL into host, org slug and project id."""
from __future__ import annotations

import re
from dataclasses import dataclass

# https://cloud.innoslate.com/myorg/p/1234/dashboard
# https://innoslate.example.org/innoslate/acme/p/12/documents/345   (self-hosted, context path)
_RX = re.compile(r"^(?P<base>https?://[^/?#]+(?:/[^?#]*?)?)/(?P<org>[^/?#]+)/p/(?P<pid>\d+)(?:[/?#].*)?$")


@dataclass(frozen=True)
class Project:
    base: str      # scheme://host[/context-path], no trailing slash
    org: str
    pid: int

    @property
    def key(self) -> str:
        return f"{self.org}-{self.pid}"

    @property
    def api(self) -> str:
        """Organisation-level API root."""
        return f"{self.base}/api/v4/o/{self.org}"

    @property
    def papi(self) -> str:
        """Project-level API root."""
        return f"{self.api}/p/{self.pid}"

    @property
    def home_url(self) -> str:
        return f"{self.base}/{self.org}/p/{self.pid}/dashboard"

    def document_url(self, doc_id: int) -> str:
        return f"{self.base}/{self.org}/p/{self.pid}/documents/{doc_id}"

    @property
    def origin(self) -> str:
        m = re.match(r"^https?://[^/]+", self.base)
        return m.group(0) if m else self.base


def parse_project_url(url: str) -> Project:
    url = (url or "").strip()
    m = _RX.match(url)
    if not m:
        raise ValueError(
            "Not an Innoslate project URL. Open the project in Innoslate and copy the address bar. "
            "It looks like https://cloud.innoslate.com/<org>/p/<projectId>/dashboard"
        )
    base = m.group("base").rstrip("/")
    return Project(base=base, org=m.group("org"), pid=int(m.group("pid")))

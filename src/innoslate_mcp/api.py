"""Innoslate REST calls, sent through the logged-in browser.

Endpoints (relative to the Innoslate host), proven on cloud.innoslate.com:
    GET  /api/v4/o/{org}/p/{pid}                         project
    GET  /api/v4/o/{org}/p/{pid}/schema                  classes, relations, labels, properties
    GET  /api/v4/o/{org}/p/{pid}/entities/counts?class=true&label=false
    GET  /api/v4/o/{org}/entities?projectId=&query=&limit=&offset=
    GET  /api/v4/o/{org}/entities/{id}
    GET  /api/v4/o/{org}/p/{pid}/comments | users | entities/changes
    GET  /api/v4/o/{org}/allocate_ids?type=entity&count=N
    POST /api/v4/o/{org}/entities                        array of full entity objects
    POST /api/v4/o/{org}/p/{pid}/files                   multipart field upload_file
"""
from __future__ import annotations

import html
import re
from typing import Any, Iterable
from urllib.parse import quote

from .browser import BrowserSession, InnoslateError
from .schema import Schema
from .urls import Project

PAGE = 500


def strip_html(h: str | None) -> str:
    t = h or ""
    t = re.sub(r"</(td|th)>", " | ", t)
    t = re.sub(r"</(tr|p|li|h\d|div)>|<br\s*/?>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def to_html(text: str) -> str:
    """Plain text to Innoslate rich text: one <p> per paragraph, escaped."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]
    return "".join(f"<p>{html.escape(p, quote=False)}</p>" for p in paras)


def num_key(n: str | None) -> list:
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n or "~")]


class InnoslateAPI:
    def __init__(self, session: BrowserSession, project: Project):
        self.s = session
        self.p = project
        self._schema: Schema | None = None
        self._project_info: dict | None = None

    # ------------------------------------------------------------------- basics
    async def _get(self, url: str) -> Any:
        r = await self.s.request(self.p, "GET", url)
        if not r.ok:
            raise InnoslateError(f"GET {url} -> HTTP {r.status}: {r.text[:300]}")
        return r.json()

    async def project_info(self) -> dict:
        if self._project_info is None:
            self._project_info = await self._get(self.p.papi) or {}
        return self._project_info

    async def schema(self, refresh: bool = False) -> Schema:
        if self._schema is None or refresh:
            self._schema = Schema(await self._get(f"{self.p.papi}/schema"))
        return self._schema

    async def counts(self) -> dict:
        return await self._get(f"{self.p.papi}/entities/counts?class=true&label=false") or {}

    async def comments(self) -> list[dict]:
        r = await self._get(f"{self.p.papi}/comments")
        return r if isinstance(r, list) else (r or {}).get("comments", []) if isinstance(r, dict) else []

    async def users(self) -> list[dict]:
        r = await self._get(f"{self.p.papi}/users")
        if isinstance(r, dict):
            r = r.get("users") or r.get("results") or []
        return r or []

    # ------------------------------------------------------------------ entities
    async def search(self, query: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """One page of results. limit is capped at 500 by Innoslate."""
        url = f"{self.p.api}/entities?projectId={self.p.pid}&query={quote(query)}&limit={min(limit, PAGE)}&offset={offset}"
        r = await self._get(url)
        return r if isinstance(r, list) else []

    async def search_all(self, query: str, cap: int = 20000) -> list[dict]:
        out: list[dict] = []
        off = 0
        while off < cap:
            page = await self.search(query, PAGE, off)
            out.extend(page)
            if len(page) < PAGE:
                break
            off += PAGE
        return out

    async def all_entities(self) -> list[dict]:
        """Every entity in the project, fetched class by class (a bare query is capped)."""
        sch = await self.schema()
        seen: dict[int, dict] = {}
        for c in sch.classes:
            name = c.get("name", "")
            q = f'class:"{name}"' if re.search(r"[^\w]", name) else f"class:{name}"
            for e in await self.search_all(q):
                if e.get("projectId") in (None, self.p.pid):
                    seen[e["id"]] = e
        return list(seen.values())

    async def get(self, entity_id: int) -> dict | None:
        r = await self.s.request(self.p, "GET", f"{self.p.api}/entities/{int(entity_id)}")
        if r.status == 404:
            return None
        if not r.ok:
            raise InnoslateError(f"GET entity {entity_id} -> HTTP {r.status}: {r.text[:200]}")
        j = r.json()
        if isinstance(j, list):
            j = j[0] if j else None
        return j

    async def get_many(self, ids: Iterable[int]) -> dict[int, dict]:
        ids = list(dict.fromkeys(int(i) for i in ids))
        urls = [f"{self.p.api}/entities/{i}" for i in ids]
        out: dict[int, dict] = {}
        for i, r in zip(ids, await self.s.get_many(self.p, urls)):
            if not r.ok:
                continue
            j = r.json()
            if isinstance(j, list):
                j = j[0] if j else None
            if j:
                out[i] = j
        return out

    async def allocate_ids(self, count: int) -> list[dict]:
        if count <= 0:
            return []
        r = await self._get(f"{self.p.api}/allocate_ids?type=entity&count={count}")
        if not isinstance(r, list) or len(r) < count:
            raise InnoslateError(f"allocate_ids returned {r!r:.200}")
        return r

    async def save(self, entities: list[dict]) -> Any:
        r = await self.s.request(self.p, "POST", f"{self.p.api}/entities", body=entities, retries=0)
        if not r.ok:
            raise InnoslateError(f"Save rejected, HTTP {r.status}: {r.text[:600]}")
        return r.json() if r.text else None

    async def upload_file(self, path) -> str:
        r = await self.s.upload(self.p, path)
        if not r.ok:
            raise InnoslateError(f"Upload rejected, HTTP {r.status}: {r.text[:300]}")
        j = r.json()
        key = (j[0] if isinstance(j, list) and j else j or {}).get("key")
        if not key:
            raise InnoslateError(f"Upload answered without a file key: {r.text[:200]}")
        return key

    async def file_meta(self, key: str) -> Any:
        r = await self.s.request(self.p, "GET", f"{self.p.api}/files/{quote(key)}")
        return r.json() if r.ok else None

    # ----------------------------------------------------------------- documents
    async def document_members(self, doc: dict, max_depth: int = 20) -> list[tuple[int, dict]]:
        """(depth, entity) for everything inside a document: items it is the source of,
        then whatever decomposes those, recursively. Depth 0 is a top-level item."""
        sch = await self.schema()
        source_of = _maybe(sch, "source of")
        decomposed_by = _maybe(sch, "decomposed by")
        top = [r["targetId"] for r in doc.get("rels", []) if r.get("relationId") == source_of]
        out: list[tuple[int, dict]] = []
        seen = {doc["id"]}
        frontier = [(0, i) for i in top]
        while frontier:
            ids = [i for _, i in frontier if i not in seen]
            depth_of = {i: d for d, i in frontier}
            got = await self.get_many(ids)
            nxt = []
            for i in ids:
                e = got.get(i)
                if not e or i in seen:
                    continue
                seen.add(i)
                out.append((depth_of[i], e))
                if depth_of[i] < max_depth and decomposed_by is not None:
                    nxt += [(depth_of[i] + 1, r["targetId"]) for r in e.get("rels", [])
                            if r.get("relationId") == decomposed_by]
            frontier = nxt
        return out

    async def find_documents(self, number: str | None = None, name: str | None = None) -> list[dict]:
        sch = await self.schema()
        art = sch.class_id("Artifact")
        q = f'"{name}"' if name else (f'"{number}"' if number else "class:Artifact")
        hits = await self.search_all(q, cap=2000)
        if number and not name and not any(h.get("number") == number for h in hits):
            hits = await self.search_all("class:Artifact", cap=5000)
        return [h for h in hits if h.get("classId") == art
                and (number is None or (h.get("number") or "") == number)
                and (name is None or (h.get("name") or "").strip() == name.strip())]

    async def find_by_number(self, number: str) -> list[dict]:
        hits = await self.search_all(f'"{number}"', cap=2000)
        exact = [h for h in hits if (h.get("number") or "") == number]
        return exact


def _maybe(sch: Schema, name: str) -> int | None:
    try:
        return sch.relation_id(name)
    except LookupError:
        return None


def describe(sch: Schema, e: dict, full: bool = True, names: dict[int, str] | None = None) -> dict:
    """An entity in plain terms: names instead of ids, text instead of HTML."""
    names = names or {}
    rels: dict[str, list] = {}
    for r in e.get("rels", []) or []:
        t = r.get("targetId")
        rels.setdefault(sch.relation_name(r.get("relationId")), []).append(
            {"id": t, "label": names[t]} if t in names else t)
    attrs = {}
    for k, v in (e.get("attrs") or {}).items():
        if v in (None, "", [], {}):
            continue
        typ = sch.property_type(k)
        if typ == "QUALITY" and isinstance(v, dict):
            v = {"value": v.get("value"), "suggestions": v.get("suggestions") or []}
        elif isinstance(v, str) and "<" in v:
            v = strip_html(v)
        attrs[sch.property_name(k)] = v
    out = {
        "id": e.get("id"),
        "number": e.get("number") or "",
        "name": e.get("name") or "",
        "class": sch.class_name(e.get("classId")),
        "labels": [sch.label_name(l) for l in e.get("labelIds") or []],
        "description": strip_html(e.get("description")) if full else strip_html(e.get("description"))[:200],
        "relations": rels,
        "attributes": attrs,
    }
    for k in ("created", "modified", "createdBy", "modifiedBy"):
        if e.get(k) is not None:
            out[k] = e[k]
    return out

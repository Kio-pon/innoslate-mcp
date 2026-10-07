"""MCP server: lets any AI client read and fill an Innoslate project through a logged-in browser."""
from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]
from mcp.types import ToolAnnotations

from . import __version__, config, docs, lint, ui
from . import workspace as wsp
from .api import InnoslateAPI, describe, num_key, strip_html
from .browser import BrowserSession, InnoslateError
from .plan import PlanError, Planner, confirm_token as make_token
from .survey import Survey
from .urls import Project, parse_project_url

log = logging.getLogger("innoslate_mcp")

WORKFLOW = """\
How to use this server (follow it in order):

1. Ask the user for their Innoslate project URL (open the project, copy the address bar,
   e.g. https://cloud.innoslate.com/<org>/p/<id>/dashboard). Do not guess it.
2. innoslate_login(project_url). A browser window opens; the user logs in there. If they saved
   INNOSLATE_USERNAME / INNOSLATE_PASSWORD in ~/.innoslate-mcp/.env, the server types them.
   Never ask the user to paste a password into the chat, and never pass one to a tool.
3. innoslate_initialize(project_url, guides_dir=<folder with the instructor's guide PDFs>).
   This reads the whole project and writes PROJECT_MAP.md and NOMENCLATURE.md.
4. Read both with innoslate_read_workspace("map") and ("nomenclature"). Read the guides there too.
   Learn: which documents exist, who made them, how items are numbered, which labels and
   relations this project uses, and what the reviewer's comments ask for.
5. Read the user's own documents with list_local_files / read_local_file.
6. Decide where each piece goes. Tell the user the plan in plain words (which document,
   which numbers, which links) and wait for their agreement. Ask when the guide is unclear.
7. innoslate_lint_requirements on any requirement text before writing it.
8. innoslate_apply(plan) with dry_run=true. Show the user the summary and warnings.
9. Only after the user says yes: innoslate_apply(plan, dry_run=false, confirm_token=<from step 8>).
   Check verify.ok in the result. If it is false, report the problems; do not retry blindly.
10. Optional: innoslate_attach_file for evidence files, innoslate_quality_check on requirement
    documents, innoslate_report to download DOCX/XLSX outputs for submission.

Rules:
- One document = one Artifact with a document label. Items join it with 'sourced by';
  deeper items join their parent with 'decomposes'. Check PROJECT_MAP section 2 for what
  this project actually does and copy it.
- Numbers repeat freely in Innoslate. Always say which document a number belongs to.
- There is no delete tool. Deleting is done by the user in the Innoslate UI.
- Never change other people's work unless the user explicitly asks for that change.
- Do not invent values (thresholds, dates, percentages). Write TBD and raise it with the user.
"""


class State:
    def __init__(self) -> None:
        self.session = BrowserSession()
        self.project: Project | None = None
        self.apis: dict[str, InnoslateAPI] = {}

    def resolve_project(self, project_url: str | None) -> Project:
        if project_url:
            p = parse_project_url(project_url)
            self.project = p
            return p
        if self.project is None:
            raise InnoslateError("No project yet. Call innoslate_login with the project URL first.")
        return self.project

    def api(self, project_url: str | None = None) -> InnoslateAPI:
        p = self.resolve_project(project_url)
        if p.key not in self.apis:
            self.apis[p.key] = InnoslateAPI(self.session, p)
        return self.apis[p.key]


STATE = State()


@contextlib.asynccontextmanager
async def _lifespan(_server: Any):
    try:
        yield {}
    finally:
        await STATE.session.close()


try:
    mcp = _Server("innoslate", instructions=WORKFLOW, lifespan=_lifespan, version=__version__)
except TypeError:  # mcp 1.x has no version argument
    mcp = _Server("innoslate", instructions=WORKFLOW, lifespan=_lifespan)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
LOCAL_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


def _fail(e: Exception) -> None:
    """Turn any exception into a ToolError, the one type MCP passes to the AI with its message.
    Messages come from this package or from Playwright; credentials never reach them."""
    if isinstance(e, ToolError):
        raise e
    if isinstance(e, (PlanError, InnoslateError, LookupError, ValueError, FileNotFoundError, docs.UnsupportedFile)):
        raise ToolError(str(e)) from None
    log.exception("tool failed")
    raise ToolError(f"{type(e).__name__}: {str(e)[:1500]}") from None


async def _document(api: InnoslateAPI, ref: Any) -> dict:
    """A document by id, number or exact name. Ambiguity is an error that lists the choices."""
    sch = await api.schema()
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        d = await api.get(int(ref))
        if not d:
            raise LookupError(f"No entity with id {ref}.")
        return d
    ref = str(ref).strip()
    hits = await api.find_documents(number=ref)
    if not hits:
        hits = await api.find_documents(name=ref)
    if len(hits) == 1:
        return await api.get(hits[0]["id"]) or hits[0]
    if not hits:
        raise LookupError(f"No document (Artifact) with number or name {ref!r}. See PROJECT_MAP.md section 4.")
    rows = "; ".join(f"id {h['id']} {h.get('number') or '-'} {h.get('name')!r} by {h.get('createdBy', '?')}" for h in hits[:10])
    raise LookupError(f"{len(hits)} documents match {ref!r}: {rows}. Pass the id.")


# =========================================================================== tools
@mcp.tool(annotations=LOCAL_RO)
async def innoslate_get_started() -> dict:
    """Start here. Returns the step-by-step workflow, the current login state and where files go."""
    p = STATE.project
    return {
        "workflow": WORKFLOW,
        "version": __version__,
        "current_project": p.home_url if p else None,
        "browser": STATE.session.mode or "not started",
        "home": str(config.home()),
        "credentials_file": str(config.home() / ".env"),
        "credentials_saved": config.credentials() is not None,
        "credentials_file_warning": config.env_file_warning(),
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def innoslate_login(project_url: str, wait_seconds: int = 300) -> dict:
    """Open the Innoslate project in a browser window and wait until the user is logged in.

    project_url: the address bar of the project, https://<host>/<org>/p/<projectId>/...
    wait_seconds: how long to wait for the user to finish logging in (default 300).
    If INNOSLATE_USERNAME and INNOSLATE_PASSWORD are set in the environment or in
    ~/.innoslate-mcp/.env, they are typed into the login form automatically. Passwords are
    never accepted as tool arguments.
    """
    try:
        api = STATE.api(project_url)
        res = await STATE.session.login(api.p, wait_seconds)
        out = {"project_url": api.p.home_url, "org": api.p.org, "project_id": api.p.pid,
               "browser": STATE.session.mode, **res}
        if res["logged_in"]:
            info = await api.project_info()
            out["project_name"] = info.get("name")
            out["next"] = "innoslate_initialize"
        else:
            out["next"] = "Ask the user to finish logging in in the browser window, then call innoslate_login again."
        return out
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=RO)
async def innoslate_status() -> dict:
    """Is the browser running, which project is current, is the login still valid."""
    p = STATE.project
    out: dict[str, Any] = {"browser": STATE.session.mode or "not started", "current_project": p.home_url if p else None}
    if p and STATE.session.running:
        ok, status = await STATE.session.is_logged_in(p)
        out.update(logged_in=ok, http_status=status)
        ws = wsp.workspace(p)
        out["workspace"] = str(ws)
        out["initialized"] = (ws / "PROJECT_MAP.md").exists()
    return out


@mcp.tool(annotations=RO)
async def innoslate_initialize(project_url: str | None = None, guides_dir: str | None = None) -> dict:
    """Survey the whole project before any write. Run once per project, and again after big changes.

    Reads every entity, the schema, review comments and members, then writes PROJECT_MAP.md,
    NOMENCLATURE.md and snapshot.json into the project workspace. guides_dir is a folder (or one
    file) with the instructor's guide documents (PDF, DOCX, ...); their text is saved under
    guides/ and the numbering patterns they mention are compared with what the project uses.
    Returns a summary. Read the full files with innoslate_read_workspace.
    """
    try:
        api = STATE.api(project_url)
        return await Survey(api, wsp.workspace(api.p)).run(guides_dir)
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=LOCAL_RO)
async def innoslate_read_workspace(name: str = "map", offset: int = 0, max_chars: int = 30000,
                                   project_url: str | None = None) -> dict:
    """Read a file from the project workspace: "map" (PROJECT_MAP.md), "nomenclature",
    "audit" (the write log) or a relative path such as "guides/Deliverable_2.txt".
    Long files come in pages: pass next_offset back as offset."""
    try:
        return wsp.read_file(STATE.resolve_project(project_url), name, offset, max_chars)
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=RO)
async def innoslate_schema(kind: str = "all", class_name: str | None = None, project_url: str | None = None) -> dict:
    """The project's classes, relations (with inverses), labels and properties.

    kind: all | classes | relations | labels | properties
    class_name: restrict labels and properties to one class (e.g. "Requirement").
    """
    try:
        api = STATE.api(project_url)
        return (await api.schema()).summary(kind, class_name)
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=RO)
async def innoslate_find(text: str | None = None, class_name: str | None = None, label: str | None = None,
                         number: str | None = None, number_prefix: str | None = None, document: str | None = None,
                         limit: int = 50, offset: int = 0, project_url: str | None = None) -> dict:
    """Search entities. Combine any filters:
    text (words in name or description), class_name, label, number (exact),
    number_prefix (e.g. "SR.1."), document (id, number or name: only items inside it).
    Returns short rows; use innoslate_get for one entity in full."""
    try:
        api = STATE.api(project_url)
        sch = await api.schema()
        cid = sch.class_id(class_name) if class_name else None
        lid = sch.label_id(label, cid) if label else None
        if document:
            doc = await _document(api, document)
            pool = [e for _, e in await api.document_members(doc)]
        elif number or text:
            pool = await api.search_all(f'"{number or text}"', cap=5000)
            if number and not any((e.get("number") or "") == number for e in pool):
                # this Innoslate's search may not index numbers: try the class, then the last snapshot
                if cid is not None:
                    pool = await api.search_all(f'class:"{sch.class_name(cid)}"')
                else:
                    pool = list((await api.get_many(wsp.number_index(api.p).get(number, []))).values())
        elif cid is not None:
            pool = await api.search_all(f'class:"{sch.class_name(cid)}"' if not sch.class_name(cid).isalnum()
                                        else f"class:{sch.class_name(cid)}")
        elif number_prefix or label:
            pool = await api.all_entities()
        else:
            raise ValueError("Give at least one filter: text, class_name, label, number, number_prefix or document.")
        t = (text or "").lower()
        rows = [e for e in pool
                if (cid is None or e.get("classId") == cid)
                and (lid is None or lid in (e.get("labelIds") or []))
                and (number is None or (e.get("number") or "") == number)
                and (number_prefix is None or (e.get("number") or "").startswith(number_prefix))
                and (not t or t in (e.get("name") or "").lower() or t in strip_html(e.get("description")).lower()
                     or t in (e.get("number") or "").lower())]
        rows.sort(key=lambda e: num_key(e.get("number")))
        page = rows[offset:offset + max(1, min(limit, 500))]
        return {"total": len(rows), "offset": offset, "returned": len(page),
                "results": [{"id": e["id"], "number": e.get("number") or "", "name": e.get("name") or "",
                             "class": sch.class_name(e.get("classId")),
                             "labels": [sch.label_name(l) for l in e.get("labelIds") or []],
                             "description": strip_html(e.get("description"))[:200]} for e in page]}
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=RO)
async def innoslate_get(ref: str, project_url: str | None = None) -> dict:
    """One entity in full: text, labels, attributes, and every relation with the target's number
    and name. ref is an id, or a number. A number can match several entities (numbers repeat in
    Innoslate), so every match is returned with the documents it sits in."""
    try:
        api = STATE.api(project_url)
        sch = await api.schema()
        if ref.strip().isdigit():
            e = await api.get(int(ref))
            cands = [e] if e else []
        else:
            cands = await api.find_by_number(ref.strip())
            if not cands:
                idx = wsp.number_index(api.p).get(ref.strip(), [])
                cands = [x for x in (await api.get_many(idx)).values() if (x.get("number") or "") == ref.strip()]
        if not cands:
            raise LookupError(f"Nothing matches {ref!r}. Try innoslate_find(text=...).")
        full = await api.get_many([c["id"] for c in cands[:10]])
        targets = {r["targetId"] for e in full.values() for r in e.get("rels", [])}
        tmap = await api.get_many(list(targets)[:300])
        names = {i: f"{t.get('number') or ''} {t.get('name') or ''}".strip() for i, t in tmap.items()}
        return {"matches": len(cands), "entities": [describe(sch, e, True, names) for e in full.values()]}
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=RO)
async def innoslate_document_tree(document: str, full_text: bool = True, max_depth: int = 10,
                                  project_url: str | None = None) -> dict:
    """Everything inside a document, in order, with depth, text, labels and trace links.
    document is an id, a number (e.g. "UN.1") or the exact name."""
    try:
        api = STATE.api(project_url)
        sch = await api.schema()
        doc = await _document(api, document)
        members = await api.document_members(doc, max_depth)
        ids = {e["id"] for _, e in members}
        outside = {r["targetId"] for _, e in members for r in e.get("rels", [])} - ids - {doc["id"]}
        tmap = await api.get_many(list(outside)[:400])
        names = {i: f"{t.get('number') or ''} {t.get('name') or ''}".strip() for i, t in tmap.items()}
        names.update({e["id"]: f"{e.get('number') or ''} {e.get('name') or ''}".strip() for _, e in members})
        names[doc["id"]] = f"{doc.get('number') or ''} {doc.get('name') or ''}".strip()
        skip = {"sourced by", "source of", "decomposes", "decomposed by"}
        items = []
        for depth, e in sorted(members, key=lambda de: num_key(de[1].get("number"))):
            d = describe(sch, e, full_text, names)
            d["depth"] = depth
            d["relations"] = {k: v for k, v in d["relations"].items() if k not in skip}
            for k in ("created", "modified"):
                d.pop(k, None)
            items.append(d)
        return {"document": describe(sch, doc, True, names), "items": len(items), "tree": items}
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=RO)
async def innoslate_comments(unresolved_only: bool = False, project_url: str | None = None) -> dict:
    """Review comments on the project (often the marker's feedback), with the entity each is on."""
    try:
        api = STATE.api(project_url)
        rows = await api.comments()
        ids = {c.get("entityId") for c in rows if c.get("entityId")}
        tmap = await api.get_many(ids)
        out = []
        for c in rows:
            if unresolved_only and c.get("resolved"):
                continue
            t = tmap.get(c.get("entityId")) or {}
            out.append({"id": c.get("id"), "on": f"{t.get('number') or ''} {t.get('name') or ''}".strip() or c.get("entityId"),
                        "entity_id": c.get("entityId"), "resolved": bool(c.get("resolved")), "by": c.get("createdBy"),
                        "text": strip_html(c.get("description") or c.get("text") or c.get("body"))})
        return {"total": len(out), "comments": out}
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=LOCAL_RO)
async def list_local_files(folder: str, recursive: bool = True) -> dict:
    """List the files in a local folder the user pointed at, with size and whether they can be read."""
    try:
        files = docs.list_files(folder, recursive)
        return {"folder": str(Path(folder).expanduser()), "files": files}
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=LOCAL_RO)
async def read_local_file(path: str, offset: int = 0, max_chars: int = 30000) -> dict:
    """Plain text of a local document: PDF, DOCX (tables kept), XLSX, PPTX, CSV, JSON, Markdown,
    TXT, VTT/SRT transcripts. Long files come in pages: pass next_offset back as offset.
    Audio and video need a transcript first."""
    try:
        return docs.read_page(path, offset, max_chars)
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=RO)
async def innoslate_lint_requirements(items: list[dict] | None = None, document: str | None = None,
                                      extra_banned: dict[str, str] | None = None,
                                      project_url: str | None = None) -> dict:
    """Check requirement wording before writing it (or check a document already in Innoslate).

    items: [{"number", "name", "text", "rationale"?}]   or   document: id, number or name.
    extra_banned: {"regex": "why"} for course-specific rules, e.g. {"\\\\bfacial\\\\b": "no worker identification"}.
    Errors should be fixed before innoslate_apply; warnings need a human look."""
    try:
        if items is None and document is None:
            raise ValueError("Give items or document.")
        if document is not None:
            api = STATE.api(project_url)
            sch = await api.schema()
            req = sch.class_id("Requirement")
            doc = await _document(api, document)
            rat = None
            try:
                rat = str(sch.property(req, "Rationale")["id"])
            except LookupError:
                pass
            items = [{"number": e.get("number"), "name": e.get("name"), "text": strip_html(e.get("description")),
                      "rationale": strip_html(str((e.get("attrs") or {}).get(rat) or "")) if rat else ""}
                     for _, e in await api.document_members(doc) if e.get("classId") == req]
        return lint.lint_items(items or [], extra_banned)
    except Exception as e:
        _fail(e)


async def _run_plan(api: InnoslateAPI, plan: dict, dry_run: bool, confirm: str | None, event: str) -> dict:
    token = make_token(api.p.key, plan)
    planner = Planner(api, api.p.pid, wsp.number_index(api.p))
    compiled = await planner.compile(plan)
    report = planner.report(compiled)
    if dry_run:
        report["dry_run"] = True
        report["confirm_token"] = token
        report["next"] = ("Nothing was written. Show the user this summary. To write it, call again with the SAME plan, "
                          "dry_run=false and confirm_token.")
        return report
    if confirm != token:
        raise ValueError("confirm_token is missing or does not match this plan. Run with dry_run=true first, show the "
                         "user the result, then send the identical plan with the token it returned.")
    result = await planner.commit(compiled)
    wsp.audit(api.p, event, token=token, summary=report["summary"], id_map=result["id_map"],
              items=[{"key": r["key"], "action": r["action"], "number": r["number"], "name": r["name"]} for r in report["items"]],
              verify_ok=result["verify"]["ok"], problems=result["verify"]["problems"])
    created = {}
    for it in compiled.items:
        if it.action == "create" and it.entity:
            created[it.key] = it.entity["id"]
    report.update(dry_run=False, saved_entities=result["saved"], created_ids=created, verify=result["verify"])
    return report


@mcp.tool(annotations=WRITE)
async def innoslate_apply(plan: dict, dry_run: bool = True, confirm_token: str | None = None,
                          project_url: str | None = None) -> dict:
    """Create or update entities and relations in one save, then read everything back to verify.

    Always call with dry_run=true first. It returns a summary and a confirm_token. Show the user,
    and only after their yes call again with the identical plan, dry_run=false and the token.

    plan = {
      "update_existing": false,          # true lets matched existing items be changed
      "entities": [{
        "key": "sr",                     # your name for it inside this plan
        "class": "Artifact", "number": "SR.1", "name": "Stakeholder Requirements",
        "labels": ["Requirements Document"], "description": "plain text"   # or description_html
      }, {
        "key": "sr1", "class": "Requirement", "number": "SR.1.1", "name": "Live view",
        "description": "The system shall ...", "labels": ["Functional Requirement"],
        "attributes": {"Rationale": "..."},             # property names from innoslate_schema
        "document": "sr",                               # top-level item of that document
        "parent": null,                                 # or a ref: nested under that item (decomposes)
        "relations": {"traced from": [{"number": "UN.1.3", "document": "UN.1"}]}
      }],
      "relations": [{"from": "sr1", "relation": "verified by", "to": 12345}],
      "unlink":    [{"from": 111, "relation": "traced to", "to": 222}]
    }
    A ref is a plan key, an id, {"number": ..., "document": <ref>} or {"number"/"name", "class"}.
    Existing items are matched by number inside their document only. Every relation is written
    on both sides. Nothing is deleted. id targets one existing entity directly."""
    try:
        api = STATE.api(project_url)
        return await _run_plan(api, plan, dry_run, confirm_token, "apply")
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=WRITE)
async def innoslate_update(id: int, fields: dict, dry_run: bool = True, confirm_token: str | None = None,
                           project_url: str | None = None) -> dict:
    """Change one existing entity by id. fields may hold name, number, description,
    description_html, labels (replaces), add_labels, attributes, relations, parent, document.
    Same dry-run and confirm_token flow as innoslate_apply."""
    try:
        api = STATE.api(project_url)
        bad = set(fields) - {"name", "number", "description", "description_html", "labels", "add_labels",
                             "attributes", "relations", "parent", "document"}
        if bad:
            raise ValueError(f"Cannot set {sorted(bad)} here.")
        plan = {"entities": [{"key": f"id{id}", "id": int(id), **fields}]}
        return await _run_plan(api, plan, dry_run, confirm_token, "update")
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=WRITE)
async def innoslate_attach_file(id: int, path: str, property_name: str = "File", dry_run: bool = True,
                                confirm_token: str | None = None, project_url: str | None = None) -> dict:
    """Upload a local file and put it in an entity's file property (on Artifacts this is "File").
    Replaces a file already there. Same dry-run and confirm_token flow as innoslate_apply."""
    try:
        api = STATE.api(project_url)
        sch = await api.schema()
        f = Path(path).expanduser()
        if not f.is_file():
            raise FileNotFoundError(f"No such file: {f}")
        e = await api.get(int(id))
        if not e:
            raise LookupError(f"No entity with id {id}.")
        prop = sch.property(e["classId"], property_name)
        if prop.get("type") != "FILE":
            raise ValueError(f"{prop.get('name')!r} is a {prop.get('type')} property, not a file property.")
        st = f.stat()
        token = make_token(api.p.key, {"attach": int(id), "path": str(f.resolve()), "size": st.st_size,
                                       "mtime": int(st.st_mtime), "property": prop["id"]})
        current = (e.get("attrs") or {}).get(str(prop["id"]))
        info = {"entity": f"{e.get('number') or ''} {e.get('name') or ''}".strip(), "id": e["id"],
                "file": f.name, "size_mb": round(st.st_size / 1e6, 2), "property": prop.get("name"),
                "replaces_existing_file": bool(current)}
        if dry_run:
            return {**info, "dry_run": True, "confirm_token": token,
                    "next": "Nothing was uploaded. Call again with dry_run=false and confirm_token."}
        if confirm_token != token:
            raise ValueError("confirm_token is missing or does not match. Run with dry_run=true first.")
        key = await api.upload_file(f)
        fresh = await api.get(int(id))
        fresh.setdefault("attrs", {})[str(prop["id"])] = key
        await api.save([fresh])
        back = await api.get(int(id))
        ok = (back.get("attrs") or {}).get(str(prop["id"])) == key
        meta = await api.file_meta(key)
        wsp.audit(api.p, "attach_file", id=int(id), file=f.name, key=key, verify_ok=ok)
        return {**info, "dry_run": False, "file_key": key, "verify": {"ok": ok, "file_meta": meta}}
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=WRITE)
async def innoslate_quality_check(document: str, timeout_seconds: int = 120, project_url: str | None = None) -> dict:
    """Run Innoslate's own Quality Check on a requirements document (UI: More > Quality Check)
    and return each requirement's Quality Score and the checks it failed, lowest first.
    Innoslate stores the scores on the requirements; nothing else changes."""
    try:
        api = STATE.api(project_url)
        doc = await _document(api, document)
        res = await ui.quality_check(STATE.session, api, doc["id"], timeout_seconds)
        wsp.audit(api.p, "quality_check", document=doc["id"], scored=res.get("scored"), average=res.get("average_score"))
        return res
    except Exception as e:
        _fail(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def innoslate_report(document: str, report_name: str = "Basic Document Output (DOCX)", out_dir: str | None = None,
                           project_url: str | None = None) -> dict:
    """Download one of Innoslate's document reports (UI: Reports > <name> > Create), e.g.
    "Basic Document Output (DOCX)" or "RTM Output (XLSX)". If the name is wrong, the reply lists
    the reports this document offers. Saved under the workspace's reports/ folder unless out_dir is given."""
    try:
        api = STATE.api(project_url)
        doc = await _document(api, document)
        out = Path(out_dir).expanduser() if out_dir else wsp.workspace(api.p) / "reports"
        return await ui.download_report(STATE.session, api, doc["id"], report_name, out)
    except Exception as e:
        _fail(e)


# ===================================================================== prompts
@mcp.prompt()
def setup_innoslate_project(project_url: str, guides_dir: str = "", my_documents_dir: str = "") -> str:
    """Log in, survey the project, read the guides and the user's documents, then propose a plan."""
    return (
        f"Use the innoslate tools. Project: {project_url}\n"
        f"1. innoslate_login on it and wait for me to log in.\n"
        f"2. innoslate_initialize with guides_dir={guides_dir or '(ask me where the guides are)'}.\n"
        f"3. Read PROJECT_MAP and NOMENCLATURE, and the guides.\n"
        f"4. Read my documents in {my_documents_dir or '(ask me)'}.\n"
        "5. Tell me, in plain words, what goes into which Innoslate document with which numbers and links. "
        "Point out anything the guide wants that my documents do not have. Do not write anything until I say yes."
    )


# ===================================================================== entry
def _doctor() -> int:
    import asyncio
    print(f"innoslate-mcp {__version__}")
    print(f"python {sys.version.split()[0]} on {sys.platform}")
    print(f"home: {config.home()}")
    print(f"credentials saved: {'yes' if config.credentials() else 'no'}")
    w = config.env_file_warning()
    if w:
        print("WARNING:", w)
    for mod in ("mcp", "playwright", "pypdf", "docx", "openpyxl"):
        try:
            __import__(mod)
            print(f"  ok   {mod}")
        except ImportError:
            print(f"  MISSING {mod}  -> pip install innoslate-mcp")

    async def launch() -> str:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            b = await pw.chromium.launch(headless=True)
            v = b.version
            await b.close()
            return v
    try:
        print(f"  ok   chromium {asyncio.run(launch())}")
    except Exception as e:
        print(f"  FAIL chromium: {str(e).splitlines()[0][:200]}")
        print("       run: python -m playwright install chromium")
        return 1
    return 0


def _setup() -> int:
    """Interactive, in a terminal: install Chromium, optionally save login details.
    The password is typed with getpass (not shown, not in shell history) and the
    file is made readable by the owner only."""
    import getpass
    from .browser import install_chromium
    print(f"innoslate-mcp {__version__} setup\n")
    print("1/2 Installing Chromium for Playwright (once, about 150 MB)...")
    try:
        install_chromium()
    except Exception as e:
        print(f"    failed: {e}")
        return 1
    print("    done.\n")
    env = config.home() / ".env"
    print("2/2 Optional: save your Innoslate login so the server can type it for you.")
    print(f"    It is stored in {env}, readable only by you. Skip it if you log in with Google/Microsoft SSO.")
    if input("    Save a username and password? [y/N] ").strip().lower() not in ("y", "yes"):
        print("\nSkipped. You will log in in the browser window the server opens.")
        return 0
    user = input("    Innoslate username or email: ").strip()
    pw = getpass.getpass("    Innoslate password (hidden): ")
    if not user or not pw:
        print("    Nothing saved.")
        return 1
    keep = [l for l in (env.read_text().splitlines() if env.exists() else [])
            if not l.strip().startswith(("INNOSLATE_USERNAME", "INNOSLATE_PASSWORD"))]
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(keep + [f"INNOSLATE_USERNAME={user}", f"INNOSLATE_PASSWORD={pw}"]) + "\n")
    if os.name == "posix":
        os.chmod(env, 0o600)
    print(f"\nSaved to {env}. Never paste your password into an AI chat; the server reads it from here.")
    return 0


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("--version", "-V"):
        print(__version__)
        return
    if args and args[0] in ("--help", "-h", "help"):
        print("innoslate-mcp            run the MCP server on stdio (what AI clients start)\n"
              "innoslate-mcp setup      install Chromium, optionally save your login\n"
              "innoslate-mcp doctor     check that everything is installed\n"
              "innoslate-mcp --version")
        return
    if args and args[0] == "doctor":
        sys.exit(_doctor())
    if args and args[0] == "setup":
        sys.exit(_setup())
    logging.basicConfig(level=os.environ.get("INNOSLATE_LOG_LEVEL", "WARNING"), stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp.run()


if __name__ == "__main__":
    main()

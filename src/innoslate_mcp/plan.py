"""Compile a plan into one Innoslate save, then verify it by reading everything back.

A plan is plain JSON the AI writes:

{
  "update_existing": false,
  "entities": [
    {"key": "sr", "class": "Artifact", "number": "SR.1", "name": "Stakeholder Requirements",
     "labels": ["Requirements Document"], "description": "..."},
    {"key": "sr1", "class": "Requirement", "number": "SR.1.1", "name": "Show live feed",
     "description": "The system shall ...", "labels": ["Functional Requirement"],
     "attributes": {"Rationale": "..."}, "document": "sr",
     "relations": {"traced from": [{"number": "UN.1.3", "document": {"number": "UN.1"}}]}}
  ],
  "relations": [{"from": "sr1", "relation": "satisfied by", "to": 12345}],
  "unlink":    [{"from": 111, "relation": "traced to", "to": 222}]
}

References (anywhere a ref is accepted):
  "sr1"                               a key defined in this plan
  12345 or {"id": 12345}              an existing entity id
  {"number": "UN.1.3", "document": R} the entity with that number inside document R
  {"number": "UN.1", "class": "Artifact", "name": "..."}  unique match in the project
  "UN.1.3"                            shorthand for {"number": "UN.1.3"}, must be unique

Rules the engine enforces, learned on a live project:
  * every relation is written on both sides, in the same save, or Innoslate rejects it
  * existing items are matched by number only inside their document, never project-wide
  * an existing entity changes only with update_existing (or an explicit id)
  * nothing is ever deleted; unlink removes a relation from both sides
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .api import describe, strip_html, to_html
from .schema import READ_ONLY_TYPES, Schema


class PlanError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Plan has problems:\n- " + "\n- ".join(problems))


def confirm_token(project_key: str, plan: Any) -> str:
    canon = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{project_key}\n{canon}".encode()).hexdigest()[:16]


def blank_entity(eid: int, global_id: str, class_id: int, project_id: int) -> dict:
    """The entity template that Innoslate accepts for a create."""
    return {
        "id": eid, "globalId": global_id, "classId": class_id, "projectId": project_id,
        "number": "", "name": "", "description": "", "labelIds": [], "rels": [], "attrs": {},
        "controlType": "SERIAL", "branches": [], "diagrams": {}, "isArchived": False,
        "isLocked": False, "isRedacted": False, "createdIn": 0, "modifiedIn": 0, "linkedLabelId": 0,
    }


def has_rel(e: dict, rel: int, target: int) -> bool:
    return any(r.get("relationId") == rel and r.get("targetId") == target for r in e.get("rels", []))


def add_rel(e: dict, rel: int, target: int) -> bool:
    if has_rel(e, rel, target):
        return False
    e.setdefault("rels", []).append({"relationId": rel, "targetId": target, "attrs": {}})
    return True


def drop_rel(e: dict, rel: int, target: int) -> bool:
    before = len(e.get("rels", []))
    e["rels"] = [r for r in e.get("rels", []) if not (r.get("relationId") == rel and r.get("targetId") == target)]
    return len(e["rels"]) != before


@dataclass
class Item:
    spec: dict
    key: str
    class_id: int | None = None
    entity: dict | None = None          # working copy
    original: dict | None = None        # as read from Innoslate, None for a create
    action: str = "create"              # create | update | link-only | unchanged
    notes: list[str] = field(default_factory=list)


@dataclass
class Compiled:
    items: list[Item]
    working: dict[int, dict]            # id -> working copy, every entity the save may touch
    originals: dict[int, dict]          # id -> as read, existing entities only
    rel_adds: list[tuple[int, int, int]]
    rel_drops: list[tuple[int, int, int]]
    warnings: list[str]
    new_ids: list[int]

    def changed(self) -> list[dict]:
        out = []
        for eid, e in self.working.items():
            o = self.originals.get(eid)
            if o is None or _canon(o) != _canon(e):
                out.append(e)
        return out


def _canon(e: dict) -> str:
    e = dict(e)
    e["rels"] = sorted(((r.get("relationId"), r.get("targetId")) for r in e.get("rels", [])))
    e["labelIds"] = sorted(e.get("labelIds") or [])
    return json.dumps(e, sort_keys=True, default=str)


class Planner:
    """Needs an object with the InnoslateAPI read methods (schema, get, get_many, search_all,
    document_members, find_documents, allocate_ids, save)."""

    def __init__(self, api: Any, project_id: int, number_index: dict[str, list[int]] | None = None):
        self.api = api
        self.pid = project_id
        self.index = number_index or {}
        self.sch: Schema | None = None
        self._members: dict[int, list[dict]] = {}
        self._next_tmp = 0

    # ----------------------------------------------------------------- compile
    async def compile(self, plan: dict) -> Compiled:
        if not isinstance(plan, dict):
            raise PlanError(["The plan must be a JSON object with an 'entities' list."])
        self.sch = sch = await self.api.schema()
        problems: list[str] = []
        warnings: list[str] = []
        unknown_top = set(plan) - {"entities", "relations", "unlink", "update_existing", "note"}
        if unknown_top:
            raise PlanError([f"Unknown top-level keys {sorted(unknown_top)}. Allowed: entities, relations, unlink, update_existing."])
        specs = plan.get("entities")
        specs = [] if specs is None else specs
        if not isinstance(specs, list):
            raise PlanError(["'entities' must be a list."])
        for k in ("relations", "unlink"):
            if plan.get(k) is not None and not isinstance(plan[k], list):
                raise PlanError([f"'{k}' must be a list."])
        if not specs and not plan.get("relations") and not plan.get("unlink"):
            raise PlanError(["The plan is empty: give entities, relations or unlink."])
        upd_all = bool(plan.get("update_existing"))

        items: list[Item] = []
        keys: dict[str, Item] = {}
        allowed = {"key", "id", "class", "number", "name", "description", "description_html", "labels",
                   "add_labels", "attributes", "document", "parent", "relations", "update_existing"}
        for n, spec in enumerate(specs):
            where = f"entities[{n}]"
            if not isinstance(spec, dict):
                problems.append(f"{where} is not an object.")
                continue
            extra = set(spec) - allowed
            if extra:
                problems.append(f"{where}: unknown fields {sorted(extra)}. Allowed: {sorted(allowed)}.")
            key = str(spec.get("key") or spec.get("number") or f"#{n}")
            if key in keys:
                problems.append(f"{where}: key {key!r} is used twice. Give each entity a unique 'key'.")
                continue
            it = Item(spec=spec, key=key)
            if spec.get("class") is not None:
                try:
                    it.class_id = sch.class_id(spec["class"])
                except LookupError as e:
                    problems.append(f"{where} ({key}): {e}")
            elif spec.get("id") is None:
                problems.append(f"{where} ({key}): 'class' is required for a new entity.")
            if "description" in spec and "description_html" in spec:
                problems.append(f"{where} ({key}): give 'description' or 'description_html', not both.")
            keys[key] = it
            items.append(it)
        if problems:
            raise PlanError(problems)

        # ------------------------------------------------ find or create each item
        working: dict[int, dict] = {}
        originals: dict[int, dict] = {}

        async def load(eid: int) -> dict | None:
            if eid in working:
                return working[eid]
            e = await self.api.get(eid)
            if e:
                if e.get("projectId") not in (None, self.pid):
                    raise PlanError([f"Entity {eid} belongs to project {e.get('projectId')}, not {self.pid}."])
                originals[eid] = copy.deepcopy(e)
                working[eid] = e
            return e

        resolving: set[str] = set()

        async def resolve(ref: Any, where: str, default_class: str | None = None) -> int | None:
            """Returns an entity id (negative for a plan item still to be created)."""
            if isinstance(ref, str) and ref in keys:
                it = keys[ref]
                if it.entity is None:
                    if ref in resolving:
                        problems.append(f"{where}: circular reference through {ref!r}.")
                        return None
                    resolving.add(ref)
                    await place(it)
                    resolving.discard(ref)
                return it.entity["id"] if it.entity else None
            if isinstance(ref, bool):
                problems.append(f"{where}: {ref!r} is not a reference.")
                return None
            if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
                ref = {"id": int(ref)}
            if isinstance(ref, str):
                ref = {"number": ref}
            if not isinstance(ref, dict):
                problems.append(f"{where}: cannot read reference {ref!r}.")
                return None
            if ref.get("id") is not None:
                e = await load(int(ref["id"]))
                if not e:
                    problems.append(f"{where}: no entity with id {ref['id']} in this project.")
                    return None
                return e["id"]
            number, name = ref.get("number"), ref.get("name")
            cls = ref.get("class") or default_class
            cid = None
            if cls:
                try:
                    cid = sch.class_id(cls)
                except LookupError as e:
                    problems.append(f"{where}: {e}")
                    return None
            if not number and not name:
                problems.append(f"{where}: a reference needs a plan key, an id, a number or a name.")
                return None
            if ref.get("document") is not None:
                doc_id = await resolve(ref["document"], where + ".document", "Artifact")
                if doc_id is None:
                    return None
                if doc_id < 0:   # a document this plan creates: look among the plan's own items
                    cands = [i2 for i2 in items
                             if isinstance(i2.spec.get("document"), str) and i2.spec["document"] in keys
                             and keys[i2.spec["document"]].entity is not None
                             and keys[i2.spec["document"]].entity["id"] == doc_id
                             and (number is None or str(i2.spec.get("number")) == number)
                             and (name is None or i2.spec.get("name") == name)]
                    if len(cands) != 1:
                        problems.append(f"{where}: {number or name!r} matches {len(cands)} items of the new document. Refer to it by plan key.")
                        return None
                    await place(cands[0])
                    return cands[0].entity["id"] if cands[0].entity else None
                members = await self.members(doc_id)
                hits = [m for m in members if (number is None or (m.get("number") or "") == number)
                        and (name is None or (m.get("name") or "").strip() == name.strip())
                        and (cid is None or m.get("classId") == cid)]
                if len(hits) != 1:
                    problems.append(self._ambiguous(where, ref, hits, f"inside document id {doc_id}"))
                    return None
                return (await load(hits[0]["id"]))["id"]
            hits = await self.lookup(number, name, cid)
            if len(hits) != 1:
                problems.append(self._ambiguous(where, ref, hits, "in the project"))
                return None
            return (await load(hits[0]["id"]))["id"]

        async def place(it: Item) -> None:
            """Decide create or update for one plan item and give it a working entity."""
            if it.entity is not None:
                return
            spec, where = it.spec, f"entity {it.key!r}"
            upd = bool(spec.get("update_existing", upd_all))
            if spec.get("id") is not None:
                e = await load(int(spec["id"]))
                if not e:
                    problems.append(f"{where}: no entity with id {spec['id']}.")
                    return
                if it.class_id is not None and e.get("classId") != it.class_id:
                    problems.append(f"{where}: id {spec['id']} is a {sch.class_name(e.get('classId'))}, not {spec.get('class')}.")
                    return
                it.class_id = e.get("classId")
                it.entity, it.original, it.action = e, originals.get(e["id"]), "update"
                return
            match = None
            scope = spec.get("document")
            parent = spec.get("parent")
            number = spec.get("number")
            if number and (scope is not None or parent is not None):
                scope_id = await resolve(scope if scope is not None else parent, where + (".document" if scope is not None else ".parent"),
                                         "Artifact" if scope is not None else None)
                if scope_id is not None and scope_id > 0:
                    cands = [m for m in await self.members(scope_id)
                             if (m.get("number") or "") == number and m.get("classId") == it.class_id]
                    if len(cands) > 1:
                        problems.append(f"{where}: the document already holds {len(cands)} entities numbered {number!r} "
                                        f"(ids {[c['id'] for c in cands]}). Target one with 'id'.")
                        return
                    match = cands[0] if cands else None
            elif number and sch.class_name(it.class_id) == "Artifact" and spec.get("name"):
                docs = await self.api.find_documents(number=number, name=spec["name"])
                if len(docs) > 1:
                    problems.append(f"{where}: {len(docs)} documents are named {spec['name']!r} with number {number!r} "
                                    f"(ids {[d['id'] for d in docs]}). Target one with 'id'.")
                    return
                match = docs[0] if docs else None
                if not match:
                    same_num = await self.api.find_documents(number=number)
                    if same_num:
                        warnings.append(f"{where}: will create a NEW document, but {len(same_num)} Artifact(s) already use number "
                                        f"{number!r}: " + "; ".join(f"id {d['id']} {d.get('name')!r}" for d in same_num[:5])
                                        + ". If you meant one of them, use its exact name or 'id'.")
            elif number and number in self.index:
                warnings.append(f"{where}: number {number!r} already exists in the project (ids {self.index[number][:5]}). "
                                "A new entity will be created anyway. Put it in a 'document' to match by number there.")
            if match:
                e = await load(match["id"])
                it.entity, it.original = e, originals.get(e["id"])
                it.action = "update" if upd else "unchanged"
                if not upd:
                    it.notes.append("exists, fields left as they are (set update_existing to change them)")
                return
            self._next_tmp -= 1
            e = blank_entity(self._next_tmp, "", it.class_id, self.pid)
            working[e["id"]] = e
            it.entity, it.action = e, "create"

        for it in items:
            await place(it)
        if problems:
            raise PlanError(problems)

        # -------------------------------------------------------- fields
        for it in items:
            e, spec = it.entity, it.spec
            if e is None or it.action == "unchanged":
                continue
            where = f"entity {it.key!r}"
            if "number" in spec and spec["number"] is not None:
                e["number"] = str(spec["number"])
            if spec.get("name") is not None:
                e["name"] = str(spec["name"])
            elif it.action == "create":
                problems.append(f"{where}: 'name' is required for a new entity.")
            if spec.get("description") is not None:
                e["description"] = to_html(str(spec["description"]))
            if spec.get("description_html") is not None:
                e["description"] = str(spec["description_html"])
            for fld, mode in (("labels", "replace"), ("add_labels", "add")):
                if spec.get(fld) is None:
                    continue
                ids = []
                for l in spec[fld]:
                    try:
                        lid = sch.label_id(l, it.class_id)
                    except LookupError as ex:
                        problems.append(f"{where}: {ex}")
                        continue
                    if not sch.label_fits(lid, it.class_id):
                        warnings.append(f"{where}: label {l!r} is not defined for class {sch.class_name(it.class_id)}.")
                    ids.append(lid)
                e["labelIds"] = ids if mode == "replace" else list(dict.fromkeys((e.get("labelIds") or []) + ids))
            for pname, val in (spec.get("attributes") or {}).items():
                try:
                    p = sch.property(it.class_id, pname)
                except LookupError as ex:
                    problems.append(f"{where}: {ex}")
                    continue
                if p.get("type") in READ_ONLY_TYPES:
                    problems.append(f"{where}: {p.get('name')!r} is computed by Innoslate ({p.get('type')}) and cannot be written.")
                    continue
                if p.get("type") == "FILE":
                    problems.append(f"{where}: {p.get('name')!r} holds a file. Use innoslate_attach_file.")
                    continue
                e.setdefault("attrs", {})[str(p["id"])] = val

        # ------------------------------------------------------ relations
        rel_adds: list[tuple[int, int, int]] = []
        rel_drops: list[tuple[int, int, int]] = []

        async def link(src: int, rel_name: Any, dst: int, where: str, unlink: bool = False) -> None:
            try:
                rid = sch.relation_id(rel_name)
                inv = sch.inverse_id(rid)
            except LookupError as ex:
                problems.append(f"{where}: {ex}")
                return
            if src == dst:
                problems.append(f"{where}: an entity cannot relate to itself.")
                return
            a = working.get(src) or await load(src)
            b = working.get(dst) or await load(dst)
            if a is None or b is None:
                problems.append(f"{where}: could not load both ends.")
                return
            if unlink:
                d1, d2 = drop_rel(a, rid, dst), drop_rel(b, inv, src)
                if d1 or d2:
                    rel_drops.append((src, rid, dst))
                else:
                    warnings.append(f"{where}: relation {sch.relation_name(rid)!r} {src} -> {dst} did not exist.")
            else:
                n1, n2 = add_rel(a, rid, dst), add_rel(b, inv, src)
                if n1 or n2:
                    rel_adds.append((src, rid, dst))

        for it in items:
            if it.entity is None:
                continue
            src = it.entity["id"]
            where = f"entity {it.key!r}"
            if it.spec.get("parent") is not None:
                pid_ = await resolve(it.spec["parent"], where + ".parent")
                if pid_ is not None:
                    await link(src, "decomposes", pid_, where + ".parent")
            elif it.spec.get("document") is not None:
                did = await resolve(it.spec["document"], where + ".document", "Artifact")
                if did is not None:
                    await link(src, "sourced by", did, where + ".document")
            rels = it.spec.get("relations") or {}
            if not isinstance(rels, dict):
                problems.append(f"{where}.relations must map a relation name to a list of references.")
                continue
            for rname, targets in rels.items():
                for k, t in enumerate(targets if isinstance(targets, list) else [targets]):
                    tid = await resolve(t, f"{where}.relations[{rname!r}][{k}]")
                    if tid is not None:
                        await link(src, rname, tid, f"{where}.relations[{rname!r}][{k}]")

        for kind, rows in (("relations", plan.get("relations") or []), ("unlink", plan.get("unlink") or [])):
            for k, row in enumerate(rows):
                where = f"{kind}[{k}]"
                if not isinstance(row, dict) or not {"from", "relation", "to"} <= set(row):
                    problems.append(f"{where}: needs 'from', 'relation' and 'to'.")
                    continue
                a = await resolve(row["from"], where + ".from")
                b = await resolve(row["to"], where + ".to")
                if a is not None and b is not None:
                    await link(a, row["relation"], b, where, unlink=(kind == "unlink"))

        if problems:
            raise PlanError(problems)

        for it in items:
            if it.action == "unchanged" and it.entity is not None:
                o = originals.get(it.entity["id"])
                if o is not None and _canon(o) != _canon(it.entity):
                    it.action = "link-only"
        new_ids = [eid for eid in working if eid < 0]
        return Compiled(items, working, originals, rel_adds, rel_drops, warnings, new_ids)

    # ----------------------------------------------------------------- helpers
    async def members(self, doc_id: int) -> list[dict]:
        if doc_id not in self._members:
            doc = await self.api.get(doc_id)
            if not doc:
                self._members[doc_id] = []
            else:
                self._members[doc_id] = [e for _, e in await self.api.document_members(doc)]
        return self._members[doc_id]

    async def lookup(self, number: str | None, name: str | None, cid: int | None) -> list[dict]:
        def keep(h: dict) -> bool:
            return ((number is None or (h.get("number") or "") == number)
                    and (name is None or (h.get("name") or "").strip() == name.strip())
                    and (cid is None or h.get("classId") == cid)
                    and h.get("projectId") in (None, self.pid))
        q = f'"{name}"' if name else f'"{number}"'
        hits = [h for h in await self.api.search_all(q, cap=2000) if keep(h)]
        if not hits and number and number in self.index:
            got = await self.api.get_many(self.index[number])
            hits = [h for h in got.values() if keep(h)]
        if not hits and cid is not None:
            name_ = self.sch.class_name(cid)
            q = f'class:"{name_}"' if not name_.isalnum() else f"class:{name_}"
            hits = [h for h in await self.api.search_all(q) if keep(h)]
        return hits

    def _ambiguous(self, where: str, ref: dict, hits: list[dict], scope: str) -> str:
        if not hits:
            return f"{where}: nothing matches {json.dumps(ref)} {scope}."
        rows = "; ".join(f"id {h['id']} {h.get('number') or '-'} {h.get('name')!r} ({self.sch.class_name(h.get('classId'))})"
                         for h in hits[:8])
        return f"{where}: {len(hits)} entities match {json.dumps(ref)} {scope}: {rows}. Add 'document', 'class' or use 'id'."

    # ------------------------------------------------------------------ report
    def report(self, c: Compiled) -> dict:
        sch = self.sch
        label = {}
        for eid, e in c.working.items():
            label[eid] = f"{e.get('number') or ''} {e.get('name') or ''}".strip() or f"id {eid}"
        rows = []
        for it in c.items:
            e = it.entity or {}
            rows.append({
                "key": it.key, "action": it.action,
                "id": e.get("id") if (e.get("id") or 0) > 0 else "(new)",
                "class": sch.class_name(it.class_id) if it.class_id else "",
                "number": e.get("number", ""), "name": e.get("name", ""),
                "labels": [sch.label_name(l) for l in e.get("labelIds") or []],
                **({"notes": it.notes} if it.notes else {}),
            })
        planned = {id(it.entity) for it in c.items}
        side = [e for e in c.changed() if id(e) not in planned]
        return {
            "summary": {
                "create": sum(1 for it in c.items if it.action == "create"),
                "update": sum(1 for it in c.items if it.action == "update"),
                "link_only": sum(1 for it in c.items if it.action == "link-only"),
                "unchanged": sum(1 for it in c.items if it.action == "unchanged"),
                "relations_added": len(c.rel_adds),
                "relations_removed": len(c.rel_drops),
                "other_entities_touched": len(side),
                "entities_in_save": len(c.changed()),
            },
            "items": rows,
            "relations_added": [f"{label[a]} --{sch.relation_name(r)}--> {label[b]}" for a, r, b in c.rel_adds],
            "relations_removed": [f"{label[a]} --{sch.relation_name(r)}--> {label[b]}" for a, r, b in c.rel_drops],
            "existing_entities_touched_for_relations": [label[e["id"]] for e in side],
            "warnings": c.warnings,
        }

    # ------------------------------------------------------------------ commit
    async def commit(self, c: Compiled) -> dict:
        changed = c.changed()
        if not changed:
            return {"saved": 0, "id_map": {}, "verify": {"ok": True, "problems": []}}
        new = sorted(c.new_ids, reverse=True)       # -1, -2, ... in plan order
        alloc = await self.api.allocate_ids(len(new))
        id_map = {tmp: a["id"] for tmp, a in zip(new, alloc)}
        gid = {tmp: a.get("globalId", "") for tmp, a in zip(new, alloc)}
        for e in c.working.values():
            if e["id"] in id_map:
                tmp = e["id"]
                e["id"], e["globalId"] = id_map[tmp], gid[tmp]
            for r in e.get("rels", []):
                if r.get("targetId") in id_map:
                    r["targetId"] = id_map[r["targetId"]]
        payload = [e for e in changed]
        await self.api.save(payload)
        fix = lambda i: id_map.get(i, i)
        verify = await self.verify(c, fix)
        return {"saved": len(payload), "id_map": {str(k): v for k, v in id_map.items()}, "verify": verify}

    async def verify(self, c: Compiled, fix) -> dict:
        sch = self.sch
        ids = [e["id"] for e in c.working.values()]
        back = await self.api.get_many(ids)
        problems = []
        for it in c.items:
            if it.entity is None:
                continue
            eid = it.entity["id"]
            got = back.get(eid)
            name = f"{it.entity.get('number') or ''} {it.entity.get('name') or ''}".strip()
            if not got:
                problems.append(f"{name}: not found after save (id {eid}).")
                continue
            if it.action in ("create", "update"):
                for k in ("number", "name", "classId"):
                    if (got.get(k) or "") != (it.entity.get(k) or ""):
                        problems.append(f"{name}: {k} is {got.get(k)!r}, expected {it.entity.get(k)!r}.")
                if strip_html(got.get("description")) != strip_html(it.entity.get("description")):
                    problems.append(f"{name}: description differs after save.")
                if set(got.get("labelIds") or []) != set(it.entity.get("labelIds") or []):
                    problems.append(f"{name}: labels are {[sch.label_name(l) for l in got.get('labelIds') or []]}.")
                for k, v in (it.spec.get("attributes") or {}).items():
                    try:
                        pid = str(sch.property(it.class_id, k)["id"])
                    except LookupError:
                        continue
                    gv = (got.get("attrs") or {}).get(pid)
                    if str(gv if gv is not None else "") != str(v if v is not None else ""):
                        problems.append(f"{name}: attribute {k!r} is {gv!r}, expected {v!r}.")
        for a, r, b in c.rel_adds:
            ea, eb = back.get(fix(a)), back.get(fix(b))
            inv = sch.inverse_id(r)
            if not (ea and has_rel(ea, r, fix(b))) or not (eb and has_rel(eb, inv, fix(a))):
                problems.append(f"relation {sch.relation_name(r)!r} {fix(a)} -> {fix(b)} is missing on one side after save.")
        for a, r, b in c.rel_drops:
            ea, eb = back.get(fix(a)), back.get(fix(b))
            inv = sch.inverse_id(r)
            if (ea and has_rel(ea, r, fix(b))) or (eb and has_rel(eb, inv, fix(a))):
                problems.append(f"relation {sch.relation_name(r)!r} {fix(a)} -> {fix(b)} is still there after unlink.")
        return {"ok": not problems, "checked_entities": len(back), "problems": problems}

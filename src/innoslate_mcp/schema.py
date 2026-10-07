"""Resolve class, relation, label and property names to the ids one project uses.

Ids differ between projects and organisations, so nothing here is hard-coded. Every
lookup is case-insensitive and ignores surrounding spaces. A failed lookup raises a
LookupError that lists the closest names, so the AI can correct itself.
"""
from __future__ import annotations

import difflib
from typing import Any


def _as_list(x: Any) -> list[dict]:
    if isinstance(x, list):
        return [i for i in x if isinstance(i, dict)]
    if isinstance(x, dict):
        return [i for i in x.values() if isinstance(i, dict)]
    return []


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


READ_ONLY_TYPES = {"QUALITY", "QUALITY_SCORE", "CALCULATED"}


class Schema:
    def __init__(self, raw: dict):
        self.raw = raw or {}
        self.classes = _as_list(self.raw.get("classes"))
        self.relations = _as_list(self.raw.get("relations"))
        self.labels = _as_list(self.raw.get("labels"))
        self.properties = _as_list(self.raw.get("properties"))

        self.class_by_id = {c["id"]: c for c in self.classes if "id" in c}
        self.rel_by_id = {r["id"]: r for r in self.relations if "id" in r}
        self.label_by_id = {l["id"]: l for l in self.labels if "id" in l}
        self.prop_by_id = {p["id"]: p for p in self.properties if "id" in p}

        self._class_by_name = {_norm(c.get("name")): c for c in self.classes}
        self._rel_by_name = {_norm(r.get("name")): r for r in self.relations}
        self._labels_by_name: dict[str, list[dict]] = {}
        for l in self.labels:
            self._labels_by_name.setdefault(_norm(l.get("name")), []).append(l)

    # ------------------------------------------------------------------ classes
    def class_id(self, name_or_id: str | int) -> int:
        if isinstance(name_or_id, int) or str(name_or_id).isdigit():
            i = int(name_or_id)
            if i in self.class_by_id:
                return i
            raise LookupError(f"No class with id {i}.")
        c = self._class_by_name.get(_norm(name_or_id))
        if not c:
            raise LookupError(self._miss("class", name_or_id, [c.get("name") for c in self.classes]))
        return c["id"]

    def class_name(self, cid: int) -> str:
        c = self.class_by_id.get(cid)
        return c.get("name", str(cid)) if c else str(cid)

    def class_ancestry(self, cid: int) -> list[int]:
        out, seen = [], set()
        while cid is not None and cid not in seen and cid in self.class_by_id:
            seen.add(cid)
            out.append(cid)
            c = self.class_by_id[cid]
            parent = c.get("parent", c.get("parentId", c.get("superClassId")))
            if isinstance(parent, dict):
                parent = parent.get("id")
            cid = parent if isinstance(parent, int) and parent > 0 else None
        return out

    # ---------------------------------------------------------------- relations
    def relation(self, name_or_id: str | int) -> dict:
        if isinstance(name_or_id, int) or str(name_or_id).isdigit():
            r = self.rel_by_id.get(int(name_or_id))
            if r:
                return r
            raise LookupError(f"No relation with id {name_or_id}.")
        r = self._rel_by_name.get(_norm(name_or_id))
        if not r:
            raise LookupError(self._miss("relation", name_or_id, [r.get("name") for r in self.relations]))
        return r

    def relation_id(self, name_or_id: str | int) -> int:
        return self.relation(name_or_id)["id"]

    def inverse_id(self, rel_id: int) -> int:
        r = self.rel_by_id.get(rel_id) or {}
        inv = r.get("inverseId", r.get("inverse"))
        if isinstance(inv, dict):
            inv = inv.get("id")
        if isinstance(inv, int):
            return inv
        # Some schemas only store the link on one side.
        for other in self.relations:
            o = other.get("inverseId", other.get("inverse"))
            if isinstance(o, dict):
                o = o.get("id")
            if o == rel_id:
                return other["id"]
        raise LookupError(f"Relation {self.relation_name(rel_id)!r} has no inverse in this schema.")

    def relation_name(self, rid: int) -> str:
        r = self.rel_by_id.get(rid)
        return r.get("name", str(rid)) if r else str(rid)

    # ------------------------------------------------------------------- labels
    def label_id(self, name_or_id: str | int, class_id: int | None = None) -> int:
        if isinstance(name_or_id, int) or str(name_or_id).isdigit():
            i = int(name_or_id)
            if i in self.label_by_id:
                return i
            raise LookupError(f"No label with id {i}.")
        cands = self._labels_by_name.get(_norm(name_or_id), [])
        if not cands:
            raise LookupError(self._miss("label", name_or_id, [l.get("name") for l in self.labels]))
        if class_id is not None and len(cands) > 1:
            anc = set(self.class_ancestry(class_id))
            fit = [l for l in cands if anc & set(l.get("classIds") or [])]
            if fit:
                return fit[0]["id"]
        return cands[0]["id"]

    def label_fits(self, label_id: int, class_id: int) -> bool:
        l = self.label_by_id.get(label_id) or {}
        ids = l.get("classIds")
        if not ids:
            return True
        return bool(set(ids) & set(self.class_ancestry(class_id)))

    def label_name(self, lid: int) -> str:
        l = self.label_by_id.get(lid)
        return l.get("name", str(lid)) if l else str(lid)

    def labels_for_class(self, class_id: int) -> list[str]:
        anc = set(self.class_ancestry(class_id))
        return sorted(l.get("name", "") for l in self.labels if not l.get("classIds") or anc & set(l["classIds"]))

    # --------------------------------------------------------------- properties
    def properties_for_class(self, class_id: int) -> list[dict]:
        anc = self.class_ancestry(class_id)
        return [p for p in self.properties if p.get("classId") in anc]

    def property(self, class_id: int, name_or_id: str | int) -> dict:
        props = self.properties_for_class(class_id)
        if isinstance(name_or_id, int) or str(name_or_id).isdigit():
            i = int(name_or_id)
            for p in props:
                if p["id"] == i:
                    return p
            raise LookupError(f"Property id {i} does not belong to class {self.class_name(class_id)}.")
        want = _norm(name_or_id)
        for p in props:
            if _norm(p.get("name")) == want:
                return p
        raise LookupError(self._miss(f"property of {self.class_name(class_id)}", name_or_id,
                                     [p.get("name") for p in props]))

    def property_name(self, pid: int | str) -> str:
        try:
            p = self.prop_by_id.get(int(pid))
        except (TypeError, ValueError):
            p = None
        return p.get("name", str(pid)) if p else str(pid)

    def property_type(self, pid: int | str) -> str:
        try:
            p = self.prop_by_id.get(int(pid))
        except (TypeError, ValueError):
            p = None
        return (p or {}).get("type", "")

    # ------------------------------------------------------------------ summary
    def summary(self, kind: str = "all", class_name: str | None = None) -> dict:
        out: dict[str, Any] = {}
        cid = self.class_id(class_name) if class_name else None
        if kind in ("all", "classes"):
            out["classes"] = sorted(c.get("name", "") for c in self.classes)
        if kind in ("all", "relations"):
            seen, pairs = set(), []
            for r in self.relations:
                if r["id"] in seen:
                    continue
                try:
                    inv = self.inverse_id(r["id"])
                except LookupError:
                    inv = None
                seen.update({r["id"], inv})
                pairs.append(f"{r.get('name')} <-> {self.relation_name(inv) if inv else '(no inverse)'}")
            out["relations"] = sorted(pairs)
        if kind in ("all", "labels"):
            if cid is not None:
                out["labels"] = self.labels_for_class(cid)
            else:
                out["labels"] = {
                    l.get("name"): [self.class_name(c) for c in (l.get("classIds") or [])] for l in self.labels
                }
        if kind in ("all", "properties"):
            if cid is not None:
                out["properties"] = [
                    {"name": p.get("name"), "type": p.get("type"),
                     "writable": p.get("type") not in READ_ONLY_TYPES and p.get("type") != "FILE"}
                    for p in self.properties_for_class(cid)
                ]
            else:
                byc: dict[str, list[str]] = {}
                for p in self.properties:
                    byc.setdefault(self.class_name(p.get("classId")), []).append(f"{p.get('name')} ({p.get('type')})")
                out["properties"] = byc
        return out

    @staticmethod
    def _miss(kind: str, name: Any, names: list) -> str:
        names = [n for n in names if n]
        close = difflib.get_close_matches(str(name), names, n=5, cutoff=0.5)
        hint = f" Closest: {', '.join(close)}." if close else ""
        return f"Unknown {kind} {name!r}.{hint} Call innoslate_schema to list valid names."

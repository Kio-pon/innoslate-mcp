"""First-run survey of a project: what is there, how it is numbered, how documents are stored.

Writes into the project workspace:
    snapshot.json      compact copy of every entity (names instead of ids)
    PROJECT_MAP.md     counts, documents with full trees, diagrams, comments, people, problems
    NOMENCLATURE.md    numbering in use per document, patterns the guides ask for, clashes
    guides/*.txt       plain text of every guide document
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from . import docs
from .api import InnoslateAPI, num_key, strip_html
from .schema import Schema

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PATTERN = re.compile(r"(?<![\w.])([A-Z][A-Z0-9]{0,7}(?:[.\-](?:\d+|[nNxX]\b|[a-z]\b))+)(?![\w])")


def _mask(t: str) -> str:
    return EMAIL.sub("<email>", t or "")


def _when(v: Any) -> str:
    if isinstance(v, (int, float)) and v > 1e11:
        return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
    return str(v or "")[:10]


def compact(sch: Schema, e: dict) -> dict:
    rels: dict[str, list[int]] = {}
    for r in e.get("rels") or []:
        rels.setdefault(sch.relation_name(r.get("relationId")), []).append(r.get("targetId"))
    attrs = {}
    for k, v in (e.get("attrs") or {}).items():
        if v in (None, "", [], {}, False):
            continue
        name, typ = sch.property_name(k), sch.property_type(k)
        if typ == "QUALITY" and isinstance(v, dict):
            v = bool(v.get("value"))
        elif typ == "FILE":
            v = f"FILE:{v}"
        elif isinstance(v, str):
            v = strip_html(v)[:300]
        attrs[name] = v
    return {
        "id": e.get("id"), "c": sch.class_name(e.get("classId")), "n": e.get("number") or "",
        "name": e.get("name") or "", "d": strip_html(e.get("description"))[:3000],
        "lab": [sch.label_name(l) for l in e.get("labelIds") or []], "rels": rels, "attrs": attrs,
        "created": _when(e.get("created")), "by": e.get("createdBy") or "",
        "modified": _when(e.get("modified")), "mby": e.get("modifiedBy") or "",
    }


def prefix_of(number: str) -> str:
    m = re.match(r"^([A-Za-z]+[-._ ]?)", number or "")
    return m.group(1) if m else "(numeric)"


def shape_of(number: str) -> str:
    """SR.1.12 -> SR.n.n, 3.2 -> n.n"""
    return re.sub(r"\d+", "n", number or "")


class Survey:
    def __init__(self, api: InnoslateAPI, workspace: Path):
        self.api, self.ws = api, workspace

    async def run(self, guides_dir: str | None = None) -> dict:
        api, ws = self.api, self.ws
        ws.mkdir(parents=True, exist_ok=True)
        sch = await api.schema(refresh=True)
        info = await api.project_info()
        raw = await api.all_entities()
        counts = await _safe(api.counts(), {})
        comments = await _safe(api.comments(), [])
        users = await _safe(api.users(), [])
        E = [compact(sch, e) for e in raw]
        B = {e["id"]: e for e in E}

        snapshot = {
            "capturedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "project": {"org": api.p.org, "id": api.p.pid, "name": info.get("name"), "url": api.p.home_url,
                        "description": strip_html(info.get("description"))[:1000]},
            "users": [_user_name(u) for u in users],
            "comments": [{"id": c.get("id"), "entity": c.get("entityId", c.get("targetId")),
                          "resolved": bool(c.get("resolved")), "by": c.get("createdBy", ""),
                          "at": _when(c.get("created")), "text": strip_html(c.get("description") or c.get("text") or c.get("body"))}
                         for c in comments],
            "entities": E,
        }
        (ws / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")

        guides = self._guides(guides_dir) if guides_dir else self._existing_guides()
        map_md, facts = self._map(sch, snapshot, B)
        (ws / "PROJECT_MAP.md").write_text(map_md, encoding="utf-8")
        nom_md, nom = self._nomenclature(sch, E, B, guides)
        (ws / "NOMENCLATURE.md").write_text(nom_md, encoding="utf-8")

        return {
            "project": snapshot["project"]["name"],
            "workspace": str(ws),
            "files": {"map": str(ws / "PROJECT_MAP.md"), "nomenclature": str(ws / "NOMENCLATURE.md"),
                      "snapshot": str(ws / "snapshot.json"), "guides": [g["txt"] for g in guides]},
            "entities": len(E),
            "counts_by_class": facts["counts"],
            "documents": facts["documents"][:60],
            "numbering_prefixes": facts["prefixes"][:40],
            "storage_conventions": facts["conventions"],
            "comments": {"total": len(comments), "unresolved": sum(1 for c in snapshot["comments"] if not c["resolved"])},
            "members": snapshot["users"],
            "guide_patterns": nom["guide_patterns"][:40],
            "numbering_clashes": nom["clashes"][:20],
            "problems": facts["problems"],
            "completeness": _completeness(sch, counts, E),
            "next": ("Read PROJECT_MAP.md and NOMENCLATURE.md (innoslate_read_workspace), then the user's documents "
                     "(read_local_file). Propose where each piece goes, get the user's agreement, then "
                     "innoslate_apply with dry_run first."),
        }

    # ------------------------------------------------------------------ guides
    def _guides(self, guides_dir: str) -> list[dict]:
        src = Path(guides_dir).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"No such folder or file: {src}")
        files = [src] if src.is_file() else [Path(f["path"]) for f in docs.list_files(src) if f["readable"]]
        out_dir = self.ws / "guides"
        out_dir.mkdir(exist_ok=True)
        out = []
        for f in files:
            try:
                text = docs.extract_text(f)
            except Exception as e:
                out.append({"file": str(f), "error": str(e), "txt": ""})
                continue
            safe = re.sub(r"[^\w.-]+", "_", f.stem)[:80] or "guide"
            dst = out_dir / f"{safe}.txt"
            dst.write_text(f"source: {f}\n\n{text}", encoding="utf-8")
            out.append({"file": str(f), "txt": str(dst), "chars": len(text), "text": text})
        return out

    def _existing_guides(self) -> list[dict]:
        d = self.ws / "guides"
        if not d.is_dir():
            return []
        return [{"file": p.name, "txt": str(p), "chars": p.stat().st_size, "text": p.read_text(encoding="utf-8", errors="replace")}
                for p in sorted(d.glob("*.txt"))]

    # --------------------------------------------------------------------- map
    def _map(self, sch: Schema, snap: dict, B: dict[int, dict]) -> tuple[str, dict]:
        E = snap["entities"]
        out: list[str] = []
        w = out.append
        p = snap["project"]

        def lbl(i: int) -> str:
            e = B.get(i)
            if not e:
                return f"<{i} outside project>"
            return f'{e["n"]} {e["name"]}'.strip() or f'({e["c"]} {i})'

        kids, tops_of = _indexes(E, B)

        def children(eid: int) -> list[dict]:
            return sorted((B[i] for i in kids.get(eid, ()) if i in B), key=lambda e: num_key(e["n"]))

        w(f"# Innoslate project map: {p['name']}")
        w("")
        w(f"Snapshot {snap['capturedAt']}. Org `{p['org']}`, project {p['id']}, {len(E)} entities. {p['url']}")
        if p.get("description"):
            w("")
            w("Project description: " + _mask(p["description"])[:600])
        w("")
        w("Members: " + (", ".join(snap["users"]) or "(not readable)"))
        w("")
        w("This file is generated by innoslate_initialize. Regenerate it after big changes. Emails are masked.")

        # counts
        counts = collections.Counter(e["c"] for e in E)
        w("\n## 1. Counts\n")
        w("| Class | Count |\n|---|---|")
        for c, n in counts.most_common():
            w(f"| {c} | {n} |")
        w("")
        w("Labels in use: " + (", ".join(f"{k} ({v})" for k, v in collections.Counter(l for e in E for l in e["lab"]).most_common()) or "none"))
        w("")
        w("Relations in use: " + (", ".join(f"{k} ({v})" for k, v in collections.Counter(r for e in E for r, ts in e["rels"].items() for _ in ts).most_common()) or "none"))

        # conventions, measured, not assumed
        arts = [e for e in E if e["c"] == "Artifact"]
        doc_labels = collections.Counter(l for e in arts for l in e["lab"])
        rel_to_art = collections.Counter()
        for e in E:
            if e["c"] == "Artifact":
                continue
            for r, ts in e["rels"].items():
                for t in ts:
                    if t in B and B[t]["c"] == "Artifact":
                        rel_to_art[r] += 1
        conventions = {
            "document_class": "Artifact",
            "document_labels": dict(doc_labels.most_common(10)),
            "item_to_document_relations": dict(rel_to_art.most_common(6)),
            "decomposes_links": sum(len(e["rels"].get("decomposes", [])) for e in E),
            "artifacts_with_file": sum(1 for e in arts if any(str(v).startswith("FILE:") for v in e["attrs"].values())),
        }
        w("\n## 2. How this project stores things (measured)\n")
        w("A document is an Artifact, usually with a document label. Labels on Artifacts here: "
          + (", ".join(f"{k} ({v})" for k, v in doc_labels.most_common(10)) or "none") + ".")
        w("Relations from other entities into Artifacts: "
          + (", ".join(f"{k} ({v})" for k, v in rel_to_art.most_common(6)) or "none") + ".")
        w(f"`decomposes` links (child to parent, used for deeper items and diagrams): {conventions['decomposes_links']}.")

        # numbering
        pref: dict[str, list[dict]] = collections.defaultdict(list)
        for e in E:
            if e["n"]:
                pref[prefix_of(e["n"])].append(e)
        prefixes = []
        w("\n## 3. Numbering in use\n")
        w("Prefix is the letters before the first digit. Numbers can repeat in Innoslate, so a number only means something inside its document.")
        w("")
        w("| Prefix | Count | Classes | Shapes | Examples |\n|---|---|---|---|---|")
        for k in sorted(pref):
            items = sorted(pref[k], key=lambda e: num_key(e["n"]))
            shapes = ", ".join(f"`{s}`" for s, _ in collections.Counter(shape_of(e["n"]) for e in items).most_common(4))
            ex = ", ".join(e["n"] for e in items[:6]) + (" ..." if len(items) > 6 else "")
            cls = ", ".join(sorted({e["c"] for e in items}))
            w(f"| `{k}` | {len(items)} | {cls} | {shapes} | {ex} |")
            prefixes.append({"prefix": k, "count": len(items), "classes": cls, "examples": ex})
        dups = sorted([n for n, c in collections.Counter(e["n"] for e in E if e["n"]).items() if c > 1], key=num_key)
        w("")
        w(f"Numbers used more than once ({len(dups)}): " + (", ".join(dups[:120]) or "none"))
        w(f"\nEntities with no number: {sum(1 for e in E if not e['n'])}.")

        # documents
        w("\n## 4. Documents\n")
        w("| Number | Name | Labels | Items | Created | By | Id |\n|---|---|---|---|---|---|---|")
        documents = []
        for d in sorted(arts, key=lambda e: num_key(e["n"])):
            tops = [B[t] for t in tops_of.get(d["id"], ()) if t in B]
            total = self._count_tree(tops, children)
            w(f'| `{d["n"] or "-"}` | {d["name"]} | {", ".join(d["lab"]) or "-"} | {total} | {d["created"]} | {d["by"]} | {d["id"]} |')
            documents.append({"id": d["id"], "number": d["n"], "name": d["name"], "labels": d["lab"], "items": total,
                              "created": d["created"], "by": d["by"]})
        for d in sorted(arts, key=lambda e: num_key(e["n"])):
            tops = sorted([B[t] for t in tops_of.get(d["id"], ()) if t in B], key=lambda e: num_key(e["n"]))
            w(f'\n### `{d["n"] or "-"}` {d["name"]} (id {d["id"]})\n')
            if d["d"]:
                w(_mask(d["d"])[:800])
                w("")
            other = {r: [lbl(t) for t in ts] for r, ts in d["rels"].items() if r != "source of"}
            if other:
                w("Other relations: " + "; ".join(f"{r}: {', '.join(v[:6])}{' ...' if len(v) > 6 else ''}" for r, v in other.items()))
            if not tops:
                w("_No items in this document._")
            budget, seen = [400], set()
            for t in tops:
                self._tree(w, t, 0, seen, children, lbl, budget)
            if budget[0] <= 0:
                w("- ... (tree cut at 400 lines, use innoslate_document_tree for the rest)")

        # diagrams
        w("\n## 5. Diagram roots (decomposition trees)\n")
        for cls in ("Action", "Asset", "Logical"):
            roots = [e for e in E if e["c"] == cls and not e["rels"].get("decomposes") and children(e["id"])]
            if not roots:
                continue
            w(f"\n### {cls}\n")
            for r in sorted(roots, key=lambda e: num_key(e["n"])):
                budget = [120]
                self._tree(w, r, 0, set(), children, lbl, budget, brief=True)

        # comments
        w("\n## 6. Review comments\n")
        cm = sorted(snap["comments"], key=lambda c: (c["resolved"], c["at"]))
        if not cm:
            w("None.")
        else:
            w("| On | Resolved | By | Date | Comment |\n|---|---|---|---|---|")
            for c in cm:
                w(f'| {lbl(c["entity"]) if c["entity"] else "-"} | {"yes" if c["resolved"] else "**no**"} | {c["by"]} | {c["at"]} | {_mask(c["text"]).replace("|", "/")[:400]} |')

        # people
        w("\n## 7. Who did what\n")
        by = collections.Counter(e["by"] for e in E if e["by"])
        mby = collections.Counter(e["mby"] for e in E if e["mby"])
        last = {}
        for e in E:
            for u in (e["by"], e["mby"]):
                if u:
                    last[u] = max(last.get(u, ""), e["modified"] or e["created"])
        w("| User | Created | Last modifier of | Last activity |\n|---|---|---|---|")
        for u in sorted(set(by) | set(mby)):
            w(f"| {u} | {by.get(u, 0)} | {mby.get(u, 0)} | {last.get(u, '')} |")
        w("\nLatest 25 changes:\n")
        for e in sorted(E, key=lambda e: e["modified"] or "", reverse=True)[:25]:
            w(f'- {e["modified"]} {e["mby"] or e["by"]}: {e["c"]} `{e["n"] or "-"}` {e["name"]}')

        # problems
        w("\n## 8. Things that look broken\n")
        problems = []
        def note(msg: str, ids: list[int]) -> None:
            if ids:
                problems.append({"problem": msg, "count": len(ids), "examples": [lbl(i) for i in ids[:5]]})
                w(f"- {msg}: {len(ids)}. e.g. " + "; ".join(f"{lbl(i)} (id {i})" for i in ids[:5]))
        note("Entities with no name", [e["id"] for e in E if not e["name"].strip()])
        note("Entities still named like a default ('New ...')", [e["id"] for e in E if re.match(r"^new \w+$", e["name"].strip().lower())])
        note("Requirements with an empty description", [e["id"] for e in E if e["c"] == "Requirement" and not e["d"].strip()])
        note("Artifacts with no items, no file and no description", [e["id"] for e in arts if not tops_of.get(e["id"]) and not e["attrs"] and not e["d"].strip()])
        one_sided = []
        for e in E:
            for r, ts in e["rels"].items():
                inv = _inverse_name(sch, r)
                for t in ts:
                    if t in B and inv and e["id"] not in B[t]["rels"].get(inv, []):
                        one_sided.append(e["id"])
        note("Relations recorded on one side only", sorted(set(one_sided)))
        if not problems:
            w("Nothing obvious.")

        facts = {"counts": dict(counts.most_common()), "documents": documents, "prefixes": prefixes,
                 "conventions": conventions, "problems": problems}
        return "\n".join(out) + "\n", facts

    @staticmethod
    def _count_tree(tops, children) -> int:
        seen, stack = set(), list(tops)
        while stack:
            e = stack.pop()
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            stack.extend(children(e["id"]))
        return len(seen)

    def _tree(self, w, e, depth, seen, children, lbl, budget, brief=False) -> None:
        if e["id"] in seen or budget[0] <= 0:
            return
        seen.add(e["id"])
        budget[0] -= 1
        pad = "  " * depth
        head = f'{pad}- `{e["n"] or "-"}` **{e["name"] or "(no name)"}** [{e["c"]}, id {e["id"]}]'
        if e["lab"]:
            head += " labels: " + ", ".join(e["lab"])
        w(head)
        if not brief:
            if e["d"]:
                t = _mask(e["d"]).replace("\n", " / ")
                w(f"{pad}  {t[:300]}{'...' if len(t) > 300 else ''}")
            tr = []
            for r, ts in e["rels"].items():
                if r in ("decomposes", "decomposed by", "sourced by", "source of"):
                    continue
                tr += [f"{r} {lbl(t)}" for t in ts]
            if tr:
                w(f"{pad}  links: " + "; ".join(tr[:12]) + (" ..." if len(tr) > 12 else ""))
        for c in children(e["id"]):
            self._tree(w, c, depth + 1, seen, children, lbl, budget, brief)

    # ------------------------------------------------------------ nomenclature
    def _nomenclature(self, sch: Schema, E: list[dict], B: dict[int, dict], guides: list[dict]) -> tuple[str, dict]:
        out: list[str] = []
        w = out.append
        w("# Nomenclature")
        w("")
        w("Generated by innoslate_initialize from the project and the guide documents. Before creating anything, "
          "pick numbers that follow the guide and do not collide with a document that already exists.")

        w("\n## 1. Numbering per document in this project\n")
        w("| Document | Name | Item shapes | Items | Last item |\n|---|---|---|---|---|")
        arts = sorted([e for e in E if e["c"] == "Artifact"], key=lambda e: num_key(e["n"]))
        doc_numbers = collections.defaultdict(list)
        kids, tops_of = _indexes(E, B)
        for d in arts:
            if d["n"]:
                doc_numbers[d["n"]].append(d)
            items = []
            stack = [B[t] for t in tops_of.get(d["id"], ()) if t in B]
            seen = set()
            while stack:
                x = stack.pop()
                if x["id"] in seen:
                    continue
                seen.add(x["id"])
                items.append(x)
                stack += [B[t] for t in kids.get(x["id"], ()) if t in B]
            shapes = collections.Counter(shape_of(x["n"]) for x in items if x["n"])
            lastn = max((x["n"] for x in items if x["n"]), key=num_key, default="")
            w(f'| `{d["n"] or "-"}` | {d["name"]} | {", ".join(f"`{s}` ({c})" for s, c in shapes.most_common(3)) or "-"} | {len(items)} | {lastn} |')

        w("\n## 2. Name prefixes on documents\n")
        pre = collections.Counter()
        for d in arts:
            m = re.match(r"^([A-Za-z]{1,6}[.:]\s)", d["name"])
            if m:
                pre[m.group(1).strip()] += 1
        w(", ".join(f"`{k}` ({v})" for k, v in pre.most_common()) or "None in use.")

        w("\n## 3. Numbers the guides mention\n")
        guide_patterns = []
        if not guides:
            w("No guide documents were given. Run innoslate_initialize again with guides_dir to fill this in.")
        else:
            found: dict[str, dict] = {}
            for g in guides:
                text = g.get("text") or ""
                for m in PATTERN.finditer(text):
                    tok = m.group(1).rstrip(".")
                    if len(tok) < 3 or re.fullmatch(r"[A-Z]+-\d{3,}", tok):   # skip ticket keys like JIRA-123
                        continue
                    f = found.setdefault(tok, {"pattern": tok, "count": 0, "files": set(), "context": []})
                    f["count"] += 1
                    f["files"].add(Path(g["file"]).name)
                    if len(f["context"]) < 2:
                        a, b = max(0, m.start() - 90), min(len(text), m.end() + 90)
                        f["context"].append(" ".join(text[a:b].split()))
            w("| Pattern | Times | Guides | Context |\n|---|---|---|---|")
            for tok, f in sorted(found.items(), key=lambda kv: (prefix_of(kv[0]), num_key(kv[0]))):
                ctx = " / ".join(c.replace("|", "/") for c in f["context"])
                w(f"| `{tok}` | {f['count']} | {', '.join(sorted(f['files']))} | {ctx[:300]} |")
                guide_patterns.append({"pattern": tok, "times": f["count"], "context": f["context"][:1]})

        clashes = []
        w("\n## 4. Clashes between guide numbers and existing documents\n")
        for gp in guide_patterns:
            tok = gp["pattern"]
            if re.search(r"[.\-][nNxX](?=$|[.\-])", tok):     # a placeholder like SR.1.n, not a real number
                continue
            for d in doc_numbers.get(tok, []):
                clashes.append({"number": tok, "existing_id": d["id"], "existing_name": d["name"], "by": d["by"], "created": d["created"]})
        if clashes:
            w("These numbers are already used by a document. Innoslate allows the duplicate, but every later reference "
              "has to name the document too. Ask the user whether to reuse the existing document or create a new one.\n")
            for c in clashes:
                w(f"- `{c['number']}` is already {c['existing_name']!r} (id {c['existing_id']}, {c['by']}, {c['created']})")
        else:
            w("None found.")

        w("\n## 5. Schema names the guides mention\n")
        alltext = "\n".join(g.get("text") or "" for g in guides).lower()
        if alltext:
            def mentioned(names):
                return sorted({n for n in names if n and len(n) > 3 and n.lower() in alltext})
            w("Classes: " + (", ".join(mentioned([c.get("name") for c in sch.classes])) or "none"))
            w("")
            w("Labels: " + (", ".join(mentioned([l.get("name") for l in sch.labels])) or "none"))
            w("")
            w("Relations: " + (", ".join(mentioned([r.get("name") for r in sch.relations])) or "none"))
        else:
            w("No guides.")
        return "\n".join(out) + "\n", {"guide_patterns": guide_patterns, "clashes": clashes}


def _inverse_name(sch: Schema, rel_name: str) -> str:
    try:
        return sch.relation_name(sch.inverse_id(sch.relation_id(rel_name)))
    except LookupError:
        return ""


def _indexes(E: list[dict], B: dict[int, dict]) -> tuple[dict[int, set], dict[int, set]]:
    """Children (decomposition) and document members, read from both sides of each relation,
    so a relation stored on one side only still shows up."""
    kids: dict[int, set] = collections.defaultdict(set)
    tops: dict[int, set] = collections.defaultdict(set)
    for e in E:
        for t in e["rels"].get("decomposes", []):
            kids[t].add(e["id"])
        for t in e["rels"].get("decomposed by", []):
            kids[e["id"]].add(t)
        for t in e["rels"].get("sourced by", []):
            tops[t].add(e["id"])
        for t in e["rels"].get("source of", []):
            tops[e["id"]].add(t)
    return kids, tops


def _user_name(u: Any) -> str:
    if isinstance(u, str):
        return u
    if isinstance(u, dict):
        x = u.get("user") or u.get("username") or u.get("name") or u.get("email") or ""
        if isinstance(x, dict):
            x = x.get("username") or x.get("name") or ""
        return _mask(str(x))
    return str(u)


async def _safe(coro, default):
    try:
        return await coro
    except Exception:
        return default


def _completeness(sch: Schema, counts: dict, E: list[dict]) -> str:
    """Compare what search returned with the project's own per-class counts."""
    if not isinstance(counts, dict) or not counts:
        return "unknown (counts endpoint unavailable)"
    got = collections.Counter(e["c"] for e in E)
    short = []
    for k, n in counts.items():
        try:
            name = sch.class_name(int(k))
        except (TypeError, ValueError):
            name = str(k)
        if isinstance(n, int) and got.get(name, 0) < n:
            short.append(f"{name}: read {got.get(name, 0)} of {n}")
    return "complete" if not short else "INCOMPLETE, " + "; ".join(short)

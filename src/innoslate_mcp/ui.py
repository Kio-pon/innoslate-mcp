"""Features Innoslate only offers in its UI, driven with Playwright clicks.

Both paths were proven on cloud.innoslate.com:
  * report: document page -> "Reports" -> report name -> modal "Create" -> download
  * Quality Check: document page -> "More" -> "Quality Check"; scores land in the
    QUALITY_SCORE property of Requirement entities (other classes get no score)
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from .api import InnoslateAPI
from .browser import BrowserSession, InnoslateError

_VISIBLE = "const vis = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';"

# offsetParent is null for position:fixed elements, and Innoslate's menus and modals are fixed,
# so visibility is judged by layout boxes instead.
_LIST_REPORTS_JS = r"""
() => {
  """ + _VISIBLE + r"""
  const rx = /\((DOCX|XLSX|PDF|CSV|HTML|XML|JSON|PPTX|DOC|XLS|ZIP|TXT|RTF)\)\s*$/i;
  const seen = new Set();
  for (const el of document.querySelectorAll('a, li, span, button, div')) {
    if (!vis(el)) continue;
    if (el.children.length > 2) continue;
    const t = (el.innerText || '').trim();
    if (t && t.length < 80 && rx.test(t)) seen.add(t);
  }
  return [...seen];
}
"""

_MODAL_TEXT_JS = r"""
() => { """ + _VISIBLE + r"""
  return [...document.querySelectorAll('.modal, [role=dialog], .toast, .alert')]
    .filter(vis).map(m => m.innerText.trim()).filter(Boolean).join('\n----\n').slice(0, 3000); }
"""


async def _open_document(session: BrowserSession, api: InnoslateAPI, doc_id: int, ready_text: str) -> Any:
    page = await session.new_page()
    await page.goto(api.p.document_url(doc_id), wait_until="domcontentloaded")
    try:
        await page.get_by_text(ready_text, exact=True).first.wait_for(state="visible", timeout=45000)
    except Exception:
        await page.close()
        raise InnoslateError(f"The document page never showed a {ready_text!r} button. Check that document {doc_id} "
                             "exists and that the account can open it.")
    await page.wait_for_timeout(800)
    return page


async def download_report(session: BrowserSession, api: InnoslateAPI, doc_id: int, report_name: str,
                          out_dir: Path, timeout_s: int = 120) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    page = await _open_document(session, api, doc_id, "Reports")
    try:
        await page.get_by_text("Reports", exact=True).first.click()
        await page.wait_for_timeout(1200)
        options = await page.evaluate(_LIST_REPORTS_JS)
        target = page.get_by_text(report_name, exact=True).filter(visible=True)
        if await target.count() == 0:
            close = [o for o in options if report_name.lower().split("(")[0].strip() in o.lower()]
            return {"ok": False, "error": f"No report called {report_name!r} on this document.",
                    "available_reports": options, "did_you_mean": close[:5]}
        await target.first.click()
        await page.wait_for_timeout(1200)
        create = page.locator(".modal button:has-text('Create'), [role=dialog] button:has-text('Create')").filter(visible=True)
        async with page.expect_download(timeout=timeout_s * 1000) as dl_info:
            if await create.count():
                await create.first.click()
        dl = await dl_info.value
        name = re.sub(r"[^\w.\- ()]+", "_", dl.suggested_filename or f"report-{doc_id}")
        dst = out_dir / name
        if dst.exists():
            dst = out_dir / f"{dst.stem}-{time.strftime('%Y%m%d-%H%M%S')}{dst.suffix}"
        await dl.save_as(str(dst))
        return {"ok": True, "file": str(dst), "bytes": dst.stat().st_size, "report": report_name}
    finally:
        await page.close()


async def quality_check(session: BrowserSession, api: InnoslateAPI, doc_id: int, timeout_s: int = 120) -> dict:
    sch = await api.schema()
    doc = await api.get(doc_id)
    if not doc:
        raise InnoslateError(f"No document with id {doc_id}.")
    score_props = {str(p["id"]) for p in sch.properties if p.get("type") == "QUALITY_SCORE"}
    quality_props = {str(p["id"]): p.get("name") for p in sch.properties if p.get("type") == "QUALITY"}

    async def snapshot() -> list[dict]:
        rows = []
        for depth, e in await api.document_members(doc):
            sc = next(((e.get("attrs") or {}).get(k) for k in score_props if (e.get("attrs") or {}).get(k) is not None), None)
            has_prop = any(p.get("classId") in sch.class_ancestry(e.get("classId")) for p in sch.properties
                           if str(p["id"]) in score_props)
            if not has_prop:
                continue
            fails = []
            for k, name in quality_props.items():
                v = (e.get("attrs") or {}).get(k)
                if isinstance(v, dict) and v.get("value") is False:
                    fails.append({"check": name, "suggestions": v.get("suggestions") or []})
            rows.append({"id": e["id"], "number": e.get("number") or "", "name": e.get("name") or "",
                         "score": sc, "failed_checks": fails, "modified": e.get("modified")})
        return rows

    before = {r["id"]: (r["score"], r["modified"]) for r in await snapshot()}
    if not before:
        return {"ok": False, "error": "This document holds no entity that has a Quality Score property "
                                      "(only Requirements get one). Nothing to check."}
    page = await _open_document(session, api, doc_id, "More")
    modal = ""
    try:
        await page.get_by_text("More", exact=True).first.click()
        await page.wait_for_timeout(800)
        qc = page.locator("span:text-is(' Quality Check'), span:text-is('Quality Check'), "
                          "a:text-is('Quality Check'), li:text-is('Quality Check'), button:text-is('Quality Check')").filter(visible=True)
        if await qc.count() == 0:
            raise InnoslateError("No 'Quality Check' entry under More on this document page.")
        await qc.first.click()
        deadline = time.monotonic() + timeout_s
        rows: list[dict] = []
        prev = None
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            m = await page.evaluate(_MODAL_TEXT_JS)
            modal = m or modal
            rows = await snapshot()
            state = [(r["id"], r["score"]) for r in rows]
            changed = any((r["score"], r["modified"]) != before.get(r["id"]) for r in rows)
            waited = time.monotonic() - (deadline - timeout_s)
            done = all(r["score"] is not None for r in rows) and (changed or waited >= 20)
            if done and state == prev:
                break
            prev = state
    finally:
        await page.close()
    rows = [{k: v for k, v in r.items() if k != "modified"} for r in rows]
    scored = [r for r in rows if r["score"] is not None]
    return {
        "ok": bool(scored),
        "document_id": doc_id,
        "requirements": len(rows),
        "scored": len(scored),
        "average_score": round(sum(float(r["score"]) for r in scored) / len(scored), 1) if scored else None,
        "lowest": sorted(scored, key=lambda r: float(r["score"]))[:10],
        "results": sorted(rows, key=lambda r: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", r["number"])]),
        "dialog_text": modal[:1500],
    }

"""Requirement wording checks that run before anything is written.

Based on the usual INCOSE / course-guide rules: one 'shall' per requirement, one sentence,
no vague or unverifiable words, no escape clauses, no invented numbers without a source.
Innoslate's own Quality Check runs later in the UI (innoslate_quality_check); this one is
local, instant and explains each finding.
"""
from __future__ import annotations

import re

RULES: list[tuple[str, str, str]] = [
    # (regex, severity, why)
    (r"\bshould\b", "error", "use 'shall'; 'should' is a goal, not a requirement"),
    (r"\bwill\b", "warning", "'will' states a fact or intent; requirements use 'shall'"),
    (r"\bmay\b|\bmight\b|\bcould\b|\bought\b", "error", "possibility or permission, not an obligation"),
    (r"\bcan\b|\bbe able to\b", "warning", "capability wording; 'shall <verb>' is testable"),
    (r"\bas (often|soon|much|far) as possible\b|\bas appropriate\b|\bas needed\b|\bas required\b|\bif possible\b|\bwhere practicable\b|\bif necessary\b",
     "error", "escape clause, it makes the requirement unverifiable"),
    (r"\buser[- ]friendly\b|\beasy\b|\beasily\b|\bintuitive\b|\bfast\b|\bquick(ly)?\b|\befficient(ly)?\b|\brobust\b|\bseamless(ly)?\b|\bflexible\b|\bsimple\b|\bmodern\b|\bstate[- ]of[- ]the[- ]art\b|\bminimi[sz]e\b|\bmaximi[sz]e\b|\boptimi[sz]e\b|\badequate\b|\bsufficient\b|\breasonable\b|\bappropriate\b|\bnormal\b|\bsignificant\b",
     "error", "vague, give a measurable condition or TBD"),
    (r"\bat least\b|\bapproximately\b|\babout\b|\baround\b|\bseveral\b|\bmany\b|\bfew\b|\bsome\b|\bmost\b",
     "warning", "imprecise amount, prefer 'greater than or equal to <value or TBD>'"),
    (r"\b100 ?%|\balways\b|\bnever\b|\bevery time\b|\ball the time\b", "warning", "absolute, usually unverifiable"),
    (r"\betc\.?|\band so on\b|\band/or\b|\bincluding but not limited to\b|\bsuch as\b", "error", "open-ended or ambiguous list"),
    (r"\bit\b|\bthey\b|\bthem\b|\bthis\b|\bthat one\b", "warning", "pronoun with an unclear referent"),
    (r"\bnot\b.*\bshall\b|\bshall not\b", "warning", "negative requirement, check that it can be verified"),
    (r"\bsupport(s|ed)?\b|\bhandle(s|d)?\b|\bprovide(s|d)? for\b", "warning", "weak verb, say what the system does"),
]

UNIT = re.compile(r"\b\d+(\.\d+)?\s*(s|sec|seconds?|ms|minutes?|mins?|hours?|hrs?|%|percent|kb|mb|gb|tb|fps|hz|khz|mhz|days?|weeks?|months?|years?|m|km|kg|users?)\b", re.I)


def lint_items(items: list[dict], extra_banned: dict[str, str] | None = None, require_tbd_source: bool = True) -> dict:
    """items: [{number, name, text, rationale?}]. Returns findings grouped per item."""
    out = []
    seen_text: dict[str, str] = {}
    seen_num: dict[str, int] = {}
    rules = list(RULES) + [(pat, "error", why) for pat, why in (extra_banned or {}).items()]
    for it in items:
        num = str(it.get("number") or it.get("name") or "?")
        text = " ".join(str(it.get("text") or it.get("description") or "").split())
        name = str(it.get("name") or "")
        f: list[dict] = []

        def add(sev: str, msg: str) -> None:
            f.append({"severity": sev, "message": msg})

        if not text:
            add("error", "no requirement text")
        else:
            n_shall = len(re.findall(r"\bshall\b", text, re.I))
            if n_shall == 0:
                add("error", "no 'shall'")
            elif n_shall > 1:
                add("error", f"{n_shall} 'shall's, split into {n_shall} requirements")
            sentences = [s for s in re.split(r"(?<=[.;!?])\s+", text) if s.strip()]
            if len(sentences) > 1 or ";" in text:
                add("error", "more than one sentence")
            if re.search(r"\bshall\b[^.]*\b(and|or)\b[^.]*\b(shall|display|allow|record|send|store|show|provide|generate|notify)\b", text, re.I):
                add("warning", "'and'/'or' may join two requirements")
            words = len(text.split())
            if words > 40:
                add("warning", f"{words} words, keep it under 40")
            low = text.lower()
            for pat, sev, why in rules:
                m = re.search(pat, low)
                if m:
                    add(sev, f"'{m.group(0)}': {why}")
            for m in UNIT.finditer(text):
                if require_tbd_source and not it.get("rationale"):
                    add("warning", f"'{m.group(0)}' is a value; cite its source in the rationale or use TBD")
            if re.search(r"\btbd\b", low) and not it.get("rationale"):
                add("warning", "TBD with no rationale; track it with an Issue")
            if not re.match(r"^(the|each|every|all|when|while|if|where|upon|after|before|during|in)\b", low):
                add("warning", "start with the subject ('The system shall ...') or a condition ('When ..., the system shall ...')")
        if len(name.split()) > 8:
            add("warning", "name is long, use a short label")
        if text:
            key = re.sub(r"\W+", " ", text.lower()).strip()
            if key in seen_text:
                add("error", f"same text as {seen_text[key]}")
            seen_text.setdefault(key, num)
        seen_num[num] = seen_num.get(num, 0) + 1
        out.append({"number": num, "name": name, "findings": f})
    for row in out:
        if seen_num.get(row["number"], 0) > 1:
            row["findings"].append({"severity": "error", "message": "number used more than once"})
    errors = sum(1 for r in out for x in r["findings"] if x["severity"] == "error")
    warnings = sum(1 for r in out for x in r["findings"] if x["severity"] == "warning")
    return {
        "checked": len(out),
        "errors": errors,
        "warnings": warnings,
        "clean": [r["number"] for r in out if not r["findings"]],
        "items": [r for r in out if r["findings"]],
    }

"""Revision compare: what changed between two issues of the same discipline's drawing set.

Plain code over the words printed on the sheets (the PDF text layer): sheet numbers and titles from the title blocks and
the drawing index, then the text of each sheet. Sheets are matched by number, then by title (a renumbered sheet), and
what is left is added or removed. A matched sheet is "changed" when its printed words differ; the lines that appeared
and disappeared are listed so the engineer can look at the right place. Drawn lines are not compared: a moved outlet
with an unchanged label does not show here, and the screen says so.

"Who else needs to know": the other disciplines whose current set was issued before this revision, so a change here
may not be in their drawings yet.
"""
from __future__ import annotations

import re
from pathlib import Path

from .project import DISCIPLINES, _run, _title_block_fields, page_text, parse_drawing_index

MAX_LINES = 14          # lines listed per changed sheet, each way
MIN_LINE = 4            # shorter lines are noise (dimensions, single letters)
_NOISE = re.compile(r"^\W*$|^[\d\s.,'\"x×/()=+°%#-]*$|^(?:[A-Z]|\d{1,2})$|^[\d\s.,'\"x×/()=+°%#-]*[A-Z]{1,2}[\d\s.,'\"x×/()=+°%#-]*$")
_WORD = re.compile(r"[A-Z]{3,}|\d+\s?(?:A|V|KW|W|MM|M|SF|KPA|PSI|L/S|CFM|GPM|LB|KG)\b")
_DATE = re.compile(r"\b(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|"
                   r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+\d{1,2},?\s+20\d{2}|"
                   r"\d{1,2}\s+(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?,?\s+20\d{2})(?![\d])", re.I)
_SHEET_NO = re.compile(r"^(?:[A-Z]{1,2}-?\d{1,3}(?:\.\d+)?[A-Z]?|\d{1,3}(?:\.\d+)?)$")


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().upper()


def _lines(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        ln = _norm(raw)
        if len(ln) < MIN_LINE or _NOISE.match(ln) or not _WORD.search(ln):
            continue
        out.append(ln)
    return out


_LABEL = re.compile(r"^(?:SCALE|DRAWN|DATE|PROJECT|CLIENT|CHECKED|DSN|CHK|APP|REV|SHEET|DRAWING|JOB|FILE|TEL|FAX|SEAL|OF)\b", re.I)


def _reading_text(pdf: Path, page: int) -> str:
    try:
        return _run(["pdftotext", "-f", str(page), "-l", str(page), str(pdf), "-"]).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _title_from_block(reading: str) -> tuple[str, str]:
    """(sheet number if the block prints it after the title label, sheet title) from the title block. Two layouts cover
    the sets seen so far: 'DRAWING TITLE:' then the title then 'DRAWING #:', or the title printed just before a bare
    'DRAWING TITLE' label with the sheet number right after it."""
    def clean(lines: list[str]) -> list[str]:
        return [ln for ln in lines if ln and not _LABEL.match(ln) and not _DATE.search(ln)
                and not re.fullmatch(r"[\d\s'\"=/.,-]+", ln) and "THESE PLANS" not in ln.upper() and "THESE DRAWINGS" not in ln.upper()]
    m = re.search(r"DRAWING\s*TITLE\s*:?\s*\n((?:[^\n]*\n){1,8}?)\s*(DRAWING\s*#|SHEET\s*(?:NUMBER|NO))\.?\s*:?\s*\n\s*([^\n]*)", reading, re.I)
    number = ""
    if m:
        raw = [re.sub(r"^(\S+)\s+OF\s+\d+$", r"\1", ln.strip(), flags=re.I) for ln in m.group(1).splitlines() if ln.strip()]
        after = re.sub(r"^(\S+)\s+OF\s+\d+$", r"\1", m.group(3).strip(), flags=re.I).upper()
        if raw and m.group(2).upper().startswith("SHEET") and _SHEET_NO.match(raw[0]):
            number = raw[0].upper()
        else:
            if _SHEET_NO.match(after):
                number = after
            parts = clean(raw)
            if parts:
                return number, " ".join(parts)
    m = re.search(r"((?:[^\n]*\n){1,5})DRAWING\s*TITLE\s*\n", reading, re.I)
    if m:
        parts = []
        for ln in reversed([x.strip() for x in m.group(1).splitlines() if x.strip()]):
            if not clean([ln]):
                break
            parts.insert(0, ln)
        if parts:
            return number, " ".join(parts)
    return number, ""


def _title_for(number: str, block_title: str, index: dict[str, str]) -> str:
    """The drawing index wording wins when the set has one; otherwise what the title block prints."""
    if number and number in index:
        return index[number]
    for num, title in index.items():
        if number and (num.lstrip("0") == number.lstrip("0") or num.replace("-", "") == number.replace("-", "")):
            return title
    return block_title


def read_issue(pdf: Path, pages: int) -> list[dict]:
    """One entry per page: sheet number, title and the printed words, straight from the file."""
    texts = [page_text(pdf, p) for p in range(1, pages + 1)]
    index: dict[str, str] = {}
    for t in texts:
        index.update(parse_drawing_index(t))
    out = []
    for p in range(1, pages + 1):
        number, _ = _title_block_fields(pdf, p)
        reading = _reading_text(pdf, p)  # one printed label per line, unlike the layout text used for the index
        hint, block = _title_from_block(reading)
        number = number or hint
        if not number and block:  # a sheet whose number did not read: the index may still name it by its title
            number = next((num for num, t in index.items() if _norm(t) == _norm(block)), "")
        out.append({"page": p, "number": number, "title": _title_for(number, block, index), "lines": _lines(reading)})
    return out


def _key(sh: dict) -> str:
    return (sh["number"] or "").upper().replace(" ", "") or f"p{sh['page']}"


def _diff(old: list[str], new: list[str]) -> tuple[list[str], list[str], float]:
    """Lines that only the new sheet has, lines only the old one has, and how alike the two are (0..1). Dates on the
    sheet (revision stamps, issue dates) are ignored so a re-dated but unchanged sheet reads as unchanged."""
    o = {ln for ln in old if not _DATE.search(ln)}
    n = {ln for ln in new if not _DATE.search(ln)}
    if not o and not n:
        return [], [], 1.0
    common = len(o & n)
    rank = lambda ln: (-len(re.findall(r"[A-Z]{3,}", ln)), -len(ln), ln)
    return sorted(n - o, key=rank), sorted(o - n, key=rank), common / max(1, len(o | n))


def compare_issues(old: list[dict], new: list[dict]) -> dict:
    """Match sheets by number, then by title; the rest is added or removed."""
    old_by = {_key(s): s for s in old}
    new_by = {_key(s): s for s in new}
    matched: list[tuple[dict, dict, bool]] = []
    for k, ns in new_by.items():
        if k in old_by:
            matched.append((old_by.pop(k), ns, False))
    left_new = {k: s for k, s in new_by.items() if not any(m[1] is s for m in matched)}
    for k, ns in list(left_new.items()):
        t = _norm(ns["title"])
        hit = next((ok for ok, os_ in old_by.items() if t and _norm(os_["title"]) == t), None)
        if hit:
            matched.append((old_by.pop(hit), ns, True))
            left_new.pop(k)
    added = [{"number": s["number"], "title": s["title"], "page": s["page"]} for s in left_new.values()]
    removed = [{"number": s["number"], "title": s["title"], "page": s["page"]} for s in old_by.values()]
    renumbered, changed, unchanged = [], [], []
    for os_, ns, renum in sorted(matched, key=lambda m: m[1]["page"]):
        now, before, alike = _diff(os_["lines"], ns["lines"])
        entry = {"number": ns["number"], "title": ns["title"], "page": ns["page"], "was": os_["number"], "alike": round(alike, 2),
                 "now_says": now[:MAX_LINES], "no_longer_says": before[:MAX_LINES], "more": max(0, len(now) - MAX_LINES) + max(0, len(before) - MAX_LINES)}
        if renum:
            renumbered.append(entry)
        if now or before:
            changed.append(entry)
        elif not renum:
            unchanged.append({"number": ns["number"], "title": ns["title"], "page": ns["page"]})
    return {"added": added, "removed": removed, "renumbered": renumbered, "changed": changed, "unchanged": unchanged}


def who_else(documents: list[dict], discipline: str, new_dated: str | None) -> list[dict]:
    """Other disciplines whose current drawing set is older than this issue: a change here may not be in theirs yet."""
    out = []
    for d in documents:
        if d.get("kind") != "drawing" or not d.get("is_current") or d.get("discipline") == discipline:
            continue
        dated = d.get("dated") or ""
        if new_dated and dated and dated < new_dated:
            out.append({"discipline": d["discipline"], "name": DISCIPLINES.get(d["discipline"], d["discipline"]), "dated": dated,
                        "file": (d.get("rel_path") or "").split("/")[-1]})
    return sorted(out, key=lambda x: x["dated"])


def issues(documents: list[dict]) -> list[dict]:
    """The drawing issues to choose from, one row per file, oldest first within each discipline. Copies of the same file
    kept in more than one sub-folder (the office files a sent set under the project-management folder too) count once."""
    seen: set[tuple[str, str, str]] = set()
    out = []
    for d in sorted(documents, key=lambda d: (d.get("discipline") or "", d.get("dated") or "", -int(d.get("is_current") or 0), d.get("rel_path") or "")):
        if d.get("kind") != "drawing":
            continue
        key = (d.get("discipline") or "", d.get("dated") or "", (d.get("rel_path") or "").split("/")[-1].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": d["id"], "discipline": d["discipline"], "name": DISCIPLINES.get(d["discipline"], d["discipline"]),
                    "dated": d.get("dated"), "pages": d.get("pages"), "file": (d.get("rel_path") or "").split("/")[-1],
                    "is_current": bool(d.get("is_current"))})
    return out


def compare_documents(root: Path, old_doc: dict, new_doc: dict, documents: list[dict]) -> dict:
    old_pdf, new_pdf = root / old_doc["rel_path"], root / new_doc["rel_path"]
    old = read_issue(old_pdf, int(old_doc["pages"]))
    new = read_issue(new_pdf, int(new_doc["pages"]))
    result = compare_issues(old, new)
    result["who_else"] = who_else(documents, new_doc["discipline"], new_doc.get("dated"))
    result["old"] = {"id": old_doc["id"], "dated": old_doc.get("dated"), "file": old_doc["rel_path"].split("/")[-1], "sheets": len(old)}
    result["new"] = {"id": new_doc["id"], "dated": new_doc.get("dated"), "file": new_doc["rel_path"].split("/")[-1], "sheets": len(new)}
    result["basis"] = "Compared from the words printed on the sheets, not the drawn lines."
    return result

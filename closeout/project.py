"""Project intake: turn a dropped project folder (drawing sets, letters, forms) into a project model.

Deterministic first: walk the folder, date and classify every PDF, pick the newest drawing set per discipline,
render each sheet and pull its text layer and drawing index. Then one Strands job per sheet reads what the sheet
shows (levels, units, rooms, elements) and one job summarises the project. What the model records is
`model_observation`; what the text layer says is `document`. Nothing here is an engineering determination.

    python -m closeout.project <folder> --slug laurel
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import re
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image
from pypdf import PdfReader
from strands import Agent, tool

from .agent import _usage
from .config import SETTINGS, Settings, make_model
from .ingest import image_bytes_for_model, sha256_of
from .store import Store

log = logging.getLogger("closeout.project")
Progress = Callable[[str, dict], None]


def _noop(event: str, data: dict) -> None:
    pass


DISCIPLINES = {
    "AR": "Architectural", "A": "Architectural", "ARCH": "Architectural",
    "ST": "Structural", "S": "Structural",
    "EL": "Electrical", "E": "Electrical", "ELEC": "Electrical",
    "PL": "Plumbing", "P": "Plumbing",
    "ME": "Mechanical", "M": "Mechanical", "MECH": "Mechanical",
    "CV": "Civil", "C": "Civil",
    "LA": "Landscape", "L": "Landscape",
    "FP": "Fire protection", "SP": "Sprinkler",
    "BCH": "BC Hydro", "PM": "Project management", "DC": "Contracts and invoices",
}
DRAWING_CODES = {"AR", "ST", "EL", "PL", "ME", "CV", "LA", "FP", "SP"}
SHEET_MIN_EDGE_PT = 1000          # a page whose long edge is under ~14" is a letter/form, not a drawing sheet
RENDER_WIDTH = 3200               # px, long edge of the stored sheet image
INDEX_TITLE_WORDS = re.compile(r"PLAN|ELEVATION|SECTION|DETAIL|NOTE|SCHEDULE|INDEX|LANDSCAPE|HYDRO|DIAGRAM|CALCULATION|"
                               r"SPECIFICATION|LEGEND|SITE|RENDER|CLOSET|SERVIC|LOAD|ROOF|FLOOR", re.I)


@dataclass
class DocInfo:
    path: Path
    rel_path: str
    discipline: str
    dated: str | None
    pages: int
    kind: str                     # drawing | document
    sha256: str
    size: int
    is_current: bool = False
    mtime: float = 0.0
    document_id: str = ""


@dataclass
class SheetInfo:
    doc: DocInfo
    page: int
    image_path: Path
    text: str
    sheet_number: str = ""
    title: str = ""
    sheet_id: str = ""


@dataclass
class ReadContext:
    store: Store
    run_id: str
    job_id: str
    recorded: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# --- deterministic scan ----------------------------------------------------

def _dated(rel: str) -> str | None:
    """YYMMDD in the file name wins over the folder; the folder is the day it was filed, the file the day it was issued."""
    parts = rel.split("/")
    for cand in [parts[-1]] + parts[:-1][::-1]:
        for m in re.finditer(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", cand):
            yy, mm, dd = (int(x) for x in m.groups())
            if 1 <= mm <= 12 and 1 <= dd <= 31 and 15 <= yy <= 40:
                return f"20{yy:02d}-{mm:02d}-{dd:02d}"
    return None


def _discipline(rel: str) -> str:
    parts = rel.split("/")
    first = parts[0].upper()
    if first in DRAWING_CODES or first in ("BCH", "DC"):
        return first
    for part in parts:  # "PM/DD/241125_EL_Sent for BC Hydro/…", "24-3672_PL_6891_…"
        m = re.search(r"(?:^|[_\-\s])(AR|ST|EL|PL|ME|CV|LA|FP|SP)(?=[_\-\s])", part.upper())
        if m:
            return m.group(1)
    return first if first in DISCIPLINES else "OTHER"


def _page_geometry(path: Path) -> tuple[int, float]:
    reader = PdfReader(str(path))
    n = len(reader.pages)
    box = reader.pages[0].mediabox
    return n, max(float(box.width), float(box.height))


def scan_folder(root: Path) -> list[DocInfo]:
    docs = []
    for p in sorted(root.rglob("*.pdf")):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            pages, long_edge = _page_geometry(p)
        except Exception as e:  # noqa: BLE001 - a broken PDF is reported, not fatal
            log.warning("skipping %s: %s", rel, e)
            continue
        disc = _discipline(str(rel))
        kind = "drawing" if long_edge >= SHEET_MIN_EDGE_PT and disc in DRAWING_CODES else "document"
        docs.append(DocInfo(path=p, rel_path=str(rel), discipline=disc, dated=_dated(str(rel)), pages=pages, kind=kind,
                            sha256=sha256_of(p), size=p.stat().st_size, mtime=p.stat().st_mtime))
    _mark_current(docs)
    return docs


def _mark_current(docs: list[DocInfo]) -> None:
    """Newest drawing set per discipline. Byte-identical copies (the PM 'Sent' folder) count once, discipline folder preferred."""
    best: dict[str, DocInfo] = {}
    for d in docs:
        if d.kind != "drawing":
            continue
        cur = best.get(d.discipline)

        def key(x: DocInfo):
            return (x.dated or "", 0 if x.rel_path.upper().startswith("PM/") else 1, x.mtime)

        if cur is None or key(d) > key(cur):
            best[d.discipline] = d
    for d in best.values():
        d.is_current = True


# --- sheets: render, text, index -------------------------------------------

def _run(cmd: list[str]) -> bytes:
    return subprocess.run(cmd, check=True, capture_output=True).stdout


def page_text(pdf: Path, page: int) -> str:
    try:
        return _run(["pdftotext", "-layout", "-f", str(page), "-l", str(page), str(pdf), "-"]).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - poppler missing: fall back to pypdf
        try:
            return PdfReader(str(pdf)).pages[page - 1].extract_text() or ""
        except Exception:
            return ""


def render_page(pdf: Path, page: int, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["pdftoppm", "-png", "-f", str(page), "-l", str(page), "-scale-to", str(RENDER_WIDTH), "-singlefile",
          str(pdf), str(out.with_suffix(""))])
    return out


def _title_block_crop(image_path: Path) -> tuple[bytes, str]:
    """Bottom-right quarter at full render resolution: where the title block lives on almost every sheet."""
    with Image.open(image_path) as im:
        w, h = im.size
        crop = im.crop((int(w * 0.72), int(h * 0.55), w, h)).convert("RGB")
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "jpeg"


def parse_drawing_index(text: str) -> dict[str, str]:
    """'1    SITE PLAN & NOTES' rows from a sheet that carries a DRAWING INDEX. Provenance: document."""
    if "DRAWING INDEX" not in text.upper() and "SHEET INDEX" not in text.upper() and "LIST OF DRAWINGS" not in text.upper():
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        for m in re.finditer(r"(?:^|\s{3,})([A-Z]{0,2}-?\d{1,3}(?:\.\d+)?[A-Z]?)\s{3,}([A-Za-z][A-Za-z0-9 &,'\-/#.()]{3,}?)\s*$", line):
            num, title = m.group(1), re.sub(r"\s{2,}", " ", m.group(2)).strip()
            if INDEX_TITLE_WORDS.search(title) and num not in out:
                out[num] = title
    return out


def _title_block_fields(pdf: Path, page: int) -> tuple[str, str]:
    """Best-effort sheet number straight from the text layer (document provenance). Empty when unsure.
    Uses the reading-order text: in a title block the label and its value are adjacent there, not in layout mode."""
    try:
        text = _run(["pdftotext", "-f", str(page), "-l", str(page), str(pdf), "-"]).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return "", ""
    for pat in (r"DRAWING\s*#\s*:?\s*\n\s*([A-Z]{1,2}-\d{1,3}[A-Z]?)\s*\n",
                r"SHEET\s*(?:NUMBER|NO\.?|#)\s*:?\s*\n\s*([A-Z]{0,2}-?\d{1,3}[A-Z]?)\s*\n"):
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).upper(), ""
    return "", ""


# --- agent jobs --------------------------------------------------------------

SHEET_SYSTEM = """You read one sheet of a construction drawing set for a consulting engineer's closeout tool.

You get: the whole sheet as an image (for layout), a close-up of its title block, the text layer of the page,
and the set's drawing index if one was found. Record what the sheet SHOWS, using the words printed on it.
Do not guess at anything that is not on the sheet. Do not record people's names, phone numbers or emails;
company names are fine.

Rules:
- sheet_number and title come from the title block; if the drawing index lists this sheet, use the index wording.
- levels: the floors/levels this sheet shows, as printed (e.g. "Main floor", "Second floor", "Roof").
- units: the unit or address labels shown (e.g. "#1 6895 Laurel St", "Unit D").
- spaces: every labelled room or area on the sheet, with its unit and level when the sheet makes that clear.
- elements: building elements, assemblies, systems and site items a field reviewer would later check on site
  (fire-rated walls, guards, stairs, roofing, cladding, service size, panels, EV charging, drainage, parking...).
  Short noun phrases, one per entry, with the value printed if there is one (e.g. "600 A 240 V service").
- levels_and_elevations: printed datum lines like "T/PLATE 292.55'".
- summary: one or two plain sentences saying what the sheet is for.

Call record_sheet exactly once. If a page has no sheet number (a cover or rendering page) use "" and say so in the summary."""

PROJECT_SYSTEM = """You summarise a construction project for a consulting engineer's closeout tool, from what its drawing
sheets show and its document index. Use only what the sheets and index state; do not invent. Never record
people's names, phone numbers or emails; company names and roles are fine.

Call record_project exactly once with:
- name: short project name a field reviewer would use (usually the civic address).
- address, city: from the drawings.
- building_type: e.g. "Six-unit multiple dwelling, three storeys, wood frame".
- description: two or three sentences on what is being built.
- units: one entry per dwelling/tenancy unit with its label, civic address and the levels it spans.
- levels: every floor/level with its printed elevation where given, lowest first. Use ONE canonical name per level
  (e.g. "Main floor", "Second floor", "Third floor", "Roof") and list the other spellings the sheets use in `aliases`.
- parties: companies and their role (architect, electrical engineer, utility, client company).
- key_facts: 5 to 12 short facts a field reviewer must know (fire rating requirements, service size, parking, cladding...).
"""


def _clean_list(v, limit=60) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()][:limit]


def make_sheet_tools(ctx: ReadContext, sheet_id: str):
    @tool
    def record_sheet(sheet_number: str, title: str, sheet_kind: str, summary: str, levels: list[str] | None = None,
                     units: list[str] | None = None, spaces: list[dict] | None = None, elements: list[str] | None = None,
                     levels_and_elevations: list[str] | None = None) -> str:
        """Record what this sheet shows. Call exactly once.

        Args:
            sheet_number: as printed in the title block, e.g. "4" or "EL-02"; "" if the page has none.
            title: drawing title as printed or as listed in the drawing index.
            sheet_kind: site_plan | floor_plan | elevation | section | roof_plan | details | notes | schedule | electrical | plumbing | landscape | rendering | cover | other
            summary: one or two sentences on what the sheet is for.
            levels: floors/levels shown, as printed.
            units: unit or address labels shown.
            spaces: labelled rooms/areas: [{"name": "Kitchen", "unit": "#1 6895 Laurel St", "level": "Main floor"}]
            elements: building elements, systems and site items a field reviewer would check, with printed values.
            levels_and_elevations: printed datum lines, e.g. "T/PLATE 292.55'".
        """
        if not title.strip() and not sheet_number.strip():
            ctx.errors.append("empty")
            return "REJECTED: give a title even when the page has no sheet number."
        if not summary.strip():
            return "REJECTED: summary is required."
        sp = []
        for s in spaces or []:
            if isinstance(s, dict) and str(s.get("name", "")).strip():
                sp.append({"name": str(s["name"]).strip(), "unit": str(s.get("unit") or "").strip(), "level": str(s.get("level") or "").strip()})
        read = {"sheet_kind": sheet_kind.strip().lower(), "summary": summary.strip(), "levels": _clean_list(levels), "units": _clean_list(units),
                "spaces": sp[:200], "elements": _clean_list(elements), "levels_and_elevations": _clean_list(levels_and_elevations),
                "provenance": "model_observation"}
        ctx.store.sheet_read(sheet_id, read, sheet_number=sheet_number.strip().upper(), title=title.strip())
        ctx.recorded.append(sheet_id)
        return "recorded"

    return [record_sheet]


def make_project_tools(ctx: ReadContext, project_id: str, base: dict):
    @tool
    def record_project(name: str, address: str, city: str, building_type: str, description: str, units: list[dict],
                       levels: list[dict], parties: list[dict] | None = None, key_facts: list[str] | None = None) -> str:
        """Record the project summary. Call exactly once.

        Args:
            name: short project name (usually the civic address).
            address: street address as on the drawings.
            city: city.
            building_type: one line, e.g. "Six-unit multiple dwelling, three storeys".
            description: two or three sentences.
            units: [{"label": "Unit A", "address": "#1 6895 Laurel St", "levels": ["Main floor", "Second floor", "Third floor"]}]
            levels: [{"name": "Main floor", "elevation": "262.21'", "aliases": ["T/MAIN", "Main"]}], lowest first.
            parties: [{"role": "Architect", "company": "JOSS Design Inc."}] (companies only).
            key_facts: short facts a field reviewer must know.
        """
        if not name.strip() or not address.strip():
            return "REJECTED: name and address are required."
        model = dict(base)
        model.update({
            "name": name.strip(), "address": address.strip(), "city": city.strip(), "building_type": building_type.strip(),
            "description": description.strip(),
            "units": [{"label": str(u.get("label", "")).strip(), "address": str(u.get("address", "")).strip(),
                       "levels": _clean_list(u.get("levels"))} for u in units if isinstance(u, dict)],
            "levels": [{"name": str(l.get("name", "")).strip(), "elevation": str(l.get("elevation") or "").strip(),
                        "aliases": _clean_list(l.get("aliases"))}
                       for l in levels if isinstance(l, dict) and str(l.get("name", "")).strip()],
            "parties": [{"role": str(p.get("role", "")).strip(), "company": str(p.get("company", "")).strip()}
                        for p in (parties or []) if isinstance(p, dict)],
            "key_facts": _clean_list(key_facts, 20),
            "provenance": "model_observation",
        })
        ctx.store.set_project_model(project_id, model)
        ctx.store.conn.execute("UPDATE projects SET name=? WHERE id=?", (model["name"], project_id))
        ctx.store.conn.commit()
        ctx.recorded.append(project_id)
        return "recorded"

    return [record_project]


def run_sheet_job(store: Store, run_id: str, job_id: str, sheet_id: str, index: dict[str, str], model=None,
                  settings: Settings = SETTINGS) -> dict:
    ctx = ReadContext(store=store, run_id=run_id, job_id=job_id)
    sh = store.sheet(sheet_id)
    agent = Agent(model=model or make_model(settings), tools=make_sheet_tools(ctx, sheet_id), system_prompt=SHEET_SYSTEM,
                  callback_handler=None)
    full, ffmt = image_bytes_for_model(Path(sh["image_path"]))
    crop, cfmt = _title_block_crop(Path(sh["image_path"]))
    text = (sh["text"] or "").strip()
    if len(text) > 24000:
        text = text[:24000] + "\n[... text layer truncated ...]"
    hint = f'Text-layer hint (document provenance): sheet number "{sh["sheet_number"]}".' if sh["sheet_number"] else ""
    content = [
        {"text": f"Discipline {DISCIPLINES.get(sh['discipline'], sh['discipline'])} ({sh['discipline']}), page {sh['page']} of the set. {hint}\n"
                 f"DRAWING INDEX for this set (document provenance): {json.dumps(index) if index else '(none found)'}"},
        {"text": "Whole sheet:"}, {"image": {"format": ffmt, "source": {"bytes": full}}},
        {"text": "Title block close-up (bottom-right of the sheet):"}, {"image": {"format": cfmt, "source": {"bytes": crop}}},
        {"text": "TEXT LAYER (document provenance):\n" + (text or "(no extractable text)")},
        {"text": "Read the sheet and call record_sheet once."},
    ]
    result = agent(content)
    if not ctx.recorded:
        raise RuntimeError("agent finished without recording the sheet" + (f"; last rejection: {ctx.errors[-1]}" if ctx.errors else ""))
    return {"usage": _usage(result)}


def run_project_summary_job(store: Store, run_id: str, job_id: str, project_id: str, model=None,
                            settings: Settings = SETTINGS) -> dict:
    ctx = ReadContext(store=store, run_id=run_id, job_id=job_id)
    prj = store.project(project_id)
    base = {k: prj["model"].get(k) for k in ("disciplines", "drawing_index", "source_root") if k in prj["model"]}
    agent = Agent(model=model or make_model(settings), tools=make_project_tools(ctx, project_id, base),
                  system_prompt=PROJECT_SYSTEM, callback_handler=None)
    lines = ["SHEETS READ (model_observation unless noted):"]
    for sh in store.sheets(project_id):
        r = sh["read"]
        lines.append(f"- [{sh['discipline']}] {sh['sheet_number'] or '(no number)'} {sh['title']}: {r.get('summary', '(not read)')}")
        if r.get("levels"):
            lines.append(f"    levels: {', '.join(r['levels'])}")
        if r.get("units"):
            lines.append(f"    units: {', '.join(r['units'])}")
        if r.get("levels_and_elevations"):
            lines.append(f"    datums: {', '.join(r['levels_and_elevations'])}")
        if r.get("elements"):
            lines.append(f"    elements: {'; '.join(r['elements'][:25])}")
        if r.get("spaces"):
            lines.append(f"    spaces: {', '.join(sorted({s['name'] for s in r['spaces']}))}")
    lines.append("\nDOCUMENT INDEX (document provenance; file names only):")
    for d in store.documents(project_id):
        lines.append(f"- [{d['discipline']}] {d['rel_path']} ({d['pages']} p, {d['dated'] or 'undated'}, {d['kind']}{', current set' if d['is_current'] else ''})")
    result = agent("Summarise this project and call record_project once.\n\n" + "\n".join(lines))
    if not ctx.recorded:
        raise RuntimeError("agent finished without recording the project")
    return {"usage": _usage(result)}


# --- orchestration -----------------------------------------------------------

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "project"


def import_project(store: Store, root: Path, slug: str, settings: Settings = SETTINGS, progress: Progress = _noop,
                   read_with_model: bool = True, workers: int = 3) -> dict:
    """Scan, render and (optionally) read a project folder. Re-importing the same slug replaces its documents and sheets."""
    root = Path(root).resolve()
    docs = scan_folder(root)
    if not docs:
        raise ValueError("no PDF files found in the dropped folder")
    project_id = store.upsert_project(slug, "New project", str(root))
    doc_ids = store.replace_documents(project_id, [d.__dict__ | {"path": str(d.path)} for d in docs])
    for d, did in zip(docs, doc_ids):
        d.document_id = did
    current = [d for d in docs if d.is_current]
    progress("project_scanned", {"project_id": project_id, "documents": len(docs),
                                 "current_sets": [{"discipline": d.discipline, "file": Path(d.rel_path).name, "pages": d.pages, "dated": d.dated} for d in current]})

    # Render + text for every page of every current drawing set.
    sheet_dir = settings.data_dir / "projects" / slug / "sheets"
    sheets: list[SheetInfo] = []
    index: dict[str, str] = {}
    for d in current:
        for page in range(1, d.pages + 1):
            img = sheet_dir / f"{d.discipline}-{d.sha256[:6]}-{page:02d}.png"
            if not img.exists():
                render_page(d.path, page, img)
            text = page_text(d.path, page)
            index.update(parse_drawing_index(text))
            num, title = _title_block_fields(d.path, page)
            sheets.append(SheetInfo(doc=d, page=page, image_path=img, text=text, sheet_number=num, title=title))
            progress("sheet_rendered", {"discipline": d.discipline, "page": page, "of": d.pages})
    for sh in sheets:  # the index is the better source for a number-only title block
        if sh.sheet_number and sh.sheet_number in index and not sh.title:
            sh.title = index[sh.sheet_number]
    ids = store.replace_sheets(project_id, [{"document_id": s.doc.document_id, "page": s.page, "discipline": s.doc.discipline,
                                             "sheet_number": s.sheet_number, "title": s.title, "image_path": str(s.image_path),
                                             "text": s.text} for s in sheets])
    for s, sid in zip(sheets, ids):
        s.sheet_id = sid
    disciplines = [{"code": d.discipline, "name": DISCIPLINES.get(d.discipline, d.discipline), "current_set": Path(d.rel_path).name,
                    "dated": d.dated, "sheets": d.pages} for d in current]
    store.set_project_model(project_id, {"disciplines": disciplines, "drawing_index": index, "source_root": str(root),
                                         "name": "New project", "provenance": "document"})
    progress("sheets_ready", {"sheets": len(sheets), "drawing_index": index})
    if not read_with_model:
        progress("project_ready", {"run_id": None, "project_id": project_id, "name": "New project", "failed_jobs": 0,
                                   "usage": {}, "json": None})
        return {"project_id": project_id, "run_id": None, "sheets": len(sheets), "status": "scanned"}

    run_id = store.create_run(project_id, f"project:{project_id}", settings.model_id, kind="project")
    for s in sheets:
        store.create_job(run_id, "sheet", s.sheet_id)
    store.create_job(run_id, "project_summary", project_id)
    progress("jobs_created", {"run_id": run_id, "sheet_jobs": len(sheets)})
    return continue_project_run(store, run_id, settings, progress, workers=workers)


def continue_project_run(store: Store, run_id: str, settings: Settings = SETTINGS, progress: Progress = _noop, workers: int = 3) -> dict:
    """Run every pending/failed sheet job (in parallel), then the summary. Safe to call again after a failure."""
    run = store.run(run_id)
    project_id = run["project_id"] or run["batch_id"].split(":", 1)[1]
    prj = store.project(project_id)
    index = prj["model"].get("drawing_index", {})
    model = make_model(settings)

    def one(job: dict) -> None:
        sh = store.sheet(job["subject"])
        label = f"{sh['discipline']} p.{sh['page']}"
        progress("job_start", {"job_id": job["id"], "kind": "sheet", "sheet_id": sh["id"], "label": label, "attempt": job["attempts"] + 1})
        store.job_start(job["id"])
        try:
            res = run_sheet_job(store, run_id, job["id"], sh["id"], index, model=model, settings=settings)
            store.job_finish(job["id"], "done", usage=res["usage"])
            sh = store.sheet(sh["id"])
            progress("job_done", {"job_id": job["id"], "kind": "sheet", "sheet_id": sh["id"], "label": label,
                                  "sheet_number": sh["sheet_number"], "title": sh["title"], "usage": res["usage"]})
        except Exception as e:  # noqa: BLE001 - recorded and retryable
            err = f"{type(e).__name__}: {e}"
            log.error("sheet job %s failed: %s\n%s", job["id"], err, traceback.format_exc())
            store.job_finish(job["id"], "failed", error=err[:1000])
            store.sheet_read(job["subject"], {}, status="failed")
            progress("job_failed", {"job_id": job["id"], "kind": "sheet", "label": label, "error": err})

    todo = [j for j in store.jobs(run_id) if j["kind"] == "sheet" and j["status"] != "done"]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, todo))

    for job in store.jobs(run_id):
        if job["kind"] != "project_summary" or job["status"] == "done":
            continue
        progress("job_start", {"job_id": job["id"], "kind": "project_summary", "attempt": job["attempts"] + 1})
        store.job_start(job["id"])
        try:
            res = run_project_summary_job(store, run_id, job["id"], project_id, model=model, settings=settings)
            store.job_finish(job["id"], "done", usage=res["usage"])
            progress("job_done", {"job_id": job["id"], "kind": "project_summary", "name": store.project(project_id)["name"], "usage": res["usage"]})
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            log.error("project summary job %s failed: %s\n%s", job["id"], err, traceback.format_exc())
            store.job_finish(job["id"], "failed", error=err[:1000])
            progress("job_failed", {"job_id": job["id"], "kind": "project_summary", "error": err})

    jobs = store.jobs(run_id)
    failed = [j for j in jobs if j["status"] != "done"]
    usage: dict[str, int] = {}
    for j in jobs:
        for k, v in json.loads(j.get("usage_json") or "{}").items():
            usage[k] = usage.get(k, 0) + int(v)
    store.finish_run(run_id, "failed" if failed else "done", usage)
    _file_the_folder(store, project_id, settings, progress)
    out_dir = settings.data_dir / "projects" / prj["slug"]
    out_dir.mkdir(parents=True, exist_ok=True)
    view = project_view(store, project_id)
    (out_dir / "project.json").write_text(json.dumps(view, indent=2, default=str))
    progress("project_ready", {"run_id": run_id, "project_id": project_id, "name": view["name"], "failed_jobs": len(failed),
                               "usage": usage, "json": str(out_dir / "project.json")})
    return {"run_id": run_id, "project_id": project_id, "status": "failed" if failed else "done",
            "failed_jobs": [j["id"] for j in failed], "usage": usage}


def _file_the_folder(store: Store, project_id: str, settings: Settings, progress: Progress) -> None:
    """The last step of an import: one call over the whole folder files the letters and forms into the building and
    discipline folders and lists what is still missing, so a new project opens already arranged. A failure here is
    recorded and the import still counts as done; the Documents tab's "Check the folder" button does the same thing."""
    from . import documents as documents_mod  # local import: documents.py does not import this module

    view = project_view(store, project_id)
    if not view or not view["sheets"]:
        return
    run_id = store.create_run(project_id, "", settings.model_id, kind="documents")
    progress("job_start", {"job_id": run_id, "kind": "documents", "label": "the folder", "attempt": 1})
    try:
        out = documents_mod.review_documents(store, project_id, view, settings=settings)
    except Exception as e:  # noqa: BLE001 - the import is not failed by this step
        err = f"{type(e).__name__}: {e}"
        log.error("folder check failed: %s\n%s", err, traceback.format_exc())
        store.finish_run(run_id, "failed", {"error": err[:1000]})
        progress("job_failed", {"job_id": run_id, "kind": "documents", "label": "the folder", "error": err})
        return
    store.finish_run(run_id, "done", out["usage"])
    store.set_docs_review(project_id, {**out, "run_id": run_id, "model_id": settings.model_id,
                                       "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    documents_mod.record_placements(store, project_id, out.get("placed") or [])
    progress("job_done", {"job_id": run_id, "kind": "documents", "label": "the folder", "usage": out["usage"],
                          "placed": len(out.get("placed") or []), "missing": len(out.get("missing") or [])})


# --- read model --------------------------------------------------------------

def project_view(store: Store, project_id: str | None = None) -> dict | None:
    """Everything the UI needs about the project: summary, disciplines, sheets with what they show, spaces by unit/level."""
    prj = store.project(project_id)
    if not prj:
        return None
    sheets = store.sheets(prj["id"])
    docs = store.documents(prj["id"])
    spaces: dict[tuple[str, str, str], dict] = {}
    for sh in sheets:
        for s in sh["read"].get("spaces", []):
            key = (s["unit"], s["level"], s["name"].lower())
            entry = spaces.setdefault(key, {"name": s["name"], "unit": s["unit"], "level": s["level"], "sheets": []})
            ref = sh["sheet_number"] or f"{sh['discipline']} p.{sh['page']}"
            if ref not in entry["sheets"]:
                entry["sheets"].append(ref)
    m = prj["model"]
    return {
        "id": prj["id"], "slug": prj["slug"], "name": prj["name"], "address": m.get("address", ""), "city": m.get("city", ""),
        "building_type": m.get("building_type", ""), "description": m.get("description", ""), "units": m.get("units", []),
        "levels": m.get("levels", []), "parties": m.get("parties", []), "key_facts": m.get("key_facts", []),
        "disciplines": m.get("disciplines", []), "drawing_index": m.get("drawing_index", {}),
        "sheets": [{"id": sh["id"], "discipline": sh["discipline"], "discipline_name": DISCIPLINES.get(sh["discipline"], sh["discipline"]),
                    "page": sh["page"], "sheet_number": sh["sheet_number"], "title": sh["title"], "read_status": sh["read_status"],
                    "image_path": sh["image_path"], "read": sh["read"], "views": sh.get("views", [])} for sh in sheets],
        "spaces": sorted(spaces.values(), key=lambda s: (s["unit"], s["level"], s["name"])),
        "documents": [{k: d[k] for k in ("id", "rel_path", "discipline", "dated", "pages", "kind", "is_current")} for d in docs],
        "updated_at": prj["updated_at"],
    }


def sheet_text_for_agent(store: Store, project_id: str | None = None, max_chars: int = 6000) -> str:
    """Compact project context for the evidence-matching agent: units, levels, rooms per sheet."""
    v = project_view(store, project_id)
    if not v:
        return ""
    lines = [f"PROJECT: {v['name']}, {v['address']} {v['city']}. {v['building_type']}".strip()]
    if v["levels"]:
        lines.append("Levels: " + "; ".join(f"{l.get('name', '')} {l.get('elevation', '')}".strip() for l in v["levels"]))
    if v["units"]:
        lines.append("Units: " + "; ".join(f"{u.get('label', '')} = {u.get('address', '')}".strip(" =") for u in v["units"]))
    for sh in v["sheets"]:
        if sh["read"].get("spaces"):
            rooms = sorted({s["name"] for s in sh["read"]["spaces"]})
            lines.append(f"Sheet {sh['sheet_number'] or sh['discipline'] + ' p.' + str(sh['page'])} ({sh['title']}): {', '.join(rooms)}")
    out = "\n".join(lines)
    return out if len(out) <= max_chars else out[:max_chars] + "\n[...]"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import a project folder into Closeout.")
    ap.add_argument("folder")
    ap.add_argument("--slug", default=None)
    ap.add_argument("--no-model", action="store_true", help="scan and render only; no Bedrock calls")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args(argv)
    from .pipeline import open_store
    store = open_store(SETTINGS)
    root = Path(args.folder)
    slug = args.slug or _slug(root.name)

    def progress(event: str, data: dict) -> None:
        print(f"[{event}] {json.dumps(data, default=str)[:300]}", flush=True)

    res = import_project(store, root, slug, SETTINGS, progress, read_with_model=not args.no_model, workers=args.workers)
    print(json.dumps(res, indent=2))
    return 0 if res.get("status") in ("done", "scanned") else 1


if __name__ == "__main__":
    sys.exit(main())

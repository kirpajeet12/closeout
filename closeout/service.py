"""Closeout job service: reads one PunchPilot run through the service door, runs the Strands agents, answers back.

HTTP contract (the same one Amazon Bedrock AgentCore Runtime speaks, so one image serves laptop and cloud):
    GET  /ping          -> {"status": "Healthy"}
    POST /invocations   {"runId": ..., "projectId": ..., "manifest": {...}}   -> 202, the run proceeds in the background

Where the run comes from and goes to is the manifest itself: every file, drawing and reference photo carries an
absolute URL on the PunchPilot service door, and `callbackUrl` is where progress and the result are posted. The
only credential is CLOSEOUT_SERVICE_SECRET, sent as a bearer token; it lives in the environment, never in code.

Three agent jobs, each one model call with narrow tools:
    read   - one drawing page  -> a sheet reading (cached by PunchPilot, so a sheet is read once per project)
    match  - one evidence file -> findings (matched / ambiguous / unrelated / conflict), validated before they count
    draft  - one open item     -> a follow-up request the engineer edits and sends

The deterministic rules (what fills a proof slot, what state an item is in) mirror app/lib/closeout.ts in PunchPilot.
The agent describes; the rules count; the engineer decides.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import mimetypes
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pypdf import PdfReader
from strands import Agent, tool

from .agent import DRAFT_SYSTEM, FORBIDDEN_WORDS, LOCATION_FLAGS, MATCH_SYSTEM, OTHER_FLAGS, PROVENANCE, STATUSES, TIERS, _usage
from .config import SETTINGS, Settings, make_model
from .ingest import _exif, _heic_to_jpeg, image_bytes_for_model
from .project import DISCIPLINES, SHEET_SYSTEM, _clean_list, _title_block_crop, page_text, render_page

log = logging.getLogger("closeout.service")

FILLING_TIERS = {"explicit", "strong"}
NON_FILLING_FLAGS = {"location_unconfirmed", "conflicting_reference"}
# Bedrock list price per million tokens; override when the model changes.
PRICE_IN = float(os.environ.get("CLOSEOUT_PRICE_IN_PER_M", "3"))
PRICE_OUT = float(os.environ.get("CLOSEOUT_PRICE_OUT_PER_M", "15"))
MAX_DRAFTS = int(os.environ.get("CLOSEOUT_MAX_DRAFTS", "12"))
MAX_SHEET_PAGES = int(os.environ.get("CLOSEOUT_MAX_SHEET_PAGES", "4"))
WORK_DIR = Path(os.environ.get("CLOSEOUT_WORK_DIR", str(SETTINGS.data_dir / "service"))).resolve()


# --- the door ----------------------------------------------------------------

class Door:
    """HTTP client for PunchPilot's service door. Bearer = CLOSEOUT_SERVICE_SECRET."""

    def __init__(self, secret: str | None = None, client: httpx.Client | None = None):
        self.secret = secret if secret is not None else os.environ.get("CLOSEOUT_SERVICE_SECRET", "")
        if len(self.secret) < 16:
            raise RuntimeError("CLOSEOUT_SERVICE_SECRET is not set (needs at least 16 characters)")
        self.client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=False)

    @property
    def headers(self) -> dict:
        return {"authorization": f"Bearer {self.secret}"}

    def get_json(self, url: str) -> dict:
        r = self.client.get(url, headers=self.headers)
        if r.status_code != 200:
            raise RuntimeError(f"GET {url.split('?')[0]} answered {r.status_code}: {r.text[:200]}")
        return r.json()

    def get_bytes(self, url: str) -> tuple[bytes, str, str]:
        """Bytes, content type and the filename PunchPilot attached."""
        r = self.client.get(url, headers=self.headers)
        if r.status_code != 200:
            raise RuntimeError(f"GET {url.split('?')[0]} answered {r.status_code}")
        ctype = (r.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
        m = re.search(r'filename="([^"]*)"', r.headers.get("content-disposition") or "")
        return r.content, ctype, (m.group(1) if m else "file")

    def post(self, url: str, payload: dict) -> tuple[int, dict]:
        r = self.client.post(url, headers={**self.headers, "content-type": "application/json"}, content=json.dumps(payload))
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {"error": r.text[:500]}
        return r.status_code, body


# --- the run -----------------------------------------------------------------

@dataclass
class Prepared:
    """One evidence file, fetched and inspected deterministically (file_metadata provenance)."""
    id: str
    filename: str
    kind: str                 # image | pdf | text
    path: Path
    mime: str
    pages: int = 1
    text: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    batch_id: str | None = None
    duplicate_of: str | None = None


@dataclass
class Run:
    run_id: str
    project_id: str
    manifest: dict
    door: Door
    settings: Settings = SETTINGS
    model: object | None = None
    work: Path = WORK_DIR
    findings: list[dict] = field(default_factory=list)      # PunchPilot shape, validated
    readings: list[dict] = field(default_factory=list)      # {drawingId, page, summary}
    drafts: list[dict] = field(default_factory=list)        # {deficiencyId, kind, subject, body}
    usage: dict = field(default_factory=lambda: {"inputTokens": 0, "outputTokens": 0})
    files: dict[str, Prepared] = field(default_factory=dict)
    refs: dict[str, list[Prepared]] = field(default_factory=dict)   # deficiency id -> reference photos
    errors: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    # -- lookups --
    @property
    def items(self) -> list[dict]:
        return self.manifest.get("findings") or []

    def item(self, item_id: str) -> dict | None:
        return next((d for d in self.items if d["id"] == item_id), None)

    @property
    def dir(self) -> Path:
        return self.work / "runs" / _safe(self.run_id)

    def add_usage(self, u: dict) -> None:
        self.usage["inputTokens"] += int(u.get("inputTokens", 0))
        self.usage["outputTokens"] += int(u.get("outputTokens", 0))

    def note(self, msg: str) -> None:
        self.log.append(f"{datetime.now().isoformat(timespec='seconds')} {msg}")
        log.info("%s %s", self.run_id[:8], msg)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:80] or "x"


def _model_for(run: Run):
    if run.model is None:
        run.model = make_model(run.settings)
    return run.model


def _estimate_usd(usage: dict) -> float:
    return round(usage.get("inputTokens", 0) / 1e6 * PRICE_IN + usage.get("outputTokens", 0) / 1e6 * PRICE_OUT, 4)


# --- deterministic text the agents read -------------------------------------

def register_text(run: Run) -> str:
    lines = []
    for d in run.items:
        head = f"{d['id']} | {d.get('title') or ''} | trade: {d.get('trade') or '-'} | location: {d.get('location') or '-'}"
        if d.get("sheetNumber"):
            head += f" | sheet {d['sheetNumber']}"
        if d.get("description"):
            head += f" | {d['description']}"
        lines.append(head)
        if d.get("notes"):
            lines.append(f"    engineer's notes: {d['notes']}")
        filled = set(d.get("filledSlotIds") or [])
        for i, s in enumerate(d.get("proofSlots") or []):
            state = "already filled by an earlier drop" if s["id"] in filled else ("required" if s.get("required", True) else "optional")
            lines.append(f"    slot {i} [{s['kind']}]: {s['description']} ({state})")
        if not d.get("proofSlots"):
            lines.append("    (no proof slots defined; record matches with slot_index -1)")
    return "\n".join(lines) or "(the register is empty)"


def project_text(run: Run, limit: int = 9000) -> str:
    lines = []
    prj = run.manifest.get("project") or {}
    if prj:
        lines.append(f"Project: {prj.get('name') or ''}" + (f" — {prj['location']}" if prj.get("location") else ""))
    fresh = {(r["drawingId"], r["page"]): r["summary"] for r in run.readings}
    for dr in run.manifest.get("drawings") or []:
        where = ""
        fl = dr.get("floor") or {}
        if fl:
            where = " (" + ", ".join(x for x in (fl.get("building"), fl.get("name")) if x) + ")"
        disc = f" [{dr['discipline']}]" if dr.get("discipline") else ""
        reads = {r["page"]: r["summary"] for r in (dr.get("readings") or [])}
        reads.update({p: s for (did, p), s in fresh.items() if did == dr["id"]})
        if not reads:
            lines.append(f"Sheet {dr.get('sheetNumber')} — {dr.get('name')}{disc}{where}: (not read)")
            continue
        for p in sorted(reads):
            lines.append(f"Sheet {dr.get('sheetNumber')} — {dr.get('name')}{disc}{where}, page {p}: {reads[p]}")
    text = "\n".join(lines)
    return text[:limit] + ("\n[... truncated ...]" if len(text) > limit else "") if text else "(no project drawings on file)"


def notes_text(run: Run) -> str:
    parts = []
    for b in run.manifest.get("batches") or []:
        if any(f.batch_id == b["id"] for f in run.files.values()) and (b.get("note") or b.get("contractor")):
            parts.append(f"### Drop \"{b.get('label')}\"" + (f" from {b['contractor']}" if b.get("contractor") else "")
                         + (f"\n{b['note']}" if b.get("note") else ""))
    for f in run.files.values():
        if f.kind == "text" and f.text:
            parts.append(f"### {f.filename}\n{f.text[0][:6000]}")
    return "\n\n".join(parts)


def _capture_time(meta: dict) -> datetime | None:
    raw = meta.get("DateTimeOriginal") or meta.get("DateTime")
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _offset(a: dict, b: dict) -> tuple[float, str]:
    lat = math.radians((a["lat"] + b["lat"]) / 2)
    dx = math.radians(b["lon"] - a["lon"]) * 6371000 * math.cos(lat)
    dy = math.radians(b["lat"] - a["lat"]) * 6371000
    dist = math.hypot(dx, dy)
    deg = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dist, names[int((deg + 22.5) // 45) % 8]


def file_context(run: Run, f: Prepared) -> tuple[str, list[str]]:
    """Capture time, neighbours within 3 minutes (and what they carry), GPS against reference photos."""
    if f.kind != "image":
        return "", []
    meta = f.metadata
    lines = [f"Captured: {meta.get('DateTimeOriginal') or meta.get('DateTime') or 'no capture time in EXIF'}"
             + (f"; camera: {meta.get('Make', '')} {meta.get('Model', '')}".rstrip() if meta.get("Model") else "")]
    neighbours: list[str] = []
    t = _capture_time(meta)
    if t:
        found = {}
        for o in run.files.values():
            if o.id == f.id or o.kind != "image" or o.batch_id != f.batch_id:
                continue
            ot = _capture_time(o.metadata)
            if ot and abs((ot - t).total_seconds()) <= 180:
                found[o] = int((ot - t).total_seconds())
        if found:
            lines.append("Photos taken within 3 minutes of this one:")
            for o, dt in sorted(found.items(), key=lambda kv: abs(kv[1])):
                neighbours.append(o.filename)
                carried = [f"matched {x['deficiencyId']} ({x['tier']})" for x in run.findings
                           if x["fileId"] == o.id and x["status"] == "matched" and x["tier"] in FILLING_TIERS]
                lines.append(f"  - {o.filename}: {'+' if dt >= 0 else ''}{dt} s; carries: "
                             + ("; ".join(carried) if carried else "no confirmed location yet (check its filename/stamp yourself)"))
        else:
            lines.append("No other photo in this drop was taken within 3 minutes of this one.")
    gps = meta.get("gps")
    if gps:
        acc = f" (accuracy ±{gps['accuracy_m']} m)" if gps.get("accuracy_m") else ""
        alt = f", altitude {gps['altitude_m']} m" if gps.get("altitude_m") is not None else ""
        lines.append(f"GPS: {gps['lat']}, {gps['lon']}{acc}{alt}. Relative to each item's reference photo:")
        for d in run.items:
            ref = next((r for r in run.refs.get(d["id"], []) if r.metadata.get("gps")), None)
            if not ref:
                lines.append(f"  - {d['id']}: reference photo has no GPS")
                continue
            rg = ref.metadata["gps"]
            dist, bearing = _offset(rg, gps)
            noise = max(10.0, float(gps.get("accuracy_m") or 0) + float(rg.get("accuracy_m") or 0))
            line = f"  - {d['id']}: {dist:.0f} m {bearing} of its reference photo" + (" (within noise)" if dist <= noise else "")
            if gps.get("altitude_m") is not None and rg.get("altitude_m") is not None:
                dz = gps["altitude_m"] - rg["altitude_m"]
                line += f"; {abs(dz):.1f} m {'higher' if dz > 0 else 'lower'} than it" + (
                    " (within noise)" if abs(dz) < 3 else " (about a floor apart)" if abs(dz) < 6 else " (more than a floor apart)")
            lines.append(line)
    else:
        lines.append("GPS: none in EXIF.")
    return "\n".join(lines), neighbours


# --- item status (mirror of computeItemStatus in app/lib/closeout.ts) --------

def compute_item_status(item: dict, rows: list[dict]) -> dict:
    slots = item.get("proofSlots") or []
    filled: dict[str, list[dict]] = {s["id"]: [] for s in slots}
    for sid in item.get("filledSlotIds") or []:          # filled by an earlier run, as PunchPilot reported it
        if sid in filled:
            filled[sid].append({"prior": True})
    unresolved, supporting = [], []
    for r in rows:
        if r["status"] == "matched" and r["deficiencyId"] == item["id"]:
            blocked = any(fl in NON_FILLING_FLAGS for fl in r["flags"])
            if r["slotId"] and r["slotId"] in filled and r["tier"] in FILLING_TIERS and not blocked:
                filled[r["slotId"]].append(r)
            elif r["slotId"] and r["slotId"] in filled:
                unresolved.append({"finding": r, "kind": "weak_match"})
            else:
                supporting.append(r)
        elif r["status"] in ("ambiguous", "conflict") and (r["deficiencyId"] == item["id"] or item["id"] in r["candidates"]):
            unresolved.append({"finding": r, "kind": r["status"]})
    required = [s for s in slots if s.get("required", True)]
    missing = [s for s in required if not filled[s["id"]]]
    filled_ids = [sid for sid, rs in filled.items() if rs]
    if required and not missing:
        state = "complete"
    elif not required and filled_ids:
        state = "complete"
    elif filled_ids:
        state = "incomplete"
    elif unresolved:
        state = "needs_clarification"
    else:
        state = "no_evidence"
    return {"state": state, "filledSlotIds": filled_ids, "missingSlots": missing, "supporting": supporting, "unresolved": unresolved}


# --- fetch & prepare ---------------------------------------------------------

def _kind(mime: str, name: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        return "pdf"
    if mime.startswith("text/") or name.lower().endswith((".txt", ".md", ".csv")):
        return "text"
    raise ValueError(f"unsupported file type {mime}")


def prepare_file(run: Run, rec: dict) -> Prepared:
    data, ctype, disp_name = run.door.get_bytes(rec["url"])
    name = rec.get("fileName") or disp_name
    mime = rec.get("mimeType") or ctype
    kind = _kind(mime, name)
    ext = Path(name).suffix.lower() or (mimetypes.guess_extension(mime) or "")
    out = run.dir / "files"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{_safe(rec['id'])}{ext}"
    path.write_bytes(data)
    p = Prepared(id=rec["id"], filename=name, kind=kind, path=path, mime=mime, batch_id=rec.get("batchId"), duplicate_of=rec.get("duplicateOfId"))
    p.metadata = {"filename": name, "size": len(data), "sha256": rec.get("sha256")}
    if kind == "image":
        if ext in (".heic", ".heif") or mime in ("image/heic", "image/heif"):
            jpg = path.with_suffix(".jpg")
            _heic_to_jpeg(path, jpg)
            p.path = jpg
        p.metadata.update(_exif(p.path))
    elif kind == "pdf":
        try:
            p.pages = len(PdfReader(str(path)).pages)
        except Exception:  # noqa: BLE001
            p.pages = 1
        p.text = [page_text(path, i) for i in range(1, min(p.pages, 40) + 1)]
    else:
        p.text = [data.decode("utf-8", "replace")]
    return p


def prepare_reference_photos(run: Run) -> None:
    for d in run.items:
        for ph in d.get("referencePhotos") or []:
            try:
                data, ctype, _ = run.door.get_bytes(ph["url"])
            except Exception as e:  # noqa: BLE001 - a missing reference photo is not fatal
                run.note(f"reference photo {ph['id']} for {d['id']} unavailable: {e}")
                continue
            out = run.dir / "refs"
            out.mkdir(parents=True, exist_ok=True)
            ext = Path(ph.get("fileName") or "").suffix.lower() or (mimetypes.guess_extension(ph.get("mimeType") or ctype) or ".jpg")
            path = out / f"{_safe(ph['id'])}{ext}"
            path.write_bytes(data)
            if ext in (".heic", ".heif"):
                jpg = path.with_suffix(".jpg")
                _heic_to_jpeg(path, jpg)
                path = jpg
            p = Prepared(id=ph["id"], filename=ph.get("fileName") or path.name, kind="image", path=path, mime=ph.get("mimeType") or ctype)
            p.metadata = {"kind": ph.get("kind"), **_exif(path)}
            run.refs.setdefault(d["id"], []).append(p)


# --- job 1: read a sheet -----------------------------------------------------

def sheet_summary(read: dict) -> str:
    """One paragraph per page, the form the match agent reads it in."""
    parts = [f"{read.get('sheet_number') or '(no number)'} {read.get('title') or ''} [{read.get('sheet_kind') or 'other'}]. {read.get('summary') or ''}".strip()]
    if read.get("levels"):
        parts.append("Levels: " + "; ".join(read["levels"]) + ".")
    if read.get("units"):
        parts.append("Units: " + "; ".join(read["units"]) + ".")
    if read.get("spaces"):
        parts.append("Spaces: " + "; ".join(
            s["name"] + (" (" + ", ".join(x for x in (s.get("unit"), s.get("level")) if x) + ")" if s.get("unit") or s.get("level") else "")
            for s in read["spaces"]) + ".")
    if read.get("elements"):
        parts.append("Elements: " + "; ".join(read["elements"]) + ".")
    if read.get("levels_and_elevations"):
        parts.append("Datums: " + "; ".join(read["levels_and_elevations"]) + ".")
    return " ".join(parts)


def read_sheet(run: Run, drawing: dict, pdf: Path, page: int) -> dict:
    """Model call: read one page of one drawing. Returns {drawingId, page, summary, usage}."""
    img = render_page(pdf, page, run.dir / "sheets" / f"{_safe(drawing['id'])}-{page}.png")
    recorded: dict = {}

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
            spaces: labelled rooms/areas: [{"name": "Kitchen", "unit": "Unit B", "level": "Main floor"}]
            elements: building elements, systems and site items a field reviewer would check, with printed values.
            levels_and_elevations: printed datum lines, e.g. "T/PLATE 292.55'".
        """
        if not title.strip() and not sheet_number.strip():
            return "REJECTED: give a title even when the page has no sheet number."
        if not summary.strip():
            return "REJECTED: summary is required."
        sp = [{"name": str(s["name"]).strip(), "unit": str(s.get("unit") or "").strip(), "level": str(s.get("level") or "").strip()}
              for s in (spaces or []) if isinstance(s, dict) and str(s.get("name", "")).strip()]
        recorded.update({"sheet_number": sheet_number.strip().upper(), "title": title.strip(), "sheet_kind": sheet_kind.strip().lower(),
                         "summary": summary.strip(), "levels": _clean_list(levels), "units": _clean_list(units), "spaces": sp[:200],
                         "elements": _clean_list(elements), "levels_and_elevations": _clean_list(levels_and_elevations)})
        return "recorded"

    agent = Agent(model=_model_for(run), tools=[record_sheet], system_prompt=SHEET_SYSTEM, callback_handler=None)
    full, ffmt = image_bytes_for_model(img)
    crop, cfmt = _title_block_crop(img)
    text = page_text(pdf, page).strip()
    if len(text) > 24000:
        text = text[:24000] + "\n[... text layer truncated ...]"
    disc = drawing.get("discipline") or ""
    fl = drawing.get("floor") or {}
    content = [
        {"text": f"Discipline {DISCIPLINES.get(disc, disc) or 'unknown'}, sheet {drawing.get('sheetNumber')} \"{drawing.get('name')}\""
                 + (f", filed under {fl.get('building')} / {fl.get('name')}" if fl else "") + f", page {page} of {drawing.get('_pages', '?')}."},
        {"text": "Whole sheet:"}, {"image": {"format": ffmt, "source": {"bytes": full}}},
        {"text": "Title block close-up (bottom-right of the sheet):"}, {"image": {"format": cfmt, "source": {"bytes": crop}}},
        {"text": "TEXT LAYER (document provenance):\n" + (text or "(no extractable text)")},
        {"text": "Read the sheet and call record_sheet once."},
    ]
    result = agent(content)
    if not recorded:
        raise RuntimeError("agent finished without recording the sheet")
    return {"drawingId": drawing["id"], "page": page, "summary": sheet_summary(recorded)[:20000], "usage": _usage(result)}


# --- job 2: match a file -----------------------------------------------------

def validate_finding(run: Run, f: Prepared, neighbours: list[str], item_id, status, tier, slot_index, candidates, flags,
                     rationale, pages, provenance, observations) -> str | None:
    if status not in STATUSES:
        return f"status must be one of {sorted(STATUSES)}"
    if provenance not in PROVENANCE:
        return f"provenance must be one of {sorted(PROVENANCE)}"
    for fl in flags or []:
        if fl not in LOCATION_FLAGS | OTHER_FLAGS:
            return f"unknown flag '{fl}'; use only {sorted(LOCATION_FLAGS | OTHER_FLAGS)}"
    if not rationale or not rationale.strip():
        return "rationale is required"
    if status == "matched":
        d = run.item(item_id or "")
        if not d:
            return "matched findings need a valid item_id from the register"
        if tier not in TIERS:
            return f"matched findings need tier in {sorted(TIERS)}"
        slots = d.get("proofSlots") or []
        if slot_index is not None and slot_index >= 0 and slot_index >= len(slots):
            return f"{item_id} has slots 0..{len(slots) - 1}" if slots else f"{item_id} has no slots; use slot_index -1"
        if "location_unconfirmed" in (flags or []) and tier != "weak":
            return "a finding flagged location_unconfirmed must use tier 'weak' (location is part of what explicit/strong mean)"
        if "location_by_reference" in (flags or []):
            if not run.refs.get(item_id):
                return f"{item_id} has no reference photo; location_by_reference cannot apply"
            if len([o for o in (observations or []) if o.get("text")]) < 2:
                return "location_by_reference needs at least two specific matching features listed in observations"
        if "location_from_sequence" in (flags or []) and not neighbours:
            return "location_from_sequence cannot apply: no other photo was taken within 3 minutes of this one"
        if slot_index is not None and slot_index >= 0:
            kind = slots[slot_index]["kind"]
            if f.kind == "image" and kind in ("document", "test_report"):
                return f"slot {slot_index} of {item_id} is a {kind} slot; a photo cannot fill it (use slot_index -1 for supporting)"
            if f.kind == "pdf" and kind == "photo":
                return f"slot {slot_index} of {item_id} is a photo slot; a PDF cannot fill it (use slot_index -1 for supporting)"
    else:
        if item_id:
            return f"status {status} must not carry an item_id; use candidates instead"
        if status == "ambiguous" and len(candidates or []) < 2:
            return "ambiguous needs at least two candidates"
        if status == "conflict" and not candidates:
            return "conflict needs candidates: the item(s) the file's own reference might have meant"
    for c in candidates or []:
        if not run.item(c):
            return f"candidate {c} is not in the register"
    lowered = (rationale + " " + " ".join(o.get("text", "") for o in observations or [])).lower()
    for w in FORBIDDEN_WORDS:
        if w in lowered:
            return f"do not use the word '{w}'; describe what is present or missing instead"
    for o in observations or []:
        if o.get("provenance") not in PROVENANCE:
            return "every observation needs a provenance"
    return None


def match_file(run: Run, f: Prepared, register: str, notes: str, project: str) -> list[dict]:
    """Model call: what does this one file support? Returns the validated findings it recorded."""
    context, neighbours = file_context(run, f)
    recorded: list[dict] = []
    rejections: list[str] = []

    @tool
    def inspect_evidence(evidence_id: str) -> dict:
        """Open one evidence file. Returns its metadata and extracted text, plus the image itself for photos.

        Args:
            evidence_id: the evidence id to open.
        """
        if evidence_id != f.id:
            return {"status": "error", "content": [{"text": f"only {f.id} is open in this job"}]}
        header = {"evidence_id": f.id, "filename": f.filename, "kind": f.kind, "pages": f.pages, "metadata": f.metadata}
        content = [{"text": "FILE (file_metadata provenance): " + json.dumps(header, default=str)}]
        if f.kind == "image":
            data, fmt = image_bytes_for_model(f.path)
            content.append({"image": {"format": fmt, "source": {"bytes": data}}})
        else:
            for i, page in enumerate(f.text, start=1):
                content.append({"text": f"--- page {i} ---\n{(page or '').strip() or '(no extractable text)'}"[:30000]})
        return {"status": "success", "content": content}

    @tool
    def get_deficiency(item_id: str) -> dict:
        """Return the full register entry for one deficiency, including its evidence slots and the engineer's reference photo.

        Args:
            item_id: the register id, e.g. D-003
        """
        d = run.item(item_id)
        if not d:
            return {"status": "error", "content": [{"text": f"no such item {item_id}"}]}
        entry = {k: d.get(k) for k in ("id", "title", "trade", "location", "sheetNumber", "severity", "description", "notes", "contractor")}
        entry["slots"] = [{"index": i, "kind": s["kind"], "description": s["description"], "required": s.get("required", True)}
                          for i, s in enumerate(d.get("proofSlots") or [])]
        content = [{"text": "REGISTER ENTRY (register provenance): " + json.dumps(entry, default=str)}]
        refs = run.refs.get(item_id, [])
        for r in refs[:2]:
            content.append({"text": f"REFERENCE PHOTO for {item_id} ({r.metadata.get('kind') or 'defect'}): the engineer's own photo of this deficiency "
                                    f"taken at the field review (register provenance). Metadata: {json.dumps({k: v for k, v in r.metadata.items() if k != 'kind'}, default=str)}"})
            data, fmt = image_bytes_for_model(r.path)
            content.append({"image": {"format": fmt, "source": {"bytes": data}}})
        if not refs:
            content.append({"text": f"{item_id} has no reference photo."})
        return {"status": "success", "content": content}

    @tool
    def record_finding(item_id: str | None, status: str, rationale: str, provenance: str, tier: str | None = None,
                       slot_index: int | None = None, candidates: list[str] | None = None, flags: list[str] | None = None,
                       pages: list[int] | None = None, observations: list[dict] | None = None) -> str:
        """Record what the current evidence file supports. Call at least once per file.

        Args:
            item_id: register item the file supports; null unless status is "matched".
            status: matched | ambiguous | unrelated | conflict
            rationale: one or two sentences on why, naming the specific signals used.
            provenance: register | contractor_claim | file_metadata | model_observation (strongest signal tying file to item)
            tier: explicit | strong | weak (required when matched)
            slot_index: which evidence slot of the item this fills; -1 if it supports the item without filling a slot.
            candidates: item ids considered plausible (required for ambiguous and conflict)
            flags: only from location_by_reference, location_from_sequence, location_by_gps, location_unconfirmed, conflicting_reference, no_label_visible, measurement_not_visible, low_quality, partial_view, date_mismatch
            pages: for PDFs, the page numbers that support this finding (1-based)
            observations: list of {"text": ..., "provenance": ...} describing what is present or missing
        """
        err = validate_finding(run, f, neighbours, item_id, status, tier, slot_index, candidates, flags, rationale, pages, provenance, observations)
        if err:
            rejections.append(err)
            return f"REJECTED: {err}. Fix and call record_finding again."
        d = run.item(item_id) if status == "matched" else None
        slot_id = None
        if d and slot_index is not None and slot_index >= 0:
            slot_id = (d.get("proofSlots") or [])[slot_index]["id"]
        obs = [{"text": str(o.get("text", ""))[:500], "provenance": o["provenance"]} for o in (observations or []) if o.get("text")]
        if status == "matched":
            obs.append({"text": f"Strongest tie between file and item: {provenance}", "provenance": provenance})
        srcs = [{"page": int(p)} for p in (pages or []) if int(p) >= 1] or [{"page": 1 if f.kind == "pdf" else None}]
        recorded.append({
            "fileId": f.id, "deficiencyId": item_id if status == "matched" else None, "slotId": slot_id, "status": status,
            "tier": tier if status == "matched" else None, "candidates": list(candidates or ([item_id] if item_id else [])),
            "flags": list(flags or []), "rationale": rationale.strip()[:2000], "sources": srcs, "observations": obs,
        })
        return f"recorded finding {len(recorded)} for {f.id}"

    system = MATCH_SYSTEM.format(register=register, notes=notes or "(none)", project=project,
                                 filenames="\n".join(x.filename for x in run.files.values()))
    agent = Agent(model=_model_for(run), tools=[inspect_evidence, get_deficiency, record_finding], system_prompt=system, callback_handler=None)
    result = agent(
        f"Process evidence_id {f.id} (filename: {f.filename}, kind: {f.kind}). Inspect it, then record your finding(s). "
        f"Finish with one line summarising what you recorded."
        + (f"\n\nFILE CONTEXT (file_metadata provenance):\n{context}" if context else "")
    )
    run.add_usage(_usage(result))
    if not recorded:
        raise RuntimeError("agent finished without recording a finding" + (f"; last rejection: {rejections[-1]}" if rejections else ""))
    return recorded


# --- job 3: draft a follow-up ------------------------------------------------

def item_brief(run: Run, item: dict, st: dict) -> str:
    lines = [f"Item {item['id']} — {item.get('title') or ''}", f"Location: {item.get('location') or '-'}"
             + (f" (sheet {item['sheetNumber']})" if item.get("sheetNumber") else ""), f"Trade: {item.get('trade') or '-'}"]
    if item.get("description"):
        lines.append(f"Description: {item['description']}")
    if item.get("contractor"):
        lines.append(f"Contractor: {item['contractor']}")
    lines.append("Requested evidence:")
    for i, s in enumerate(item.get("proofSlots") or []):
        lines.append(f"  slot {i} [{s['kind']}]: {s['description']}" + ("" if s.get("required", True) else " (optional)"))
    lines.append(f"Completeness: {st['state']}")
    if st["missingSlots"]:
        lines.append("Missing: " + "; ".join(f"[{m['kind']}] {m['description']}" for m in st["missingSlots"]))
    filled = [r for r in run.findings if r["status"] == "matched" and r["deficiencyId"] == item["id"] and r["slotId"] in st["filledSlotIds"]]
    if filled:
        lines.append("Received and filling a slot (contractor-supplied, not verified; do not ask for these again):")
        for r in filled:
            lines.append(f"  - {run.files[r['fileId']].filename} ({r['tier']} match; flags: {', '.join(r['flags']) or 'none'}): {r['rationale']}")
    prior = [sid for sid in st["filledSlotIds"] if not any(r["slotId"] == sid for r in filled)]
    if prior:
        lines.append("Also on file from an earlier drop (do not ask again): " + ", ".join(
            f"[{s['kind']}] {s['description']}" for s in (item.get("proofSlots") or []) if s["id"] in prior))
    if st["supporting"]:
        lines.append("Supporting references only (do not fill any slot):")
        for r in st["supporting"]:
            pages = ", ".join(f"p.{s['page']}" for s in r["sources"] if s.get("page"))
            lines.append(f"  - {run.files[r['fileId']].filename}{' ' + pages if pages else ''}: {r['rationale']}")
    if st["unresolved"]:
        lines.append("Unresolved evidence touching this item:")
        for u in st["unresolved"]:
            r = u["finding"]
            lines.append(f"  - {run.files[r['fileId']].filename}: {u['kind']}; flags {', '.join(r['flags']) or 'none'}. {r['rationale']}")
    return "\n".join(lines)


def draft_item(run: Run, item: dict, st: dict) -> dict:
    """Model call: one follow-up request for one open item."""
    saved: dict = {}

    @tool
    def save_draft(subject: str, body: str) -> str:
        """Save the follow-up draft for the current item. Call exactly once.

        Args:
            subject: email subject line
            body: the message body, plain text
        """
        lowered = (subject + " " + body).lower()
        for w in FORBIDDEN_WORDS:
            if w in lowered:
                return f"REJECTED: do not use the word '{w}'. Rewrite and call save_draft again."
        if not subject.strip() or not body.strip():
            return "REJECTED: subject and body are both required."
        saved.update({"subject": subject.strip()[:200], "body": body.strip()[:8000]})
        return "saved"

    agent = Agent(model=_model_for(run), tools=[save_draft], system_prompt=DRAFT_SYSTEM, callback_handler=None)
    result = agent(f"Draft the follow-up for this item.\n\n{item_brief(run, item, st)}")
    run.add_usage(_usage(result))
    if not saved:
        raise RuntimeError("agent finished without saving a draft")
    kind = "clarification" if st["state"] == "needs_clarification" else "followup"
    return {"deficiencyId": item["id"], "kind": kind, **saved}


# --- the pipeline ------------------------------------------------------------

def _post_status(run: Run, payload: dict) -> tuple[int, dict]:
    url = run.manifest.get("callbackUrl")
    if not url:
        raise RuntimeError("manifest has no callbackUrl")
    return run.door.post(url, {"runId": run.run_id, "modelId": run.settings.model_id, **payload})


def _progress(run: Run, done: int, total: int) -> None:
    code, body = _post_status(run, {"status": "running", "progress": {"done": done, "total": total}})
    if code == 409:
        raise RuntimeError(f"PunchPilot says the run is already finished: {body.get('error')}")
    if code != 200:
        run.note(f"progress update answered {code}: {body}")


def execute(run: Run) -> dict:
    """Run the whole job and report to PunchPilot. Returns the final payload. Never raises past a 'failed' post."""
    run.dir.mkdir(parents=True, exist_ok=True)
    try:
        return _execute(run)
    except Exception as e:  # noqa: BLE001 - whatever broke, the run must not look alive
        run.note(f"FAILED: {e}")
        try:
            _post_status(run, {"status": "failed", "error": str(e)[:1000]})
        except Exception as e2:  # noqa: BLE001
            run.note(f"could not report the failure: {e2}")
        raise
    finally:
        (run.dir / "log.txt").write_text("\n".join(run.log) + "\n")


def _execute(run: Run) -> dict:
    m = run.manifest
    file_recs = m.get("files") or []
    drawings = m.get("drawings") or []
    unread = [d for d in drawings if not d.get("readings")]
    total = len(unread) + len(file_recs)
    done = 0
    run.note(f"run {run.run_id}: {len(file_recs)} files, {len(unread)} of {len(drawings)} sheets to read, {len(run.items)} open items")
    _progress(run, 0, total)

    # 1. sheets nobody has read yet (cached by PunchPilot afterwards)
    for d in unread:
        data, _, _ = run.door.get_bytes(d["url"])
        pdf = run.dir / "sheets" / f"{_safe(d['id'])}.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(data)
        try:
            pages = len(PdfReader(str(pdf)).pages)
        except Exception:  # noqa: BLE001
            pages = 1
        d["_pages"] = pages
        for page in range(1, min(pages, MAX_SHEET_PAGES) + 1):
            r = read_sheet(run, d, pdf, page)
            run.add_usage(r.pop("usage", {}))
            run.readings.append(r)
            run.note(f"read sheet {d.get('sheetNumber')} page {page}")
        done += 1
        _progress(run, done, total)

    # 2. fetch everything the match agent may open
    prepare_reference_photos(run)
    for rec in file_recs:
        run.files[rec["id"]] = prepare_file(run, rec)
    register = register_text(run)
    notes = notes_text(run)
    project = project_text(run)

    # 3. notes are context, recorded without a model call; then one match job per file, oldest capture first
    order = sorted(run.files.values(), key=lambda p: (_capture_time(p.metadata) or datetime.max, p.filename))
    for f in order:
        if f.kind == "text":
            run.findings.append({"fileId": f.id, "deficiencyId": None, "slotId": None, "status": "note", "tier": None, "candidates": [],
                                 "flags": [], "rationale": "Contractor note; used as context for the other files in this drop.",
                                 "sources": [{"page": None}], "observations": [{"text": (f.text[0] if f.text else "")[:500], "provenance": "contractor_claim"}]})
            done += 1
            _progress(run, done, total)
            continue
        twin = run.files.get(f.duplicate_of or "")
        twin_rows = [r for r in run.findings if twin and r["fileId"] == twin.id]
        if twin_rows:
            for r in twin_rows:
                run.findings.append({**r, "fileId": f.id, "rationale": f"Byte-identical to {twin.filename}: " + r["rationale"]})
            run.note(f"{f.filename}: duplicate of {twin.filename}, findings copied")
        else:
            attempt = 0
            while True:
                attempt += 1
                try:
                    rows = match_file(run, f, register, notes, project)
                    break
                except Exception as e:  # noqa: BLE001 - one retry for throttles and empty runs
                    if attempt >= 2:
                        raise RuntimeError(f"{f.filename}: {e}") from e
                    run.note(f"{f.filename}: retrying after: {e}")
                    time.sleep(3)
            run.findings.extend(rows)
            run.note(f"{f.filename}: " + ", ".join(f"{r['status']}" + (f" {r['deficiencyId']} ({r['tier']})" if r["deficiencyId"] else "") for r in rows))
        done += 1
        _progress(run, done, total)

    # 4. one draft per open item this drop touched
    touched = {r["deficiencyId"] for r in run.findings if r["deficiencyId"]} | {c for r in run.findings for c in r["candidates"]}
    for item in run.items:
        if item["id"] not in touched or len(run.drafts) >= MAX_DRAFTS:
            continue
        st = compute_item_status(item, run.findings)
        if st["state"] == "complete":
            continue
        try:
            run.drafts.append(draft_item(run, item, st))
            run.note(f"drafted follow-up for {item['id']} ({st['state']})")
        except Exception as e:  # noqa: BLE001 - a missing draft is not worth failing the run
            run.note(f"draft for {item['id']} skipped: {e}")

    # 5. hand it all back in one piece
    payload = {"status": "complete", "usage": {**run.usage, "estimatedUsd": _estimate_usd(run.usage)},
               "readings": run.readings, "findings": run.findings, "drafts": run.drafts}
    (run.dir / "result.json").write_text(json.dumps(payload, indent=2, default=str))
    code, body = _post_status(run, payload)
    if code != 200:
        raise RuntimeError(f"PunchPilot refused the result ({code}): {json.dumps(body)[:900]}")
    run.note(f"complete: {body.get('recorded')} usage {payload['usage']}")
    return payload


# --- the service -------------------------------------------------------------

_lock = threading.Lock()
_active: dict[str, str] = {}


def _work(run: Run) -> None:
    with _lock:                       # one run at a time per process: Bedrock throttles, and the laptop is small
        _active[run.run_id] = "running"
        try:
            execute(run)
            _active[run.run_id] = "complete"
        except Exception:  # noqa: BLE001 - already reported to PunchPilot and the log
            _active[run.run_id] = "failed"


def start_run(run_id: str, project_id: str, manifest: dict | None, door: Door, app_url: str | None = None, wait: bool = False) -> Run:
    if manifest is None:
        base = (app_url or os.environ.get("CLOSEOUT_APP_URL") or "").rstrip("/")
        if not base:
            raise RuntimeError("no manifest in the request and CLOSEOUT_APP_URL is not set")
        manifest = door.get_json(f"{base}/api/closeout/service?project={project_id}&run={run_id}")
    run = Run(run_id=run_id, project_id=project_id, manifest=manifest, door=door)
    if wait:
        _work(run)
    else:
        threading.Thread(target=_work, args=(run,), daemon=True, name=f"closeout-{run_id[:8]}").start()
    return run


def create_service(door: Door | None = None) -> FastAPI:
    app = FastAPI(title="Closeout agent", version="1.0")
    inbound = os.environ.get("CLOSEOUT_AGENT_TOKEN", "")

    def _door() -> Door:
        return door or Door()

    @app.get("/ping")
    def ping():
        return {"status": "Healthy", "runs": dict(_active)}

    @app.post("/invocations", status_code=202)
    async def invocations(request: Request, authorization: str | None = Header(default=None)):
        if inbound and (authorization or "").strip() != f"Bearer {inbound}":
            raise HTTPException(401, "bad token")
        body = await request.json()
        run_id, project_id = str(body.get("runId") or ""), str(body.get("projectId") or "")
        if not run_id or not project_id:
            raise HTTPException(400, "runId and projectId are required")
        if _active.get(run_id) == "running":
            return {"accepted": False, "runId": run_id, "detail": "already running"}
        try:
            start_run(run_id, project_id, body.get("manifest"), _door())
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from e
        return {"accepted": True, "runId": run_id}

    return app


def poll(app_url: str, interval: float, door: Door | None = None, once: bool = False) -> None:
    """Collect queued runs from PunchPilot when it cannot reach this service (no public address)."""
    door = door or Door()
    base = app_url.rstrip("/")
    while True:
        try:
            runs = door.get_json(f"{base}/api/closeout/service?queued=1").get("runs") or []
            for r in sorted(runs, key=lambda x: x.get("startedAt") or ""):
                if _active.get(r["id"]) in ("running", "complete", "failed"):
                    continue
                log.info("collecting queued run %s", r["id"])
                start_run(r["id"], r["projectId"], None, door, app_url=base, wait=True)
        except Exception as e:  # noqa: BLE001
            log.warning("poll: %s", e)
        if once:
            return
        time.sleep(interval)


app = None  # created lazily by `serve`; tests build their own with create_service()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="closeout.service", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="answer POST /invocations and GET /ping")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    s.add_argument("--host", default="0.0.0.0")
    p = sub.add_parser("poll", help="collect queued runs from PunchPilot")
    p.add_argument("--app-url", default=os.environ.get("CLOSEOUT_APP_URL", "http://localhost:3000"))
    p.add_argument("--interval", type=float, default=20)
    p.add_argument("--once", action="store_true")
    r = sub.add_parser("run", help="process one run now and wait")
    r.add_argument("--app-url", default=os.environ.get("CLOSEOUT_APP_URL", "http://localhost:3000"))
    r.add_argument("--run", required=True)
    r.add_argument("--project", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        import uvicorn

        global app
        app = create_service()
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.cmd == "poll":
        poll(args.app_url, args.interval, once=args.once)
        return 0
    run = start_run(args.run, args.project, None, Door(), app_url=args.app_url, wait=True)
    print(json.dumps({"runId": run.run_id, "status": _active.get(run.run_id), "usage": run.usage,
                      "findings": len(run.findings), "readings": len(run.readings), "drafts": len(run.drafts)}, indent=2))
    return 0 if _active.get(run.run_id) == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())

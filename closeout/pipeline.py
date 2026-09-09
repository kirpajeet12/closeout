"""Batch pipeline: ingest -> match jobs -> completeness -> draft jobs -> packet. Retry-able per job."""
from __future__ import annotations

import json
import logging
import math
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

from .agent import run_draft_job, run_match_job
from .completeness import compute_item_status
from .config import SETTINGS, Settings, make_model
from .ingest import ingest_batch, _exif
from .packet import save_packet
from .project import sheet_text_for_agent
from .register import load_register, register_as_text, Deficiency
from .store import Store

log = logging.getLogger("closeout")
Progress = Callable[[str, dict], None]


def _noop(_event: str, _data: dict) -> None:
    pass


def open_store(settings: Settings = SETTINGS) -> Store:
    return Store(settings.data_dir / "closeout.db")


def import_register(store: Store, project_id: str, csv_path: Path) -> list[Deficiency]:
    items = load_register(csv_path)
    for d in items:
        d.ref_meta = _exif(Path(d.reference_photo)) if d.reference_photo else {}  # type: ignore[attr-defined]
    store.upsert_deficiencies(project_id, items)
    store.touch_project(project_id)
    return items


def _capture_time(meta: dict) -> datetime | None:
    raw = meta.get("DateTimeOriginal") or meta.get("DateTime")
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _location_carried(e: dict, findings_by_evidence: dict) -> str:
    """What location a neighbouring photo carries on its own: filename hint or an explicit/strong finding."""
    hints = []
    for f in findings_by_evidence.get(e["id"], []):
        if f["status"] == "matched" and f["tier"] in ("explicit", "strong"):
            hints.append(f"matched {f['item_id']} ({f['tier']})")
    return "; ".join(hints) if hints else "no confirmed location yet (check its filename/stamp yourself)"


def file_context(store: Store, project_id: str, ev: dict, batch_evidence: list[dict]) -> tuple[str, list[str]]:
    """Deterministic, metadata-only context for one photo: capture time, neighbours within 3 min, GPS vs references."""
    if ev["kind"] != "image":
        return "", []
    meta = ev["metadata"] or {}
    lines = []
    t = _capture_time(meta)
    if meta.get("folder"):
        lines.append(f"Folder the contractor put it in: {meta['folder']} (file_metadata; a folder name is a claim about the file, not a location fact)")
    lines.append(f"Captured: {meta.get('DateTimeOriginal') or meta.get('DateTime') or 'no capture time in EXIF'}"
                 + (f"; camera: {meta.get('Make', '')} {meta.get('Model', '')}".rstrip() if meta.get("Model") else ""))
    neighbours: list[str] = []
    if t:
        found = {}
        for other in batch_evidence:
            if other["id"] == ev["id"] or other["kind"] != "image":
                continue
            ot = _capture_time(other["metadata"] or {})
            if ot and abs((ot - t).total_seconds()) <= 180:
                found[other["filename"]] = int((ot - t).total_seconds())
        if found:
            fb = {}
            for other in batch_evidence:
                fb[other["id"]] = store.findings_for_evidence(other["id"])
            by_name = {o["filename"]: o for o in batch_evidence}
            lines.append("Photos taken within 3 minutes of this one:")
            for name, dt in sorted(found.items(), key=lambda kv: abs(kv[1])):
                neighbours.append(name)
                lines.append(f"  - {name}: {'+' if dt >= 0 else ''}{dt} s; carries: {_location_carried(by_name[name], fb)}")
        else:
            lines.append("No other photo in this batch was taken within 3 minutes of this one.")
    gps = meta.get("gps")
    if gps:
        acc = f" (accuracy ±{gps['accuracy_m']} m)" if gps.get("accuracy_m") else ""
        alt = f", altitude {gps['altitude_m']} m" if gps.get("altitude_m") is not None else ""
        # horizontal noise: the phone's own accuracy figures for both photos, never below 10 m
        lines.append(f"GPS: {gps['lat']}, {gps['lon']}{acc}{alt}. Relative to each item's reference photo:")
        for d in store.deficiencies(project_id):
            rg = (d.get("ref_meta") or {}).get("gps")
            if not rg:
                lines.append(f"  - {d['item_id']}: reference photo has no GPS")
                continue
            dist, bearing = _offset(rg, gps)
            noise = max(10.0, float(gps.get("accuracy_m") or 0) + float(rg.get("accuracy_m") or 0))
            line = f"  - {d['item_id']}: {dist:.0f} m {bearing} of its reference photo" + (" (within noise)" if dist <= noise else "")
            if gps.get("altitude_m") is not None and rg.get("altitude_m") is not None:
                dz = gps["altitude_m"] - rg["altitude_m"]
                line += f"; {abs(dz):.1f} m {'higher' if dz > 0 else 'lower'} than it" + (" (within noise)" if abs(dz) < 3 else " (about a floor apart)" if abs(dz) < 6 else " (more than a floor apart)")
            lines.append(line)
    else:
        lines.append("GPS: none in EXIF.")
    return "\n".join(lines), neighbours


def _offset(a: dict, b: dict) -> tuple[float, str]:
    """Metres and compass direction from point a to point b (equirectangular; fine at site scale)."""
    lat = math.radians((a["lat"] + b["lat"]) / 2)
    dx = math.radians(b["lon"] - a["lon"]) * 6371000 * math.cos(lat)
    dy = math.radians(b["lat"] - a["lat"]) * 6371000
    dist = math.hypot(dx, dy)
    deg = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dist, names[int((deg + 22.5) // 45) % 8]


def _register_text(store: Store, project_id: str) -> str:
    lines = []
    for d in store.deficiencies(project_id):
        lines.append(f"{d['item_id']} | location: {d['location']}" + (f" | sheet {d['sheet']}" if d.get("sheet") else "")
                     + f" | {d['description']}")
        for s in d["slots"]:
            lines.append(f"    slot {s['index']} [{s['type']}]: {s['description']}")
    return "\n".join(lines)


def _notes_text(evidence: list[dict]) -> str:
    parts = []
    for e in evidence:
        if e["kind"] == "text":
            parts.append(f"### {e['filename']}\n{e['text'][0]}")
    return "\n\n".join(parts)


def _sum_usage(jobs: list[dict]) -> dict:
    total: dict[str, int] = {}
    for j in jobs:
        for k, v in (json.loads(j.get("usage_json") or "{}")).items():
            total[k] = total.get(k, 0) + int(v)
    return total


def process_batch(store: Store, project_id: str, files: list[Path], label: str, settings: Settings = SETTINGS,
                  progress: Progress = _noop, reprocess_all: bool = False, root: Path | None = None) -> dict:
    """Ingest a batch into a project and run the agent over it. Returns a summary with run_id and packet paths."""
    ingest = ingest_batch(store, project_id, files, label, settings.data_dir / "evidence", root=root)
    progress("ingested", {"batch_id": ingest.batch_id, "new": len(ingest.new), "existing": len(ingest.existing),
                          "duplicates": ingest.duplicates_in_batch, "rejected": ingest.rejected})

    run_id = store.create_run(project_id, ingest.batch_id, settings.model_id)
    batch_evidence = store.batch_evidence(ingest.batch_id)
    # Notes are context for the agent, recorded as findings without a model call.
    for e in batch_evidence:
        if e["kind"] == "text":
            store.add_finding(run_id=run_id, evidence_id=e["id"], status="note", provenance="contractor_claim",
                              rationale="Contractor note; used as context for other files in this batch.",
                              sources=[{"evidence_id": e["id"], "page": None}])
    # Match jobs for everything that needs the model. Re-runs of already-processed evidence are allowed
    # (that is how reprocessing works); existing findings stay in history.
    to_process = [e for e in batch_evidence if e["kind"] != "text"]
    if not reprocess_all:
        to_process = [e for e in to_process if e in ingest.new or not store.findings_for_evidence(e["id"])]
    for e in to_process:
        store.create_job(run_id, "match", e["id"])
    progress("jobs_created", {"run_id": run_id, "match_jobs": len(to_process)})
    return continue_run(store, run_id, settings, progress)


def continue_run(store: Store, run_id: str, settings: Settings = SETTINGS, progress: Progress = _noop) -> dict:
    """Run every pending/failed job in the run, then completeness, drafts, packet. Safe to call again after a failure."""
    run = store.run(run_id)
    pid = run["project_id"]
    batch_evidence = store.batch_evidence(run["batch_id"])
    register_text = _register_text(store, pid)
    notes_text = _notes_text(batch_evidence)
    filenames = [e["filename"] for e in batch_evidence]
    project_text = sheet_text_for_agent(store, pid)   # "" when the project has no drawings yet
    model = make_model(settings)

    for job in store.jobs(run_id):
        if job["kind"] != "match" or job["status"] == "done":
            continue
        ev = store.evidence(job["subject"])
        progress("job_start", {"job_id": job["id"], "kind": "match", "filename": ev["filename"], "attempt": job["attempts"] + 1})
        store.job_start(job["id"])
        store.delete_findings(job["id"])  # a retry replaces its own partial output only
        try:
            fctx, neighbours = file_context(store, pid, ev, batch_evidence)
            res = run_match_job(store, run_id, job["id"], ev["id"], register_text, notes_text, filenames, model=model,
                                file_context=fctx, neighbours=neighbours, project_text=project_text)
            store.job_finish(job["id"], "done", usage=res["usage"])
            progress("job_done", {"job_id": job["id"], "filename": ev["filename"], "findings": res["findings"], "usage": res["usage"]})
        except Exception as e:  # noqa: BLE001 - we want every failure recorded and retryable
            err = f"{type(e).__name__}: {e}"
            log.error("match job %s failed: %s\n%s", job["id"], err, traceback.format_exc())
            store.job_finish(job["id"], "failed", error=err[:1000])
            progress("job_failed", {"job_id": job["id"], "filename": ev["filename"], "error": err})

    # Completeness over the current findings of ALL evidence, snapshot per run (history).
    findings = store.current_findings(pid)
    statuses = {}
    for d in store.deficiencies(pid):
        st = compute_item_status(d, findings)
        statuses[d["item_id"]] = st
        store.add_item_status(run_id, d["item_id"], st["completeness"], st["missing_slots"], st["filled_slots"], st["unresolved"])
    progress("completeness", {k: v["completeness"] for k, v in statuses.items()})

    # Drafts for anything not complete.
    existing_draft_items = {d["item_id"] for d in store.drafts_for_run(run_id)}
    for item_id, st in statuses.items():
        if st["completeness"] == "complete" or item_id in existing_draft_items:
            continue
        if not any(j["kind"] == "draft" and j["subject"] == item_id for j in store.jobs(run_id)):
            store.create_job(run_id, "draft", item_id)
    for job in store.jobs(run_id):
        if job["kind"] != "draft" or job["status"] == "done":
            continue
        item_id = job["subject"]
        brief = _item_brief(store, pid, item_id, statuses[item_id], findings)
        progress("job_start", {"job_id": job["id"], "kind": "draft", "item_id": item_id, "attempt": job["attempts"] + 1})
        store.job_start(job["id"])
        try:
            res = run_draft_job(store, run_id, job["id"], item_id, brief, model=model)
            store.job_finish(job["id"], "done", usage=res["usage"])
            progress("job_done", {"job_id": job["id"], "item_id": item_id, "draft_id": res["draft_id"], "usage": res["usage"]})
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            log.error("draft job %s failed: %s\n%s", job["id"], err, traceback.format_exc())
            store.job_finish(job["id"], "failed", error=err[:1000])
            progress("job_failed", {"job_id": job["id"], "item_id": item_id, "error": err})

    jobs = store.jobs(run_id)
    failed = [j for j in jobs if j["status"] != "done"]
    usage = _sum_usage(jobs)
    store.finish_run(run_id, "failed" if failed else "done", usage)
    store.touch_project(pid)
    pj, pm = save_packet(store, run_id, settings.data_dir / "runs" / run_id)
    progress("packet", {"json": str(pj), "markdown": str(pm), "failed_jobs": len(failed), "usage": usage})
    return {"run_id": run_id, "batch_id": run["batch_id"], "status": "failed" if failed else "done",
            "failed_jobs": [j["id"] for j in failed], "packet_json": str(pj), "packet_md": str(pm), "usage": usage}


def _item_brief(store: Store, project_id: str, item_id: str, st: dict, findings: list[dict]) -> str:
    d = store.deficiency(project_id, item_id)
    lines = [f"Item {d['item_id']} — {d['description']}", f"Location: {d['location']}",
             "Requested evidence:"]
    for s in d["slots"]:
        lines.append(f"  slot {s['index']} [{s['type']}]: {s['description']}")
    lines.append(f"Completeness: {st['completeness']}")
    if st["missing_slots"]:
        lines.append("Missing: " + "; ".join(f"[{m['type']}] {m['description']}" for m in st["missing_slots"]))
    filled_ids = {fid for fs in st["filled_slots"] for fid in fs["finding_ids"]}
    linked = [f for f in findings if f["status"] == "matched" and f["item_id"] == item_id]
    filled = [f for f in linked if f["id"] in filled_ids]
    supporting = [f for f in linked if f["id"] in set(st["supporting"])]
    if filled:
        lines.append("Received and filling a slot (contractor-supplied, not verified; do not ask for these again):")
        for f in filled:
            ev = store.evidence(f["evidence_id"])
            lines.append(f"  - slot {f['slot_index']}: {ev['filename']} ({f['tier']} match, {f['provenance']}; flags: {', '.join(f['flags']) or 'none'}): {f['rationale']}")
    if supporting:
        lines.append("Supporting references only (do not fill any slot):")
        for f in supporting:
            ev = store.evidence(f["evidence_id"])
            pages = ", ".join(f"p.{s['page']}" for s in f["sources"] if s.get("page"))
            lines.append(f"  - {ev['filename']}{' ' + pages if pages else ''} ({f['provenance']}): {f['rationale']}")
    if st["unresolved"]:
        lines.append("Unresolved evidence touching this item:")
        for u in st["unresolved"]:
            f = next((x for x in findings if x["id"] == u["finding_id"]), None)
            if f:
                ev = store.evidence(f["evidence_id"])
                lines.append(f"  - {ev['filename']}: {u['kind']}; flags {', '.join(f['flags']) or 'none'}. {f['rationale']}")
    return "\n".join(lines)

"""Closeout API: one FastAPI app over the pipeline. Runs execute on a worker thread and stream progress as SSE.

    uvicorn closeout.api:app --reload

Everything hangs off a project: /api/projects/{slug}/... Nothing here calls the model directly; it only drives
`pipeline.process_batch` / `continue_run` / `project.import_project` and reads the store.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import re
import shutil
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import ask as ask_mod, documents as documents_mod, pipeline, plans as plans_mod, project as project_mod, review as review_mod
from .config import SETTINGS, Settings
from .ingest import _exif, _heic_to_jpeg
from .packet import build_packet, packet_markdown
from .store import Store

log = logging.getLogger("closeout.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TERMINAL_EVENTS = {"packet", "project_ready", "run_error"}


class RunFeed:
    """Progress events of one run, kept in memory so late subscribers replay the history."""

    def __init__(self, run_id: str | None = None):
        self.run_id = run_id
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.done = False

    def push(self, event: str, data: dict) -> None:
        with self.cond:
            self.events.append({"event": event, "data": data, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            if event in TERMINAL_EVENTS:
                self.done = True
            self.cond.notify_all()

    def stream(self, start: int = 0) -> Iterator[dict]:
        i = start
        while True:
            with self.cond:
                while i >= len(self.events) and not self.done:
                    self.cond.wait(timeout=15)
                    if i >= len(self.events) and not self.done:
                        yield {"event": "ping", "data": {}}
                if i < len(self.events):
                    ev = self.events[i]
                    i += 1
                else:
                    return
            yield ev


class DraftPatch(BaseModel):
    subject: str | None = None
    body: str | None = None


class Decision(BaseModel):
    decision: str
    note: str | None = None


class NewReview(BaseModel):
    discipline: str
    title: str = ""


class SheetView(BaseModel):
    title: str = ""
    level: str
    x: float
    y: float
    w: float
    h: float
    source: str = "engineer"


class SheetViews(BaseModel):
    views: list[SheetView]


class FindingPatch(BaseModel):
    location: str | None = None
    description: str | None = None
    evidence_required: str | None = None
    unit: str | None = None
    level: str | None = None
    space: str | None = None
    note: str | None = None
    pin_x: float | None = None
    pin_y: float | None = None


class AskBody(BaseModel):
    question: str
    where: dict | None = None
    history: list[dict] = []   # earlier turns of the same conversation, {"q": ..., "a": ...}
    spoken: bool = False       # the answer will be read aloud: keep it short and natural


class SpeakBody(BaseModel):
    text: str


class LiveBody(BaseModel):
    sdp: str   # the browser's WebRTC offer; the answer comes back the same way


class DocsReviewIn(BaseModel):
    already: list[str] = []


class DocsAnswerIn(BaseModel):
    index: int
    answer: str = ""


class DocsScopeIn(BaseModel):
    name: str             # exact checklist row name
    in_scope: bool = True      # the gaps the web app's rules already show, so the agent does not repeat them


class NewProject(BaseModel):
    name: str
    address: str | None = None


def _sorted_files(root: Path) -> list[Path]:
    return sorted((p for p in root.rglob("*")
                   if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)),
                  key=lambda p: (str(p.parent.relative_to(root)).lower(),
                                 re.sub(r" \(\d+\)$", "", p.stem).lower(), len(p.name), p.name))


def _safe_relpath(name: str) -> Path:
    parts = [p for p in re.split(r"[\\/]+", name) if p not in ("", ".", "..")]
    if not parts:
        raise HTTPException(400, f"bad file name: {name!r}")
    return Path(*parts)


ZIP_MAX_MEMBERS = 20_000
ZIP_MAX_BYTES = 6 * 1024 ** 3        # unpacked; a whole project folder with photos fits, a zip bomb does not


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    """Windows zips store names in cp437 unless the UTF-8 flag is set; fix the mojibake when it decodes cleanly."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _unpack_zips(root: Path, rel: list[str]) -> list[str]:
    """Any .zip in the upload is unpacked where it sits and removed; its members take its place in the path list.

    Members are written only under the zip's own folder (no absolute paths, no '..'), Finder's __MACOSX copies and
    dot-files are skipped, and the unpacked size is capped."""
    out: list[str] = []
    for name in rel:
        path = root / _safe_relpath(name)
        if not name.lower().endswith(".zip") or not zipfile.is_zipfile(path):
            out.append(name)
            continue
        base = path.parent
        prefix = str(path.parent.relative_to(root)).replace("\\", "/")
        prefix = "" if prefix == "." else prefix + "/"
        total = 0
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > ZIP_MAX_MEMBERS:
                raise HTTPException(400, f"{Path(name).name} holds more than {ZIP_MAX_MEMBERS} files")
            for info in infos:
                if info.is_dir():
                    continue
                parts = [p for p in re.split(r"[\\/]+", _zip_member_name(info)) if p not in ("", ".", "..")]
                if not parts or parts[0] == "__MACOSX" or any(p.startswith(".") for p in parts):
                    continue
                total += info.file_size
                if total > ZIP_MAX_BYTES:
                    raise HTTPException(400, f"{Path(name).name} unpacks to more than {ZIP_MAX_BYTES // 1024 ** 3} GB")
                dest = base.joinpath(*parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                out.append(prefix + "/".join(parts))
        path.unlink()
    return out


def _office_name(s: str) -> str:
    """The office names project folders '<job no>_<disciplines>_<address>_<city code>_<contact>_<phone>'.
    A project's web address must never carry the contact's name or phone, so keep only job number, address and city."""
    parts = [x.strip() for x in s.split("_")]
    if len(parts) < 3 or not re.match(r"^\d{2}-\d{4}$", parts[0]):
        return s
    keep = [parts[0]]
    for x in parts[2:]:
        if re.search(r"\d[\d\s().-]{6,}\d", x):      # a phone number: stop here
            break
        keep.append(x)
        if re.match(r"^[A-Z]{3}$", x):                # the city code ends the address
            break
    return " ".join(keep)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _office_name(s or "project").lower()).strip("-") or "project"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def project_card(st: Store, prj: dict, active_run_id: str | None) -> dict:
    """What the projects home shows per project: name, address, sheets, how many items are ready to close."""
    pid = prj["id"]
    items = st.deficiencies(pid)
    batch_runs = st.runs(pid, kind="batch")
    latest = batch_runs[-1] if batch_runs else None
    status = st.item_status_for_run(latest["id"]) if latest else {}
    n = {"complete": 0, "incomplete": 0, "needs_clarification": 0, "no_evidence": 0}
    for d in items:
        n[(status.get(d["item_id"]) or {}).get("completeness", "no_evidence")] += 1
    decisions = {d["item_id"]: d["decision"] for d in st.decisions(pid)}
    m = prj["model"]
    runs = st.runs(pid)
    reviews, drafts, batches = st.reviews(pid), st.all_drafts(pid), st.batches(pid)
    return {
        "id": pid, "slug": prj["slug"], "name": prj["name"], "address": m.get("address", ""), "city": m.get("city", ""),
        "building_type": m.get("building_type", ""), "sheets": len(st.sheets(pid)), "documents": len(st.documents(pid)),
        "items": len(items), "ready": n["complete"], "needs": n["incomplete"], "unclear": n["needs_clarification"], "nothing": n["no_evidence"],
        "closed": sum(1 for v in decisions.values() if v == "accept"),
        "drops": len(st.batches(pid)), "last_activity": prj["updated_at"],
        "units": len(m.get("units") or []), "disciplines": sorted({s["discipline"] for s in st.sheets(pid) if s.get("discipline")}),
        "reviews_active": sum(1 for r in reviews if r["status"] == "active"),
        "reviews_finished": sum(1 for r in reviews if r["status"] == "finished"),
        "messages": len(drafts), "last_message_at": max((d["updated_at"] or d["created_at"] for d in drafts), default=None),
        "links": sum(1 for sh in st.shares(pid) if not sh["revoked_at"]),
        "last_drop_at": batches[-1]["created_at"] if batches else None,
        "from_contractor": sum(1 for b in batches if b.get("via")),
        "latest_run": {k: latest[k] for k in ("id", "status", "started_at", "finished_at")} if latest else None,
        "active": bool(active_run_id) and any(r["id"] == active_run_id for r in runs),
        "created_at": prj["created_at"],
    }


SIGNIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Closeout</title>
<style>
  :root { --ink:#151412; --mute:#6f6a60; --line:#e6e1d6; --bg:#f6f4ee; }
  * { box-sizing:border-box } body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Inter","Helvetica Neue",Arial,sans-serif; }
  form { width:min(360px,calc(100vw - 40px)); background:#fff; border:1px solid var(--line); border-radius:20px; padding:32px 28px 28px; box-shadow:0 20px 60px rgba(20,18,10,.08) }
  .wordmark { font-weight:800; letter-spacing:.22em; font-size:12px } h1 { font-size:22px; margin:18px 0 6px; letter-spacing:-.01em }
  p { margin:0 0 20px; color:var(--mute); font-size:14px; line-height:1.45 }
  input { width:100%; font-size:17px; padding:13px 14px; border:1px solid var(--line); border-radius:12px; outline:none; background:#fbfaf7 }
  input:focus { border-color:var(--ink) } button { margin-top:12px; width:100%; padding:13px; font-size:16px; font-weight:600; border:0; border-radius:12px; background:var(--ink); color:#fff; cursor:pointer }
  .bad { color:#a13d2d; font-size:13px; margin:10px 0 0 } .foot { margin-top:18px; font-size:12px; color:var(--mute) }
</style></head><body>
<form method="post" action="/signin"><div class="wordmark">CLOSEOUT</div><h1>Office access</h1><p>Enter the office code to open the projects. Contractor links open without it.</p>
<input type="password" name="code" autocomplete="current-password" autofocus placeholder="Office code" required>__BAD__<button type="submit">Open</button>
<div class="foot">Closeout keeps records and prepares reviews; the engineer decides.</div></form></body></html>"""


def create_app(settings: Settings = SETTINGS) -> FastAPI:
    app = FastAPI(title="Closeout", version="0.3")
    feeds: dict[str, RunFeed] = {}
    pending: list[RunFeed] = []  # feeds whose run_id is not known yet (ingest still running)
    lock = threading.Lock()
    state = {"active": None}  # run_id of the run currently executing, if any

    def store() -> Store:
        return pipeline.open_store(settings)

    def _project(st: Store, slug: str) -> dict:
        prj = st.project_by_slug(slug)
        if not prj:
            raise HTTPException(404, f"no project '{slug}'")
        return prj

    def _feed_for(run_id: str) -> RunFeed | None:
        with lock:
            return feeds.get(run_id)

    def _reserve() -> RunFeed:
        with lock:
            if state["active"] or pending:
                raise HTTPException(409, "a run is already in progress")
            feed = RunFeed()
            pending.append(feed)
            return feed

    def _release(feed: RunFeed) -> None:
        with lock:  # never leave a dead pending feed blocking the desk
            if feed in pending:
                pending.remove(feed)

    def _launch(target, feed: RunFeed) -> None:
        def progress(event: str, data: dict) -> None:
            if "run_id" in data and feed.run_id is None:
                feed.run_id = data["run_id"]
                with lock:
                    feeds[feed.run_id] = feed
                    if feed in pending:
                        pending.remove(feed)
                    state["active"] = feed.run_id
            feed.push(event, data)

        def body() -> None:
            try:
                target(progress)
            except Exception as e:  # noqa: BLE001 - surfaced to the UI, never swallowed
                feed.push("run_error", {"error": f"{type(e).__name__}: {e}"})
            finally:
                with lock:
                    if state["active"] == feed.run_id:
                        state["active"] = None
                    if feed in pending:
                        pending.remove(feed)
                if not feed.done:
                    feed.push("run_error", {"error": "run ended without a packet"})

        threading.Thread(target=body, name="closeout-run", daemon=True).start()

    async def _save_upload(files: list[UploadFile], paths: list[str] | None, folder: str) -> tuple[Path, list[str]]:
        """Write the upload to disk as sent, streamed (a whole project zip must not sit in memory), then unpack any zip in place."""
        root = settings.data_dir / "uploads" / folder
        root.mkdir(parents=True, exist_ok=True)
        rel = paths if paths and len(paths) == len(files) else [f.filename or "file" for f in files]
        for f, name in zip(files, rel):
            dest = root / _safe_relpath(name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            await f.seek(0)
            with dest.open("wb") as out:
                shutil.copyfileobj(f.file, out, 1024 * 1024)
        return root, _unpack_zips(root, rel)

    # --- projects -------------------------------------------------------
    @app.get("/api/projects")
    def projects() -> dict:
        st = store()
        with lock:
            active = state["active"]
        return {"model_id": settings.model_id, "active_run_id": active,
                "projects": [project_card(st, p, active) for p in st.projects()]}

    @app.post("/api/projects/blank")
    def new_project(body: NewProject) -> dict:
        """A project started by name only; drawings, list and evidence come later."""
        st = store()
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "give the project a name")
        slug = _slug(name)
        if st.project_by_slug(slug):
            raise HTTPException(409, f"a project called '{name}' already exists")
        pid = st.upsert_project(slug, name, "", {"address": (body.address or "").strip(), "provenance": "user"})
        return {"id": pid, "slug": slug, "name": name}

    async def _drawings_drop(slug: str | None, files: list[UploadFile], paths: list[str] | None, read_with_model: bool) -> dict:
        """A project folder is dropped as-is: the drawing sets, letters and forms in their sub-folders."""
        feed = _reserve()
        try:
            zips = [Path(f.filename or "").stem for f in files if (f.filename or "").lower().endswith(".zip")]
            root, rel = await _save_upload(files, paths, f"project-{_stamp()}")
            if not rel:
                raise HTTPException(400, "the upload held no files (an empty zip?)")
            top = {_safe_relpath(n).parts[0] for n in rel}
            # the browser sends "<dropped folder>/<sub path>" and a zip usually wraps one folder: that folder is the project root
            if len(top) == 1 and (root / next(iter(top))).is_dir():
                root = root / next(iter(top))
            slug_v = _slug(slug or (next(iter(top)) if len(top) == 1 else (zips[0] if len(zips) == 1 else "project")))
        except Exception:
            _release(feed)
            raise

        def target(progress):
            project_mod.import_project(store(), root, slug_v, settings, progress=progress, read_with_model=read_with_model)

        feed.push("uploaded", {"label": f"project {slug_v}", "files": len(rel), "folder": str(root), "slug": slug_v})
        _launch(target, feed)
        return {"slug": slug_v, "files": len(rel), "feed": "/api/runs/pending/events"}

    @app.post("/api/projects")
    async def upload_project(files: list[UploadFile] = File(...), paths: list[str] | None = Form(None),
                             slug: str | None = Form(None), read_with_model: bool = Form(True)) -> dict:
        """New project from a dropped folder. The folder name becomes the project until the agent reads its title block."""
        return await _drawings_drop(slug, files, paths, read_with_model)

    @app.post("/api/projects/{slug}/drawings")
    async def upload_drawings(slug: str, files: list[UploadFile] = File(...), paths: list[str] | None = Form(None),
                              read_with_model: bool = Form(True)) -> dict:
        """Drop (or re-drop) the drawings folder into an existing project."""
        _project(store(), slug)
        return await _drawings_drop(slug, files, paths, read_with_model)

    @app.get("/api/projects/{slug}")
    def project_detail(slug: str) -> dict:
        """Everything one project page needs: drawings, deficiency list, latest packet, drops, messages."""
        st = store()
        prj = _project(st, slug)
        pid = prj["id"]
        view = project_mod.project_view(st, pid) or {"id": pid, "slug": prj["slug"], "name": prj["name"], "sheets": [], "units": [],
                                                    "levels": [], "spaces": [], "documents": [], "disciplines": [], "address": "", "city": ""}
        for sh in view["sheets"]:
            sh["image_url"] = f"/api/sheets/{sh['id']}/image"
            sh["thumb_url"] = f"/api/sheets/{sh['id']}/thumb"
            sh.pop("image_path", None)
        batch_runs = st.runs(pid, kind="batch")
        latest = batch_runs[-1]["id"] if batch_runs else None
        with lock:
            active = state["active"]
        runs = st.runs(pid)
        batches = st.batches(pid)
        for b in batches:
            files = st.batch_files(b["id"])
            b["files"] = len(files)
            b["duplicates"] = sum(1 for f in files if f["duplicate_of_name"])
            b["runs"] = [{k: r[k] for k in ("id", "status", "started_at", "finished_at", "usage_json")} for r in runs if r["batch_id"] == b["id"]]
        return {"project": view, "card": project_card(st, prj, active), "register": st.deficiencies(pid),
                "runs": runs, "latest_run_id": latest, "active_run_id": active if any(r["id"] == active for r in runs) else None,
                "packet": build_packet(st, latest) if latest else None, "batches": batches,
                "messages": st.all_drafts(pid), "decisions": st.decisions(pid), "model_id": settings.model_id,
                "reviews": st.reviews(pid), "shares": st.shares(pid), "office": settings.office,
                "docs_review": prj.get("docs_review"), "docs_scope": prj.get("docs_scope") or [],
                "occupancy_docs": [list(row) for row in documents_mod.OCCUPANCY_DOCS]}

    @app.get("/api/sheets/{sheet_id}/image")
    def sheet_image(sheet_id: str):
        sh = store().sheet(sheet_id)
        if not sh or not Path(sh["image_path"]).exists():
            raise HTTPException(404, "no such sheet")
        return FileResponse(sh["image_path"], media_type="image/png")

    @app.get("/api/sheets/{sheet_id}/thumb")
    def sheet_thumb(sheet_id: str):
        """A small JPEG for the gallery; made once from the full render and kept next to it."""
        sh = store().sheet(sheet_id)
        if not sh or not Path(sh["image_path"]).exists():
            raise HTTPException(404, "no such sheet")
        src = Path(sh["image_path"])
        thumb = src.with_name(src.stem + ".thumb.jpg")
        if not thumb.exists() or thumb.stat().st_mtime < src.stat().st_mtime:
            from PIL import Image
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail((640, 640))
                im.save(thumb, "JPEG", quality=82)
        return FileResponse(thumb, media_type="image/jpeg")

    # --- register -------------------------------------------------------
    @app.post("/api/projects/{slug}/register")
    async def upload_register(slug: str, files: list[UploadFile] = File(...), paths: list[str] | None = Form(None)) -> dict:
        """The register is dropped as a folder: one CSV plus the reference photos it points at (relative paths)."""
        st = store()
        prj = _project(st, slug)
        root, rel = await _save_upload(files, paths, f"register-{_stamp()}")
        csvs = [p for p in root.rglob("*.csv")]
        if len(csvs) != 1:
            raise HTTPException(400, f"expected exactly one .csv in the register drop, got {len(csvs)}")
        try:
            items = pipeline.import_register(st, prj["id"], csvs[0])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"register rejected: {e}") from e
        return {"imported": len(items), "items": [d.item_id for d in items]}

    @app.get("/api/projects/{slug}/register/{item_id}/reference")
    def reference_photo(slug: str, item_id: str):
        st = store()
        d = st.deficiency(_project(st, slug)["id"], item_id)
        if not d or not d.get("reference_photo"):
            raise HTTPException(404, "no reference photo")
        p = Path(d["reference_photo"])
        if not p.exists():
            raise HTTPException(404, "reference photo file missing")
        return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream")

    # --- field review ---------------------------------------------------
    def _field_photo_path(slug: str, item_id: str) -> Path:
        d = settings.data_dir / "projects" / slug / "field"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{item_id}.jpg"

    async def _read_photo(photo: UploadFile | None) -> tuple[bytes | None, str]:
        """Bytes of the reviewer's photo as JPEG (phones send HEIC) plus the original name."""
        if photo is None:
            return None, ""
        raw = await photo.read()
        if not raw:
            return None, ""
        name = photo.filename or "photo"
        if name.lower().endswith((".heic", ".heif")) or (photo.content_type or "").endswith("heic"):
            tmp_dir = settings.data_dir / "uploads" / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            src = tmp_dir / f"{_stamp()}.heic"
            dst = src.with_suffix(".jpg")
            src.write_bytes(raw)
            try:
                _heic_to_jpeg(src, dst)
                raw = dst.read_bytes()
            finally:
                src.unlink(missing_ok=True)
                dst.unlink(missing_ok=True)
        return raw, name

    @app.post("/api/projects/{slug}/documents/review")
    def review_documents(slug: str, body: DocsReviewIn = DocsReviewIn()) -> dict:
        """One agent call over the folder: what is still missing, arranged by site, building and discipline. Every claim is
        checked against the real disciplines, buildings, checklist rows and file names before it is kept."""
        st = store()
        prj = _project(st, slug)
        view = project_mod.project_view(st, prj["id"])
        if not view or not view["sheets"]:
            raise HTTPException(400, "add the project drawings first")
        run_id = st.create_run(prj["id"], batch_id="", model_id=settings.model_id, kind="documents")
        try:
            out = documents_mod.review_documents(st, prj["id"], view, already=body.already, settings=settings)
        except ValueError as e:
            st.finish_run(run_id, "failed", {"error": str(e)})
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            st.finish_run(run_id, "failed", {"error": f"{type(e).__name__}: {e}"})
            raise HTTPException(502, f"the agent could not review the folder ({type(e).__name__}); try again")
        st.finish_run(run_id, "done", out["usage"])
        review = {**out, "run_id": run_id, "model_id": settings.model_id, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        st.set_docs_review(prj["id"], review)
        return {"docs_review": review}

    @app.post("/api/projects/{slug}/documents/answer")
    def answer_document_question(slug: str, body: DocsAnswerIn) -> dict:
        """The engineer fills in a blank the agent left. Stored on the review; no agent call."""
        st = store()
        prj = _project(st, slug)
        review = prj.get("docs_review")
        qs = (review or {}).get("questions") or []
        if not review or not 0 <= body.index < len(qs):
            raise HTTPException(404, "no such question")
        qs[body.index]["answer"] = " ".join(body.answer.split())[:600]
        st.set_docs_review(prj["id"], review)
        return {"docs_review": review}

    @app.post("/api/projects/{slug}/documents/scope")
    def set_document_scope(slug: str, body: DocsScopeIn) -> dict:
        """The engineer ticks a checklist row out of (or back into) scope for this project. No agent call."""
        st = store()
        prj = _project(st, slug)
        if body.name not in {name for _, name, _ in documents_mod.OCCUPANCY_DOCS}:
            raise HTTPException(404, "no such checklist row")
        names = set(prj.get("docs_scope") or [])
        (names.discard if body.in_scope else names.add)(body.name)
        st.set_docs_scope(prj["id"], sorted(names))
        return {"docs_scope": sorted(names)}

    @app.get("/api/projects/{slug}/documents/{doc_id}/file")
    def document_file(slug: str, doc_id: str):
        """The file itself, straight from the project folder, so a row in the Documents tab opens the PDF."""
        st = store()
        prj = _project(st, slug)
        d = next((x for x in st.documents(prj["id"]) if x["id"] == doc_id), None)
        if not d:
            raise HTTPException(404, "no such document")
        root = Path(prj["source_root"] or "").resolve()
        p = (root / d["rel_path"]).resolve()
        if not prj["source_root"] or not p.is_relative_to(root) or not p.is_file():
            raise HTTPException(404, "file missing from the project folder")
        return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream", filename=p.name,
                            content_disposition_type="inline")

    @app.get("/api/projects/{slug}/documents/tree")
    def documents_tree(slug: str) -> dict:
        """The folder arranged site → building → discipline → sheets, plain code over the readings."""
        st = store()
        prj = _project(st, slug)
        view = project_mod.project_view(st, prj["id"]) or {}
        return documents_mod.site_tree(view)

    @app.post("/api/projects/{slug}/reviews")
    def start_review(slug: str, body: NewReview) -> dict:
        st = store()
        prj = _project(st, slug)
        code = body.discipline.strip().upper()
        if code not in project_mod.DISCIPLINES:
            raise HTTPException(400, f"unknown discipline '{body.discipline}'")
        for r in st.reviews(prj["id"]):
            if r["discipline"] == code and r["status"] == "active":
                return {"review": r, "resumed": True}
        return {"review": st.create_review(prj["id"], code, body.title.strip()), "resumed": False}

    @app.get("/api/projects/{slug}/plans/todo")
    def plans_todo(slug: str) -> dict:
        """Plan sheets whose floor boxes have not been found yet (the 'map the floors' button)."""
        st = store()
        prj = _project(st, slug)
        return {"sheets": [{"id": sh["id"], "sheet_number": sh["sheet_number"], "title": sh["title"], "discipline": sh["discipline"]}
                           for sh in plans_mod.plan_sheets(st, prj["id"])]}

    @app.post("/api/projects/{slug}/sheets/{sheet_id}/views")
    def find_views(slug: str, sheet_id: str) -> dict:
        """One agent call: where each floor plan drawing sits on this sheet. The viewer zooms to it; pins stay on the sheet."""
        st = store()
        prj = _project(st, slug)
        sh = st.sheet(sheet_id)
        if not sh or sh["project_id"] != prj["id"]:
            raise HTTPException(404, "no such sheet in this project")
        run_id = st.create_run(prj["id"], batch_id="", model_id=settings.model_id, kind="review")
        try:
            out = plans_mod.find_plan_views(st, prj["id"], sheet_id, settings)
        except ValueError as e:
            st.finish_run(run_id, "failed", {"error": str(e)})
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # noqa: BLE001
            st.finish_run(run_id, "failed", {"error": f"{type(e).__name__}: {e}"})
            raise HTTPException(502, f"the agent could not read the sheet ({type(e).__name__}); try again") from e
        st.finish_run(run_id, "done", out["usage"])
        return out

    @app.put("/api/projects/{slug}/sheets/{sheet_id}/views")
    def set_views(slug: str, sheet_id: str, body: SheetViews) -> dict:
        """The engineer's correction: keep, drop or fix the boxes by hand."""
        st = store()
        prj = _project(st, slug)
        sh = st.sheet(sheet_id)
        if not sh or sh["project_id"] != prj["id"]:
            raise HTTPException(404, "no such sheet in this project")
        views = []
        for v in body.views:
            if not (0 <= v.x < 1 and 0 <= v.y < 1 and 0 < v.w <= 1 - v.x + 1e-6 and 0 < v.h <= 1 - v.y + 1e-6):
                raise HTTPException(400, "a box must lie within the sheet")
            # a box the engineer did not touch keeps its "agent" mark; anything else is his
            views.append({"title": v.title.strip()[:80], "level": v.level.strip(), "x": v.x, "y": v.y, "w": v.w, "h": v.h,
                          "source": "agent" if v.source == "agent" else "engineer"})
        st.set_sheet_views(sheet_id, views)
        return {"sheet_id": sheet_id, "views": views}

    def _draft_review_message(st: Store, prj: dict, review_id: str) -> tuple[dict | None, str | None]:
        """One agent call that writes the covering message; the review is finished whether or not it succeeds."""
        if not st.review_items(prj["id"], review_id):
            return None, "no deficiencies were recorded, so there is nothing to send"
        run_id = st.create_run(prj["id"], batch_id="", model_id=settings.model_id, kind="review")
        try:
            out = review_mod.draft_review_message(st, prj["id"], review_id, settings, office=settings.office)
        except Exception as e:  # noqa: BLE001
            st.finish_run(run_id, "failed", {"error": f"{type(e).__name__}: {e}"})
            return None, f"the agent could not draft the message ({type(e).__name__}); finish again to retry"
        st.finish_run(run_id, "done", out["usage"])
        did = st.upsert_draft(run_id, "", out["subject"], out["body"], review_id=review_id)
        return st.draft_for_review(review_id) if did else None, None

    @app.post("/api/projects/{slug}/ask")
    def ask_project(slug: str, body: AskBody) -> dict:
        """One call on the fast model: answer from the records, optionally move the screen. Reads only."""
        st = store()
        prj = _project(st, slug)
        run_id = st.create_run(prj["id"], batch_id="", model_id=settings.fast_model_id, kind="ask")
        try:
            out = ask_mod.ask(st, prj["id"], body.question, body.where, settings, office=settings.office,
                              history=body.history, spoken=body.spoken)
        except ValueError as e:
            st.finish_run(run_id, "failed", {"error": str(e)})
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            st.finish_run(run_id, "failed", {"error": f"{type(e).__name__}: {e}"})
            raise HTTPException(502, "Closeout could not answer that just now; ask again")
        st.finish_run(run_id, "done", out["usage"])
        return {"answer": out["text"], "go": out["go"], "action": out.get("action"), "usage": out["usage"]}

    VOICE_TONE = ("A calm, clear colleague reading a short note aloud to an engineer on a building site. "
                  "Natural pace, plain, no drama. Item numbers like EL-01 are read as letters and digits.")

    @app.get("/api/voice")
    def voice_status() -> dict:
        """Whether the natural voice and the spoken conversation are on. The key itself never leaves the server."""
        return {"available": bool(settings.voice_key), "live": bool(settings.voice_key)}

    LIVE_INSTRUCTIONS = (
        "You are Closeout, a calm colleague on a call with a field engineer who is walking a building site. "
        "The project is \"{name}\". You know nothing about this project yourself: every fact comes from the office "
        "records through the app. Whenever the engineer asks anything about the project (items, findings, floors, "
        "plans, photos, drawings, the contractor, messages, what was done, what is left, or asks to change, add or "
        "send something), delegate it to the client and wait for the result. Never guess and never invent an item, "
        "a number, a date or a status; never answer a project question from memory. "
        "When the result arrives, say it once, naturally, in one or two short sentences. Read item codes as letters "
        "and digits (EL-01 is E L zero one). If the result asks the engineer to confirm a change, ask for a yes or a "
        "no, and delegate that answer to the client too: the app carries the change out, you never confirm anything "
        "yourself. Never judge whether the work is acceptable; that is the engineer's call. Keep small talk to a "
        "sentence. Be brief; the engineer is working."
    )

    @app.post("/api/projects/{slug}/live/session", status_code=201)
    def live_session(slug: str, body: LiveBody) -> dict:
        """Open a spoken conversation for this project. The browser sends its WebRTC offer; the voice service
        answers it. The voice only talks; every project answer is delegated back to the Closeout agent."""
        project = _project(store(), slug)
        sdp = str(body.sdp or "")
        if not settings.voice_key:
            raise HTTPException(404, "the spoken conversation is not set up here")
        if not sdp.strip().startswith("v=0"):
            raise HTTPException(400, "no connection offer")
        payload = {
            "session": {
                "model": settings.live_model,
                "instructions": LIVE_INSTRUCTIONS.format(name=project["name"]),
                "delegation": {"type": "client"},
                "audio": {"output": {"voice": settings.voice_name}},
            },
            "transport": {"type": "webrtc", "sdp": sdp},
        }
        try:
            r = httpx.post("https://api.openai.com/v1/live/sessions", timeout=30,
                           headers={"authorization": f"Bearer {settings.voice_key}"}, json=payload)
        except httpx.HTTPError:
            raise HTTPException(502, "the voice service did not answer")
        if r.status_code not in (200, 201):
            log.warning("live session refused: %s %s", r.status_code, r.text[:300])
            raise HTTPException(502, "the voice service could not start the conversation")
        j = r.json()
        return {"session_id": (j.get("session") or {}).get("id", ""), "sdp": (j.get("transport") or {}).get("sdp", "")}

    @app.post("/api/projects/{slug}/speak")
    def speak(slug: str, body: SpeakBody):
        """Turn one answer into speech with the voice service. Text in, audio out; nothing is stored."""
        _project(store(), slug)
        text = " ".join(str(body.text or "").split())[:1500]
        if not settings.voice_key:
            raise HTTPException(404, "the natural voice is not set up here")
        if not text:
            raise HTTPException(400, "nothing to say")
        try:
            r = httpx.post("https://api.openai.com/v1/audio/speech", timeout=30,
                           headers={"authorization": f"Bearer {settings.voice_key}"},
                           json={"model": settings.voice_model, "voice": settings.voice_name, "input": text,
                                 "instructions": VOICE_TONE, "response_format": "mp3"})
        except httpx.HTTPError:
            raise HTTPException(502, "the voice service did not answer")
        if r.status_code != 200:
            raise HTTPException(502, "the voice service could not read that")
        return Response(content=r.content, media_type="audio/mpeg", headers={"cache-control": "no-store"})

    @app.post("/api/projects/{slug}/reviews/{review_id}/finish")
    def finish_review(slug: str, review_id: str) -> dict:
        """Close the walk: freeze what goes to the contractor and have the agent draft the covering message.
        Nothing is sent; the draft waits on the Messages tab for the engineer."""
        st = store()
        prj = _project(st, slug)
        r = st.review(review_id)
        if not r or r["project_id"] != prj["id"]:
            raise HTTPException(404, "no such review")
        st.finish_review(review_id)
        pkg = review_mod.review_package(st, prj["id"], review_id)
        st.set_review_package(review_id, pkg)
        message, error = (st.draft_for_review(review_id), None) if st.draft_for_review(review_id) else _draft_review_message(st, prj, review_id)
        return {"review": st.review(review_id), "package": pkg, "message": message, "error": error}

    LINK_LINE = "Send your photos and documents through this link: "

    def _share_url(request: Request, token: str) -> str:
        base = str(request.base_url).rstrip("/")
        return f"{base}/c/{token}"

    @app.post("/api/projects/{slug}/reviews/{review_id}/share")
    def share_review(slug: str, review_id: str, request: Request) -> dict:
        """The contractor's link for one finished review: they open it, see the items, and send evidence back through it.
        The covering message gets the link added so the engineer can copy it as is. Nothing is sent by the app."""
        st = store()
        prj = _project(st, slug)
        r = st.review(review_id)
        if not r or r["project_id"] != prj["id"]:
            raise HTTPException(404, "no such review")
        if r["status"] != "finished":
            raise HTTPException(409, "finish the field review first")
        share = st.share_for_review(review_id) or st.create_share(prj["id"], review_id)
        url = _share_url(request, share["id"])
        msg = st.draft_for_review(review_id)
        if msg and "/c/" not in msg["body"]:
            st.update_draft(msg["id"], body=msg["body"].rstrip() + "\n\n" + LINK_LINE + url)
        st.touch_project(prj["id"])
        return {"share": share, "url": url, "message": st.draft_for_review(review_id)}

    @app.delete("/api/projects/{slug}/shares/{token}")
    def revoke_share(slug: str, token: str) -> dict:
        st = store()
        prj = _project(st, slug)
        sh = st.share(token)
        if not sh or sh["project_id"] != prj["id"]:
            raise HTTPException(404, "no such link")
        st.revoke_share(token)
        return {"share": st.share(token)}

    def _contractor_share(st: Store, token: str) -> tuple[dict, dict]:
        sh = st.share(token)
        if not sh:
            raise HTTPException(404, "this link is not known")
        prj = st.project(sh["project_id"])
        if not prj:
            raise HTTPException(404, "this link is not known")
        return sh, prj

    @app.get("/api/c/{token}")
    def contractor_view(token: str) -> dict:
        """What the contractor sees: the review's items with where each stands, and what they have sent so far.
        No decisions, no other reviews, no drafts: only their own package."""
        st = store()
        sh, prj = _contractor_share(st, token)
        pid = prj["id"]
        rv = st.review(sh["review_id"]) or {}
        pkg = rv.get("package") or {}
        if sh["revoked_at"]:
            return {"active": False, "project": {"name": prj["name"]}, "office": settings.office, "review": None, "items": [], "drops": []}
        batch_runs = st.runs(pid, kind="batch")
        latest = batch_runs[-1] if batch_runs else None
        status = st.item_status_for_run(latest["id"]) if latest else {}
        decisions = {d["item_id"]: d["decision"] for d in st.decisions(pid)}
        items = []
        for it in pkg.get("items", []):
            stt = status.get(it["item_id"]) or {}
            items.append({"item": {k: it.get(k, "") for k in ("item_id", "location", "description", "evidence_required", "sheet", "unit", "level", "space")},
                          "completeness": "complete" if decisions.get(it["item_id"]) == "accept" else stt.get("completeness", "no_evidence"),
                          "missing_slots": stt.get("missing_slots", it.get("slots") or []), "closed": decisions.get(it["item_id"]) == "accept"})
        runs = st.runs(pid)
        drops = []
        for b in st.batches(pid):
            if b.get("via") != token:
                continue
            last = [r for r in runs if r["batch_id"] == b["id"]]
            drops.append({"label": b["label"], "created_at": b["created_at"], "files": len(st.batch_files(b["id"])),
                          "status": last[-1]["status"] if last else "queued"})
        return {"active": True, "project": {"name": prj["name"]}, "office": settings.office,
                "review": {"title": rv.get("title", ""), "discipline_name": pkg.get("discipline_name", ""), "finished_at": rv.get("finished_at"), "count": len(items)},
                "items": items, "drops": drops, "busy": bool(state["active"]) or bool(pending)}

    @app.post("/api/c/{token}/batches")
    async def contractor_upload(token: str, files: list[UploadFile] = File(...), paths: list[str] | None = Form(None)) -> dict:
        """The contractor sends photos and documents back through their link; they are filed like any other drop."""
        st = store()
        sh, prj = _contractor_share(st, token)
        if sh["revoked_at"]:
            raise HTTPException(410, "this link is no longer active")
        feed = _reserve()
        try:
            label = f"from-contractor-{_stamp()}"
            root, _ = await _save_upload(files, paths, label)
            file_list = _sorted_files(root)
        except Exception:
            _release(feed)
            raise

        def target(progress):
            def tagged(event, data):
                if event == "ingested" and data.get("batch_id"):
                    store().set_batch_via(data["batch_id"], token)
                progress(event, data)
            pipeline.process_batch(store(), prj["id"], file_list, label, settings, progress=tagged, root=root)

        feed.push("uploaded", {"label": label, "files": len(file_list), "folder": str(root)})
        _launch(target, feed)
        return {"label": label, "files": len(file_list), "feed": "/api/runs/pending/events"}

    @app.post("/api/projects/{slug}/reviews/{review_id}/message")
    def redraft_review_message(slug: str, review_id: str) -> dict:
        """Ask the agent for a fresh covering message for a finished review (the package is rebuilt too)."""
        st = store()
        prj = _project(st, slug)
        r = st.review(review_id)
        if not r or r["project_id"] != prj["id"]:
            raise HTTPException(404, "no such review")
        if r["status"] != "finished":
            raise HTTPException(400, "finish the review first")
        pkg = review_mod.review_package(st, prj["id"], review_id)
        st.set_review_package(review_id, pkg)
        message, error = _draft_review_message(st, prj, review_id)
        if error:
            raise HTTPException(502 if "agent" in error else 400, error)
        return {"review": st.review(review_id), "package": pkg, "message": message}

    @app.post("/api/projects/{slug}/findings/suggest")
    async def suggest_finding(slug: str, sheet_id: str = Form(...), pin_x: float = Form(...), pin_y: float = Form(...),
                              note: str = Form(""), discipline: str = Form(""), photo: UploadFile | None = File(None)) -> dict:
        """One model call: photo + pinned sheet -> proposed location / wording / evidence. The reviewer edits and saves."""
        st = store()
        prj = _project(st, slug)
        sh = st.sheet(sheet_id)
        if not sh or sh["project_id"] != prj["id"]:
            raise HTTPException(404, "no such sheet in this project")
        if not (0 <= pin_x <= 1 and 0 <= pin_y <= 1):
            raise HTTPException(400, "pin must be inside the sheet")
        raw, _ = await _read_photo(photo)
        photo_bytes = None
        if raw:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(raw)) as im:
                im = im.convert("RGB")
                im.thumbnail((1568, 1568))
                buf = io.BytesIO(); im.save(buf, "JPEG", quality=85); photo_bytes = buf.getvalue()
        try:
            out = review_mod.suggest_field_note(st, prj["id"], sheet_id, pin_x, pin_y, photo_bytes, note, settings,
                                                discipline_hint=discipline.strip().upper())
        except Exception as e:  # noqa: BLE001 - the reviewer sees why and can still write it by hand
            raise HTTPException(502, f"agent could not suggest: {type(e).__name__}: {e}") from e
        return {"suggestion": out, "model_id": settings.model_id}

    @app.post("/api/projects/{slug}/findings/locate")
    async def locate_finding(slug: str, discipline: str = Form(...), unit: str = Form(""), level: str = Form(""),
                             note: str = Form(""), gps: str = Form(""), photo: UploadFile = File(...)) -> dict:
        """Photo first: one model call picks the sheet and writes the record; the reviewer then pins the spot on that sheet."""
        st = store()
        prj = _project(st, slug)
        code = discipline.strip().upper()
        if code not in project_mod.DISCIPLINES:
            raise HTTPException(400, "unknown discipline")
        raw, _ = await _read_photo(photo)
        if not raw:
            raise HTTPException(400, "a photo is required")
        from PIL import Image
        import io
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            im.thumbnail((1568, 1568))
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=85); photo_bytes = buf.getvalue()
        try:
            out = review_mod.locate_field_note(st, prj["id"], code, photo_bytes, unit.strip(), level.strip(), note, gps.strip()[:80], settings)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"agent could not place it: {type(e).__name__}: {e}") from e
        return {"suggestion": out, "model_id": settings.model_id}

    @app.post("/api/projects/{slug}/findings")
    async def save_finding(slug: str, sheet_id: str = Form(...), pin_x: float = Form(...), pin_y: float = Form(...),
                           review_id: str = Form(...), location: str = Form(...), description: str = Form(...),
                           evidence_required: str = Form(...), discipline: str = Form(""), unit: str = Form(""),
                           level: str = Form(""), space: str = Form(""), note: str = Form(""), gps: str = Form(""),
                           photo: UploadFile | None = File(None)) -> dict:
        """The reviewer's confirmed deficiency: numbered, pinned to the sheet, photo kept as the reference."""
        from .register import RegisterError, parse_slots
        st = store()
        prj = _project(st, slug)
        pid = prj["id"]
        sh = st.sheet(sheet_id)
        if not sh or sh["project_id"] != pid:
            raise HTTPException(404, "no such sheet in this project")
        rv = st.review(review_id)
        if not rv or rv["project_id"] != pid:
            raise HTTPException(404, "no such review")
        if rv["status"] != "active":
            raise HTTPException(409, "that review is finished; start a new one")
        location, description = " ".join(location.split()), " ".join(description.split())
        if len(location) < 4 or len(description) < 4:
            raise HTTPException(400, "location and description are required")
        try:
            slots = parse_slots(evidence_required)
        except RegisterError as e:
            raise HTTPException(400, f"evidence required: {e}") from e
        code = (discipline.strip().upper() or rv["discipline"])
        if code not in project_mod.DISCIPLINES:
            raise HTTPException(400, f"unknown discipline '{discipline}'")
        item_id = st.next_item_id(pid, rv["discipline"])
        raw, original = await _read_photo(photo)
        ref_path, meta = "", {}
        if raw:
            p = _field_photo_path(prj["slug"], item_id)
            p.write_bytes(raw)
            ref_path = str(p)
            meta = {**_exif(p), "original_name": original}
        if gps.strip():
            meta["gps"] = gps.strip()[:80]
        st.add_field_item(pid, item_id, location, description, evidence_required, slots, code, review_id,
                          sheet=sh["sheet_number"] or f"{sh['discipline']} p.{sh['page']}", sheet_id=sheet_id,
                          pin_x=pin_x, pin_y=pin_y, unit=unit.strip(), level=level.strip(), space=space.strip(),
                          note=note.strip(), reference_photo=ref_path, ref_meta=meta,
                          review_date=datetime.now(timezone.utc).date().isoformat())
        return {"item": st.deficiency(pid, item_id)}

    @app.patch("/api/projects/{slug}/findings/{item_id}")
    def patch_finding(slug: str, item_id: str, body: FindingPatch) -> dict:
        from .register import RegisterError, parse_slots
        st = store()
        pid = _project(st, slug)["id"]
        d = st.deficiency(pid, item_id)
        if not d or d.get("source") != "field":
            raise HTTPException(404, "no such field finding")
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if "evidence_required" in fields:
            try:
                fields["slots_json"] = json.dumps([s.__dict__ for s in parse_slots(fields["evidence_required"])])
            except RegisterError as e:
                raise HTTPException(400, f"evidence required: {e}") from e
        st.update_field_item(pid, item_id, **fields)
        return {"item": st.deficiency(pid, item_id)}

    @app.delete("/api/projects/{slug}/findings/{item_id}")
    def delete_finding(slug: str, item_id: str) -> dict:
        st = store()
        pid = _project(st, slug)["id"]
        d = st.deficiency(pid, item_id)
        if not d or d.get("source") != "field":
            raise HTTPException(404, "no such field finding")
        st.delete_deficiency(pid, item_id)
        if d.get("reference_photo"):
            Path(d["reference_photo"]).unlink(missing_ok=True)
        return {"deleted": item_id}

    # --- batches / runs -------------------------------------------------
    @app.post("/api/projects/{slug}/batches")
    async def upload_batch(slug: str, files: list[UploadFile] = File(...), paths: list[str] | None = Form(None),
                           label: str | None = Form(None), reprocess_all: bool = Form(False)) -> dict:
        st = store()
        prj = _project(st, slug)
        if not st.deficiencies(prj["id"]):
            raise HTTPException(409, "this project has no deficiency list yet")
        feed = _reserve()
        try:
            label = (label or f"drop-{_stamp()}").strip()
            root, _ = await _save_upload(files, paths, re.sub(r"[^A-Za-z0-9._-]+", "_", label) + f"-{_stamp()}")
            file_list = _sorted_files(root)
        except Exception:
            _release(feed)
            raise

        def target(progress):
            pipeline.process_batch(store(), prj["id"], file_list, label, settings, progress=progress,
                                   reprocess_all=reprocess_all, root=root)

        feed.push("uploaded", {"label": label, "files": len(file_list), "folder": str(root)})
        _launch(target, feed)
        return {"label": label, "files": len(file_list), "feed": "/api/runs/pending/events"}

    @app.post("/api/runs/{run_id}/retry")
    def retry(run_id: str) -> dict:
        st = store()
        if not st.run(run_id):
            raise HTTPException(404, "no such run")
        with lock:
            if state["active"] or pending:
                raise HTTPException(409, "a run is already in progress")
            feed = RunFeed(run_id)
            feeds[run_id] = feed
            state["active"] = run_id

        def target(progress):
            if st.run(run_id).get("kind") == "project":
                project_mod.continue_project_run(st, run_id, settings, progress=progress)
            else:
                pipeline.continue_run(st, run_id, settings, progress=progress)

        feed.push("retry", {"run_id": run_id})
        _launch(target, feed)
        return {"run_id": run_id}

    @app.get("/api/runs")
    def runs() -> list[dict]:
        st = store()
        return [dict(r, jobs=st.jobs(r["id"])) for r in st.runs()]

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict:
        st = store()
        run = st.run(run_id)
        if not run:
            raise HTTPException(404, "no such run")
        return dict(run, jobs=st.jobs(run_id), findings=st.findings_for_run(run_id))

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, start: int = 0):
        """Server-sent events. `pending` follows the run that was just uploaded and has no id yet."""
        if run_id == "pending":
            with lock:
                feed = pending[0] if pending else None
                if feed is None:  # ingest already finished; fall through to the newest feed
                    feed = feeds[state["active"]] if state["active"] else (list(feeds.values()) or [None])[-1]
        else:
            feed = _feed_for(run_id)
        if feed is None:
            st = store()
            run = st.run(run_id)
            if not run:
                raise HTTPException(404, "no such run")
            # A run from a previous process: nothing live to stream, replay what the store knows.
            feed = RunFeed(run_id)
            for j in st.jobs(run_id):
                feed.push("job_done" if j["status"] == "done" else "job_failed",
                          {"job_id": j["id"], "kind": j["kind"], "subject": j["subject"], "error": j.get("error"), "replay": True})
            feed.push("project_ready" if run.get("kind") == "project" else "packet",
                      {"run_id": run_id, "replay": True, "status": run["status"]})

        def gen():
            for ev in feed.stream(start):
                yield f"event: {ev['event']}\ndata: {json.dumps(ev, default=str)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/runs/{run_id}/packet")
    def packet(run_id: str) -> dict:
        st = store()
        if not st.run(run_id):
            raise HTTPException(404, "no such run")
        return build_packet(st, run_id)

    @app.get("/api/runs/{run_id}/packet.md")
    def packet_md(run_id: str):
        st = store()
        if not st.run(run_id):
            raise HTTPException(404, "no such run")
        return PlainTextResponse(packet_markdown(st, build_packet(st, run_id)), media_type="text/markdown")

    # --- evidence -------------------------------------------------------
    @app.get("/api/evidence/{evidence_id}/file")
    def evidence_file(evidence_id: str):
        ev = store().evidence(evidence_id)
        if not ev:
            raise HTTPException(404, "no such evidence")
        p = Path(ev["stored_path"])
        if not p.exists():
            raise HTTPException(404, "stored file missing")
        return FileResponse(p, media_type=ev.get("mime") or mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                            filename=ev["filename"])

    # --- review actions -------------------------------------------------
    @app.patch("/api/drafts/{draft_id}")
    def patch_draft(draft_id: str, body: DraftPatch) -> dict:
        st = store()
        try:
            st.update_draft(draft_id, body=body.body, subject=body.subject)
        except KeyError:
            raise HTTPException(404, "no such draft") from None
        return {"ok": True}

    @app.post("/api/projects/{slug}/items/{item_id}/decision")
    def decide(slug: str, item_id: str, body: Decision) -> dict:
        st = store()
        pid = _project(st, slug)["id"]
        if not st.deficiency(pid, item_id):
            raise HTTPException(404, "no such item")
        if body.decision not in ("accept", "reject", "hold"):
            raise HTTPException(400, "decision must be accept, reject or hold")
        st.touch_project(pid)
        return {"decision_id": st.add_decision(pid, item_id, body.decision, body.note)}

    # --- UI -------------------------------------------------------------
    @app.get("/c/{token}", include_in_schema=False)
    def contractor_page(token: str):
        return index()

    @app.get("/", include_in_schema=False)
    def index():
        page = WEB_DIR / "index.html"
        if not page.exists():
            return JSONResponse({"detail": "web/index.html not built yet; API is at /docs"}, status_code=404)
        return HTMLResponse(page.read_text())

    # --- office access code: everything except contractor links and the sign-in page needs the cookie --------------
    access_code = settings.access_code
    open_prefixes = ("/c/", "/api/c/")

    def _access_token() -> str:
        return hashlib.sha256(f"closeout-access:{access_code}".encode()).hexdigest()

    def _signed_in(request: Request) -> bool:
        return not access_code or hmac.compare_digest(request.cookies.get("closeout_access", ""), _access_token())

    @app.middleware("http")
    async def access_gate(request: Request, call_next):
        path = request.url.path
        if _signed_in(request) or path == "/signin" or path.startswith(open_prefixes):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "sign in first"}, status_code=401)
        return RedirectResponse("/signin", status_code=303)

    @app.get("/signin", include_in_schema=False)
    def signin_page(request: Request):
        if _signed_in(request):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(SIGNIN_HTML.replace("__BAD__", ""))

    @app.post("/signin", include_in_schema=False)
    async def signin(request: Request):
        form = await request.form()
        given = str(form.get("code", ""))
        if not access_code or not hmac.compare_digest(given.strip(), access_code):
            return HTMLResponse(SIGNIN_HTML.replace("__BAD__", '<p class="bad">That code did not match. Try again.</p>'), status_code=403)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("closeout_access", _access_token(), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")
        return resp

    @app.post("/signout", include_in_schema=False)
    def signout():
        resp = RedirectResponse("/signin", status_code=303)
        resp.delete_cookie("closeout_access", path="/")
        return resp

    return app


app = create_app()

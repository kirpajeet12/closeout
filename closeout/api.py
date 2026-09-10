"""Closeout API: one FastAPI app over the pipeline. Runs execute on a worker thread and stream progress as SSE.

    uvicorn closeout.api:app --reload

Everything hangs off a project: /api/projects/{slug}/... Nothing here calls the model directly; it only drives
`pipeline.process_batch` / `continue_run` / `project.import_project` and reads the store.
"""
from __future__ import annotations

import json
import mimetypes
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from . import pipeline, plans as plans_mod, project as project_mod, review as review_mod
from .config import SETTINGS, Settings
from .ingest import _exif, _heic_to_jpeg
from .packet import build_packet, packet_markdown
from .store import Store

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


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "project").lower()).strip("-") or "project"


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
    return {
        "id": pid, "slug": prj["slug"], "name": prj["name"], "address": m.get("address", ""), "city": m.get("city", ""),
        "building_type": m.get("building_type", ""), "sheets": len(st.sheets(pid)), "documents": len(st.documents(pid)),
        "items": len(items), "ready": n["complete"], "needs": n["incomplete"], "unclear": n["needs_clarification"], "nothing": n["no_evidence"],
        "closed": sum(1 for v in decisions.values() if v == "accept"),
        "drops": len(st.batches(pid)), "last_activity": prj["updated_at"],
        "latest_run": {k: latest[k] for k in ("id", "status", "started_at", "finished_at")} if latest else None,
        "active": bool(active_run_id) and any(r["id"] == active_run_id for r in runs),
        "created_at": prj["created_at"],
    }


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
        root = settings.data_dir / "uploads" / folder
        root.mkdir(parents=True, exist_ok=True)
        rel = paths if paths and len(paths) == len(files) else [f.filename or "file" for f in files]
        for f, name in zip(files, rel):
            dest = root / _safe_relpath(name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(await f.read())
        return root, rel

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
            root, rel = await _save_upload(files, paths, f"project-{_stamp()}")
            top = {_safe_relpath(n).parts[0] for n in rel}
            # the browser sends "<dropped folder>/<sub path>"; the dropped folder itself is the project root
            if len(top) == 1 and (root / next(iter(top))).is_dir():
                root = root / next(iter(top))
            slug_v = _slug(slug or (next(iter(top)) if len(top) == 1 else "project"))
        except Exception:
            _release(feed)
            raise

        def target(progress):
            project_mod.import_project(store(), root, slug_v, settings, progress=progress, read_with_model=read_with_model)

        feed.push("uploaded", {"label": f"project {slug_v}", "files": len(files), "folder": str(root), "slug": slug_v})
        _launch(target, feed)
        return {"slug": slug_v, "files": len(files), "feed": "/api/runs/pending/events"}

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
                "reviews": st.reviews(pid)}

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
    @app.get("/", include_in_schema=False)
    def index():
        page = WEB_DIR / "index.html"
        if not page.exists():
            return JSONResponse({"detail": "web/index.html not built yet; API is at /docs"}, status_code=404)
        return HTMLResponse(page.read_text())

    return app


app = create_app()

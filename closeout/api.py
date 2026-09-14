"""Closeout API: one FastAPI app over the pipeline. Runs execute on a worker thread and stream progress as SSE.

    uvicorn closeout.api:app --reload

Everything hangs off a project: /api/projects/{slug}/... Nothing here calls the model directly; it only drives
`pipeline.process_batch` / `continue_run` / `project.import_project` and reads the store.
"""
from __future__ import annotations

import hashlib
import hmac
from html import escape as html_escape
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

from . import activity as activity_mod, ask as ask_mod, documents as documents_mod, gmail as gmail_mod, inbox as inbox_mod, mail as mail_mod, mailbox as mailbox_mod, outlook as outlook_mod, drawings as drawings_mod, usage as usage_mod, pipeline, plans as plans_mod, project as project_mod, review as review_mod, revisions as revisions_mod
import dataclasses

from .config import SETTINGS, Settings
from .ingest import _exif, _heic_to_jpeg
from .packet import build_packet, packet_markdown
from . import replies as replies_mod, report as report_mod
from . import notice as notice_mod
from . import history as history_mod
from . import brief as brief_mod
from . import coverage as coverage_mod
from . import accounts as accounts_mod
from .store import Store, now

log = logging.getLogger("closeout.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TERMINAL_EVENTS = {"packet", "project_ready", "run_error"}
MORE_PHOTOS = 7                      # photos after the first on one deficiency


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


class PersonIn(BaseModel):
    email: str
    name: str = ""


class IssueIn(BaseModel):
    what: str
    page: str = ""


class IssueStatusIn(BaseModel):
    status: str


class DraftPatch(BaseModel):
    subject: str | None = None
    body: str | None = None
    to: str | None = None


class Decision(BaseModel):
    decision: str
    note: str | None = None


class NewReview(BaseModel):
    discipline: str
    title: str = ""
    stage: str = ""


class ReviewPatch(BaseModel):
    stage: str | None = None
    units: list[str] | None = None      # the units walked; [] = none; omit = leave as is
    units_reset: bool = False           # back to what the deficiencies say


class StagesPatch(BaseModel):
    discipline: str
    stages: list[str]


class SheetView(BaseModel):
    title: str = ""
    level: str
    unit: str = ""
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
    conversation_id: str | None = None   # continue a saved conversation; omitted = a new one is started


class ConversationBody(BaseModel):
    title: str = ""


class SpeakBody(BaseModel):
    text: str


class LiveBody(BaseModel):
    sdp: str   # the browser's WebRTC offer; the answer comes back the same way


class DrawingsReviewIn(BaseModel):
    discipline: str = "EL"


class DocsReviewIn(BaseModel):
    already: list[str] = []


class DocsAnswerIn(BaseModel):
    index: int
    answer: str = ""


class DocsScopeIn(BaseModel):
    name: str             # exact checklist row name
    in_scope: bool = True      # the gaps the web app's rules already show, so the agent does not repeat them


class EmailIn(BaseModel):
    address: str
    password: str              # an app password; kept in the database only, never echoed back
    host: str = ""             # google | microsoft | hostinger | godaddy | zoho, when the address alone does not say
    imap_host: str = ""
    imap_port: int = 0
    smtp_host: str = ""
    smtp_port: int = 0


class FinishIn(BaseModel):
    to: str = ""               # who gets the report, asked when the review is finished; kept on the draft, nothing is sent


class SendIn(BaseModel):
    to: str                    # the contractor's address, typed by the engineer
    via: str = ""              # "" = send from the app when a sender is configured; "mail-app" = it was handed to the mail app
    subject: str | None = None # the message as it stood when Send was pressed; None = the current draft
    body: str | None = None


class PlaceIn(BaseModel):
    slug: str
    review_id: str


class DisciplineIn(BaseModel):
    code: str                  # short code such as SP
    name: str = ""             # what the folder is called; a known code fills it in


class DisciplineRename(BaseModel):
    name: str                  # the folder's new name; the code stays


class FilingIn(BaseModel):
    file: str                  # file name as listed in the project folder
    building: str | None = None    # None = keep the current one; "" = the site, no building
    discipline: str | None = None  # None = keep; "" = the file's own discipline
    name: str | None = None        # None = keep; "" = back to the file's own name
    folder: str | None = None      # None = keep; "" = out of the engineer's own folder, back by building and discipline
    # (a folder id from /folders otherwise)


class FolderIn(BaseModel):
    parent: str                    # the Documents tree key it goes inside: prj, site, site/AR, b/<building>, b/<building>/AR, u/<id>
    name: str


class FolderRename(BaseModel):
    name: str


class FilingUndoIn(BaseModel):
    file: str


class RevisionCompareIn(BaseModel):
    old_document_id: str
    new_document_id: str


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
    decisions = {d["item_id"]: d["decision"] for d in st.decisions(pid)}
    call = {"reject": "incomplete", "hold": "needs_clarification"}   # the engineer's call counts over the evidence reading
    n = {"complete": 0, "incomplete": 0, "needs_clarification": 0, "no_evidence": 0}
    for d in items:
        if decisions.get(d["item_id"]) == "accept":    # closed: off the open counts, still on its field review and in the log
            continue
        n[call.get(decisions.get(d["item_id"])) or (status.get(d["item_id"]) or {}).get("completeness", "no_evidence")] += 1
    m = prj["model"]
    runs = st.runs(pid)
    reviews, drafts, batches = st.reviews(pid), st.all_drafts(pid), st.batches(pid)
    return {
        "id": pid, "slug": prj["slug"], "name": prj["name"], "address": m.get("address", ""), "city": m.get("city", ""),
        "building_type": m.get("building_type", ""), "sheets": len(st.sheets(pid)), "documents": len(st.documents(pid)),
        "items": len(items), "ready": n["complete"], "needs": n["incomplete"], "unclear": n["needs_clarification"], "nothing": n["no_evidence"],
        "closed": sum(1 for d in items if decisions.get(d["item_id"]) == "accept"),
        "open": sum(1 for d in items if decisions.get(d["item_id"]) != "accept"),
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


AUTH_CSS = """
  :root { --ink:#151412; --mute:#6f6a60; --line:#e6e1d6; --bg:#f6f4ee; }
  * { box-sizing:border-box } body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Inter","Helvetica Neue",Arial,sans-serif; }
  .card { width:min(380px,calc(100vw - 40px)); background:#fff; border:1px solid var(--line); border-radius:20px; padding:32px 28px 26px; box-shadow:0 20px 60px rgba(20,18,10,.08); margin:24px 0 }
  .wordmark { font-weight:800; letter-spacing:.22em; font-size:12px } h1 { font-size:22px; margin:18px 0 6px; letter-spacing:-.01em; text-wrap:balance }
  p { margin:0 0 18px; color:var(--mute); font-size:14px; line-height:1.45 }
  label { display:block; font-size:13px; font-weight:600; margin:0 0 6px } label + input { margin-bottom:14px }
  input { width:100%; font-size:17px; padding:12px 14px; border:1px solid var(--line); border-radius:12px; outline:none; background:#fbfaf7; color:var(--ink) }
  input:focus { border-color:var(--ink) } button { margin-top:4px; width:100%; padding:13px; font-size:16px; font-weight:600; border:0; border-radius:12px; background:var(--ink); color:#fff; cursor:pointer }
  button:focus-visible, a:focus-visible { outline:2px solid var(--ink); outline-offset:2px }
  .bad { color:#a13d2d; font-size:13px; margin:0 0 14px } .ok { color:#2f6b3a; font-size:14px; margin:0 0 14px }
  .row { display:flex; justify-content:space-between; gap:12px; margin-top:16px; font-size:13px } a { color:var(--ink) }
  details { margin-top:18px; border-top:1px solid var(--line); padding-top:14px; font-size:13px } summary { cursor:pointer; color:var(--mute) }
  details form { margin-top:12px } .foot { margin-top:18px; font-size:12px; color:var(--mute) }
"""


def auth_page(title: str, body: str) -> str:
    """The pages a person sees before they are signed in: sign in, forgot password, set a password."""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html_escape(title)} · Closeout</title><style>{AUTH_CSS}</style></head><body><main class="card"><div class="wordmark">CLOSEOUT</div>{body}</main></body></html>')


def create_app(settings: Settings = SETTINGS) -> FastAPI:
    app = FastAPI(title="Closeout", version="0.3")
    feeds: dict[str, RunFeed] = {}
    pending: list[RunFeed] = []  # feeds whose run_id is not known yet (ingest still running)
    lock = threading.Lock()
    state = {"active": None}  # run_id of the run currently executing, if any
    compare_cache: dict[tuple, dict] = {}

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
        return {"model_id": settings.model_id, "active_run_id": active, "office": settings.office,
                "projects": [project_card(st, p, active) for p in st.projects()], "usage": usage_mod.summary(st)}

    @app.get("/api/activity")
    def activity() -> dict:
        """Every project's email in and out, and what happened, newest first, for the office's Emails and Updates pages."""
        return activity_mod.summary(store())

    @app.get("/api/usage")
    def usage() -> dict:
        """What the reading and answering has cost so far, run by run, for the office's home page."""
        return usage_mod.summary(store())

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

    def _worked_out(st: Store, run: dict) -> dict:
        """What one filing run did with a drop's files, in plain shape: which items got which files, what stayed loose."""
        byitem: dict[str, list[str]] = {}
        loose: list[str] = []
        for f in st.findings_for_run(run["id"]):
            name = (st.evidence(f["evidence_id"]) or {}).get("filename") or ""
            if f["status"] == "matched" and f.get("item_id"):
                if name not in byitem.setdefault(f["item_id"], []):
                    byitem[f["item_id"]].append(name)
            elif f["status"] in ("ambiguous", "unrelated", "conflict") and name and name not in loose:
                loose.append(name)
        return {"items": [{"item_id": k, "files": v} for k, v in byitem.items()], "loose": loose}

    @app.get("/api/projects/{slug}")
    def project_detail(slug: str) -> dict:
        """Everything one project page needs: drawings, deficiency list, latest packet, drops, messages."""
        st = store()
        prj = _project(st, slug)
        pid = prj["id"]
        view = project_mod.project_view(st, pid) or {"id": pid, "slug": prj["slug"], "name": prj["name"], "sheets": [], "units": [],
                                                    "levels": [], "spaces": [], "documents": [], "disciplines": [], "address": "", "city": ""}
        in_set: set[str] = set()
        for code in {d["discipline"] for d in view["documents"] if d.get("kind") == "drawing"}:
            in_set |= revisions_mod.set_members(view["documents"], code)
        for d in view["documents"]:
            d["in_set"] = d["id"] in in_set
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
        out = {"project": view, "card": project_card(st, prj, active), "register": st.deficiencies(pid),
                "runs": runs, "latest_run_id": latest, "active_run_id": active if any(r["id"] == active for r in runs) else None,
                "packet": build_packet(st, latest) if latest else None, "batches": batches,
                "messages": st.all_drafts(pid), "decisions": st.decisions(pid), "model_id": settings.model_id,
                "reviews": coverage_mod.decorate_reviews(st, prj, st.reviews(pid)), "shares": st.shares(pid), "office": settings.office,
                "sends": st.sends(pid), "inbound": st.inbound_for_project(pid),
                "notes": [_note_view(prj["slug"], n) for n in st.site_notes(pid)],
                "mail": {"from": settings.mail_from, "gmail": _mail_address(st), "can_connect": True},
                "stages": {code: coverage_mod.stages_for(prj, code) for code in sorted({*(prj.get("stages") or {}), *(r["discipline"] for r in st.reviews(pid)), *(d["discipline"] for d in st.documents(pid) if d.get("kind") == "drawing" and d.get("discipline"))})},
                "units": coverage_mod.buildings(prj),
                "docs_review": prj.get("docs_review"), "docs_scope": prj.get("docs_scope") or [],
                "occupancy_docs": [list(row) for row in documents_mod.OCCUPANCY_DOCS],
                "filings": st.filings(pid), "filing_history": st.filing_history(pid), "document_log": st.document_log(pid), "folders": st.folders(pid),
                "drawings_reviews": st.drawings_reviews(pid), "seen": st.seen(pid)}
        checks = replies_mod.check(st, pid, out["inbound"])
        for x in out["inbound"]:
            x["check"] = checks.get(x["id"])
        out["history"] = history_mod.build(sends=out["sends"], inbound=out["inbound"], batches=batches, shares=out["shares"], reviews=out["reviews"],
                                           messages=out["messages"], filings=out["filing_history"], document_log=out["document_log"],
                                           drawings_reviews=out["drawings_reviews"], runs=runs, decisions=out["decisions"], register=out["register"])
        return out

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

    @app.get("/api/sheets/{sheet_id}/crop")
    def sheet_crop(sheet_id: str, x: float, y: float, w: float, h: float):
        """One box of a sheet as a JPEG (a unit's floor plan, say); cut once from the full render and kept next to it."""
        sh = store().sheet(sheet_id)
        if not sh or not Path(sh["image_path"]).exists():
            raise HTTPException(404, "no such sheet")
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x + 1e-6 and 0 < h <= 1 - y + 1e-6):
            raise HTTPException(400, "the box must lie within the sheet")
        src = Path(sh["image_path"])
        key = hashlib.sha1(f"{x:.4f},{y:.4f},{w:.4f},{h:.4f}".encode()).hexdigest()[:10]
        out = src.with_name(f"{src.stem}.crop-{key}.jpg")
        if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
            from PIL import Image
            with Image.open(src) as im:
                im = im.convert("RGB")
                W, H = im.size
                box = (int(x * W), int(y * H), min(W, int((x + w) * W) + 1), min(H, int((y + h) * H) + 1))
                im = im.crop(box)
                im.thumbnail((1100, 1100))
                im.save(out, "JPEG", quality=84)
        return FileResponse(out, media_type="image/jpeg")

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

    @app.get("/api/projects/{slug}/register/{item_id}/photos/{n}")
    def more_photo(slug: str, item_id: str, n: int):
        """Photo n (2, 3, ...) of a deficiency; photo 1 is the reference above."""
        st = store()
        d = st.deficiency(_project(st, slug)["id"], item_id)
        more = ((d or {}).get("ref_meta") or {}).get("more_photos") or []
        if not 2 <= n < len(more) + 2 or not Path(more[n - 2]["path"]).exists():
            raise HTTPException(404, "no such photo")
        return FileResponse(Path(more[n - 2]["path"]), media_type="image/jpeg")

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
        documents_mod.record_placements(st, prj["id"], out.get("placed") or [])
        return {"docs_review": review, "filings": st.filings(prj["id"])}

    # --- drawings review: read one discipline's set before the walk, bought sheet by sheet --------------------------
    def _drawings_review(st: Store, prj: dict, review_id: str) -> dict:
        rv = st.drawings_review(review_id)
        if not rv or rv["project_id"] != prj["id"]:
            raise HTTPException(404, "no such drawings review")
        return rv

    @app.get("/api/projects/{slug}/drawings/estimate")
    def drawings_estimate(slug: str, discipline: str = "EL") -> dict:
        """What reading this discipline's current sheets will cost, before anything is bought."""
        st = store()
        prj = _project(st, slug)
        return drawings_mod.estimate(st, prj["id"], discipline.upper(), settings)

    @app.post("/api/projects/{slug}/drawings/reviews")
    def drawings_start(slug: str, body: DrawingsReviewIn = DrawingsReviewIn()) -> dict:
        """Open a review of one discipline's set. Costs nothing until a sheet is read."""
        st = store()
        prj = _project(st, slug)
        try:
            return {"review": drawings_mod.start(st, prj["id"], body.discipline.upper(), settings)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/projects/{slug}/drawings/reviews/{review_id}/sheets/{sheet_id}")
    def drawings_sheet(slug: str, review_id: str, sheet_id: str) -> dict:
        """Read one sheet: one paid call. Returns the review with that sheet's findings and the cost so far."""
        st = store()
        prj = _project(st, slug)
        _drawings_review(st, prj, review_id)
        try:
            out = drawings_mod.review_sheet(st, review_id, sheet_id, settings)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"that sheet could not be read ({type(e).__name__}); continue to try it again")
        return {**out, "review": st.drawings_review(review_id)}

    @app.post("/api/projects/{slug}/drawings/reviews/{review_id}/finish")
    def drawings_finish(slug: str, review_id: str) -> dict:
        """One short call over every sheet's findings: gaps in the set and its summary. Closes the review."""
        st = store()
        prj = _project(st, slug)
        rv = _drawings_review(st, prj, review_id)
        if rv["status"] == "done":
            return {"review": rv}
        try:
            return {"review": drawings_mod.finish(st, review_id, settings)}
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"the set summary could not be written ({type(e).__name__}); try finish again")

    @app.delete("/api/projects/{slug}/drawings/reviews/{review_id}")
    def drawings_delete(slug: str, review_id: str) -> dict:
        st = store()
        prj = _project(st, slug)
        _drawings_review(st, prj, review_id)
        st.delete_drawings_review(review_id)
        return {"ok": True}

    @app.post("/api/projects/{slug}/disciplines")
    def add_discipline(slug: str, body: DisciplineIn) -> dict:
        """The engineer adds a discipline folder the drawings never brought (a sprinkler set still to come, say). It shows
        under the site and in every building's move list from now on; files can be filed into it and every drop logs it."""
        st = store()
        prj = _project(st, slug)
        code = re.sub(r"[^A-Z0-9]", "", body.code.strip().upper())[:4]
        if not code:
            raise HTTPException(400, "give the folder a short code, such as SP")
        name = " ".join(body.name.split())[:60] or project_mod.DISCIPLINES.get(code, "")
        if not name:
            raise HTTPException(400, "say what the folder is called, such as Sprinkler")
        discs = list(prj["model"].get("disciplines") or [])
        if any(d.get("code") == code for d in discs):
            raise HTTPException(400, f"{code} is already a folder on this project")
        discs.append({"code": code, "name": name, "current_set": "", "dated": None, "sheets": 0, "added_by": "engineer", "added_at": now()})
        st.set_project_model(prj["id"], {**prj["model"], "disciplines": discs})
        return {"disciplines": discs}

    @app.patch("/api/projects/{slug}/disciplines/{code}")
    def rename_discipline(slug: str, code: str, body: DisciplineRename) -> dict:
        """The engineer renames a folder they added themselves (SP from Sprinkler to Sump Pump, say). The code stays, so
        every file and review filed under it stays where it is. Folders the drawings brought keep their names."""
        st = store()
        prj = _project(st, slug)
        code = code.strip().upper()
        name = " ".join(body.name.split())[:60]
        if len(name) < 2:
            raise HTTPException(400, "say what the folder should be called")
        discs = list(prj["model"].get("disciplines") or [])
        d = next((x for x in discs if x.get("code") == code), None)
        if (d is None and any(s.get("discipline") == code for s in st.sheets(prj["id"]))) or (d and d.get("added_by") != "engineer"):
            raise HTTPException(400, f"{code} came with the drawings and keeps its name")
        if d is None:
            raise HTTPException(404, f"{code} is not a folder on this project")
        discs = [{**x, "name": name, "renamed_at": now()} if x.get("code") == code else x for x in discs]
        st.set_project_model(prj["id"], {**prj["model"], "disciplines": discs})
        return {"disciplines": discs}

    @app.delete("/api/projects/{slug}/disciplines/{code}")
    def remove_discipline(slug: str, code: str) -> dict:
        """The engineer removes a folder they added themselves, while it is still empty: nothing filed into it, no
        drawings of it, no review walked under it. Folders the drawings brought cannot be removed."""
        st = store()
        prj = _project(st, slug)
        pid = prj["id"]
        code = code.strip().upper()
        discs = list(prj["model"].get("disciplines") or [])
        d = next((x for x in discs if x.get("code") == code), None)
        if d is None:
            raise HTTPException(404, f"{code} is not a folder on this project")
        if d.get("added_by") != "engineer":
            raise HTTPException(400, f"{code} came with the drawings and stays")
        if any(s.get("discipline") == code for s in st.sheets(pid)) or any(x.get("discipline") == code for x in st.documents(pid)) \
                or any((f or {}).get("discipline") == code for f in st.filings(pid).values()):
            raise HTTPException(400, f"{code} has files in it; move them first")
        if any(r["discipline"] == code for r in st.reviews(pid)):
            raise HTTPException(400, f"a field review was walked under {code}; it stays")
        discs = [x for x in discs if x.get("code") != code]
        st.set_project_model(pid, {**prj["model"], "disciplines": discs})
        return {"disciplines": discs}

    def _folder_parents(st, prj: dict) -> set[str]:
        """Every place in the Documents tree a folder of the engineer's own can go: the project folder, the site, each
        building, a discipline under either, or inside another folder of their own."""
        pid = prj["id"]
        codes = {d.get("code") for d in prj["model"].get("disciplines") or []} | {s["discipline"] for s in st.sheets(pid)} \
            | {d["discipline"] for d in st.documents(pid)}
        codes.discard(None); codes.discard("")
        keys = {"prj", "site"} | {f"site/{c}" for c in codes} | {"u/" + f["id"] for f in st.folders(pid)}
        for b in coverage_mod.buildings(prj):
            keys |= {f"b/{b['key']}"} | {f"b/{b['key']}/{c}" for c in codes}
        return keys

    def _folder_name(body_name: str) -> str:
        name = " ".join((body_name or "").split())[:60]
        if not name:
            raise HTTPException(400, "say what the folder is called")
        if any(ch in name for ch in "/\\|"):
            raise HTTPException(400, "a folder name cannot hold / \\ or |")
        return name

    @app.post("/api/projects/{slug}/folders")
    def add_folder(slug: str, body: FolderIn) -> dict:
        """The engineer makes a folder of their own anywhere in the project folder: under the site, a building, a
        discipline, or inside another of their folders, as deep as they like. Nothing on disk changes; files are
        filed into it the same way they are moved, with every move kept in the history."""
        st = store()
        prj = _project(st, slug)
        pid = prj["id"]
        name, parent = _folder_name(body.name), (body.parent or "").strip()
        if parent not in _folder_parents(st, prj):
            raise HTTPException(400, "that is not a folder in the project folder")
        if any(f["parent"] == parent and f["name"].lower() == name.lower() for f in st.folders(pid)):
            raise HTTPException(400, f"there is already a folder called {name} there")
        folder = st.add_folder(pid, parent, name)
        return {"folder": folder, "folders": st.folders(pid)}

    @app.patch("/api/projects/{slug}/folders/{folder_id}")
    def rename_folder(slug: str, folder_id: str, body: FolderRename) -> dict:
        """Rename one of the engineer's own folders. Its files and folders stay inside it."""
        st = store()
        prj = _project(st, slug)
        pid = prj["id"]
        name, folders = _folder_name(body.name), st.folders(pid)
        f = next((x for x in folders if x["id"] == folder_id), None)
        if f is None:
            raise HTTPException(404, "no such folder")
        if any(x["id"] != folder_id and x["parent"] == f["parent"] and x["name"].lower() == name.lower() for x in folders):
            raise HTTPException(400, f"there is already a folder called {name} there")
        st.rename_folder(pid, folder_id, name)
        return {"folders": st.folders(pid)}

    @app.delete("/api/projects/{slug}/folders/{folder_id}")
    def remove_folder(slug: str, folder_id: str) -> dict:
        """Remove one of the engineer's own folders while it is empty: no folders inside it and no file filed in it."""
        st = store()
        prj = _project(st, slug)
        pid = prj["id"]
        folders = st.folders(pid)
        if not any(x["id"] == folder_id for x in folders):
            raise HTTPException(404, "no such folder")
        if any(x["parent"] == "u/" + folder_id for x in folders):
            raise HTTPException(400, "it has folders inside it; remove or empty those first")
        if any((f or {}).get("folder") == folder_id for f in st.filings(pid).values()):
            raise HTTPException(400, "it has files in it; move them first")
        st.delete_folder(pid, folder_id)
        return {"folders": st.folders(pid)}

    @app.get("/api/projects/{slug}/filing")
    def filing(slug: str) -> dict:
        """Where every file sits in the project folder tree now, and every move behind it."""
        st = store()
        prj = _project(st, slug)
        return {"filings": st.filings(prj["id"]), "history": st.filing_history(prj["id"])}

    @app.post("/api/projects/{slug}/filing")
    def file_document(slug: str, body: FilingIn) -> dict:
        """The engineer moves or renames a file: a new row in its history, the old one kept so it can be undone."""
        st = store()
        prj = _project(st, slug)
        view = project_mod.project_view(st, prj["id"]) or {}
        try:
            row = documents_mod.file_by_engineer(st, prj["id"], view, body.file, body.building, body.discipline, body.name, body.folder)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"filing": row, "filings": st.filings(prj["id"]), "history": st.filing_history(prj["id"])}

    @app.post("/api/projects/{slug}/filing/undo")
    def undo_filing(slug: str, body: FilingUndoIn) -> dict:
        """Put the file back where it was before the last move or rename."""
        st = store()
        prj = _project(st, slug)
        before = st.undo_filing(prj["id"], body.file)
        if before is None:
            raise HTTPException(404, "nothing to undo for that file")
        return {"filing": before, "filings": st.filings(prj["id"]), "history": st.filing_history(prj["id"])}

    @app.get("/api/projects/{slug}/revisions")
    def revisions(slug: str) -> dict:
        """Every issue of every drawing set, oldest first per discipline, so a later issue can be compared with the one before."""
        st = store()
        prj = _project(st, slug)
        return {"issues": revisions_mod.issues(st.documents(prj["id"]))}

    @app.post("/api/projects/{slug}/revisions/compare")
    def compare_revisions(slug: str, body: RevisionCompareIn) -> dict:
        """What changed between two issues of the same discipline's set: sheets added, removed, renumbered, and the printed
        words that differ on the sheets both issues hold. Plain code over the text layer; nothing is sent to a model."""
        st = store()
        prj = _project(st, slug)
        docs = st.documents(prj["id"])
        old = next((d for d in docs if d["id"] == body.old_document_id), None)
        new = next((d for d in docs if d["id"] == body.new_document_id), None)
        if not old or not new:
            raise HTTPException(404, "no such document")
        if old["kind"] != "drawing" or new["kind"] != "drawing" or old["discipline"] != new["discipline"]:
            raise HTTPException(400, "compare two issues of the same discipline's drawing set")
        return _compare(prj, old, new, docs)

    def _compare(prj: dict, old: dict, new: dict, docs: list[dict]) -> dict:
        root = Path(prj["source_root"] or "").resolve()
        for d in (old, new):
            f = (root / d["rel_path"]).resolve()
            if not prj["source_root"] or not f.is_relative_to(root) or not f.is_file():
                raise HTTPException(404, "file missing from the project folder")
        key = (prj["id"], old["id"], old["sha256"], new["id"], new["sha256"])
        if key not in compare_cache:  # a big set takes seconds to read; the files do not change under their hash
            compare_cache[key] = revisions_mod.compare_documents(root, old, new, docs)
        return compare_cache[key]

    @app.get("/api/projects/{slug}/field/{disc}/brief")
    def field_brief(slug: str, disc: str) -> dict:
        """Before the walk: the set to carry, what changed since the issue before, what is still open, what is
        missing, who to ask. Read from what the project already holds; nothing is sent to a model."""
        st = store()
        prj = _project(st, slug)
        return brief_mod.field_brief(st, prj["id"], disc.upper(), compare=lambda o, n: _compare(prj, o, n, st.documents(prj["id"])))

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
    def document_file(slug: str, doc_id: str, download: bool = False):
        """The file itself, straight from the project folder, so a row in the Documents tab opens the PDF.
        With ?download=1 the browser saves it under its own name instead of showing it."""
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
                            content_disposition_type="attachment" if download else "inline")

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
        if code not in project_mod.DISCIPLINES and not any(d.get("code") == code for d in prj["model"].get("disciplines") or []):
            raise HTTPException(400, f"unknown discipline '{body.discipline}'")
        for r in st.reviews(prj["id"]):
            if r["discipline"] == code and r["status"] == "active":
                return {"review": coverage_mod.decorate_reviews(st, prj, [r])[0], "resumed": True}
        r = st.create_review(prj["id"], code, body.title.strip(), stage=body.stage)
        return {"review": coverage_mod.decorate_reviews(st, prj, [r])[0], "resumed": False}

    @app.patch("/api/projects/{slug}/reviews/{review_id}")
    def patch_review(slug: str, review_id: str, body: ReviewPatch) -> dict:
        """The reviewer's own words on the walk: which stage it was, which units were covered."""
        st = store()
        prj = _project(st, slug)
        r = st.review(review_id)
        if not r or r["project_id"] != prj["id"]:
            raise HTTPException(404, "no such review")
        if body.stage is not None:
            st.set_review_stage(review_id, body.stage[:80])
        if body.units_reset:
            st.set_review_units(review_id, None)
        elif body.units is not None:
            known = coverage_mod.unit_labels(prj)
            bad = [u for u in body.units if u not in known]
            if bad:
                raise HTTPException(400, f"not a unit of this project: {', '.join(bad[:3])}")
            st.set_review_units(review_id, [u for u in known if u in body.units])
        return {"review": coverage_mod.decorate_reviews(st, prj, [st.review(review_id)])[0]}

    @app.patch("/api/projects/{slug}/stages")
    def patch_stages(slug: str, body: StagesPatch) -> dict:
        """The office's list of walks for one discipline on this project, in order."""
        st = store()
        prj = _project(st, slug)
        code = body.discipline.strip().upper()
        if code not in project_mod.DISCIPLINES:
            raise HTTPException(400, f"unknown discipline '{body.discipline}'")
        stages = [x.strip()[:60] for x in body.stages if x.strip()]
        if not stages:
            raise HTTPException(400, "give at least one stage")
        st.set_stages(prj["id"], code, stages)
        return {"discipline": code, "stages": coverage_mod.stages_for(st.project(prj["id"]), code)}

    @app.get("/api/projects/{slug}/field/{disc}/coverage")
    def field_coverage(slug: str, disc: str) -> dict:
        """Units × stages for one discipline: which walks covered which unit, what is still pending. Read, not generated."""
        st = store()
        prj = _project(st, slug)
        return coverage_mod.coverage(st, prj, disc.upper())

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
            views.append({"title": v.title.strip()[:80], "level": v.level.strip(), "unit": v.unit.strip()[:80], "x": v.x, "y": v.y, "w": v.w, "h": v.h,
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
        before = st.draft_for_review(review_id)
        did = st.upsert_draft(run_id, "", out["subject"], out["body"], review_id=review_id)
        if before and before.get("to_addr"):   # drafting again keeps who it is for
            st.update_draft(did, status="draft", to=before["to_addr"])
        return st.draft_for_review(review_id) if did else None, None

    def _conversation(st: Store, prj: dict, conversation_id: str) -> dict:
        conv = st.conversation(conversation_id)
        if not conv or conv["project_id"] != prj["id"]:
            raise HTTPException(404, "no such conversation on this project")
        return conv

    @app.post("/api/projects/{slug}/ask")
    def ask_project(slug: str, body: AskBody) -> dict:
        """One model call: answer from the records, optionally move the screen. Reads only.
        Every question and answer is kept in a conversation on the project, so it can be read or continued later."""
        st = store()
        prj = _project(st, slug)
        q = " ".join(str(body.question or "").split())
        conv = _conversation(st, prj, body.conversation_id) if body.conversation_id else None
        history = body.history or ([{"q": t["q"], "a": t["a"]} for t in st.turns(conv["id"])[-8:]] if conv else [])
        run_id = st.create_run(prj["id"], batch_id="", model_id=ask_mod.ask_model_id(settings), kind="ask")
        try:
            out = ask_mod.ask(st, prj["id"], q, body.where, settings, office=settings.office,
                              history=history, spoken=body.spoken)
        except ValueError as e:
            st.finish_run(run_id, "failed", {"error": str(e)})
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            st.finish_run(run_id, "failed", {"error": f"{type(e).__name__}: {e}"})
            raise HTTPException(502, "Closeout could not answer that just now; ask again")
        st.finish_run(run_id, "done", out["usage"])
        if conv is None:
            conv = st.create_conversation(prj["id"], q[:72])
        turn_id = st.add_turn(conv["id"], q, out["text"], out.get("go"), out.get("action"), body.spoken)
        return {"answer": out["text"], "go": out["go"], "action": out.get("action"), "usage": out["usage"],
                "conversation_id": conv["id"], "conversation_title": conv["title"], "turn_id": turn_id}

    @app.get("/api/projects/{slug}/conversations")
    def list_conversations(slug: str) -> dict:
        st = store()
        prj = _project(st, slug)
        return {"conversations": st.conversations(prj["id"])}

    @app.post("/api/projects/{slug}/conversations")
    def start_conversation(slug: str, body: ConversationBody) -> dict:
        st = store()
        prj = _project(st, slug)
        return st.create_conversation(prj["id"], body.title)

    @app.get("/api/projects/{slug}/conversations/{conversation_id}")
    def read_conversation(slug: str, conversation_id: str) -> dict:
        st = store()
        conv = _conversation(st, _project(st, slug), conversation_id)
        return {**conv, "turns": st.turns(conversation_id)}

    @app.patch("/api/projects/{slug}/conversations/{conversation_id}")
    def rename_conversation(slug: str, conversation_id: str, body: ConversationBody) -> dict:
        st = store()
        _conversation(st, _project(st, slug), conversation_id)
        if not body.title.strip():
            raise HTTPException(400, "give it a name")
        st.rename_conversation(conversation_id, body.title)
        return st.conversation(conversation_id)

    @app.delete("/api/projects/{slug}/conversations/{conversation_id}")
    def delete_conversation(slug: str, conversation_id: str) -> dict:
        st = store()
        _conversation(st, _project(st, slug), conversation_id)
        st.delete_conversation(conversation_id)
        return {"deleted": conversation_id}

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
    def finish_review(slug: str, review_id: str, body: FinishIn | None = None) -> dict:
        """Close the walk: freeze what goes to the contractor and have the agent draft the covering message.
        Nothing is sent; the draft waits, with the address the engineer gave, for the engineer to press Send."""
        st = store()
        prj = _project(st, slug)
        r = st.review(review_id)
        if not r or r["project_id"] != prj["id"]:
            raise HTTPException(404, "no such review")
        to = " ".join((body.to if body else "").split())
        if to and not mail_mod.valid_address(to):
            raise HTTPException(400, "that email address does not look right")
        st.finish_review(review_id)
        pkg = review_mod.review_package(st, prj["id"], review_id)
        st.set_review_package(review_id, pkg)
        message, error = (st.draft_for_review(review_id), None) if st.draft_for_review(review_id) else _draft_review_message(st, prj, review_id)
        if message and to:
            st.update_draft(message["id"], status=message["status"], to=to)
            message = st.draft_for_review(review_id)
        return {"review": st.review(review_id), "package": pkg, "message": message, "error": error}

    @app.get("/api/projects/{slug}/reviews/{review_id}/report.json")
    def review_report_json(slug: str, review_id: str) -> dict:
        """The field review report as data: every item with its status, the drawings on file, the sign-off fields."""
        st = store()
        prj = _project(st, slug)
        try:
            return report_mod.review_report(st, prj["id"], review_id, office=settings.office)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/api/projects/{slug}/reviews/{review_id}/report", include_in_schema=False)
    def review_report_page(slug: str, review_id: str):
        """The printable report. Built from the saved walk; nothing is sent and no wording is generated."""
        st = store()
        prj = _project(st, slug)
        try:
            data = report_mod.review_report(st, prj["id"], review_id, office=settings.office)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        return HTMLResponse(report_mod.report_html(data))

    @app.get("/api/projects/{slug}/items/{item_id}/pin.jpg")
    def item_pin_crop(slug: str, item_id: str):
        """A close-up of the sheet around the item's pin, with the pin drawn on. For the report and the item page."""
        st = store()
        d = st.deficiency(_project(st, slug)["id"], item_id)
        if not d or d.get("pin_x") is None or not d.get("sheet_id"):
            raise HTTPException(404, "no pin for this item")
        sh = st.sheet(d["sheet_id"])
        if not sh or not Path(sh["image_path"]).exists():
            raise HTTPException(404, "sheet image missing")
        crop, _whole = review_mod.pin_images(Path(sh["image_path"]), d["pin_x"], d["pin_y"])
        return Response(crop, media_type="image/jpeg")

    LINK_LINE = "Or send your photos through this page: "

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

    @app.post("/api/projects/{slug}/reviews/{review_id}/send")
    def send_review_message(slug: str, review_id: str, body: SendIn, request: Request) -> dict:
        """The engineer sends the covering message to the contractor. Only on their press, one message at a time.
        From the connected mailbox (Gmail or the office's work email) or a verified sender the app sends it; otherwise the
        mail app sends it and this records that. The subject carries the review's reference, so a reply finds its way home
        even outside the thread."""
        st = store()
        prj = _project(st, slug)
        r = st.review(review_id)
        if not r or r["project_id"] != prj["id"]:
            raise HTTPException(404, "no such review")
        to = body.to.strip()
        if not mail_mod.valid_address(to):
            raise HTTPException(400, "give the contractor's email address")
        share = st.share_for_review(review_id)
        if share and share.get("revoked_at"):
            share = None
        msg = st.draft_for_review(review_id)
        if not msg:
            raise HTTPException(409, "there is no message for this review yet")
        subject = inbox_mod.with_ref(" ".join((body.subject if body.subject is not None else msg["subject"]).split())[:190], review_id)
        text = (body.body if body.body is not None else msg["body"]).strip()
        account = _usable_account(st)
        if body.via == "mail-app":
            via = "mail-app"
        elif account:
            via = "gmail" if account["kind"] == "gmail" else "email"
        elif mail_mod.can_send(settings):
            via = "ses"
        else:
            via = "mail-app"
        message_id = thread_id = report = ""
        if via in ("gmail", "email", "ses"):    # the items report travels with it: photos, plan marks, the page link
            report, pdf = notice_mod.build_notice(st, prj["id"], review_id, settings.office, _share_url(request, share["id"]) if share else "")
            _keep_report(slug, report, pdf)
        try:
            if via in ("gmail", "email"):
                out = _mailbox(account).send(to, subject, text, [(report, pdf, "application/pdf")])
                message_id, thread_id = out["id"], out["thread_id"]
            elif via == "ses":
                message_id = mail_mod.send_email(settings, to, subject, text, [(report, pdf, "application/pdf")])
        except Exception as e:  # the mail service refused; nothing recorded, the engineer sees why
            log.warning("send refused: %s", type(e).__name__)
            raise HTTPException(502, "the email could not be sent; the message is unchanged, try again or use your mail app")
        sent = st.record_send(prj["id"], review_id, msg["id"], to, subject, text, via, message_id, thread_id, report)
        st.update_draft(msg["id"], status=msg["status"], to=to)
        st.touch_project(prj["id"])
        return {"send": sent, "sends": st.sends(prj["id"])}

    @app.post("/api/projects/{slug}/items/{item_id}/send")
    def send_item_message(slug: str, item_id: str, body: SendIn) -> dict:
        """The engineer tells the contractor about one item: not accepted, on hold, or what is still needed. Only on their
        press. It carries the review's reference, so the reply is filed to that review like any other."""
        st = store()
        prj = _project(st, slug)
        d = st.deficiency(prj["id"], item_id)
        if not d:
            raise HTTPException(404, "no such item")
        review_id = d.get("review_id") or ""
        if not review_id or not st.review(review_id):
            raise HTTPException(409, "this item is not from a field review, so a reply would have nowhere to go; copy the wording instead")
        to = body.to.strip()
        if not mail_mod.valid_address(to):
            raise HTTPException(400, "give the contractor's email address")
        subject = " ".join((body.subject or "").split())[:190]
        text = (body.body or "").strip()
        if not subject or not text:
            raise HTTPException(400, "write the subject and the message")
        subject = inbox_mod.with_ref(subject, review_id)
        account = _usable_account(st)
        via = "mail-app" if body.via == "mail-app" else ("gmail" if account["kind"] == "gmail" else "email") if account else "ses" if mail_mod.can_send(settings) else "mail-app"
        message_id = thread_id = ""
        try:
            if via in ("gmail", "email"):
                out = _mailbox(account).send(to, subject, text)
                message_id, thread_id = out["id"], out["thread_id"]
            elif via == "ses":
                message_id = mail_mod.send_email(settings, to, subject, text)
        except Exception as e:  # the mail service refused; nothing recorded, the engineer sees why
            log.warning("item send refused: %s", type(e).__name__)
            raise HTTPException(502, "the email could not be sent; the message is unchanged, try again or use your mail app")
        draft = next((x for x in st.all_drafts(prj["id"]) if x["item_id"] == item_id and not x.get("review_id")), None)
        sent = st.record_send(prj["id"], review_id, draft["id"] if draft else "", to, subject, text, via, message_id, thread_id)
        st.touch_project(prj["id"])
        return {"send": sent, "sends": st.sends(prj["id"])}

    def _keep_report(slug: str, name: str, pdf: bytes) -> None:
        """A copy of what was sent, in the project's data folder, so the record can be opened later."""
        d = settings.data_dir / "projects" / slug / "reports"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(pdf)

    @app.get("/api/projects/{slug}/reviews/{review_id}/items.pdf", include_in_schema=False)
    def review_items_pdf(slug: str, review_id: str, request: Request):
        """The items report as it travels with the message: to look at before sending, or to attach by hand."""
        st = store()
        prj = _project(st, slug)
        r = st.review(review_id)
        if not r or r["project_id"] != prj["id"]:
            raise HTTPException(404, "no such review")
        share = st.share_for_review(review_id)
        link = _share_url(request, share["id"]) if share and not share.get("revoked_at") else ""
        name, pdf = notice_mod.build_notice(st, prj["id"], review_id, settings.office, link)
        return Response(pdf, media_type="application/pdf", headers={"content-disposition": f'inline; filename="{name}"'})

    # --- the office's mailbox: connected once, read only where Closeout is expected ---------------------------------
    def gs() -> Settings:
        """Settings with the Google client the office saved on its page, when the server's own is not set."""
        if settings.google_client_id and settings.google_client_secret:
            return settings
        st = store()
        cid, sec = st.setting("google_client_id"), st.setting("google_client_secret")
        return dataclasses.replace(settings, google_client_id=cid, google_client_secret=sec) if cid and sec else settings

    def _mailbox(account: dict):
        """The connected mailbox: Google's API for a Gmail sign-in, IMAP and SMTP for a work email."""
        if account.get("kind", "gmail") == "gmail":
            return gmail_mod.Gmail(gs(), account["refresh_token"], account["address"])
        if account["kind"] == "outlook":
            return outlook_mod.Outlook(settings, account["refresh_token"], account["address"], on_token=store().set_mail_token)
        return mailbox_mod.ImapMail(account["address"], account["password"], account["imap_host"], account["imap_port"],
                                    account["smtp_host"], account["smtp_port"], account["username"])

    def _usable_account(st: Store) -> dict | None:
        """The connected mailbox when it can be used: a Gmail or Microsoft sign-in also needs the server's client for it."""
        account = st.mail_account()
        if account and account.get("kind", "gmail") == "gmail" and not gmail_mod.configured(gs()):
            return None
        if account and account.get("kind") == "outlook" and not outlook_mod.configured(settings):
            return None
        return account

    def _mail_address(st: Store) -> str:
        return (_usable_account(st) or {}).get("address", "")

    def _mail_state(request: Request, provider: str = "gmail") -> str:
        return hmac.new(_access_token().encode(), f"connect-{provider}".encode(), hashlib.sha256).hexdigest()[:32]

    def _redirect_uri(request: Request, provider: str = "") -> str:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        return f"{proto}://{host}/api/mail/callback" + (f"/{provider}" if provider else "")

    def _run_batch(project_id: str, files: list[Path], label: str, root: Path, via: str) -> None:
        """The filing desk for mail: same guard and same pipeline as a drop, tagged with where it came from."""
        try:
            feed = _reserve()
        except HTTPException:
            raise RuntimeError("busy")

        def target(progress):
            def tagged(event, data):
                if event == "ingested" and data.get("batch_id"):
                    store().set_batch_via(data["batch_id"], via)
                progress(event, data)
            pipeline.process_batch(store(), project_id, files, label, settings, progress=tagged, root=root)

        feed.push("uploaded", {"label": label, "files": len(files), "folder": str(root)})
        _launch(target, feed)

    def _mail_check() -> dict:
        st = store()
        account = _usable_account(st)
        if not account:
            raise HTTPException(409, "no mailbox is connected")
        try:
            return inbox_mod.check(st, settings, _mailbox(account), _run_batch)
        except mailbox_mod.MailError as e:  # a sentence the office can act on; never the password
            log.warning("mail check failed: %s", type(e).__name__)
            st.touch_mail_check(str(e)[:200])
            raise HTTPException(502, f"the mailbox could not be read: {e}")
        except Exception as e:  # the mailbox could not be read; the Office page shows why
            log.warning("mail check failed: %s", type(e).__name__)
            st.touch_mail_check(f"{type(e).__name__}: {str(e)[:120]}")
            raise HTTPException(502, "the mailbox could not be read just now")

    def _mail_status() -> dict:
        st = store()
        account = st.mail_account()
        unplaced = []
        for r in st.inbound_unplaced():
            unplaced.append({k: r[k] for k in ("id", "from_addr", "subject", "text", "sent_at", "files", "at")})
        eff = gs()
        client = {"where": "server" if settings.google_client_id and settings.google_client_secret else "office" if eff.google_client_id else "",
                  "hint": eff.google_client_id[:8] if eff.google_client_id else ""}
        return {"configured": gmail_mod.configured(eff), "microsoft": outlook_mod.configured(settings), "label": settings.mail_label, "client": client,
                "account": {"address": account["address"], "kind": account.get("kind", "gmail"), "connected_at": account["connected_at"],
                            "last_check": account["last_check"], "last_error": account["last_error"],
                            "servers": f"{account['imap_host']} · {account['smtp_host']}" if account.get("kind") == "email" else "",
                            "ready": bool(_usable_account(st))} if account else None,
                "unplaced": unplaced, "projects": [{"slug": p["slug"], "name": p["name"]} for p in st.projects()]}

    @app.get("/api/mail")
    def mail_status() -> dict:
        return _mail_status()

    class SeenIn(BaseModel):
        what: str = "messages"

    @app.post("/api/projects/{slug}/seen")
    def mark_seen(slug: str, body: SeenIn) -> dict:
        st = store()
        prj = _project(st, slug)
        return {"seen": st.mark_seen(prj["id"], body.what[:40])}

    class ClientIn(BaseModel):
        client_id: str
        client_secret: str

    @app.post("/api/mail/client")
    def save_client(body: ClientIn) -> dict:
        """The office pastes its Google client here once; both values stay in the database, never in a log or a page."""
        if settings.google_client_id and settings.google_client_secret:
            raise HTTPException(409, "the server already has a Google client; it is set on the server, not here")
        cid, sec = body.client_id.strip(), body.client_secret.strip()
        if not cid.endswith(".apps.googleusercontent.com") or len(sec) < 10:
            raise HTTPException(400, "that does not look like a Google client id and secret")
        st = store()
        st.set_setting("google_client_id", cid)
        st.set_setting("google_client_secret", sec)
        return _mail_status()

    @app.delete("/api/mail/client")
    def drop_client() -> dict:
        st = store()
        if st.mail_account():
            raise HTTPException(409, "disconnect the mailbox first")
        st.drop_setting("google_client_id")
        st.drop_setting("google_client_secret")
        return _mail_status()

    @app.get("/api/mail/connect")
    def mail_connect(request: Request):
        """Hands the engineer to Google's own consent screen; the secret never reaches the browser."""
        if not gmail_mod.configured(gs()):
            raise HTTPException(409, "the server has no Google client yet; see deploy/env.example")
        return RedirectResponse(gmail_mod.auth_url(gs(), _redirect_uri(request), _mail_state(request)), status_code=303)

    @app.get("/api/mail/callback")
    def mail_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if error or not code or not hmac.compare_digest(state, _mail_state(request)):
            return RedirectResponse("/#/office?mail=refused", status_code=303)
        try:
            tokens = gmail_mod.exchange_code(gs(), code, _redirect_uri(request))
            refresh = tokens.get("refresh_token", "")
            if not refresh:
                raise RuntimeError("no refresh token")
            address = gmail_mod.Gmail(gs(), refresh).profile()
        except Exception as e:
            log.warning("gmail connect failed: %s", type(e).__name__)
            return RedirectResponse("/#/office?mail=failed", status_code=303)
        store().connect_mail(address, refresh)
        return RedirectResponse("/#/office?mail=connected", status_code=303)

    @app.get("/api/mail/connect/microsoft")
    def mail_connect_microsoft(request: Request):
        """Hands the engineer to Microsoft's own sign-in; the secret never reaches the browser."""
        if not outlook_mod.configured(settings):
            raise HTTPException(409, "the server has no Microsoft app yet; see deploy/env.example")
        uri, state = _redirect_uri(request, "microsoft"), _mail_state(request, "microsoft")
        return RedirectResponse(outlook_mod.auth_url(settings, uri, state), status_code=303)

    @app.get("/api/mail/callback/microsoft")
    def mail_callback_microsoft(request: Request, code: str = "", state: str = "", error: str = ""):
        if error or not code or not hmac.compare_digest(state, _mail_state(request, "microsoft")):
            return RedirectResponse("/#/office?mail=refused", status_code=303)
        try:
            tokens = outlook_mod.exchange_code(settings, code, _redirect_uri(request, "microsoft"))
            refresh = tokens.get("refresh_token", "")
            if not refresh:
                raise RuntimeError("no refresh token")
            box = outlook_mod.Outlook(settings, refresh)
            address = box.profile()
            if not address:
                raise RuntimeError("no address")
        except Exception as e:
            log.warning("microsoft connect failed: %s", type(e).__name__)
            return RedirectResponse("/#/office?mail=failed", status_code=303)
        store().connect_mail(address, box.refresh_token, kind="outlook")
        return RedirectResponse("/#/office?mail=connected", status_code=303)

    @app.post("/api/mail/email")
    def mail_connect_email(body: EmailIn) -> dict:
        """The office's work email, any provider: address and app password. Closeout logs in once to prove both work
        before anything is kept. The password is never logged or sent back."""
        address, password, host = body.address.strip(), body.password, body.host.strip().lower()
        if host == "google" or address.lower().endswith(("@gmail.com", "@googlemail.com")):
            password = password.replace(" ", "")          # Google shows app passwords in groups of four
        if not mail_mod.valid_address(address):
            raise HTTPException(400, "type the full work email address")
        if not password.strip():
            raise HTTPException(400, "type the app password for that address")
        box = mailbox_mod.ImapMail(address, password, body.imap_host.strip(), body.imap_port, body.smtp_host.strip(), body.smtp_port,
                                   host=host)
        try:
            box.verify()
        except mailbox_mod.MailError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            log.warning("work email connect failed: %s", type(e).__name__)
            raise HTTPException(400, "Closeout could not sign in to that mailbox; check the address, the app password and the server names")
        store().connect_mail(address, kind="email", password=password, imap_host=box.imap_host, imap_port=box.imap_port,
                             smtp_host=box.smtp_host, smtp_port=box.smtp_port)
        return _mail_status()

    @app.delete("/api/mail")
    def mail_disconnect() -> dict:
        store().disconnect_mail()
        return _mail_status()

    @app.post("/api/mail/check")
    def mail_check_now() -> dict:
        out = _mail_check()
        return {"check": out, **_mail_status()}

    @app.post("/api/mail/{inbound_id}/place")
    def mail_place(inbound_id: str, body: PlaceIn) -> dict:
        """The engineer says which review an unplaced email belongs to; its files are filed there."""
        st = store()
        row = st.inbound(inbound_id)
        if not row:
            raise HTTPException(404, "no such email")
        prj = _project(st, body.slug)
        r = st.review(body.review_id)
        if not r or r["project_id"] != prj["id"]:
            raise HTTPException(404, "no such review")
        row = st.place_inbound(inbound_id, prj["id"], body.review_id)
        if row["status"] == "queued":
            inbox_mod.file_queued(st, inbound_id, _run_batch)
        st.touch_project(prj["id"])
        return {"email": st.inbound(inbound_id), **_mail_status()}

    def _mail_poller() -> None:
        import time
        while True:
            time.sleep(settings.mail_check_seconds)
            try:
                if _usable_account(store()):
                    _mail_check()
            except Exception:  # already recorded on the account row
                pass

    if settings.mail_check_seconds > 0:
        threading.Thread(target=_mail_poller, name="closeout-mail", daemon=True).start()

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
            call = decisions.get(it["item_id"])    # the office's latest call; its note stays with the office
            items.append({"item": {k: it.get(k, "") for k in ("item_id", "location", "description", "evidence_required", "sheet", "unit", "level", "space")},
                          "completeness": "complete" if call == "accept" else stt.get("completeness", "no_evidence"),
                          "missing_slots": stt.get("missing_slots", it.get("slots") or []), "closed": call == "accept",
                          "call": call if call in ("reject", "hold") else None})
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
                              note: str = Form(""), discipline: str = Form(""), tidy: bool = Form(False), place: bool = Form(False),
                              photo: UploadFile | None = File(None)) -> dict:
        """One model call: photo + pinned sheet -> proposed location / wording / evidence. The reviewer edits and saves.
        With tidy, the reviewer's own words are the record: Closeout only puts them in order, and needs no photo."""
        st = store()
        prj = _project(st, slug)
        sh = st.sheet(sheet_id)
        if not sh or sh["project_id"] != prj["id"]:
            raise HTTPException(404, "no such sheet in this project")
        if not (0 <= pin_x <= 1 and 0 <= pin_y <= 1):
            raise HTTPException(400, "pin must be inside the sheet")
        if tidy and len(" ".join(note.split())) < 3:
            raise HTTPException(400, "write or say what you saw first; tidying works on your words")
        raw, _ = await _read_photo(photo) if not place else (None, "")
        photo_bytes = None
        if raw and not tidy:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(raw)) as im:
                im = im.convert("RGB")
                im.thumbnail((1568, 1568))
                buf = io.BytesIO(); im.save(buf, "JPEG", quality=85); photo_bytes = buf.getvalue()
        try:
            out = review_mod.suggest_field_note(st, prj["id"], sheet_id, pin_x, pin_y, photo_bytes, note, settings,
                                                discipline_hint=discipline.strip().upper(), tidy=tidy, place=place and not tidy)
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
    async def save_finding(slug: str, sheet_id: str = Form(""), pin_x: float | None = Form(None), pin_y: float | None = Form(None),
                           review_id: str = Form(...), location: str = Form(...), description: str = Form(...),
                           evidence_required: str = Form(...), discipline: str = Form(""), unit: str = Form(""),
                           level: str = Form(""), space: str = Form(""), note: str = Form(""), gps: str = Form(""),
                           photo: UploadFile | None = File(None), more_photos: list[UploadFile] | None = File(None)) -> dict:
        """The reviewer's confirmed deficiency: numbered, photo kept as the reference, and any more photos of the same spot. The sheet and the pin on it are
        optional: the photo, the unit, the level and the words are enough to record it; a pin can be added later."""
        from .register import RegisterError, parse_slots
        st = store()
        prj = _project(st, slug)
        pid = prj["id"]
        sh = st.sheet(sheet_id) if sheet_id else None
        if sheet_id and (not sh or sh["project_id"] != pid):
            raise HTTPException(404, "no such sheet in this project")
        if (pin_x is None) != (pin_y is None):
            raise HTTPException(400, "a pin needs both pin_x and pin_y")
        if pin_x is not None and (not sh or not (0 <= pin_x <= 1 and 0 <= pin_y <= 1)):
            raise HTTPException(400, "a pin needs a sheet and must be inside it")
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
        if code not in project_mod.DISCIPLINES and not any(d.get("code") == code for d in prj["model"].get("disciplines") or []):
            raise HTTPException(400, f"unknown discipline '{discipline}'")
        item_id = st.next_item_id(pid, rv["discipline"])
        raw, original = await _read_photo(photo)
        ref_path, meta = "", {}
        if raw:
            p = _field_photo_path(prj["slug"], item_id)
            p.write_bytes(raw)
            ref_path = str(p)
            meta = {**_exif(p), "original_name": original}
        more = []
        for n, extra in enumerate((more_photos or [])[:MORE_PHOTOS], start=2):
            data, name = await _read_photo(extra)
            if not data:
                continue
            q = _field_photo_path(prj["slug"], f"{item_id}-{n}")
            q.write_bytes(data)
            more.append({"path": str(q), "original_name": name, "taken_at": _exif(q).get("taken_at", "")})
        if more:
            if not ref_path:                 # the first photo that came is the reference
                first = more.pop(0)
                ref_path, meta = first["path"], {**_exif(Path(first["path"])), "original_name": first["original_name"]}
            if more:
                meta["more_photos"] = more
        if gps.strip():
            meta["gps"] = gps.strip()[:80]
        st.add_field_item(pid, item_id, location, description, evidence_required, slots, code, review_id,
                          sheet=(sh["sheet_number"] or f"{sh['discipline']} p.{sh['page']}") if sh else "", sheet_id=sheet_id,
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
        for f in [d.get("reference_photo")] + [m.get("path") for m in (d.get("ref_meta") or {}).get("more_photos") or []]:
            if f:
                Path(f).unlink(missing_ok=True)
        return {"deleted": item_id}

    # --- site photos and notes ------------------------------------------
    # Kept as they are for the office: not deficiencies, never numbered, never in the package or the message.
    def _note_view(slug: str, n: dict) -> dict:
        return {**{k: n[k] for k in ("id", "review_id", "discipline", "unit", "level", "space", "note", "created_at")},
                "sheet_id": n.get("sheet_id") or "", "pin_x": n.get("pin_x"), "pin_y": n.get("pin_y"),
                "photo_url": f"/api/projects/{slug}/notes/{n['id']}/photo" if n.get("photo") else "",
                "taken_at": (n.get("meta") or {}).get("taken_at", "")}

    @app.post("/api/projects/{slug}/notes")
    async def save_site_note(slug: str, review_id: str = Form(...), discipline: str = Form(""), unit: str = Form(""),
                             level: str = Form(""), space: str = Form(""), note: str = Form(""), gps: str = Form(""),
                             sheet_id: str = Form(""), pin_x: float | None = Form(None), pin_y: float | None = Form(None),
                             photo: UploadFile | None = File(None)) -> dict:
        st = store()
        prj = _project(st, slug)
        pid = prj["id"]
        rv = st.review(review_id)
        if not rv or rv["project_id"] != pid:
            raise HTTPException(404, "no such review")
        if rv["status"] != "active":
            raise HTTPException(409, "that review is finished; start a new one")
        note = " ".join(note.split())
        raw, original = await _read_photo(photo)
        if not raw and len(note) < 2:
            raise HTTPException(400, "a photo or a few words are needed")
        code = (discipline.strip().upper() or rv["discipline"])
        meta: dict = {}
        sheet_id = sheet_id.strip()
        if sheet_id:
            sh = st.sheet(sheet_id)
            if not sh or sh["project_id"] != pid:
                raise HTTPException(404, "no such sheet in this project")
        if (pin_x is None) != (pin_y is None) or (pin_x is not None and not (sheet_id and 0 <= pin_x <= 1 and 0 <= pin_y <= 1)):
            raise HTTPException(400, "pin must be inside a sheet")
        n = st.add_site_note(pid, review_id, code, unit=unit.strip(), level=level.strip(), space=space.strip(), note=note,
                             sheet_id=sheet_id, pin_x=pin_x, pin_y=pin_y)
        if gps.strip() and not raw:
            st.conn.execute("UPDATE site_notes SET meta_json=? WHERE id=?", (json.dumps({"gps": gps.strip()[:80]}), n["id"]))
            st.conn.commit()
            n = st.site_note(n["id"])
        if raw:
            d = settings.data_dir / "projects" / prj["slug"] / "field" / "notes"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{n['id']}.jpg"
            p.write_bytes(raw)
            meta = {**_exif(p), "original_name": original}
            if gps.strip():
                meta["gps"] = gps.strip()[:80]
            st.conn.execute("UPDATE site_notes SET photo=?, meta_json=? WHERE id=?", (str(p), json.dumps(meta), n["id"]))
            st.conn.commit()
            n = st.site_note(n["id"])
        return {"note": _note_view(prj["slug"], n)}

    @app.get("/api/projects/{slug}/notes/{note_id}/photo")
    def site_note_photo(slug: str, note_id: str):
        st = store()
        pid = _project(st, slug)["id"]
        n = st.site_note(note_id)
        if not n or n["project_id"] != pid or not n.get("photo"):
            raise HTTPException(404, "no photo")
        p = Path(n["photo"])
        if not p.exists():
            raise HTTPException(404, "photo file missing")
        return FileResponse(p, media_type="image/jpeg")

    @app.delete("/api/projects/{slug}/notes/{note_id}")
    def delete_site_note(slug: str, note_id: str) -> dict:
        st = store()
        pid = _project(st, slug)["id"]
        n = st.site_note(note_id)
        if not n or n["project_id"] != pid:
            raise HTTPException(404, "no such note")
        st.delete_site_note(note_id)
        if n.get("photo"):
            Path(n["photo"]).unlink(missing_ok=True)
        return {"deleted": note_id}

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

    @app.post("/api/projects/{slug}/items/{item_id}/evidence")
    async def file_evidence_to_item(slug: str, item_id: str, files: list[UploadFile] = File(...),
                                    slot: int | None = Form(None), note: str | None = Form(None)) -> dict:
        """What the contractor handed over for one item, filed by the office straight to it. No model call."""
        st = store()
        prj = _project(st, slug)
        if not any(d["item_id"] == item_id for d in st.deficiencies(prj["id"])):
            raise HTTPException(404, "no such item on this project")
        label = f"for-{item_id}-{_stamp()}"
        root, _ = await _save_upload(files, None, re.sub(r"[^A-Za-z0-9._-]+", "_", label))
        file_list = _sorted_files(root)
        if not file_list:
            raise HTTPException(400, "nothing to file")
        out = pipeline.file_to_item(st, prj["id"], item_id, file_list, label, settings, slot_index=slot, note=note or "", root=root)
        return out

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
        to = " ".join(body.to.split()) if body.to is not None else None
        if to and not mail_mod.valid_address(to):
            raise HTTPException(400, "that email address does not look right")
        try:
            st.update_draft(draft_id, body=body.body, subject=body.subject, to=to)
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

    # --- signing in: an office account (email and password) or the office code; contractor links need neither --------
    access_code = settings.access_code
    open_prefixes = ("/c/", "/api/c/", "/set-password/")
    open_paths = {"/signin", "/forgot"}
    failures: dict[str, list[float]] = {}
    SESSION_COOKIE, CODE_COOKIE = "closeout_session", "closeout_access"

    def _access_token() -> str:
        return hashlib.sha256(f"closeout-access:{access_code}".encode()).hexdigest()

    def _who(request: Request) -> dict | None:
        """Who this request is from: {'via': 'account', 'user': …}, {'via': 'code'}, or None. With no office code and no
        accounts yet (a fresh local copy), everyone is let in as the office."""
        token = request.cookies.get(SESSION_COOKIE, "")
        st = store()
        if token:
            user = st.session_user(accounts_mod.token_hash(token))
            if user:
                return {"via": "account", "user": user}
        if access_code and hmac.compare_digest(request.cookies.get(CODE_COOKIE, ""), _access_token()):
            return {"via": "code"}
        if not access_code and not st.users():
            return {"via": "open"}
        return None

    def _too_many(key: str) -> bool:
        cutoff = datetime.now(timezone.utc).timestamp() - 15 * 60
        failures[key] = [x for x in failures.get(key, []) if x > cutoff]
        return len(failures[key]) >= 8

    def _failed(key: str) -> None:
        failures.setdefault(key, []).append(datetime.now(timezone.utc).timestamp())

    def _client(request: Request) -> str:
        return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")

    def _base(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    def _cookie(resp: Response, request: Request, name: str, value: str, days: int) -> None:
        resp.set_cookie(name, value, max_age=60 * 60 * 24 * days, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")

    def _office_email(st: Store, to: str, subject: str, text: str) -> bool:
        """Account email (welcome, reset) from the office's connected mailbox, or Amazon SES when set up. False when
        neither can send; the caller then says so instead of pretending it went."""
        account = _usable_account(st)
        try:
            if account:
                _mailbox(account).send(to, subject, text)
                return True
            if mail_mod.can_send(settings):
                mail_mod.send_email(settings, to, subject, text)
                return True
        except Exception as e:  # the mail service refused; the reason type is logged, never the link
            log.warning("account email refused: %s", type(e).__name__)
        return False

    def _send_link(request: Request, st: Store, user: dict, kind: str, invited_by: str = "") -> tuple[bool, str]:
        """Make a one-time link for this person and email it. Returns (emailed, link)."""
        token, th = accounts_mod.new_token()
        expires = accounts_mod.later(days=accounts_mod.WELCOME_DAYS) if kind == "welcome" else accounts_mod.later(minutes=accounts_mod.RESET_MINUTES)
        st.add_user_link(user["id"], th, kind, expires)
        link = f"{_base(request)}/set-password/{token}"
        office = settings.office
        hello = f"Hello {user['name'].split()[0]}," if user.get("name") else "Hello,"
        if kind == "welcome":
            subject = f"Your Closeout account at {office}"
            text = (f"{hello}\n\n{invited_by or office} added you to Closeout at {office}. Closeout holds the office's field reviews, "
                    f"deficiency lists and contractor replies.\n\nSet your password here. The link works once, for {accounts_mod.WELCOME_DAYS} days:\n{link}\n\n"
                    f"You sign in with this email address, {user['email']}, at {_base(request)}/signin\n\n{office}")
        else:
            subject = "Reset your Closeout password"
            text = (f"{hello}\n\nSomeone asked to reset the Closeout password for {user['email']} at {office}.\n\n"
                    f"Choose a new password here. The link works once, for {accounts_mod.RESET_MINUTES} minutes:\n{link}\n\n"
                    f"If you did not ask for this, ignore this email. Your password stays as it is.\n\n{office}")
        return _office_email(st, user["email"], subject, text), link

    @app.middleware("http")
    async def access_gate(request: Request, call_next):
        path = request.url.path
        if path in open_paths or path.startswith(open_prefixes):
            return await call_next(request)
        who = _who(request)
        if who:
            request.state.who = who
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "sign in first"}, status_code=401)
        return RedirectResponse("/signin", status_code=303)

    def _signin_html(bad: str = "", email: str = "", note: str = "") -> str:
        code = ('<details><summary>Use the office code instead</summary><form method="post" action="/signin">'
                '<label for="code">Office code</label><input id="code" type="password" name="code" autocomplete="off" required>'
                '<button type="submit">Open with the code</button></form></details>') if access_code else ""
        return auth_page("Sign in", f"""<h1>Sign in</h1><p>Sign in with the email address the office added you with. Contractor links open without signing in.</p>
            {f'<p class="ok">{html_escape(note)}</p>' if note else ''}{f'<p class="bad">{html_escape(bad)}</p>' if bad else ''}
            <form method="post" action="/signin"><label for="email">Email</label><input id="email" type="email" name="email" autocomplete="username" value="{html_escape(email)}" required {'' if email else 'autofocus'}>
            <label for="password">Password</label><input id="password" type="password" name="password" autocomplete="current-password" required {'autofocus' if email else ''}>
            <button type="submit">Sign in</button></form>
            <div class="row"><a href="/forgot">Forgot your password?</a></div>{code}""")

    @app.get("/signin", include_in_schema=False)
    def signin_page(request: Request):
        if _who(request):
            return RedirectResponse("/", status_code=303)
        note = {"reset": "Your password is set. Sign in with it now."}.get(request.query_params.get("done", ""), "")
        return HTMLResponse(_signin_html(note=note))

    @app.post("/signin", include_in_schema=False)
    async def signin(request: Request):
        form = await request.form()
        ip = _client(request)
        if _too_many(ip):
            return HTMLResponse(_signin_html("Too many tries from this device. Wait 15 minutes and try again."), status_code=429)
        if "code" in form:
            given = str(form.get("code", "")).strip()
            if not access_code or not hmac.compare_digest(given, access_code):
                _failed(ip)
                return HTMLResponse(_signin_html("That office code did not match. Try again."), status_code=403)
            resp = RedirectResponse("/", status_code=303)
            _cookie(resp, request, CODE_COOKIE, _access_token(), accounts_mod.SESSION_DAYS)
            return resp
        email, password = str(form.get("email", "")).strip().lower(), str(form.get("password", ""))
        st = store()
        user = st.user_by_email(email)
        if not user or not user["pw_hash"] or not accounts_mod.check_password(password, user["pw_hash"]):
            _failed(ip)
            msg = ("This account has no password yet. Use the link in the welcome email, or ask for a new one below."
                   if user and not user["pw_hash"] else "That email and password do not match. Try again, or reset your password.")
            return HTMLResponse(_signin_html(msg, email=email), status_code=403)
        token, th = accounts_mod.new_token()
        st.add_session(th, user["id"], accounts_mod.later(days=accounts_mod.SESSION_DAYS))
        resp = RedirectResponse("/", status_code=303)
        _cookie(resp, request, SESSION_COOKIE, token, accounts_mod.SESSION_DAYS)
        return resp

    @app.post("/signout", include_in_schema=False)
    def signout(request: Request):
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            store().end_session(accounts_mod.token_hash(token))
        resp = RedirectResponse("/signin", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        resp.delete_cookie(CODE_COOKIE, path="/")
        return resp

    def _forgot_html(bad: str = "", sent: bool = False) -> str:
        if sent:
            return auth_page("Check your email", """<h1>Check your email</h1><p>If that address has a Closeout account, a link to choose a new
                password is on its way. It works once, for 60 minutes.</p><p>Nothing after a few minutes? Look in junk mail, or ask someone
                in the office to send you a new link from the Office page.</p><div class="row"><a href="/signin">Back to sign in</a></div>""")
        return auth_page("Forgot password", f"""<h1>Forgot your password?</h1><p>Enter the email you sign in with. We will email you a link to choose a new one.</p>
            {f'<p class="bad">{html_escape(bad)}</p>' if bad else ''}
            <form method="post" action="/forgot"><label for="email">Email</label><input id="email" type="email" name="email" autocomplete="username" autofocus required>
            <button type="submit">Email me a link</button></form><div class="row"><a href="/signin">Back to sign in</a></div>""")

    @app.get("/forgot", include_in_schema=False)
    def forgot_page():
        return HTMLResponse(_forgot_html())

    @app.post("/forgot", include_in_schema=False)
    async def forgot(request: Request):
        form = await request.form()
        email = str(form.get("email", "")).strip().lower()
        if not mail_mod.valid_address(email):
            return HTMLResponse(_forgot_html("Enter a full email address."), status_code=400)
        ip = _client(request)
        if _too_many("forgot:" + ip):
            return HTMLResponse(_forgot_html("Too many requests from this device. Wait 15 minutes and try again."), status_code=429)
        _failed("forgot:" + ip)
        st = store()
        user = st.user_by_email(email)
        # the same answer whether or not the address has an account, so the page cannot be used to find who does
        if user and st.recent_links(user["id"], accounts_mod.ago(hours=1)) < 3:
            _send_link(request, st, user, "reset" if user["pw_hash"] else "welcome")
        return HTMLResponse(_forgot_html(sent=True))

    def _set_html(link: dict | None, user: dict | None, token: str, bad: str = "") -> str:
        if not link or not user:
            return auth_page("Link not valid", """<h1>This link no longer works</h1><p>It was already used, it ran out, or a newer link was sent.
                Ask for a new one and use the newest email.</p><div class="row"><a href="/forgot">Email me a new link</a><a href="/signin">Sign in</a></div>""")
        welcome = link["kind"] == "welcome"
        return auth_page("Set your password", f"""<h1>{'Welcome to Closeout' if welcome else 'Choose a new password'}</h1>
            <p>{'Set a password for ' if welcome else 'New password for '}<b>{html_escape(user['email'])}</b>. Use at least {accounts_mod.MIN_PASSWORD} characters.</p>
            {f'<p class="bad">{html_escape(bad)}</p>' if bad else ''}
            <form method="post" action="/set-password/{html_escape(token)}"><input type="email" name="username" value="{html_escape(user['email'])}" autocomplete="username" hidden>
            <label for="password">New password</label><input id="password" type="password" name="password" autocomplete="new-password" minlength="{accounts_mod.MIN_PASSWORD}" autofocus required>
            <label for="confirm">Type it again</label><input id="confirm" type="password" name="confirm" autocomplete="new-password" required>
            <button type="submit">{'Set password and open Closeout' if welcome else 'Save the new password'}</button></form>""")

    @app.get("/set-password/{token}", include_in_schema=False)
    def set_password_page(token: str):
        st = store()
        link = st.user_link(accounts_mod.token_hash(token))
        return HTMLResponse(_set_html(link, st.user(link["user_id"]) if link else None, token), status_code=200 if link else 410)

    @app.post("/set-password/{token}", include_in_schema=False)
    async def set_password(token: str, request: Request):
        form = await request.form()
        st = store()
        link = st.user_link(accounts_mod.token_hash(token))
        user = st.user(link["user_id"]) if link else None
        if not link or not user:
            return HTMLResponse(_set_html(None, None, token), status_code=410)
        password, confirm = str(form.get("password", "")), str(form.get("confirm", ""))
        problem = accounts_mod.password_problem(password, confirm)
        if problem:
            return HTMLResponse(_set_html(link, user, token, problem), status_code=400)
        st.set_user_password(user["id"], accounts_mod.hash_password(password))   # also ends old sessions and used links
        session, th = accounts_mod.new_token()
        st.add_session(th, user["id"], accounts_mod.later(days=accounts_mod.SESSION_DAYS))
        resp = RedirectResponse("/", status_code=303)
        _cookie(resp, request, SESSION_COOKIE, session, accounts_mod.SESSION_DAYS)
        return resp

    # --- the office's people: add someone (welcome email), send a new link, remove access ---------------------------
    def _person(u: dict) -> dict:
        return {k: u[k] for k in ("id", "email", "name", "created_at", "last_signin")} | {"has_password": u["has_password"]}

    def _can_email(st: Store) -> bool:
        return bool(_usable_account(st)) or mail_mod.can_send(settings)

    @app.get("/api/me")
    def me(request: Request) -> dict:
        who = getattr(request.state, "who", None) or {"via": "open"}
        st = store()
        return {"via": who["via"], "user": _person(who["user"]) if who.get("user") else None, "office": settings.office,
                "office_code": bool(access_code), "can_email": _can_email(st)}

    @app.get("/api/users")
    def list_users() -> dict:
        st = store()
        return {"users": [_person(u) for u in st.users()], "can_email": _can_email(st)}

    @app.post("/api/users")
    def add_person(body: PersonIn, request: Request) -> dict:
        email, name = body.email.strip().lower(), " ".join(body.name.split())[:80]
        if not mail_mod.valid_address(email):
            raise HTTPException(400, "give the person's email address")
        st = store()
        if st.user_by_email(email):
            raise HTTPException(409, "that email already has an account; send them a new link instead")
        who = getattr(request.state, "who", None) or {}
        inviter = (who.get("user") or {}).get("name", "")
        user = st.add_user(email, name, invited_by=(who.get("user") or {}).get("id", ""))
        emailed, link = _send_link(request, st, user, "welcome", inviter)
        return {"user": _person(user), "emailed": emailed, "link": "" if emailed else link, "users": [_person(u) for u in st.users()]}

    @app.post("/api/users/{user_id}/link")
    def resend_link(user_id: str, request: Request) -> dict:
        st = store()
        user = st.user(user_id)
        if not user:
            raise HTTPException(404, "no such person")
        emailed, link = _send_link(request, st, user, "reset" if user["pw_hash"] else "welcome")
        return {"user": _person(user), "emailed": emailed, "link": "" if emailed else link}

    @app.delete("/api/users/{user_id}")
    def remove_person(user_id: str, request: Request) -> dict:
        st = store()
        who = getattr(request.state, "who", None) or {}
        if (who.get("user") or {}).get("id") == user_id:
            raise HTTPException(409, "you cannot remove your own account; ask someone else in the office")
        if not st.user(user_id):
            raise HTTPException(404, "no such person")
        if who.get("via") == "account" and len(st.users()) == 1:
            raise HTTPException(409, "keep at least one account")
        st.remove_user(user_id)
        return {"users": [_person(u) for u in st.users()]}

    # --- report an issue: anyone signed in writes what went wrong; the office sees the list and marks it fixed -------
    @app.get("/api/issues")
    def list_issues() -> dict:
        return {"issues": store().issues()}

    @app.post("/api/issues")
    def report_issue(body: IssueIn, request: Request) -> dict:
        what = body.what.strip()[:4000]
        if len(what) < 5:
            raise HTTPException(400, "say what went wrong in a few words")
        who = getattr(request.state, "who", None) or {}
        by = (who.get("user") or {}).get("email", "") or ("office code" if who.get("via") == "code" else "")
        st = store()
        issue = st.add_issue(what, body.page.strip()[:300], by)
        return {"issue": issue, "issues": st.issues()}

    @app.post("/api/issues/{issue_id}")
    def set_issue(issue_id: str, body: IssueStatusIn) -> dict:
        if body.status not in ("open", "fixed"):
            raise HTTPException(400, "status is open or fixed")
        st = store()
        if not st.set_issue_status(issue_id, body.status):
            raise HTTPException(404, "no such issue")
        return {"issues": st.issues()}

    return app


app = create_app()

"""Evidence Desk API: one FastAPI app over the pipeline. Runs execute on a worker thread and stream progress as SSE.

    uvicorn closeout.api:app --reload

Nothing here calls the model directly; it only drives `pipeline.process_batch` / `continue_run` and reads the store.
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

from . import pipeline
from .config import SETTINGS, Settings
from .packet import build_packet, packet_markdown
from .store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TERMINAL_EVENTS = {"packet", "run_error"}


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


def create_app(settings: Settings = SETTINGS) -> FastAPI:
    app = FastAPI(title="Closeout Evidence Desk", version="0.2")
    feeds: dict[str, RunFeed] = {}
    pending: list[RunFeed] = []  # feeds whose run_id is not known yet (ingest still running)
    lock = threading.Lock()
    state = {"active": None}  # run_id of the run currently executing, if any

    def store() -> Store:
        return pipeline.open_store(settings)

    def _feed_for(run_id: str) -> RunFeed | None:
        with lock:
            return feeds.get(run_id)

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
                if not feed.done:
                    feed.push("run_error", {"error": "run ended without a packet"})

        threading.Thread(target=body, name="closeout-run", daemon=True).start()

    # --- overview -------------------------------------------------------
    @app.get("/api/overview")
    def overview() -> dict:
        st = store()
        runs = st.runs()
        latest = runs[-1]["id"] if runs else None
        packet = build_packet(st, latest) if latest else None
        with lock:
            active = state["active"]
        return {"model_id": settings.model_id, "register": st.deficiencies(), "runs": runs, "latest_run_id": latest,
                "active_run_id": active, "packet": packet, "batches": st.batches()}

    # --- register -------------------------------------------------------
    @app.post("/api/register")
    async def upload_register(files: list[UploadFile] = File(...), paths: list[str] | None = Form(None)) -> dict:
        """The register is dropped as a folder: one CSV plus the reference photos it points at (relative paths)."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        root = settings.data_dir / "uploads" / f"register-{stamp}"
        rel = paths if paths and len(paths) == len(files) else [f.filename or "file" for f in files]
        csvs = []
        for f, name in zip(files, rel):
            dest = root / _safe_relpath(name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(await f.read())
            if dest.suffix.lower() == ".csv":
                csvs.append(dest)
        if len(csvs) != 1:
            raise HTTPException(400, f"expected exactly one .csv in the register drop, got {len(csvs)}")
        try:
            items = pipeline.import_register(store(), csvs[0])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"register rejected: {e}") from e
        return {"imported": len(items), "items": [d.item_id for d in items]}

    @app.get("/api/register/{item_id}/reference")
    def reference_photo(item_id: str):
        d = store().deficiency(item_id)
        if not d or not d.get("reference_photo"):
            raise HTTPException(404, "no reference photo")
        p = Path(d["reference_photo"])
        if not p.exists():
            raise HTTPException(404, "reference photo file missing")
        return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream")

    # --- batches / runs -------------------------------------------------
    @app.post("/api/batches")
    async def upload_batch(files: list[UploadFile] = File(...), paths: list[str] | None = Form(None),
                           label: str | None = Form(None), reprocess_all: bool = Form(False)) -> dict:
        if not store().deficiencies():
            raise HTTPException(409, "import a register first")
        with lock:
            if state["active"] or pending:
                raise HTTPException(409, "a run is already in progress")
            feed = RunFeed()
            pending.append(feed)
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            label = (label or f"batch-{stamp}").strip()
            root = settings.data_dir / "uploads" / (re.sub(r"[^A-Za-z0-9._-]+", "_", label) + f"-{stamp}")
            root.mkdir(parents=True, exist_ok=True)
            rel = paths if paths and len(paths) == len(files) else [f.filename or "file" for f in files]
            for f, name in zip(files, rel):
                dest = root / _safe_relpath(name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(await f.read())
            file_list = _sorted_files(root)
        except Exception:
            with lock:  # never leave a dead pending feed blocking the desk
                if feed in pending:
                    pending.remove(feed)
            raise

        def target(progress):
            pipeline.process_batch(store(), file_list, label, settings, progress=progress,
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
            feed.push("packet", {"run_id": run_id, "replay": True, "status": run["status"]})

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

    @app.post("/api/items/{item_id}/decision")
    def decide(item_id: str, body: Decision) -> dict:
        st = store()
        if not st.deficiency(item_id):
            raise HTTPException(404, "no such item")
        if body.decision not in ("accept", "reject", "hold"):
            raise HTTPException(400, "decision must be accept, reject or hold")
        return {"decision_id": st.add_decision(item_id, body.decision, body.note)}

    # --- UI -------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        page = WEB_DIR / "index.html"
        if not page.exists():
            return JSONResponse({"detail": "web/index.html not built yet; API is at /docs"}, status_code=404)
        return HTMLResponse(page.read_text())

    return app


app = create_app()

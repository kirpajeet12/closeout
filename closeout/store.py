"""SQLite persistence. Every table is append-friendly so history survives reprocessing."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS deficiencies (
  item_id TEXT PRIMARY KEY,
  location TEXT NOT NULL,
  description TEXT NOT NULL,
  evidence_required TEXT NOT NULL,
  slots_json TEXT NOT NULL,
  review_date TEXT,
  discipline TEXT,
  reference_photo TEXT NOT NULL DEFAULT '',
  ref_meta_json TEXT NOT NULL DEFAULT '{}',
  imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  sha256 TEXT UNIQUE NOT NULL,
  filename TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  kind TEXT NOT NULL,            -- image | pdf | text
  mime TEXT NOT NULL,
  size INTEGER NOT NULL,
  pages INTEGER NOT NULL DEFAULT 1,
  metadata_json TEXT NOT NULL,   -- exif etc.
  text_json TEXT NOT NULL,       -- list of per-page extracted text
  first_batch_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL,
  evidence_id TEXT NOT NULL,
  uploaded_name TEXT NOT NULL,
  duplicate_of_name TEXT,        -- set when this upload was a byte-identical repeat
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  status TEXT NOT NULL,          -- running | done | failed
  started_at TEXT NOT NULL,
  finished_at TEXT,
  usage_json TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,            -- match | draft
  subject TEXT NOT NULL,         -- evidence_id or item_id
  status TEXT NOT NULL,          -- pending | running | done | failed
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  usage_json TEXT,
  started_at TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  job_id TEXT,
  evidence_id TEXT NOT NULL,
  item_id TEXT,                  -- NULL when not matched to one item
  status TEXT NOT NULL,          -- matched | ambiguous | unrelated | conflict | note
  tier TEXT,                     -- explicit | strong | weak
  slot_index INTEGER,
  candidates_json TEXT NOT NULL, -- item ids considered plausible
  flags_json TEXT NOT NULL,
  rationale TEXT NOT NULL,
  sources_json TEXT NOT NULL,    -- [{evidence_id, page}]
  provenance TEXT NOT NULL,      -- register | contractor_claim | file_metadata | model_observation
  observations_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_status (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  completeness TEXT NOT NULL,    -- complete | incomplete | needs_clarification | no_evidence
  missing_slots_json TEXT NOT NULL,
  filled_slots_json TEXT NOT NULL,
  unresolved_json TEXT NOT NULL, -- ambiguous / weak findings touching this item
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL,          -- draft | edited | approved
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  decision TEXT NOT NULL,        -- accepted | rejected | needs_more
  note TEXT,
  created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class _LockedConn:
    """Serialises access to one SQLite connection. Strands runs tools on worker threads."""

    def __init__(self, path: Path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()

    def execute(self, *a, **kw):
        with self._lock:
            return self._conn.execute(*a, **kw)

    def executescript(self, *a, **kw):
        with self._lock:
            return self._conn.executescript(*a, **kw)

    def commit(self):
        with self._lock:
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _LockedConn(self.db_path)
        self.conn.executescript(SCHEMA)
        for col, ddl in (("reference_photo", "TEXT NOT NULL DEFAULT ''"), ("ref_meta_json", "TEXT NOT NULL DEFAULT '{}'")):
            if col not in [r[1] for r in self.conn.execute("PRAGMA table_info(deficiencies)")]:
                self.conn.execute(f"ALTER TABLE deficiencies ADD COLUMN {col} {ddl}")

    # --- register -------------------------------------------------------
    def upsert_deficiencies(self, items) -> None:
        for d in items:
            self.conn.execute(
                """INSERT INTO deficiencies(item_id, location, description, evidence_required, slots_json, review_date, discipline,
                                            reference_photo, ref_meta_json, imported_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(item_id) DO UPDATE SET location=excluded.location, description=excluded.description,
                     evidence_required=excluded.evidence_required, slots_json=excluded.slots_json,
                     review_date=excluded.review_date, discipline=excluded.discipline,
                     reference_photo=excluded.reference_photo, ref_meta_json=excluded.ref_meta_json""",
                (d.item_id, d.location, d.description, d.evidence_required,
                 json.dumps([s.__dict__ for s in d.slots]), d.review_date, d.discipline,
                 d.reference_photo, json.dumps(getattr(d, "ref_meta", {}) or {}), now()),
            )
        self.conn.commit()

    def deficiencies(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM deficiencies ORDER BY item_id").fetchall()
        out = []
        for r in rows:
            out.append(self._d(r))
        return out

    def deficiency(self, item_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM deficiencies WHERE item_id=?", (item_id,)).fetchone()
        if not r:
            return None
        return self._d(r)

    @staticmethod
    def _d(r) -> dict:
        d = dict(r)
        d["slots"] = json.loads(d.pop("slots_json"))
        d["ref_meta"] = json.loads(d.pop("ref_meta_json") or "{}")
        return d

    # --- evidence -------------------------------------------------------
    def evidence_by_hash(self, sha256: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM evidence WHERE sha256=?", (sha256,)).fetchone()
        return self._ev(r) if r else None

    def evidence(self, evidence_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        return self._ev(r) if r else None

    def all_evidence(self) -> list[dict]:
        return [self._ev(r) for r in self.conn.execute("SELECT * FROM evidence ORDER BY created_at, filename")]

    @staticmethod
    def _ev(r) -> dict:
        d = dict(r)
        d["metadata"] = json.loads(d.pop("metadata_json"))
        d["text"] = json.loads(d.pop("text_json"))
        return d

    def insert_evidence(self, **kw) -> str:
        eid = kw.get("id") or new_id("ev")
        self.conn.execute(
            """INSERT INTO evidence(id, sha256, filename, stored_path, kind, mime, size, pages, metadata_json, text_json, first_batch_id, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, kw["sha256"], kw["filename"], kw["stored_path"], kw["kind"], kw["mime"], kw["size"], kw["pages"],
             json.dumps(kw["metadata"]), json.dumps(kw["text"]), kw["batch_id"], now()),
        )
        self.conn.commit()
        return eid

    def create_batch(self, label: str) -> str:
        bid = new_id("batch")
        self.conn.execute("INSERT INTO batches(id, label, created_at) VALUES(?,?,?)", (bid, label, now()))
        self.conn.commit()
        return bid

    def batches(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM batches ORDER BY created_at")]

    def add_batch_file(self, batch_id: str, evidence_id: str, uploaded_name: str, duplicate_of_name: str | None) -> None:
        self.conn.execute(
            "INSERT INTO batch_files(batch_id, evidence_id, uploaded_name, duplicate_of_name, created_at) VALUES(?,?,?,?,?)",
            (batch_id, evidence_id, uploaded_name, duplicate_of_name, now()),
        )
        self.conn.commit()

    def batch_files(self, batch_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM batch_files WHERE batch_id=? ORDER BY id", (batch_id,))]

    def batch_evidence(self, batch_id: str) -> list[dict]:
        """Distinct evidence records in a batch (duplicates collapsed)."""
        rows = self.conn.execute(
            """SELECT e.* FROM evidence e JOIN batch_files bf ON bf.evidence_id = e.id
               WHERE bf.batch_id=? AND bf.duplicate_of_name IS NULL ORDER BY bf.id""", (batch_id,)).fetchall()
        return [self._ev(r) for r in rows]

    # --- runs and jobs --------------------------------------------------
    def create_run(self, batch_id: str, model_id: str) -> str:
        rid = new_id("run")
        self.conn.execute("INSERT INTO runs(id, batch_id, model_id, status, started_at) VALUES(?,?,?,?,?)",
                          (rid, batch_id, model_id, "running", now()))
        self.conn.commit()
        return rid

    def finish_run(self, run_id: str, status: str, usage: dict) -> None:
        self.conn.execute("UPDATE runs SET status=?, finished_at=?, usage_json=? WHERE id=?",
                          (status, now(), json.dumps(usage), run_id))
        self.conn.commit()

    def run(self, run_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def runs(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM runs ORDER BY started_at")]

    def create_job(self, run_id: str, kind: str, subject: str) -> str:
        jid = new_id("job")
        self.conn.execute("INSERT INTO jobs(id, run_id, kind, subject, status) VALUES(?,?,?,?,?)",
                          (jid, run_id, kind, subject, "pending"))
        self.conn.commit()
        return jid

    def jobs(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY rowid", (run_id,))]

    def job(self, job_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(r) if r else None

    def job_start(self, job_id: str) -> None:
        self.conn.execute("UPDATE jobs SET status='running', attempts=attempts+1, started_at=?, error=NULL WHERE id=?",
                          (now(), job_id))
        self.conn.commit()

    def job_finish(self, job_id: str, status: str, error: str | None = None, usage: dict | None = None) -> None:
        self.conn.execute("UPDATE jobs SET status=?, error=?, usage_json=?, finished_at=? WHERE id=?",
                          (status, error, json.dumps(usage or {}), now(), job_id))
        self.conn.commit()

    # --- findings -------------------------------------------------------
    def add_finding(self, **kw) -> str:
        fid = new_id("f")
        self.conn.execute(
            """INSERT INTO findings(id, run_id, job_id, evidence_id, item_id, status, tier, slot_index, candidates_json,
               flags_json, rationale, sources_json, provenance, observations_json, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, kw["run_id"], kw.get("job_id"), kw["evidence_id"], kw.get("item_id"), kw["status"], kw.get("tier"),
             kw.get("slot_index"), json.dumps(kw.get("candidates") or []), json.dumps(kw.get("flags") or []),
             kw["rationale"], json.dumps(kw.get("sources") or []), kw["provenance"],
             json.dumps(kw.get("observations") or []), now()),
        )
        self.conn.commit()
        return fid

    def delete_findings(self, job_id: str) -> None:
        self.conn.execute("DELETE FROM findings WHERE job_id=?", (job_id,))
        self.conn.commit()

    def findings_for_run(self, run_id: str) -> list[dict]:
        return [self._f(r) for r in self.conn.execute("SELECT * FROM findings WHERE run_id=? ORDER BY created_at", (run_id,))]

    def current_findings(self) -> list[dict]:
        """Latest run's findings per evidence, across all runs (history-preserving reprocessing)."""
        rows = self.conn.execute(
            """SELECT f.* FROM findings f
               JOIN (SELECT evidence_id, MAX(created_at) AS latest FROM findings GROUP BY evidence_id) l
                 ON l.evidence_id = f.evidence_id
               JOIN runs r ON r.id = f.run_id
               WHERE f.run_id = (SELECT run_id FROM findings f2 WHERE f2.evidence_id=f.evidence_id ORDER BY created_at DESC LIMIT 1)
               ORDER BY f.created_at""").fetchall()
        return [self._f(r) for r in rows]

    def findings_for_evidence(self, evidence_id: str) -> list[dict]:
        return [self._f(r) for r in self.conn.execute(
            "SELECT * FROM findings WHERE evidence_id=? ORDER BY created_at", (evidence_id,))]

    @staticmethod
    def _f(r) -> dict:
        d = dict(r)
        d["candidates"] = json.loads(d.pop("candidates_json"))
        d["flags"] = json.loads(d.pop("flags_json"))
        d["sources"] = json.loads(d.pop("sources_json"))
        d["observations"] = json.loads(d.pop("observations_json"))
        return d

    # --- item status, drafts, decisions ---------------------------------
    def add_item_status(self, run_id: str, item_id: str, completeness: str, missing: list, filled: list, unresolved: list) -> None:
        self.conn.execute(
            "INSERT INTO item_status(run_id, item_id, completeness, missing_slots_json, filled_slots_json, unresolved_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (run_id, item_id, completeness, json.dumps(missing), json.dumps(filled), json.dumps(unresolved), now()))
        self.conn.commit()

    def item_status_for_run(self, run_id: str) -> dict[str, dict]:
        out = {}
        for r in self.conn.execute("SELECT * FROM item_status WHERE run_id=? ORDER BY id", (run_id,)):
            d = dict(r)
            d["missing_slots"] = json.loads(d.pop("missing_slots_json"))
            d["filled_slots"] = json.loads(d.pop("filled_slots_json"))
            d["unresolved"] = json.loads(d.pop("unresolved_json"))
            out[d["item_id"]] = d
        return out

    def item_history(self, item_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT s.*, r.started_at AS run_started_at, r.batch_id FROM item_status s JOIN runs r ON r.id=s.run_id
               WHERE s.item_id=? ORDER BY s.id""", (item_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["missing_slots"] = json.loads(d.pop("missing_slots_json"))
            d["filled_slots"] = json.loads(d.pop("filled_slots_json"))
            d["unresolved"] = json.loads(d.pop("unresolved_json"))
            out.append(d)
        return out

    def upsert_draft(self, run_id: str, item_id: str, subject: str, body: str, status: str = "draft") -> str:
        did = new_id("draft")
        self.conn.execute("INSERT INTO drafts(id, run_id, item_id, subject, body, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                          (did, run_id, item_id, subject, body, status, now(), now()))
        self.conn.commit()
        return did

    def update_draft(self, draft_id: str, body: str | None = None, subject: str | None = None, status: str | None = None) -> None:
        d = self.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not d:
            raise KeyError(draft_id)
        self.conn.execute("UPDATE drafts SET body=?, subject=?, status=?, updated_at=? WHERE id=?",
                          (body if body is not None else d["body"], subject if subject is not None else d["subject"],
                           status if status is not None else "edited", now(), draft_id))
        self.conn.commit()

    def drafts_for_run(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM drafts WHERE run_id=? ORDER BY item_id", (run_id,))]

    def latest_drafts(self) -> dict[str, dict]:
        out = {}
        for r in self.conn.execute("SELECT * FROM drafts ORDER BY updated_at"):
            out[r["item_id"]] = dict(r)
        return out

    def add_decision(self, item_id: str, decision: str, note: str | None) -> str:
        did = new_id("dec")
        self.conn.execute("INSERT INTO decisions(id, item_id, decision, note, created_at) VALUES(?,?,?,?,?)",
                          (did, item_id, decision, note, now()))
        self.conn.commit()
        return did

    def decisions(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM decisions ORDER BY created_at")]

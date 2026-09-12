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
  project_id TEXT NOT NULL DEFAULT '',
  item_id TEXT NOT NULL,
  location TEXT NOT NULL,
  description TEXT NOT NULL,
  evidence_required TEXT NOT NULL,
  slots_json TEXT NOT NULL,
  review_date TEXT,
  discipline TEXT,
  reference_photo TEXT NOT NULL DEFAULT '',
  ref_meta_json TEXT NOT NULL DEFAULT '{}',
  sheet TEXT NOT NULL DEFAULT '',
  imported_at TEXT NOT NULL,
  PRIMARY KEY (project_id, item_id)
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
  project_id TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  via TEXT NOT NULL DEFAULT ''    -- share token when the contractor sent it through their link
);
CREATE TABLE IF NOT EXISTS sends (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  review_id TEXT NOT NULL,
  draft_id TEXT NOT NULL DEFAULT '',
  to_addr TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  via TEXT NOT NULL,              -- ses (the app sent it) | mail-app (handed to the engineer's mail app)
  message_id TEXT NOT NULL DEFAULT '',
  report TEXT NOT NULL DEFAULT '',   -- file name of the items report that went with it, '' when none
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_accounts (
  id TEXT PRIMARY KEY,
  address TEXT NOT NULL,
  refresh_token TEXT NOT NULL,     -- Google's long-lived token; never leaves this database
  connected_at TEXT NOT NULL,
  last_check TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS inbound (
  id TEXT PRIMARY KEY,
  gmail_id TEXT NOT NULL UNIQUE,
  thread_id TEXT NOT NULL DEFAULT '',
  project_id TEXT NOT NULL DEFAULT '',
  review_id TEXT NOT NULL DEFAULT '',
  from_addr TEXT NOT NULL,
  subject TEXT NOT NULL,
  text TEXT NOT NULL,
  sent_at TEXT NOT NULL DEFAULT '',
  files INTEGER NOT NULL DEFAULT 0,
  folder TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,            -- placed | queued (files wait for the desk) | unplaced (no review matched)
  how TEXT NOT NULL DEFAULT '',    -- thread | link | engineer
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shares (
  id TEXT PRIMARY KEY,            -- the token in the contractor's link
  project_id TEXT NOT NULL,
  review_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT
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
  project_id TEXT NOT NULL DEFAULT '',
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
  updated_at TEXT NOT NULL,
  review_id TEXT NOT NULL DEFAULT ''   -- set when the message covers a whole field review
);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL DEFAULT '',
  item_id TEXT NOT NULL,
  decision TEXT NOT NULL,        -- accepted | rejected | needs_more
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  source_root TEXT NOT NULL,
  model_json TEXT NOT NULL DEFAULT '{}',   -- address, units, levels, parties, summary (model_observation + document)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  discipline TEXT NOT NULL,
  dated TEXT,                    -- YYYY-MM-DD from the folder or file name
  pages INTEGER NOT NULL,
  kind TEXT NOT NULL,            -- drawing | document
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 0   -- 1 = newest drawing set of its discipline
);
CREATE TABLE IF NOT EXISTS item_counters (
  project_id TEXT NOT NULL,
  prefix TEXT NOT NULL,
  last INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (project_id, prefix)
);
CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  discipline TEXT NOT NULL,       -- AR | EL | PL ...
  sequence INTEGER NOT NULL,      -- 1, 2, 3 per discipline; never reused
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',   -- active | finished
  started_at TEXT NOT NULL,
  finished_at TEXT,
  package_json TEXT               -- what goes to the contractor, built when the review is finished
);
CREATE TABLE IF NOT EXISTS sheets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  page INTEGER NOT NULL,
  discipline TEXT NOT NULL,
  sheet_number TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  image_path TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  read_json TEXT NOT NULL DEFAULT '{}',    -- what the agent read: levels, units, spaces, elements, notes
  read_status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
  views_json TEXT NOT NULL DEFAULT '[]',   -- where each floor plan drawing sits on the sheet (fractions), for the viewer
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filings (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  file TEXT NOT NULL,             -- file base name as it sits in the project folder
  building TEXT NOT NULL DEFAULT '',
  discipline TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',  -- display name; '' = the file name itself
  who TEXT NOT NULL,              -- closeout | engineer
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS filings_by_file ON filings(project_id, file, at);
CREATE TABLE IF NOT EXISTS document_log (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  file TEXT NOT NULL,             -- file base name, the same key the filings use
  kind TEXT NOT NULL,             -- received | updated | current | superseded | removed
  note TEXT NOT NULL DEFAULT '',
  rel_path TEXT NOT NULL DEFAULT '',
  sha256 TEXT NOT NULL DEFAULT '',
  dated TEXT,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS document_log_by_file ON document_log(project_id, file, at);
CREATE TABLE IF NOT EXISTS drawings_reviews (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  discipline TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'reading',   -- reading | done
  sheets_json TEXT NOT NULL DEFAULT '[]',   -- one row per sheet: status, summary, findings, usage
  summary TEXT NOT NULL DEFAULT '',
  gaps_json TEXT NOT NULL DEFAULT '[]',
  usage_json TEXT NOT NULL DEFAULT '{}',
  cost_usd REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  finished_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS drawings_reviews_by_project ON drawings_reviews(project_id, created_at);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  q TEXT NOT NULL,
  a TEXT NOT NULL,
  go_json TEXT NOT NULL DEFAULT '',   -- where the answer took the screen, if anywhere
  action_json TEXT NOT NULL DEFAULT '', -- a prepared change, if one was offered
  spoken INTEGER NOT NULL DEFAULT 0,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_by_conv ON turns(conversation_id, at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class _Rows(list):
    """Fully materialised query result with the two cursor methods the store uses."""

    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class _LockedConn:
    """Serialises access to one SQLite connection. Strands runs tools on worker threads."""

    def __init__(self, path: Path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()

    def execute(self, *a, **kw):
        """Rows are fetched under the lock: a cursor read after another thread's execute/commit is not safe."""
        with self._lock:
            cur = self._conn.execute(*a, **kw)
            return _Rows(cur.fetchall() if cur.description else [])

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
        self._migrate()

    def _cols(self, table: str) -> list[str]:
        return [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]

    def _migrate(self) -> None:
        """Older databases: add columns, then tie every existing row to the one project they belonged to."""
        for table, col, ddl in (("deficiencies", "reference_photo", "TEXT NOT NULL DEFAULT ''"),
                                ("deficiencies", "ref_meta_json", "TEXT NOT NULL DEFAULT '{}'"),
                                ("deficiencies", "sheet", "TEXT NOT NULL DEFAULT ''"),
                                ("deficiencies", "sheet_id", "TEXT NOT NULL DEFAULT ''"),
                                ("deficiencies", "pin_x", "REAL"),
                                ("deficiencies", "pin_y", "REAL"),
                                ("deficiencies", "review_id", "TEXT NOT NULL DEFAULT ''"),
                                ("deficiencies", "unit", "TEXT NOT NULL DEFAULT ''"),
                                ("deficiencies", "level", "TEXT NOT NULL DEFAULT ''"),
                                ("deficiencies", "space", "TEXT NOT NULL DEFAULT ''"),
                                ("deficiencies", "note", "TEXT NOT NULL DEFAULT ''"),
                                ("deficiencies", "source", "TEXT NOT NULL DEFAULT 'register'"),
                                ("runs", "kind", "TEXT NOT NULL DEFAULT 'batch'"),
                                ("batches", "project_id", "TEXT NOT NULL DEFAULT ''"),
                                ("runs", "project_id", "TEXT NOT NULL DEFAULT ''"),
                                ("decisions", "project_id", "TEXT NOT NULL DEFAULT ''"),
                                ("reviews", "package_json", "TEXT"),
                                ("reviews", "stage", "TEXT NOT NULL DEFAULT ''"),
                                ("reviews", "units_json", "TEXT"),
                                ("projects", "stages_json", "TEXT"),
                                ("drafts", "review_id", "TEXT NOT NULL DEFAULT ''"),
                                ("sheets", "views_json", "TEXT NOT NULL DEFAULT '[]'"),
                                ("projects", "docs_review_json", "TEXT"),
                                ("projects", "docs_scope_json", "TEXT"),
                                ("batches", "via", "TEXT NOT NULL DEFAULT ''"),
                                ("sends", "thread_id", "TEXT NOT NULL DEFAULT ''"),
                                ("sends", "report", "TEXT NOT NULL DEFAULT ''")):
            if col not in self._cols(table):
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        if "project_id" not in self._cols("deficiencies"):
            # the old table had item_id as its primary key: rebuild it with (project_id, item_id)
            self.conn.executescript("""
                ALTER TABLE deficiencies RENAME TO deficiencies_old;
                CREATE TABLE deficiencies (
                  project_id TEXT NOT NULL DEFAULT '', item_id TEXT NOT NULL, location TEXT NOT NULL, description TEXT NOT NULL,
                  evidence_required TEXT NOT NULL, slots_json TEXT NOT NULL, review_date TEXT, discipline TEXT,
                  reference_photo TEXT NOT NULL DEFAULT '', ref_meta_json TEXT NOT NULL DEFAULT '{}', sheet TEXT NOT NULL DEFAULT '',
                  imported_at TEXT NOT NULL, PRIMARY KEY (project_id, item_id));
                INSERT INTO deficiencies(item_id, location, description, evidence_required, slots_json, review_date, discipline,
                                         reference_photo, ref_meta_json, sheet, imported_at)
                  SELECT item_id, location, description, evidence_required, slots_json, review_date, discipline,
                         reference_photo, ref_meta_json, sheet, imported_at FROM deficiencies_old;
                DROP TABLE deficiencies_old;""")
        projects = self.conn.execute("SELECT id FROM projects ORDER BY created_at").fetchall()
        if len(projects) == 1:
            pid = projects[0]["id"]
            for table in ("deficiencies", "batches", "decisions"):
                self.conn.execute(f"UPDATE {table} SET project_id=? WHERE project_id=''", (pid,))
            self.conn.execute("UPDATE runs SET project_id=? WHERE project_id=''", (pid,))
        # project runs carry their project in batch_id ('project:<id>') regardless of how many projects exist
        self.conn.execute("UPDATE runs SET project_id=substr(batch_id, 9) WHERE project_id='' AND batch_id LIKE 'project:%'")
        self.conn.commit()

    # --- register -------------------------------------------------------
    def upsert_deficiencies(self, project_id: str, items, replace: bool = True) -> None:
        """Import a register into one project. With replace=True (a CSV import) the project's rows not in this import are
        removed (their findings stay in history); with replace=False (adding items one at a time) nothing is removed."""
        ids = [d.item_id for d in items]
        if replace and ids:
            self.conn.execute(f"DELETE FROM deficiencies WHERE project_id=? AND item_id NOT IN ({','.join('?' * len(ids))})", [project_id, *ids])
        for d in items:
            self.conn.execute(
                """INSERT INTO deficiencies(project_id, item_id, location, description, evidence_required, slots_json, review_date, discipline,
                                            reference_photo, ref_meta_json, sheet, imported_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(project_id, item_id) DO UPDATE SET location=excluded.location, description=excluded.description,
                     evidence_required=excluded.evidence_required, slots_json=excluded.slots_json,
                     review_date=excluded.review_date, discipline=excluded.discipline,
                     reference_photo=excluded.reference_photo, ref_meta_json=excluded.ref_meta_json, sheet=excluded.sheet""",
                (project_id, d.item_id, d.location, d.description, d.evidence_required,
                 json.dumps([s.__dict__ for s in d.slots]), d.review_date, d.discipline,
                 d.reference_photo, json.dumps(getattr(d, "ref_meta", {}) or {}), getattr(d, "sheet", "") or "", now()),
            )
        self.conn.commit()

    def next_item_id(self, project_id: str, prefix: str) -> str:
        """EL-01, EL-02 ... from a per-project counter that only ever goes up, so a deleted item's number is never
        handed to a different deficiency later. Items already in the register under that prefix count too."""
        rows = self.conn.execute("SELECT item_id FROM deficiencies WHERE project_id=? AND item_id LIKE ?",
                                 (project_id, f"{prefix}-%")).fetchall()
        highest = 0
        for r in rows:
            tail = r["item_id"][len(prefix) + 1:]
            if tail.isdigit():
                highest = max(highest, int(tail))
        c = self.conn.execute("SELECT last FROM item_counters WHERE project_id=? AND prefix=?", (project_id, prefix)).fetchone()
        n = max(highest, int(c["last"]) if c else 0) + 1
        self.conn.execute("INSERT INTO item_counters(project_id, prefix, last) VALUES(?,?,?) "
                          "ON CONFLICT(project_id, prefix) DO UPDATE SET last=excluded.last", (project_id, prefix, n))
        self.conn.commit()
        return f"{prefix}-{n:02d}"

    def add_field_item(self, project_id: str, item_id: str, location: str, description: str, evidence_required: str,
                       slots: list, discipline: str, review_id: str, sheet: str, sheet_id: str, pin_x: float | None,
                       pin_y: float | None, unit: str = "", level: str = "", space: str = "", note: str = "",
                       reference_photo: str = "", ref_meta: dict | None = None, review_date: str = "") -> None:
        """One deficiency recorded on site, pinned to a sheet. Never overwrites: the number is fresh."""
        self.conn.execute(
            """INSERT INTO deficiencies(project_id, item_id, location, description, evidence_required, slots_json, review_date,
                                        discipline, reference_photo, ref_meta_json, sheet, imported_at, sheet_id, pin_x, pin_y,
                                        review_id, unit, level, space, note, source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'field')""",
            (project_id, item_id, location, description, evidence_required, json.dumps([dict(x) if not hasattr(x, "__dict__") else x.__dict__ for x in slots]),
             review_date or None, discipline, reference_photo, json.dumps(ref_meta or {}), sheet, now(), sheet_id, pin_x, pin_y,
             review_id, unit, level, space, note))
        self.conn.commit()

    def update_field_item(self, project_id: str, item_id: str, **fields) -> None:
        allowed = {"location", "description", "evidence_required", "slots_json", "unit", "level", "space", "note", "pin_x", "pin_y"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        self.conn.execute(f"UPDATE deficiencies SET {sets} WHERE project_id=? AND item_id=?", (*cols.values(), project_id, item_id))
        self.conn.commit()

    # --- field reviews ----------------------------------------------------
    def create_review(self, project_id: str, discipline: str, title: str = "", stage: str = "") -> dict:
        r = self.conn.execute("SELECT COALESCE(MAX(sequence), 0) AS n FROM reviews WHERE project_id=? AND discipline=?",
                              (project_id, discipline)).fetchone()
        seq = int(r["n"]) + 1
        rid = new_id("rev")
        self.conn.execute("INSERT INTO reviews(id, project_id, discipline, sequence, title, status, started_at, stage) VALUES(?,?,?,?,?,'active',?,?)",
                          (rid, project_id, discipline, seq, title or f"Field review {seq}", now(), stage.strip()))
        self.conn.commit()
        return self.review(rid)

    def set_review_stage(self, review_id: str, stage: str) -> None:
        """What this walk was for (rough-in, final …). Free text; the project's stage list is the usual pick."""
        self.conn.execute("UPDATE reviews SET stage=? WHERE id=?", (stage.strip(), review_id))
        self.conn.commit()

    def set_review_units(self, review_id: str, units: list[str] | None) -> None:
        """The units the reviewer says were walked. None = not edited: the recorded deficiencies decide."""
        self.conn.execute("UPDATE reviews SET units_json=? WHERE id=?", (json.dumps(list(units)) if units is not None else None, review_id))
        self.conn.commit()

    def review(self, review_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        return self._rv(r) if r else None

    def reviews(self, project_id: str) -> list[dict]:
        return [self._rv(r) for r in self.conn.execute("SELECT * FROM reviews WHERE project_id=? ORDER BY started_at", (project_id,))]

    @staticmethod
    def _rv(r) -> dict:
        d = dict(r)
        raw = d.pop("package_json", None)
        d["package"] = json.loads(raw) if raw else None
        units = d.pop("units_json", None)
        d["units_set"] = json.loads(units) if units else None
        d["stage"] = d.get("stage") or ""
        return d

    def finish_review(self, review_id: str) -> None:
        self.conn.execute("UPDATE reviews SET status='finished', finished_at=? WHERE id=?", (now(), review_id))
        self.conn.commit()

    def set_review_package(self, review_id: str, package: dict) -> None:
        self.conn.execute("UPDATE reviews SET package_json=? WHERE id=?", (json.dumps(package), review_id))
        self.conn.commit()

    def review_package(self, review_id: str) -> dict | None:
        r = self.conn.execute("SELECT package_json FROM reviews WHERE id=?", (review_id,)).fetchone()
        return json.loads(r["package_json"]) if r and r["package_json"] else None

    def draft_for_review(self, review_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM drafts WHERE review_id=? ORDER BY updated_at DESC, rowid DESC LIMIT 1", (review_id,)).fetchone()
        return dict(r) if r else None

    def review_items(self, project_id: str, review_id: str) -> list[dict]:
        return [d for d in self.deficiencies(project_id) if d.get("review_id") == review_id]

    def deficiencies(self, project_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM deficiencies WHERE project_id=? ORDER BY item_id", (project_id,)).fetchall()
        return [self._d(r) for r in rows]

    def deficiency(self, project_id: str, item_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM deficiencies WHERE project_id=? AND item_id=?", (project_id, item_id)).fetchone()
        return self._d(r) if r else None

    def delete_deficiency(self, project_id: str, item_id: str) -> None:
        self.conn.execute("DELETE FROM deficiencies WHERE project_id=? AND item_id=?", (project_id, item_id))
        self.conn.commit()

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

    def all_evidence(self, project_id: str | None = None) -> list[dict]:
        """Every evidence record, or only those that arrived in one project's batches."""
        if project_id is None:
            return [self._ev(r) for r in self.conn.execute("SELECT * FROM evidence ORDER BY created_at, filename")]
        rows = self.conn.execute(
            """SELECT DISTINCT e.* FROM evidence e JOIN batch_files bf ON bf.evidence_id=e.id JOIN batches b ON b.id=bf.batch_id
               WHERE b.project_id=? ORDER BY e.created_at, e.filename""", (project_id,)).fetchall()
        return [self._ev(r) for r in rows]

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

    def create_batch(self, project_id: str, label: str) -> str:
        bid = new_id("batch")
        self.conn.execute("INSERT INTO batches(id, project_id, label, created_at) VALUES(?,?,?,?)", (bid, project_id, label, now()))
        self.conn.commit()
        return bid

    def batches(self, project_id: str | None = None) -> list[dict]:
        if project_id is None:
            return [dict(r) for r in self.conn.execute("SELECT * FROM batches ORDER BY created_at")]
        return [dict(r) for r in self.conn.execute("SELECT * FROM batches WHERE project_id=? ORDER BY created_at", (project_id,))]

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

    def set_batch_via(self, batch_id: str, via: str) -> None:
        self.conn.execute("UPDATE batches SET via=? WHERE id=?", (via, batch_id))
        self.conn.commit()

    # --- contractor links -----------------------------------------------
    def create_share(self, project_id: str, review_id: str) -> dict:
        """One link per finished review: the contractor opens it, sees the items and sends evidence back through it."""
        import secrets
        token = secrets.token_urlsafe(24)
        self.conn.execute("INSERT INTO shares(id, project_id, review_id, created_at) VALUES(?,?,?,?)", (token, project_id, review_id, now()))
        self.conn.commit()
        return self.share(token)

    def record_send(self, project_id: str, review_id: str, draft_id: str, to_addr: str, subject: str, body: str, via: str,
                    message_id: str = "", thread_id: str = "", report: str = "") -> dict:
        """One row per message that left for the contractor, however it left."""
        sid = "send_" + uuid.uuid4().hex[:10]
        self.conn.execute("INSERT INTO sends(id, project_id, review_id, draft_id, to_addr, subject, body, via, message_id, thread_id, report, at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (sid, project_id, review_id, draft_id, to_addr, subject, body, via, message_id, thread_id, report, now()))
        self.conn.commit()
        return self.send(sid)

    def sends_with_threads(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM sends WHERE thread_id!='' ORDER BY at, rowid")]

    def send_by_thread(self, thread_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM sends WHERE thread_id=? ORDER BY at DESC", (thread_id,)).fetchone()
        return dict(r) if r else None

    # --- the connected mailbox and what came in through it ---------------------------------------------------------
    def mail_account(self) -> dict | None:
        r = self.conn.execute("SELECT * FROM mail_accounts ORDER BY connected_at DESC").fetchone()
        return dict(r) if r else None

    def connect_mail(self, address: str, refresh_token: str) -> dict:
        """One mailbox per office: connecting again replaces the old one."""
        self.conn.execute("DELETE FROM mail_accounts")
        self.conn.execute("INSERT INTO mail_accounts(id, address, refresh_token, connected_at) VALUES(?,?,?,?)",
                          ("mail_" + uuid.uuid4().hex[:10], address, refresh_token, now()))
        self.conn.commit()
        return self.mail_account()

    def disconnect_mail(self) -> None:
        self.conn.execute("DELETE FROM mail_accounts")
        self.conn.commit()

    def touch_mail_check(self, error: str = "") -> None:
        self.conn.execute("UPDATE mail_accounts SET last_check=?, last_error=?", (now(), error))
        self.conn.commit()

    def record_inbound(self, gmail_id: str, thread_id: str, project_id: str, review_id: str, from_addr: str, subject: str,
                       text: str, sent_at: str, files: int, status: str, how: str) -> dict:
        iid = "in_" + uuid.uuid4().hex[:10]
        self.conn.execute("INSERT INTO inbound(id, gmail_id, thread_id, project_id, review_id, from_addr, subject, text, sent_at, files, status, how, at) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (iid, gmail_id, thread_id, project_id, review_id, from_addr, subject, text[:20000], sent_at, files, status, how, now()))
        self.conn.commit()
        return self.inbound(iid)

    def inbound(self, inbound_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM inbound WHERE id=?", (inbound_id,)).fetchone()
        return dict(r) if r else None

    def inbound_by_gmail_id(self, gmail_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM inbound WHERE gmail_id=?", (gmail_id,)).fetchone()
        return dict(r) if r else None

    def inbound_for_project(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM inbound WHERE project_id=? ORDER BY at, rowid", (project_id,))]

    def inbound_unplaced(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM inbound WHERE status='unplaced' ORDER BY at, rowid")]

    def inbound_queued(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM inbound WHERE status='queued' ORDER BY at, rowid")]

    def set_inbound_folder(self, inbound_id: str, folder: str) -> None:
        self.conn.execute("UPDATE inbound SET folder=? WHERE id=?", (folder, inbound_id))
        self.conn.commit()

    def set_inbound_status(self, inbound_id: str, status: str) -> None:
        self.conn.execute("UPDATE inbound SET status=? WHERE id=?", (status, inbound_id))
        self.conn.commit()

    def place_inbound(self, inbound_id: str, project_id: str, review_id: str) -> dict | None:
        row = self.inbound(inbound_id)
        if not row:
            return None
        status = "queued" if row["files"] else "placed"
        self.conn.execute("UPDATE inbound SET project_id=?, review_id=?, status=?, how='engineer' WHERE id=?", (project_id, review_id, status, inbound_id))
        self.conn.commit()
        return self.inbound(inbound_id)

    def send(self, send_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM sends WHERE id=?", (send_id,)).fetchone()
        return dict(r) if r else None

    def sends(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM sends WHERE project_id=? ORDER BY at, rowid", (project_id,))]

    def share(self, token: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM shares WHERE id=?", (token,)).fetchone()
        return dict(r) if r else None

    def shares(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM shares WHERE project_id=? ORDER BY created_at", (project_id,))]

    def share_for_review(self, review_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM shares WHERE review_id=? AND revoked_at IS NULL ORDER BY created_at DESC", (review_id,)).fetchone()
        return dict(r) if r else None

    def revoke_share(self, token: str) -> None:
        self.conn.execute("UPDATE shares SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now(), token))
        self.conn.commit()

    # --- runs and jobs --------------------------------------------------
    def create_run(self, project_id: str, batch_id: str, model_id: str, kind: str = "batch") -> str:
        rid = new_id("run")
        self.conn.execute("INSERT INTO runs(id, project_id, batch_id, model_id, status, started_at, kind) VALUES(?,?,?,?,?,?,?)",
                          (rid, project_id, batch_id, model_id, "running", now(), kind))
        self.conn.commit()
        return rid

    def finish_run(self, run_id: str, status: str, usage: dict) -> None:
        self.conn.execute("UPDATE runs SET status=?, finished_at=?, usage_json=? WHERE id=?",
                          (status, now(), json.dumps(usage), run_id))
        self.conn.commit()

    def run(self, run_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def runs(self, project_id: str | None = None, kind: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM runs", []
        conds = []
        if project_id is not None:
            conds.append("project_id=?"); args.append(project_id)
        if kind is not None:
            conds.append("kind=?"); args.append(kind)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY started_at", args)]

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

    def current_findings(self, project_id: str) -> list[dict]:
        """Latest run's findings per evidence within one project, across all its runs (history-preserving reprocessing)."""
        rows = self.conn.execute(
            """SELECT f.* FROM findings f JOIN runs r ON r.id = f.run_id
               WHERE r.project_id = ?
                 AND f.run_id = (SELECT f2.run_id FROM findings f2 JOIN runs r2 ON r2.id = f2.run_id
                                 WHERE f2.evidence_id = f.evidence_id AND r2.project_id = r.project_id
                                 ORDER BY f2.created_at DESC LIMIT 1)
               ORDER BY f.created_at""", (project_id,)).fetchall()
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

    def item_history(self, project_id: str, item_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT s.*, r.started_at AS run_started_at, r.batch_id FROM item_status s JOIN runs r ON r.id=s.run_id
               WHERE r.project_id=? AND s.item_id=? ORDER BY s.id""", (project_id, item_id)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["missing_slots"] = json.loads(d.pop("missing_slots_json"))
            d["filled_slots"] = json.loads(d.pop("filled_slots_json"))
            d["unresolved"] = json.loads(d.pop("unresolved_json"))
            out.append(d)
        return out

    def upsert_draft(self, run_id: str, item_id: str, subject: str, body: str, status: str = "draft", review_id: str = "") -> str:
        did = new_id("draft")
        self.conn.execute("INSERT INTO drafts(id, run_id, item_id, subject, body, status, created_at, updated_at, review_id) VALUES(?,?,?,?,?,?,?,?,?)",
                          (did, run_id, item_id, subject, body, status, now(), now(), review_id))
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

    def latest_drafts(self, project_id: str) -> dict[str, dict]:
        out = {}
        for r in self.conn.execute("SELECT d.* FROM drafts d JOIN runs r ON r.id=d.run_id WHERE r.project_id=? ORDER BY d.updated_at", (project_id,)):
            out[r["item_id"]] = dict(r)
        return out

    def all_drafts(self, project_id: str) -> list[dict]:
        """Every draft ever written for the project, newest first (the Messages tab)."""
        return [dict(r) for r in self.conn.execute(
            "SELECT d.* FROM drafts d JOIN runs r ON r.id=d.run_id WHERE r.project_id=? ORDER BY d.updated_at DESC, d.rowid DESC", (project_id,))]

    def add_decision(self, project_id: str, item_id: str, decision: str, note: str | None) -> str:
        did = new_id("dec")
        self.conn.execute("INSERT INTO decisions(id, project_id, item_id, decision, note, created_at) VALUES(?,?,?,?,?,?)",
                          (did, project_id, item_id, decision, note, now()))
        self.conn.commit()
        return did

    def decisions(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM decisions WHERE project_id=? ORDER BY created_at", (project_id,))]

    # --- conversations: every question and answer kept per project, so a chat can be picked up later ---
    # --- drawings reviews ------------------------------------------------
    def _drawings_row(self, r) -> dict:
        d = dict(r)
        d["sheets"] = json.loads(d.pop("sheets_json") or "[]")
        d["gaps"] = json.loads(d.pop("gaps_json") or "[]")
        d["usage"] = json.loads(d.pop("usage_json") or "{}")
        return d

    def create_drawings_review(self, project_id: str, discipline: str, run_id: str, sheets: list[dict]) -> dict:
        rid = new_id("drw")
        self.conn.execute("INSERT INTO drawings_reviews(id, project_id, discipline, run_id, sheets_json, created_at) VALUES(?,?,?,?,?,?)",
                          (rid, project_id, discipline, run_id, json.dumps(sheets), now()))
        self.conn.commit()
        return self.drawings_review(rid)

    def drawings_review(self, review_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM drawings_reviews WHERE id=?", (review_id,)).fetchone()
        return self._drawings_row(r) if r else None

    def drawings_reviews(self, project_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM drawings_reviews WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
        return [self._drawings_row(r) for r in rows]

    def update_drawings_review(self, review_id: str, sheets: list | None = None, status: str | None = None, summary: str | None = None,
                               gaps: list | None = None, usage: dict | None = None, cost_usd: float | None = None, finished: bool = False) -> None:
        sets, vals = [], []
        for col, v in (("sheets_json", json.dumps(sheets) if sheets is not None else None), ("status", status), ("summary", summary),
                       ("gaps_json", json.dumps(gaps) if gaps is not None else None), ("usage_json", json.dumps(usage) if usage is not None else None),
                       ("cost_usd", cost_usd)):
            if v is not None:
                sets.append(f"{col}=?"); vals.append(v)
        if finished:
            sets.append("finished_at=?"); vals.append(now())
        if sets:
            self.conn.execute(f"UPDATE drawings_reviews SET {', '.join(sets)} WHERE id=?", (*vals, review_id))
            self.conn.commit()

    def delete_drawings_review(self, review_id: str) -> None:
        self.conn.execute("DELETE FROM drawings_reviews WHERE id=?", (review_id,))
        self.conn.commit()

    def create_conversation(self, project_id: str, title: str = "") -> dict:
        cid, t = new_id("conv"), now()
        self.conn.execute("INSERT INTO conversations(id, project_id, title, created_at, updated_at) VALUES(?,?,?,?,?)",
                          (cid, project_id, title.strip(), t, t))
        self.conn.commit()
        return self.conversation(cid)

    def conversation(self, conversation_id: str) -> dict | None:
        r = self.conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM turns t WHERE t.conversation_id=c.id) AS turns FROM conversations c WHERE c.id=?",
            (conversation_id,)).fetchone()
        return dict(r) if r else None

    def conversations(self, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM turns t WHERE t.conversation_id=c.id) AS turns, "
            "(SELECT a FROM turns t WHERE t.conversation_id=c.id ORDER BY at DESC LIMIT 1) AS last_answer "
            "FROM conversations c WHERE c.project_id=? ORDER BY updated_at DESC", (project_id,))
        return [dict(r) for r in rows]

    def turns(self, conversation_id: str) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT * FROM turns WHERE conversation_id=? ORDER BY at, rowid", (conversation_id,)):
            d = dict(r)
            d["action"] = json.loads(d.pop("action_json") or "null")
            d["go"] = json.loads(d.pop("go_json") or "null")
            d["spoken"] = bool(d["spoken"])
            out.append(d)
        return out

    def add_turn(self, conversation_id: str, q: str, a: str, go: dict | None = None, action: dict | None = None, spoken: bool = False) -> str:
        tid, t = new_id("turn"), now()
        self.conn.execute("INSERT INTO turns(id, conversation_id, q, a, go_json, action_json, spoken, at) VALUES(?,?,?,?,?,?,?,?)",
                          (tid, conversation_id, q, a, json.dumps(go) if go else "", json.dumps(action) if action else "", 1 if spoken else 0, t))
        self.conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (t, conversation_id))
        self.conn.commit()
        return tid

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        self.conn.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?", (title.strip(), now(), conversation_id))
        self.conn.commit()

    def delete_conversation(self, conversation_id: str) -> None:
        self.conn.execute("DELETE FROM turns WHERE conversation_id=?", (conversation_id,))
        self.conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        self.conn.commit()

    # --- project --------------------------------------------------------
    def upsert_project(self, slug: str, name: str, source_root: str, model: dict | None = None) -> str:
        r = self.conn.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone()
        if r:
            self.conn.execute("UPDATE projects SET name=?, source_root=?, updated_at=? WHERE id=?", (name, source_root, now(), r["id"]))
            if model is not None:
                self.conn.execute("UPDATE projects SET model_json=? WHERE id=?", (json.dumps(model), r["id"]))
            self.conn.commit()
            return r["id"]
        pid = new_id("prj")
        self.conn.execute("INSERT INTO projects(id, slug, name, source_root, model_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                          (pid, slug, name, source_root, json.dumps(model or {}), now(), now()))
        self.conn.commit()
        return pid

    def set_project_model(self, project_id: str, model: dict) -> None:
        self.conn.execute("UPDATE projects SET model_json=?, updated_at=? WHERE id=?", (json.dumps(model), now(), project_id))
        self.conn.commit()

    def project(self, project_id: str | None = None) -> dict | None:
        q = "SELECT * FROM projects WHERE id=?" if project_id else "SELECT * FROM projects ORDER BY updated_at DESC LIMIT 1"
        r = self.conn.execute(q, (project_id,) if project_id else ()).fetchone()
        return self._p(r) if r else None

    def project_by_slug(self, slug: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
        return self._p(r) if r else None

    def projects(self) -> list[dict]:
        return [self._p(r) for r in self.conn.execute("SELECT * FROM projects ORDER BY updated_at DESC")]

    def rename_project(self, project_id: str, name: str) -> None:
        self.conn.execute("UPDATE projects SET name=?, updated_at=? WHERE id=?", (name, now(), project_id))
        self.conn.commit()

    def touch_project(self, project_id: str) -> None:
        self.conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now(), project_id))
        self.conn.commit()

    @staticmethod
    def _p(r) -> dict:
        d = dict(r)
        d["model"] = json.loads(d.pop("model_json") or "{}")
        d["docs_review"] = json.loads(d.pop("docs_review_json", None) or "null")
        d["docs_scope"] = json.loads(d.pop("docs_scope_json", None) or "[]")
        d["stages"] = json.loads(d.pop("stages_json", None) or "{}")
        return d

    def set_stages(self, project_id: str, discipline: str, stages: list[str]) -> None:
        """The office's list of walks for one discipline on this project, in order. Replaces the default list."""
        r = self.conn.execute("SELECT stages_json FROM projects WHERE id=?", (project_id,)).fetchone()
        cur = json.loads((r["stages_json"] if r else None) or "{}")
        cur[discipline] = [s.strip() for s in stages if s.strip()]
        self.conn.execute("UPDATE projects SET stages_json=?, updated_at=? WHERE id=?", (json.dumps(cur), now(), project_id))
        self.conn.commit()

    def set_docs_scope(self, project_id: str, names: list[str]) -> None:
        """Checklist rows the engineer marked not in scope for this project (by exact name)."""
        self.conn.execute("UPDATE projects SET docs_scope_json=?, updated_at=? WHERE id=?",
                          (json.dumps(sorted(set(names))), now(), project_id))
        self.conn.commit()

    def set_docs_review(self, project_id: str, review: dict | None) -> None:
        """The agent's last answer on what the folder is missing; one per project, replaced on each call."""
        self.conn.execute("UPDATE projects SET docs_review_json=?, updated_at=? WHERE id=?",
                          (json.dumps(review) if review is not None else None, now(), project_id))
        self.conn.commit()

    def replace_documents(self, project_id: str, docs: list[dict]) -> list[str]:
        self.conn.execute("DELETE FROM documents WHERE project_id=?", (project_id,))
        ids = []
        for d in docs:
            did = new_id("doc")
            self.conn.execute("INSERT INTO documents(id, project_id, rel_path, discipline, dated, pages, kind, sha256, size, is_current) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (did, project_id, d["rel_path"], d["discipline"], d.get("dated"), d["pages"], d["kind"], d["sha256"],
                               d["size"], 1 if d.get("is_current") else 0))
            ids.append(did)
        self.conn.commit()
        return ids

    def documents(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM documents WHERE project_id=? ORDER BY discipline, dated, rel_path", (project_id,))]

    # --- document log: what happened to each file, drop after drop -----------------------------------
    # Import replaces the document rows, so the log is what remembers that a file arrived, changed or went away.

    def log_document_changes(self, project_id: str, before: list[dict], after: list[dict]) -> list[dict]:
        """Compare the folder as it was with the folder as it came in, keyed on the file name, and write one line per
        change: received (a new name), updated (the same name with different contents), current / superseded (a drawing
        set becoming, or ceasing to be, the one to walk with) and removed (a name no longer in the folder)."""
        def by_name(docs):
            out: dict[str, list[dict]] = {}
            for d in docs:
                out.setdefault(d["rel_path"].split("/")[-1], []).append(d)
            return out
        old, new = by_name(before), by_name(after)
        at, events = now(), []
        def put(file, kind, note, d=None):
            events.append({"id": new_id("dlog"), "project_id": project_id, "file": file, "kind": kind, "note": note,
                           "rel_path": (d or {}).get("rel_path", ""), "sha256": (d or {}).get("sha256", ""), "dated": (d or {}).get("dated"), "at": at})
        for file in sorted(new, key=str.lower):
            docs, prev = new[file], old.get(file, [])
            shas, prev_shas = {d["sha256"] for d in docs}, {d["sha256"] for d in prev}
            lead = next((d for d in docs if d.get("is_current")), docs[0])
            was_current, is_current = any(d.get("is_current") for d in prev), any(d.get("is_current") for d in docs)
            if not prev:
                put(file, "received", "new in the folder", lead)
            elif shas - prev_shas:
                put(file, "updated", "the same file name came in with different contents", lead)
            if is_current and not was_current:
                put(file, "current", "now the set to walk with", lead)
            elif was_current and not is_current:
                put(file, "superseded", "a newer issue is the set to walk with now", lead)
        for file in sorted(set(old) - set(new), key=str.lower):
            put(file, "removed", "no longer in the folder", old[file][0])
        for e in events:
            self.conn.execute("INSERT INTO document_log(id, project_id, file, kind, note, rel_path, sha256, dated, at) VALUES(?,?,?,?,?,?,?,?,?)",
                              (e["id"], e["project_id"], e["file"], e["kind"], e["note"], e["rel_path"], e["sha256"], e["dated"], e["at"]))
        self.conn.commit()
        return events

    def document_log(self, project_id: str, file: str | None = None) -> list[dict]:
        if file is None:
            rows = self.conn.execute("SELECT * FROM document_log WHERE project_id=? ORDER BY at, rowid", (project_id,))
        else:
            rows = self.conn.execute("SELECT * FROM document_log WHERE project_id=? AND file=? ORDER BY at, rowid", (project_id, file))
        return [dict(r) for r in rows]

    # --- filings: where a file sits in the project folder tree, and who put it there -------------------
    # Every change is a new row; the newest row per file is the current filing, the rest is its history.

    def file_document(self, project_id: str, file: str, building: str = "", discipline: str = "", name: str = "",
                      who: str = "engineer") -> dict:
        fid = new_id("fil")
        self.conn.execute("INSERT INTO filings(id, project_id, file, building, discipline, name, who, at) VALUES(?,?,?,?,?,?,?,?)",
                          (fid, project_id, file, building or "", discipline or "", name or "", who, now()))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM filings WHERE id=?", (fid,)).fetchone())

    def filing_history(self, project_id: str, file: str | None = None) -> list[dict]:
        if file is None:
            rows = self.conn.execute("SELECT * FROM filings WHERE project_id=? ORDER BY at, rowid", (project_id,))
        else:
            rows = self.conn.execute("SELECT * FROM filings WHERE project_id=? AND file=? ORDER BY at, rowid", (project_id, file))
        return [dict(r) for r in rows]

    def filings(self, project_id: str) -> dict[str, dict]:
        """Current filing per file (the newest row), with the engineer's own row winning over a later Closeout row."""
        out: dict[str, dict] = {}
        for r in self.filing_history(project_id):
            cur = out.get(r["file"])
            if cur and cur["who"] == "engineer" and r["who"] != "engineer":
                continue
            out[r["file"]] = r
        return out

    def undo_filing(self, project_id: str, file: str) -> dict | None:
        """Drop the newest filing row for the file, so the one before it is current again. None when nothing to undo."""
        rows = self.filing_history(project_id, file)
        if not rows:
            return None
        self.conn.execute("DELETE FROM filings WHERE id=?", (rows[-1]["id"],))
        self.conn.commit()
        return rows[-2] if len(rows) > 1 else {"file": file, "building": "", "discipline": "", "name": "", "who": "", "at": ""}

    def replace_sheets(self, project_id: str, sheets: list[dict]) -> list[str]:
        self.conn.execute("DELETE FROM sheets WHERE project_id=?", (project_id,))
        ids = []
        for sh in sheets:
            sid = new_id("sht")
            self.conn.execute("INSERT INTO sheets(id, project_id, document_id, page, discipline, sheet_number, title, image_path, text, created_at) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (sid, project_id, sh["document_id"], sh["page"], sh["discipline"], sh.get("sheet_number", ""),
                               sh.get("title", ""), sh["image_path"], sh.get("text", ""), now()))
            ids.append(sid)
        self.conn.commit()
        return ids

    def sheet_read(self, sheet_id: str, read: dict, sheet_number: str | None = None, title: str | None = None,
                   status: str = "done") -> None:
        self.conn.execute("UPDATE sheets SET read_json=?, read_status=?, sheet_number=COALESCE(?, sheet_number), "
                          "title=COALESCE(?, title) WHERE id=?", (json.dumps(read), status, sheet_number, title, sheet_id))
        self.conn.commit()

    def set_sheet_views(self, sheet_id: str, views: list[dict]) -> None:
        self.conn.execute("UPDATE sheets SET views_json=? WHERE id=?", (json.dumps(views), sheet_id))
        self.conn.commit()

    def sheet(self, sheet_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM sheets WHERE id=?", (sheet_id,)).fetchone()
        return self._sh(r) if r else None

    def sheets(self, project_id: str) -> list[dict]:
        return [self._sh(r) for r in self.conn.execute("SELECT * FROM sheets WHERE project_id=? ORDER BY discipline, document_id, page", (project_id,))]

    @staticmethod
    def _sh(r) -> dict:
        d = dict(r)
        d["read"] = json.loads(d.pop("read_json") or "{}")
        d["views"] = json.loads(d.pop("views_json", None) or "[]")
        return d


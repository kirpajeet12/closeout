"""Deficiency register: CSV import and evidence-slot parsing."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

SLOT_TYPES = {"photo", "report", "letter", "document"}
REQUIRED_COLUMNS = {"item_id", "location", "description", "evidence_required"}
OPTIONAL_COLUMNS = {"review_date", "discipline", "reference_photo", "sheet"}


@dataclass
class Slot:
    index: int
    type: str
    description: str


@dataclass
class Deficiency:
    item_id: str
    location: str
    description: str
    evidence_required: str
    slots: list[Slot] = field(default_factory=list)
    review_date: str = ""
    discipline: str = ""
    reference_photo: str = ""   # absolute path to the engineer's field-review photo of this deficiency, if any
    sheet: str = ""             # drawing sheet number the item is pinned to (e.g. "4", "EL-2"), if any

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class RegisterError(ValueError):
    pass


def parse_slots(spec: str) -> list[Slot]:
    slots: list[Slot] = []
    for i, raw in enumerate(s.strip() for s in spec.split(";") if s.strip()):
        if ":" not in raw:
            raise RegisterError(f"evidence_required slot {i} is missing a type prefix: {raw!r}")
        t, desc = raw.split(":", 1)
        t = t.strip().lower()
        if t not in SLOT_TYPES:
            raise RegisterError(f"evidence_required slot {i} has unknown type {t!r}; expected one of {sorted(SLOT_TYPES)}")
        slots.append(Slot(index=i, type=t, description=desc.strip()))
    if not slots:
        raise RegisterError("evidence_required must contain at least one slot")
    return slots


def load_register(path: str | Path) -> list[Deficiency]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - cols
        if missing:
            raise RegisterError(f"register is missing columns: {sorted(missing)}")
        items: list[Deficiency] = []
        seen: set[str] = set()
        for n, row in enumerate(reader, start=2):
            item_id = (row.get("item_id") or "").strip()
            if not item_id:
                raise RegisterError(f"row {n}: item_id is empty")
            if item_id in seen:
                raise RegisterError(f"row {n}: duplicate item_id {item_id}")
            seen.add(item_id)
            try:
                slots = parse_slots(row["evidence_required"])
            except RegisterError as e:
                raise RegisterError(f"row {n} ({item_id}): {e}") from e
            items.append(
                Deficiency(
                    item_id=item_id,
                    location=row["location"].strip(),
                    description=row["description"].strip(),
                    evidence_required=row["evidence_required"].strip(),
                    slots=slots,
                    review_date=(row.get("review_date") or "").strip(),
                    discipline=(row.get("discipline") or "").strip(),
                    reference_photo=_resolve_reference(path, row.get("reference_photo"), n, item_id),
                    sheet=(row.get("sheet") or "").strip(),
                )
            )
    return items


def _resolve_reference(csv_path: Path, raw: str | None, row_n: int, item_id: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    ref = (csv_path.parent / raw).resolve()
    if not ref.is_file():
        raise RegisterError(f"row {row_n} ({item_id}): reference_photo {raw!r} not found next to the CSV")
    if ref.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        raise RegisterError(f"row {row_n} ({item_id}): reference_photo must be JPEG or PNG")
    return str(ref)


def register_as_text(items: list[Deficiency]) -> str:
    """Compact, model-readable rendering of the register."""
    lines = []
    for d in items:
        lines.append(f"{d.item_id} | location: {d.location}" + (f" | sheet {d.sheet}" if d.sheet else "") + f" | {d.description}")
        for s in d.slots:
            lines.append(f"    slot {s.index} [{s.type}]: {s.description}")
    return "\n".join(lines)


def slots_json(items: list[Deficiency]) -> str:
    return json.dumps({d.item_id: [asdict(s) for s in d.slots] for d in items})

"""A dropped folder is ingested as-is: sub-folders walked, hidden entries skipped, HEIC converted with EXIF kept."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from closeout.ingest import ingest_batch
from closeout.pipeline import file_context, import_register
from closeout.store import Store

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "samples" / "evidence" / "batch-01"


def _collect(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and not any(x.startswith(".") for x in p.relative_to(root).parts))


def test_subfolders_and_hidden(tmp_path):
    root = tmp_path / "batch"
    (root / "Firestopping" / "L2").mkdir(parents=True)
    (root / ".hidden").mkdir()
    shutil.copy(SRC / "IMG_2201_L2_corridor_firestop.jpg", root / "Firestopping" / "L2")
    shutil.copy(SRC / "cover_note.txt", root / ".hidden" / "x.txt")
    files = _collect(root)
    assert [str(f.relative_to(root)) for f in files] == ["Firestopping/L2/IMG_2201_L2_corridor_firestop.jpg"]
    st = Store(tmp_path / "db.sqlite")
    import_register(st, ROOT / "samples/register/register.csv")
    r = ingest_batch(st, files, "t", tmp_path / "storage", root=root)
    assert not r.rejected and len(r.new) == 1
    ev = r.new[0]
    assert ev["metadata"]["folder"] == "Firestopping/L2"
    ctx, _ = file_context(st, ev, [])
    assert ctx.startswith("Folder the contractor put it in: Firestopping/L2")


@pytest.mark.skipif(shutil.which("sips") is None, reason="HEIC conversion uses macOS sips")
def test_heic_converted_with_gps(tmp_path):
    heic = tmp_path / "brick.HEIC"
    subprocess.run(["sips", "-s", "format", "heic", str(SRC / "IMG_2210.jpg"), "--out", str(heic)], check=True, capture_output=True)
    st = Store(tmp_path / "db.sqlite")
    r = ingest_batch(st, [heic], "t", tmp_path / "storage")
    assert not r.rejected and len(r.new) == 1
    ev = r.new[0]
    assert ev["stored_path"].endswith(".jpg") and ev["mime"] == "image/jpeg"
    assert ev["metadata"]["gps"]["altitude_m"] == pytest.approx(12.4, abs=0.1)
    assert ev["metadata"]["DateTimeOriginal"] == "2026:09:03 14:45:22"

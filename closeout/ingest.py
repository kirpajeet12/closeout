"""Deterministic evidence ingest: hashing, de-duplication, metadata and text extraction.

No model is involved here. Everything this module records is `file_metadata` provenance.
"""
from __future__ import annotations

import hashlib
import io
import mimetypes
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ExifTags
from pypdf import PdfReader

from .store import Store

SUPPORTED = {
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".png": ("image", "image/png"),
    ".pdf": ("pdf", "application/pdf"),
    ".txt": ("text", "text/plain"),
    ".md": ("text", "text/markdown"),
}

MAX_MODEL_EDGE = 1568  # longest edge sent to the model


class UnsupportedFile(ValueError):
    pass


@dataclass
class IngestResult:
    batch_id: str
    new: list[dict]          # evidence records created by this batch
    existing: list[dict]     # already known (byte-identical) records seen again
    duplicates_in_batch: list[tuple[str, str]]  # (uploaded_name, duplicate_of_name)
    rejected: list[tuple[str, str]]  # (filename, reason)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _exif(path: Path) -> dict:
    out: dict = {}
    try:
        with Image.open(path) as im:
            out["width"], out["height"] = im.size
            exif = im.getexif()
            if exif:
                tagmap = {v: k for k, v in ExifTags.TAGS.items()}
                for name in ("DateTimeOriginal", "DateTime", "Make", "Model", "ImageDescription", "Orientation"):
                    tag = tagmap.get(name)
                    if tag is not None and tag in exif:
                        out[name] = str(exif[tag])
                ifd = exif.get_ifd(ExifTags.IFD.Exif) if hasattr(ExifTags, "IFD") else {}
                if ifd and tagmap.get("DateTimeOriginal") in ifd:
                    out["DateTimeOriginal"] = str(ifd[tagmap["DateTimeOriginal"]])
                gps = exif.get_ifd(ExifTags.IFD.GPSInfo) if hasattr(ExifTags, "IFD") else {}
                if gps:
                    out["has_gps"] = True
    except Exception as e:  # metadata is best-effort
        out["exif_error"] = str(e)[:200]
    return out


def _pdf_text(path: Path) -> list[str]:
    reader = PdfReader(str(path))
    pages = []
    for p in reader.pages:
        try:
            pages.append((p.extract_text() or "").strip())
        except Exception:
            pages.append("")
    return pages


def image_bytes_for_model(path: Path) -> tuple[bytes, str]:
    """Downscaled JPEG bytes for the model (never sent to disk)."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((MAX_MODEL_EDGE, MAX_MODEL_EDGE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "jpeg"


def ingest_batch(store: Store, files: list[Path], label: str, storage_dir: Path) -> IngestResult:
    batch_id = store.create_batch(label)
    storage_dir.mkdir(parents=True, exist_ok=True)
    res = IngestResult(batch_id=batch_id, new=[], existing=[], duplicates_in_batch=[], rejected=[])
    seen_hashes: dict[str, str] = {}  # sha -> first uploaded name in this batch

    for path in files:
        ext = path.suffix.lower()
        if ext not in SUPPORTED:
            res.rejected.append((path.name, f"unsupported format {ext or '(none)'}; supported: {', '.join(sorted(SUPPORTED))}"))
            continue
        kind, mime = SUPPORTED[ext]
        sha = sha256_of(path)

        if sha in seen_hashes:
            existing = store.evidence_by_hash(sha)
            store.add_batch_file(batch_id, existing["id"], path.name, seen_hashes[sha])
            res.duplicates_in_batch.append((path.name, seen_hashes[sha]))
            continue
        seen_hashes[sha] = path.name

        known = store.evidence_by_hash(sha)
        if known:
            store.add_batch_file(batch_id, known["id"], path.name, None)
            res.existing.append(known)
            continue

        eid = f"ev_{sha[:12]}"
        stored = storage_dir / f"{eid}{ext}"
        shutil.copyfile(path, stored)

        metadata: dict = {"original_name": path.name}
        text: list[str] = []
        pages = 1
        if kind == "image":
            metadata.update(_exif(path))
        elif kind == "pdf":
            try:
                text = _pdf_text(path)
            except Exception as e:
                res.rejected.append((path.name, f"could not read PDF: {e}"))
                stored.unlink(missing_ok=True)
                continue
            pages = len(text)
            if not any(text):
                metadata["warning"] = "no extractable text; scanned PDFs are not supported yet"
        else:
            text = [path.read_text(encoding="utf-8", errors="replace")]

        store.insert_evidence(id=eid, sha256=sha, filename=path.name, stored_path=str(stored), kind=kind, mime=mime,
                              size=path.stat().st_size, pages=pages, metadata=metadata, text=text, batch_id=batch_id)
        store.add_batch_file(batch_id, eid, path.name, None)
        res.new.append(store.evidence(eid))
    return res

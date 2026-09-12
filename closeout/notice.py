"""The items report that travels with the covering message: one PDF per field review, built only from what was
recorded on the walk. Each item gets its site photo, the plan close-up with the pin, where it is, what is wrong,
and what the contractor must send to close it. Deterministic; nothing is sent from here.
"""
from __future__ import annotations

import io
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image as PILImage, ImageOps
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import review as review_mod
from .report import _day, _safe
from .review import DISCIPLINES
from .store import Store

MARGIN = 0.75 * inch
WIDTH = letter[0] - 2 * MARGIN
INK = colors.HexColor("#111111")
MUTED = colors.HexColor("#6b6b6b")
RULE = colors.HexColor("#d9d9d9")
ACCENT = colors.HexColor("#e8590c")

S = {
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=INK, spaceAfter=4),
    "sub": ParagraphStyle("sub", fontName="Helvetica", fontSize=10.5, leading=14, textColor=MUTED),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14, textColor=INK),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8.5, leading=11, textColor=MUTED),
    "item": ParagraphStyle("item", fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=INK),
    "where": ParagraphStyle("where", fontName="Helvetica", fontSize=9.5, leading=12, textColor=MUTED, spaceAfter=6),
    "label": ParagraphStyle("label", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=MUTED),
    "cap": ParagraphStyle("cap", fontName="Helvetica", fontSize=8, leading=10, textColor=MUTED),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=INK, spaceBefore=6, spaceAfter=4),
}


def notice_name(prj: dict, rv: dict) -> str:
    return f"{_safe(prj.get('name') or prj.get('slug') or 'project')}_{rv['discipline']}{rv['sequence']}_items-to-close.pdf"


def build_notice(store: Store, project_id: str, review_id: str, office: str = "the engineer's office", link_url: str = "") -> tuple[str, bytes]:
    """(file name, PDF bytes) for one finished review. Raises ValueError when the review is not on file."""
    rv = store.review(review_id)
    if not rv or rv["project_id"] != project_id:
        raise ValueError("no such review")
    prj = store.project(project_id) or {}
    sheets = {s["id"]: s for s in store.sheets(project_id)}
    items = store.review_items(project_id, review_id)
    disc = DISCIPLINES.get(rv["discipline"], rv["discipline"])
    n = len(items)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
                            title=f"{rv['title']} ({disc}) · items to close", author=office, subject=prj.get("name", ""),
                            pageCompression=0)
    flow = [
        Paragraph(escape(f"{n} item{'' if n == 1 else 's'} to close"), S["title"]),
        Paragraph(escape(" · ".join(x for x in (prj.get("name", ""), f"{rv['title']} ({disc})", f"walked {_day(rv['started_at'])}" if rv.get("started_at") else "") if x)), S["sub"]),
        Spacer(1, 6),
        _rule(),
        Spacer(1, 4),
        Paragraph(escape(f"Each item below was recorded on site by {office}. The photo shows the spot as found; the plan "
                         f"close-up marks where it is. Close an item by sending back what its “Send to close” line asks for."), S["body"]),
        Spacer(1, 10),
    ]
    for d in items:
        flow.append(KeepTogether(_item_block(d, sheets)))
        flow.append(Spacer(1, 14))
    flow += [_rule(), Spacer(1, 6), Paragraph("How to send it back", S["h2"]),
             Paragraph(escape("Reply to the email this report came with and attach the photos or documents. Name the item "
                              "(for example " + (items[0]["item_id"] if items else "EL-01") + ") in your reply so each one is filed to the right place."), S["body"])]
    if link_url:
        flow.append(Spacer(1, 4))
        flow.append(Paragraph("Or send them through this page: " + f'<link href="{escape(link_url)}" color="#0b57d0">{escape(link_url)}</link>', S["body"]))
    flow += [Spacer(1, 16), Paragraph(escape(office[0].upper() + office[1:]), S["body"])]

    footer = escape(" · ".join(x for x in (prj.get("name", ""), f"{rv['title']} ({disc})") if x))

    def on_page(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 0.5 * inch, footer.replace("&amp;", "&"))
        canvas.drawRightString(letter[0] - MARGIN, 0.5 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(flow, onFirstPage=on_page, onLaterPages=on_page)
    return notice_name(prj, rv), buf.getvalue()


def _rule():
    t = Table([[""]], colWidths=[WIDTH], rowHeights=[1])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.6, RULE)]))
    return t


def _item_block(d: dict, sheets: dict) -> list:
    where = " · ".join(x for x in (d.get("unit", ""), d.get("level", ""), d.get("space", "")) if x)
    head = [Paragraph(escape(f"{d['item_id']}  ·  {d['description']}"), S["item"]),
            Paragraph(escape(where + (("  ·  sheet " + d["sheet"]) if d.get("sheet") else "")), S["where"])]
    pics = []
    photo = _photo_bytes(d.get("reference_photo") or "")
    if photo:
        pics.append((photo, "Photo on site"))
    sh = sheets.get(d.get("sheet_id") or "")
    if sh and d.get("pin_x") is not None and Path(sh.get("image_path", "")).exists():
        try:
            crop, _ = review_mod.pin_images(Path(sh["image_path"]), d["pin_x"], d["pin_y"])
            pics.append((crop, f"Sheet {d.get('sheet') or ''}".strip() + (f" · {sh['title'].title()}" if sh.get("title") else "")))
        except Exception:
            pass
    cells = []
    for data, cap in pics:
        img = _fit(data, (WIDTH - 12) / 2 if len(pics) == 2 else WIDTH * 0.6, 2.7 * inch)
        cells.append([img, Paragraph(escape(cap), S["cap"])])
    body = head
    if cells:
        widths = [(WIDTH - 12) / 2] * 2 if len(cells) == 2 else [WIDTH]
        t = Table([[c[0] for c in cells], [c[1] for c in cells]], colWidths=widths, hAlign="LEFT")
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, 0), 3), ("BOTTOMPADDING", (0, 1), (-1, 1), 6)]))
        body = body + [t]
    rows = [[Paragraph("WHERE", S["label"]), Paragraph(escape(d["location"]), S["body"])],
            [Paragraph("WHAT IS WRONG", S["label"]), Paragraph(escape(d["description"]), S["body"])],
            [Paragraph("SEND TO CLOSE", S["label"]), Paragraph(escape(d["evidence_required"]), S["body"])]]
    dt = Table(rows, colWidths=[1.15 * inch, WIDTH - 1.15 * inch], hAlign="LEFT")
    dt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 3),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
                            ("LINEBELOW", (0, -1), (-1, -1), 1.2, ACCENT)]))
    return body + [dt]


def _photo_bytes(path: str) -> bytes:
    p = Path(path) if path else None
    if not p or not p.exists():
        return b""
    try:
        with PILImage.open(p) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((1400, 1400))
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=82)
            return out.getvalue()
    except Exception:
        return b""


def _fit(data: bytes, max_w: float, max_h: float) -> Image:
    with PILImage.open(io.BytesIO(data)) as im:
        w, h = im.size
    scale = min(max_w / w, max_h / h)
    return Image(io.BytesIO(data), width=w * scale, height=h * scale)

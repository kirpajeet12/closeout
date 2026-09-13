#!/usr/bin/env python3
"""Cut the Closeout demo film: stills from capture.py in device frames, camera moves, taps, captions, score.

    python3 demo-video/assemble.py                 # full film -> output/closeout-demo.mp4
    python3 demo-video/assemble.py --preview 7     # one still per scene at its midpoint -> output/preview/
    python3 demo-video/assemble.py --free          # the cut without the AI steps (stills from output/cap)
"""
import json
import math
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
FREE = "--free" in sys.argv
CAP = HERE / "output" / ("cap" if FREE else "cap-ai")
PHOTOS = Path.home() / "Documents/New project/PunchPilot/demo-assets/photos"
SCORE = HERE / "audio" / "score.mp3"       # the site walk
SCORE2 = HERE / "audio" / "score2.mp3"     # the office and the contractor
OUT = HERE / "output" / ("closeout-demo-free.mp4" if FREE else "closeout-demo.mp4")
W, H, FPS, SS = 1920, 1080, 30, 2          # output size; stage is rendered SS times larger for crisp zooms
SW, SH = W * SS, H * SS
META = json.loads((CAP / "shots.json").read_text())
# the new-project stills from capture_new.py; the dry rehearsal stands in until the real run exists
CAPNEW = HERE / "output" / ("cap-new" if (HERE / "output" / "cap-new" / "shots.json").exists() else "cap-new-dry")
NEW = json.loads((CAPNEW / "shots.json").read_text())
META.update({k: v for k, v in NEW.items() if k != "project"})
INK, GREY, ORANGE, STAGE = (29, 29, 31), (110, 110, 115), (242, 92, 5), (245, 245, 247)
SF = "/System/Library/Fonts/SFNS.ttf"


@lru_cache(None)
def font(size, weight="Semibold"):
    f = ImageFont.truetype(SF, size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass
    return f


def ease(x):
    x = max(0.0, min(1.0, x))
    return 4 * x * x * x if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def ramp(t, a, b):
    return ease((t - a) / (b - a)) if b > a else float(t >= a)


def rrect_mask(size, r):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), r, fill=255)
    return m


@lru_cache(None)
def shot(name):
    return Image.open((CAPNEW if name.startswith("n") else CAP) / f"{name}.png").convert("RGB")


def shadow(canvas, box, r, blur=60, alpha=70, dy=30):
    x0, y0, x1, y1 = box
    pad = blur * 3
    sh = Image.new("L", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), 0)
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad, pad + x1 - x0, pad + y1 - y0), r, fill=alpha)
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    canvas.paste((0, 0, 0), (x0 - pad, y0 - pad + dy), sh)


def stage_bg():
    bg = Image.new("RGB", (SW, SH), STAGE)
    grad = Image.linear_gradient("L").resize((SW, SH))
    return Image.composite(Image.new("RGB", (SW, SH), (232, 232, 237)), bg, grad.point(lambda v: v * 0.8))


BG = None


# ---------- devices. Each returns (stage image, mapper from screenshot px -> stage px) ----------

def status_bar(img):
    dsf = img.width / 390
    pad = round(50 * dsf)
    col = img.getpixel((4, 4))
    out = Image.new("RGB", (img.width, img.height + pad), col)
    out.paste(img, (0, pad))
    d = ImageDraw.Draw(out)
    fg = (255, 255, 255) if sum(col) < 380 else (0, 0, 0)
    d.text((round(52 * dsf), round(27 * dsf)), "9:41", font=font(round(16 * dsf), "Semibold"), fill=fg, anchor="mm")
    bx, by = img.width - round(52 * dsf), round(27 * dsf)
    d.rounded_rectangle((bx - 12 * dsf, by - 6 * dsf, bx + 12 * dsf, by + 6 * dsf), 3 * dsf, outline=fg, width=max(2, round(dsf)))
    d.rounded_rectangle((bx - 10 * dsf, by - 4 * dsf, bx + 6 * dsf, by + 4 * dsf), 2 * dsf, fill=fg)
    for i in range(4):
        x = img.width - round((98 - i * 6) * dsf)
        d.rectangle((x, by + 5 * dsf - (i + 1) * 2.6 * dsf, x + 3.6 * dsf, by + 5 * dsf), fill=fg)
    return out, pad


def phone(img, cx, cy, screen_h, canvas):
    """iPhone-like frame; screen_h in stage px."""
    img, pad = status_bar(img)
    sw = round(screen_h * img.width / img.height)
    bez, r = round(screen_h * 0.018), round(screen_h * 0.075)
    x0, y0 = round(cx - sw / 2), round(cy - screen_h / 2)
    body = (x0 - bez, y0 - bez, x0 + sw + bez, y0 + screen_h + bez)
    shadow(canvas, body, r + bez, blur=70, alpha=90, dy=40)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle(body, r + bez, fill=(18, 18, 20))
    d.rounded_rectangle((body[0] + 3, body[1] + 3, body[2] - 3, body[3] - 3), r + bez - 3, outline=(70, 70, 76), width=3)
    scr = img.resize((sw, screen_h), Image.LANCZOS)
    canvas.paste(scr, (x0, y0), rrect_mask((sw, screen_h), r))
    iw, ih = round(sw * 0.3), round(screen_h * 0.034)          # the island sits in the status bar, over blank page
    d.rounded_rectangle((cx - iw / 2, y0 + ih * 0.35, cx + iw / 2, y0 + ih * 1.35), ih / 2, fill=(8, 8, 10))
    k = screen_h / img.height
    return lambda px, py: (x0 + px * k, y0 + (py + pad) * k)


def browser(img, cx, top, width, canvas, crop_h=None, scroll=0):
    """Mac window: title bar + page. crop_h = visible page height in screenshot px (for tall full-page stills)."""
    vis_h = crop_h or img.height
    k = width / img.width
    bar = round(52 * SS)
    ph = round(vis_h * k)
    x0 = round(cx - width / 2)
    r = round(14 * SS)
    box = (x0, top, x0 + width, top + bar + ph)
    shadow(canvas, box, r, blur=80, alpha=60, dy=40)
    win = Image.new("RGB", (width, bar + ph), (255, 255, 255))
    d = ImageDraw.Draw(win)
    d.rectangle((0, 0, width, bar), fill=(236, 236, 238))
    d.line((0, bar - 1, width, bar - 1), fill=(210, 210, 214), width=2)
    for i, c in enumerate([(255, 95, 87), (254, 188, 46), (40, 200, 64)]):
        ccx, ccy, rr = 30 * SS + i * 22 * SS, bar / 2, 6.5 * SS
        d.ellipse((ccx - rr, ccy - rr, ccx + rr, ccy + rr), fill=c)
    pw_, phh = round(width * 0.3), round(bar * 0.58)
    d.rounded_rectangle((width / 2 - pw_ / 2, bar / 2 - phh / 2, width / 2 + pw_ / 2, bar / 2 + phh / 2), phh / 2, fill=(222, 222, 226))
    f = font(round(13 * SS), "Medium")
    d.text((width / 2, bar / 2), "Closeout", font=f, fill=(90, 90, 96), anchor="mm")
    page = img.crop((0, round(scroll), img.width, round(scroll) + vis_h)).resize((width, ph), Image.LANCZOS)
    win.paste(page, (0, bar))
    canvas.paste(win, (x0, top), rrect_mask(win.size, r))
    return lambda px, py: (x0 + px * k, top + bar + (py - scroll) * k)


# ---------- overlays drawn at output resolution ----------

def captions(frame, items, t):
    """items: dicts with text lines, t0, t1, x, y, align, size; maxw wraps each line to that width."""
    d = ImageDraw.Draw(frame, "RGBA")
    for c in items:
        t0, t1 = c["t0"], c.get("t1", 1e9)
        a_in, a_out = ramp(t, t0, t0 + 0.7), 1 - ramp(t, t1 - 0.5, t1)
        a = min(a_in, a_out)
        if a <= 0:
            continue
        rise = (1 - a_in) * 26
        col = c.get("color", INK)
        f = font(c.get("size", 72), c.get("weight", "Semibold"))
        rows = []
        for i, line in enumerate(c["lines"]):
            head = i == 0 or c.get("same")
            ff = f if head else font(round(c.get("size", 72) * 0.5), "Regular")
            for k, part in enumerate(wrap(line, ff, c.get("maxw"))):
                rows.append((i, part, ff, head, k == len(wrap(line, ff, c.get("maxw"))) - 1))
        step = lambda r: r[2].size * (1.14 if r[3] else 1.45) + (18 if r[0] == 0 and r[4] else 0)
        y = (c["y"] if c["y"] is not None else (H - sum(map(step, rows)) + rows[-1][2].size * 0.3) / 2) + rise
        for n, (i, line, ff, head, last) in enumerate(rows):
            li = ramp(t, t0 + n * 0.18, t0 + n * 0.18 + 0.7) if a_out >= 1 else a
            fill = col if head else c.get("sub", GREY)
            anchor = {"l": "la", "c": "ma", "r": "ra"}[c.get("align", "l")]
            d.text((c["x"], y), line, font=ff, fill=fill + (round(255 * min(li, a)),), anchor=anchor)
            y += step((i, line, ff, head, last))


@lru_cache(None)
def wrap(line, ff, maxw):
    if not maxw or ff.getlength(line) <= maxw:
        return (line,)
    out, cur = [], ""
    for w in line.split(" "):
        if cur and ff.getlength(cur + " " + w) > maxw:
            out.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    return tuple(out + [cur])


def tap(frame, x, y, t, t0):
    """Finger tap ripple at output px."""
    u = (t - t0) / 0.7
    if not 0 <= u <= 1:
        return
    ov = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    r0 = 26 * (1 - 0.25 * math.sin(min(u * 2, 1) * math.pi))
    d.ellipse((x - r0, y - r0, x + r0, y + r0), fill=(255, 255, 255, round(150 * (1 - u))), outline=(40, 40, 45, round(90 * (1 - u))), width=2)
    rr = 26 + 50 * u
    d.ellipse((x - rr, y - rr, x + rr, y + rr), outline=(242, 92, 5, round(200 * (1 - u))), width=4)
    frame.paste(ov, (0, 0), ov)


@lru_cache(None)
def cursor_img():
    s = 3
    im = Image.new("RGBA", (26 * s, 38 * s), (0, 0, 0, 0))
    pts = [(1, 1), (1, 28), (8, 21), (13, 33), (18, 31), (13, 20), (22, 20)]
    ImageDraw.Draw(im).polygon([(x * s, y * s) for x, y in pts], fill=(0, 0, 0), outline=(255, 255, 255))
    d = ImageDraw.Draw(im)
    d.line([(x * s, y * s) for x, y in pts + [pts[0]]], fill=(255, 255, 255), width=2 * s)
    d.polygon([(x * s, y * s) for x, y in pts], fill=(10, 10, 10))
    return im.resize((26, 38), Image.LANCZOS)


def cursor(frame, a, b, t, t0, t1, click=None):
    u = ramp(t, t0, t1)
    x, y = a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u
    if click is not None and click <= t <= click + 0.5:
        v = (t - click) / 0.5
        ov = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        rr = 10 + 34 * v
        ImageDraw.Draw(ov).ellipse((x - rr, y - rr, x + rr, y + rr), outline=(242, 92, 5, round(220 * (1 - v))), width=4)
        frame.paste(ov, (0, 0), ov)
    frame.paste(cursor_img(), (round(x - 2), round(y - 2)), cursor_img())


def spotlight(frame, rects, dim, clip, clip_mask=None, radius=14, pad=10):
    """Point at what matters: the screen area `clip` (output box) dims outside `rects`, and each rect gets an orange
    outline. rects: [(x0, y0, x1, y1, a)] in output px, a = 0..1 fade; dim = 0..1 fade of the dimming."""
    cx0, cy0, cx1, cy1 = (round(v) for v in clip)
    size = (cx1 - cx0, cy1 - cy0)
    if size[0] <= 0 or size[1] <= 0 or not rects:
        return
    holes = Image.new("L", size, 255)
    ring = Image.new("RGBA", size, (0, 0, 0, 0))
    dh, dr = ImageDraw.Draw(holes), ImageDraw.Draw(ring)
    for x0, y0, x1, y1, a in rects:
        box = (x0 - pad - cx0, y0 - pad - cy0, x1 + pad - cx0, y1 + pad - cy0)
        dh.rounded_rectangle(box, radius, fill=round(255 * (1 - a)))
        dr.rounded_rectangle((box[0] - 5, box[1] - 5, box[2] + 5, box[3] + 5), radius + 5, outline=ORANGE + (round(60 * a),), width=6)
        dr.rounded_rectangle(box, radius, outline=ORANGE + (round(255 * a),), width=4)
    shade = holes.point(lambda v: round(v * 0.34 * dim))
    if clip_mask is not None:
        shade = ImageChops.multiply(shade, clip_mask.resize(size))
    region = frame.crop((cx0, cy0, cx1, cy1))
    region.paste((0, 0, 0), (0, 0), shade)
    region.paste(ring, (0, 0), ImageChops.multiply(ring.getchannel("A"), clip_mask.resize(size)) if clip_mask is not None else ring)
    frame.paste(region, (cx0, cy0))


def lit(hl, t, name):
    """The highlights showing at t on still `name`: hl = [(t0, t1, still or None, css rect)] -> [(rect, dsf still, fade)]."""
    out = []
    for t0, t1, nm, rect in hl:
        if t0 <= t <= t1 and (nm is None or nm == name):
            a = ramp(t, t0, t0 + 0.35) * (1 - ramp(t, t1 - 0.35, t1))
            if a > 0.01:
                out.append((rect, nm or name, a))
    return out


# ---------- camera ----------

def camera(stage, keys, t):
    """keys: [(time, cx, cy, zoom)] in stage-normalised centre + zoom; eased between keys."""
    k0 = keys[0]
    for k1 in keys[1:]:
        if t <= k1[0]:
            u = ease((t - k0[0]) / max(1e-6, k1[0] - k0[0]))
            cx, cy, z = (k0[i] + (k1[i] - k0[i]) * u for i in (1, 2, 3))
            break
        k0 = k1
    else:
        cx, cy, z = k0[1:]
    cw, ch = SW / z, SH / z
    x0 = min(max(cx * SW - cw / 2, 0), SW - cw)
    y0 = min(max(cy * SH - ch / 2, 0), SH - ch)
    frame = stage.crop((round(x0), round(y0), round(x0 + cw), round(y0 + ch))).resize((W, H), Image.BILINEAR)
    return frame, (lambda sx, sy: ((sx - x0) * W / cw, (sy - y0) * H / ch))


# ---------- scenes ----------

@lru_cache(8)
def phone_stage(name, cx=0.70, h=0.86):
    st = BG.copy()
    m = phone(shot(name), cx * SW, SH * 0.5, round(SH * h), st)
    return st, m


# the desk window, drawn at output size: right of the caption column, the page moves inside it
DESK_W, DESK_R, DESK_BAR = 1100, 90, 30
DESK_H = round(DESK_W / 1.6)
DESK_X, DESK_Y = W - DESK_R - DESK_W, (H - DESK_H - DESK_BAR) // 2
CAP_X, CAP_W = 120, DESK_X - 120 - 60
DESK_Z = 1.3


@lru_cache(1)
def desk_base():
    fr = BG.resize((W, H), Image.LANCZOS)
    box = (DESK_X, DESK_Y, DESK_X + DESK_W, DESK_Y + DESK_BAR + DESK_H)
    shadow(fr, box, 10, blur=40, alpha=55, dy=20)
    win = Image.new("RGB", (DESK_W, DESK_BAR), (236, 236, 238))
    d = ImageDraw.Draw(win)
    d.line((0, DESK_BAR - 1, DESK_W, DESK_BAR - 1), fill=(210, 210, 214), width=1)
    for i, c in enumerate([(255, 95, 87), (254, 188, 46), (40, 200, 64)]):
        ccx, ccy, rr = 18 + i * 13, DESK_BAR / 2, 4
        d.ellipse((ccx - rr, ccy - rr, ccx + rr, ccy + rr), fill=c)
    pw_, phh = round(DESK_W * 0.3), round(DESK_BAR * 0.6)
    d.rounded_rectangle((DESK_W / 2 - pw_ / 2, DESK_BAR / 2 - phh / 2, DESK_W / 2 + pw_ / 2, DESK_BAR / 2 + phh / 2), phh / 2, fill=(222, 222, 226))
    d.text((DESK_W / 2, DESK_BAR / 2), "Closeout", font=font(12, "Medium"), fill=(90, 90, 96), anchor="mm")
    fr.paste((255, 255, 255), box, rrect_mask((DESK_W, DESK_BAR + DESK_H), 10))
    fr.paste(win, (DESK_X, DESK_Y), rrect_mask((DESK_W, DESK_BAR + 20), 10).crop((0, 0, DESK_W, DESK_BAR)))
    mask = rrect_mask((DESK_W, DESK_H + 20), 10).crop((0, 20, DESK_W, DESK_H + 20))
    return fr, mask


def view_at(keys, t):
    """keys: [(time, fx, fy, zoom)]: the point of the page at the window centre, as fractions of the still, eased."""
    k0 = keys[0]
    for k1 in keys[1:]:
        if t <= k1[0]:
            u = ease((t - k0[0]) / max(1e-6, k1[0] - k0[0]))
            return tuple(k0[i] + (k1[i] - k0[i]) * u for i in (1, 2, 3))
        k0 = k1
    return k0[1:]


def desk_frame(name, view):
    """One frame of the desk window showing still `name` at view (fx, fy, zoom). Returns frame, screenshot px -> output px."""
    img = shot(name)
    fx, fy, z = view
    z *= DESK_Z
    rw = img.width / z
    rh = rw * DESK_H / DESK_W
    x0 = min(max(fx * img.width - rw / 2, 0), img.width - rw)
    y0 = min(max(fy * img.height - rh / 2, 0), max(0, img.height - rh))
    page = img.crop((round(x0), round(y0), round(x0 + rw), round(y0 + rh))).resize((DESK_W, DESK_H), Image.BILINEAR)
    base, mask = desk_base()
    fr = base.copy()
    fr.paste(page, (DESK_X, DESK_Y + DESK_BAR), mask)
    k = DESK_W / rw
    return fr, (lambda px, py: (DESK_X + (px - x0) * k, DESK_Y + DESK_BAR + (py - y0) * k))


def dual_stage(dname, pname):
    st = BG.copy()
    img = shot(dname)
    md = browser(img, SW * 0.625, round(SH * 0.22), round(SW * 0.5), st, crop_h=1800)
    mp = phone(shot(pname), SW * 0.875, SH * 0.56, round(SH * 0.72), st)
    return st, md, mp


def scene_black(lines, dur, sizes=(120, 44), extra=None):
    def draw(t):
        fr = Image.new("RGB", (W, H), (0, 0, 0))
        captions(fr, [{"lines": [lines[0]], "t0": 0.4, "x": W / 2, "y": H / 2 - sizes[0] * 0.75, "align": "c",
                       "size": sizes[0], "color": (245, 245, 247), "weight": "Bold"}], t)
        if len(lines) > 1:
            captions(fr, [{"lines": [lines[1]], "t0": 1.3, "x": W / 2, "y": H / 2 + sizes[0] * 0.45, "align": "c",
                           "size": sizes[1], "color": (161, 161, 166), "weight": "Regular", "same": True}], t)
        if extra:
            extra(fr, t)
        return fr
    return dur, draw


def scene_photo(photo, lines, dur):
    src = Image.open(PHOTOS / photo).convert("RGB")
    k = max(W / src.width, H / src.height) * 1.12
    base = src.resize((round(src.width * k), round(src.height * k)), Image.LANCZOS)
    shade = Image.linear_gradient("L").rotate(90).resize((W, H)).point(lambda v: 60 + v * 0.55)

    def draw(t):
        z = 1 + 0.06 * t / dur
        cw, ch = W / z * 1.0, H / z
        x0, y0 = (base.width - cw) / 2 - 30 * t / dur, (base.height - ch) / 2
        fr = base.crop((round(x0), round(y0), round(x0 + cw), round(y0 + ch))).resize((W, H), Image.BILINEAR)
        fr = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), fr, shade)
        captions(fr, [{"lines": [lines[0]], "t0": 0.8, "t1": dur, "x": 150, "y": 690, "size": 84, "color": (255, 255, 255), "weight": "Bold"},
                      {"lines": [lines[1]], "t0": 2.6, "t1": dur, "x": 150, "y": 800, "size": 84, "color": (255, 255, 255, ), "weight": "Bold"}], t)
        return fr
    return dur, draw


def scene_phone(beats, dur, cap, cam=None, taps=(), hl=()):
    """beats: [(t_start, still)]; cap: caption dicts; cam: camera keys; taps: [(t, still[, key])] uses shots.json tap (or key);
    hl: [(t0, t1, still or None, (x0, y0, x1, y1) css px)] outlines that part of the screen and dims the rest."""
    cam = cam or [pz(0, 1.0), pz(dur, 1.06)]

    def draw(t):
        name = [b for b in beats if b[0] <= t][-1][1]
        st, m = phone_stage(name)
        fr, to_out = camera(st, cam, t)
        on = lit(hl, t, name)
        if on:
            img, d = shot(name), META[name]["dsf"]
            pad = round(50 * img.width / 390)
            (sx0, sy0), (sx1, sy1) = to_out(*m(0, -pad)), to_out(*m(img.width, img.height))
            r = round((sy1 - sy0) * 0.075)
            rects = [(*to_out(*m(x0 * d, y0 * d)), *to_out(*m(x1 * d, y1 * d)), a) for (x0, y0, x1, y1), _, a in on]
            spotlight(fr, rects, max(a for *_, a in on), (sx0, sy0, sx1, sy1), rrect_mask((round(sx1 - sx0), round(sy1 - sy0)), r))
        for tt, nm, *key in taps:
            p = META[nm].get(key[0] if key else "tap")
            if p:
                sx, sy = phone_stage(nm)[1](p[0] * META[nm]["dsf"], p[1] * META[nm]["dsf"])
                tap(fr, *to_out(sx, sy), t, tt)
        captions(fr, cap, t)
        return fr
    return dur, draw


def scene_desk(beats, dur, cap, cam=None, cur=None, hl=()):
    """beats: [(t_start, still)]; cam: view keys inside the window; cur: (t0, t1, click_t, still, start_out_xy) moves the
    cursor to that still's tap point; hl: [(t0, t1, still or None, css rect)] outlines that part of the page, dims the rest."""
    cam = cam or [(0, 0.5, 0.5, 1.0)]

    def draw(t):
        name = [b for b in beats if b[0] <= t][-1][1]
        fr, m = desk_frame(name, view_at(cam, t))
        on = lit(hl, t, name)
        if on:
            d = META[name]["dsf"]
            rects = [(*m(x0 * d, y0 * d), *m(x1 * d, y1 * d), a) for (x0, y0, x1, y1), _, a in on]
            spotlight(fr, rects, max(a for *_, a in on), (DESK_X, DESK_Y + DESK_BAR, DESK_X + DESK_W, DESK_Y + DESK_BAR + DESK_H),
                      desk_base()[1])
        captions(fr, cap, t)
        if cur:
            t0, t1, ct, nm, start, *key = cur
            p = META[nm].get(key[0] if key else "tap")
            if p and t0 - 0.4 <= t <= ct + 1.2:
                cursor(fr, start, m(p[0] * META[nm]["dsf"], p[1] * META[nm]["dsf"]), t, t0, t1, click=ct)
        return fr
    return dur, draw


def scene_report(dur, cap):
    img = shot("d06-report")
    top_f = DESK_H / DESK_W * img.width / DESK_Z / 2 / img.height         # the page centre when the top of the report is in view

    def draw(t):
        fy = top_f + (0.62 - top_f) * ramp(t, 2.2, dur - 1.0)
        fr, _ = desk_frame("d06-report", (0.5, fy, 1.0))
        captions(fr, cap, t)
        return fr
    return dur, draw


def scene_dual(dur, cap):
    def draw(t):
        fr = _dual().copy()
        captions(fr, cap, t)
        return fr
    return dur, draw


@lru_cache(1)
def _dual():
    return dual_stage("d11-overview-closed", "p15-contractor-closed")[0].resize((W, H), Image.LANCZOS)


def pz(t, z, cy=0.5, px=0.70):
    """Camera key that enlarges the phone around its own spot, leaving the caption column clear."""
    return (t, px - (px - 0.5) / z, cy, z)


def D(name, rect, z=None, zmax=1.2):
    """Desk view centred on a css rect of still `name`, zoomed in as far as the rect still fits (at most zmax)."""
    img, d = shot(name), META[name]["dsf"]
    vw = img.width / DESK_Z
    vh = vw * DESK_H / DESK_W
    rw, rh = (rect[2] - rect[0]) * d, (rect[3] - rect[1]) * d
    z = z or max(1.0, min(zmax, 0.82 * vw / rw, 0.72 * vh / rh))
    fx, fy = (rect[0] + rect[2]) / 2 * d / img.width, (rect[1] + rect[3]) / 2 * d / img.height
    return lambda t, k=1.0: (t, fx, fy, z * k)


def P(name, rect, z=1.2):
    """Phone camera enlarged around the height of a css rect of still `name`."""
    d = META[name]["dsf"]
    cy = phone_stage(name)[1](0, (rect[1] + rect[3]) / 2 * d)[1] / SH
    return lambda t, k=1.0: pz(t, z * k, cy)


def track(dur, stops, move=0.9, drift=1.025):
    """Camera keys that settle on each stop [(t, D(...) or P(...))], moving over `move` s once its beat starts, drifting in."""
    keys = []
    for i, (t, f) in enumerate(stops):
        start = t + (move if i else 0)
        end = stops[i + 1][0] if i + 1 < len(stops) else dur
        keys += [f(start), f(max(start + 0.01, end), drift)]
    return keys


def left(lines, t0, t1, y=380, size=76):
    return {"lines": lines, "t0": t0, "t1": t1, "x": 170, "y": y, "size": size, "weight": "Bold"}


def side(lines, t0, t1, size=58, y=None):
    """Caption for a desk scene: the column left of the window, centred on it."""
    return {"lines": lines, "t0": t0, "t1": t1, "x": CAP_X, "y": y, "size": size, "weight": "Bold", "maxw": CAP_W}


top = side   # the free cut's captions sit in the same column


def build():
    return build_free() if FREE else build_ai()


def build_free():
    P = lambda nm, i=None: nm
    S = []
    S.append(scene_photo("site-access-2.jpg", ["A field review finds deficiencies.", "Each one needs proof it was fixed."], 7.0))
    S.append(scene_black(["Closeout", "Deficiency tracking for field reviews."], 6.5))
    S.append(scene_desk([(0, "d01-projects"), (3.6, "d02-docs")], 9.0,
                        [top(["Cedar Row Townhomes", "Its drawings and documents, filed by discipline."], 0.5, 8.6)],
                        cam=[(0, 0.5, 0.5, 1.0), (3.6, 0.5, 0.5, 1.0), (8.5, 0.42, 0.55, 1.25)],
                        cur=(0.8, 2.4, 2.7, "d01-projects", (W * 0.7, H * 0.8))))
    S.append(scene_phone([(0, "p01-field"), (2.6, "p02-walk")], 6.5,
                         [left(["On site,", "open the field tab.", "Start the review."], 0.4, 6.2)],
                         taps=[(1.9, "p01-field"), (5.2, "p02-walk")]))
    S.append(scene_phone([(0, "p03-photo"), (2.8, "p04-where")], 7.0,
                         [left(["Take a photo.", "Say which unit and floor."], 0.3, 6.8)],
                         taps=[(5.8, "p04-where")]))
    S.append(scene_phone([(0, "p05-plan")], 6.5,
                         [left(["The plan opens", "on your floor."], 0.3, 6.2)],
                         cam=[pz(0, 1.0), pz(2.0, 1.0), pz(6.5, 1.55, 0.36)]))
    S.append(scene_phone([(0, "p05-plan"), (1.6, "p06-tapped")], 7.5,
                         [left(["Tap the spot.", "Closeout can write it up from the photo,", "or you type it yourself."], 2.2, 7.3)],
                         cam=[pz(0, 1.55, 0.36), pz(1.9, 1.55, 0.36), pz(3.4, 1.0)],
                         taps=[(1.1, "p06-tapped")]))
    S.append(scene_phone([(0, "p07-type1"), (0.5, "p07-type2"), (1.0, "p07-type3"), (1.5, "p07-type4"), (2.4, "p08-form")], 7.0,
                         [left(["Nothing is saved", "until you save it."], 0.3, 6.8)],
                         taps=[(5.6, "p08-form")]))
    S.append(scene_phone([(0, "p09-saved1"), (2.4, "p09-saved2"), (4.6, "p09-saved3")], 7.5,
                         [left(["AR-01. AR-02. AR-03."], 0.3, 7.2),
                          {"lines": ["Numbered, pinned to the plan,", "filed by unit."], "t0": 1.2, "t1": 7.2, "x": 170, "y": 500,
                           "size": 44, "weight": "Regular", "color": GREY, "same": True}],
                         cam=[pz(0, 1.2, 0.45), pz(7.5, 1.32, 0.42)]))
    S.append(scene_phone([(0, "p10-list"), (2.8, "p11-finish-ask")], 8.0,
                         [left(["Finish.", "It asks which units you walked."], 0.3, 7.7)],
                         taps=[(2.2, "p10-list"), (6.8, "p11-finish-ask")]))
    S.append(scene_phone([(0, "p12-finished")], 5.5,
                         [left(["Field review 1", "is finished."], 0.3, 5.2)]))
    split = len(S)
    S.append(scene_desk([(0, "d04-overview")], 7.0, [top(["The overview", "Items ready to close, and what is still missing."], 0.4, 6.7)],
                        cam=[(0, 0.5, 0.5, 1.0), (7.0, 0.5, 0.42, 1.12)]))
    S.append(scene_desk([(0, "d05-deficiencies")], 7.0, [top(["0 of 3 ready to close", "Each item with its unit, floor and sheet."], 0.4, 6.7)],
                        cam=[(0, 0.5, 0.5, 1.0), (7.0, 0.5, 0.8, 1.3)]))
    S.append(scene_report(12.0, [top(["The field review report", "Each item with its photo, plan pin and what closes it."], 0.4, 10.6)]))
    S.append(scene_phone([(0, "p13-contractor"), (5.0, "p14-contractor-list")], 11.0,
                         [left(["The contractor", "gets one link."], 0.3, 5.0),
                          left(["No account.", "Only their items,", "and what closes each."], 5.4, 10.7)]))
    S.append(scene_desk([(0, "d08-item"), (4.0, "d09-item-evidence")], 9.5,
                        [top(["Their photo is filed", "on the item it belongs to."], 0.4, 9.2)],
                        cur=(0.6, 2.2, 2.6, "d08-item", (W * 0.6, H * 0.9))))
    S.append(scene_desk([(0, "d09-item-evidence"), (2.7, "d10-item-closed")], 7.5,
                        [top(["The engineer decides", "Ready to close, hold, or not accepted."], 0.4, 7.2)],
                        cam=[(0, 0.5, 0.5, 1.0), (7.5, 0.36, 0.62, 1.35)],
                        cur=(0.4, 2.0, 2.4, "d09-item-evidence", (W * 0.7, H * 0.4))))
    S.append(scene_dual(8.0, [top(["Office and contractor", "The same list, on both screens."], 0.4, 7.7)]))
    S.append(scene_black(["Closeout"], 7.0))
    return S, split


def progress_beats(t0, t1, most=6):
    """The upload's status stills, one per step (sorting, rendering, reading, summarising, filing), spread over t0..t1."""
    names, kinds = [], []
    for n in sorted(k for k in NEW if k.startswith("n02-progress")):
        w = (NEW[n].get("status") or "").split(" ")
        kind = w[0] + ("#" if len(w) > 1 and w[1][:1].isdigit() else "")
        if kind and kind not in kinds:
            kinds.append(kind); names.append(n)
    if len(names) > most:
        names = [names[round(i * (len(names) - 1) / (most - 1))] for i in range(most)]
    step = (t1 - t0) / max(1, len(names))
    return [(t0 + i * step, n) for i, n in enumerate(names)]


def build_ai():
    S = []
    S.append(scene_photo("site-access-2.jpg", ["A field review finds deficiencies.", "Each one needs proof it was fixed."], 6.5))
    S.append(scene_black(["Closeout", "Deficiency tracking for field reviews."], 5.5))
    # a new project: the whole folder as one zip, filed and read
    S.append(scene_desk([(0, "n01-home")] + progress_beats(3.4, 12.0), 12.5,
                        [side(["New project", "Upload the project folder as one zip. Here, 418 Alder Court: 5 PDFs."], 0.4, 3.9),
                         side(["Closeout reads the files", "and files them by building and discipline."], 4.3, 12.2)],
                        cam=track(12.5, [(0, D("n01-home", (579, 480, 861, 552), z=1.06)), (3.4, D("n02-progress03", (208, 228, 1232, 278), z=1.0))]),
                        cur=(0.6, 2.1, 2.5, "n01-home", (W * 0.62, H * 0.9)),
                        hl=[(0.3, 3.3, "n01-home", (579, 480, 861, 552)), (3.5, 12.2, None, (208, 228, 1232, 278))]))
    S.append(scene_desk([(0, "n05-drawings"), (4.0, "n06-folders1"), (8.0, "n06-folders2")], 12.5,
                        [side(["Drawings tab", "Architectural: 3 current sheets. Electrical: 2."], 0.4, 3.8),
                         side(["The folders it set up", "Documents before occupancy, the project folder, and one per building."], 4.2, 7.8),
                         side(["Site › Architectural", "Both issues of the set. September is current, June is kept as older."], 8.2, 12.2)],
                        cam=track(12.5, [(0, D("n05-drawings", (208, 240, 950, 508))), (4.0, D("n06-folders1", (217, 160, 480, 258), z=1.1)),
                                         (8.0, D("n06-folders2", (217, 160, 1215, 425), z=1.05))]),
                        hl=[(0.4, 3.8, "n05-drawings", (208, 332, 886, 508)), (4.3, 7.8, "n06-folders1", (217, 160, 480, 258)),
                            (8.3, 12.2, "n06-folders2", (500, 255, 1215, 358))]))
    # folders of your own, inside any folder, and a file filed into one
    S.append(scene_desk([(0, "n06-folders2"), (2.0, "n08-mkfolder"), (5.2, "n08-made")], 9.0,
                        [side(["New folder", "Make a folder of your own inside any folder. Here, Older issues, inside Site › Architectural."], 0.4, 8.7)],
                        cam=track(9.0, [(0, D("n06-folders2", (505, 200, 1215, 360), z=1.12)), (2.0, D("n08-mkfolder", (505, 200, 1215, 300), z=1.12)),
                                        (5.2, D("n08-made", (283, 250, 1215, 365), z=1.06))]),
                        cur=(0.4, 1.3, 1.6, "n06-folders2", (W * 0.6, H * 0.85)),
                        hl=[(0.2, 2.0, "n06-folders2", (1126, 209, 1214, 242)), (2.1, 5.2, "n08-mkfolder", (505, 209, 1215, 255)),
                            (5.3, 8.7, "n08-made", (505, 255, 1215, 300)), (5.3, 8.7, "n08-made", (283, 328, 480, 361))]))
    S.append(scene_desk([(0, "n08-nested"), (4.2, "n08-move")], 8.5,
                        [side(["Folders inside folders", "June 2026 issue, inside Older issues."], 0.4, 3.9),
                         side(["Move…", "The June set goes into June 2026 issue."], 4.4, 8.2)],
                        cam=track(8.5, [(0, D("n08-nested", (217, 250, 1215, 400), z=1.06)), (4.2, D("n08-move", (505, 250, 1215, 630), z=1.06))]),
                        cur=(5.6, 6.8, 7.2, "n08-move", (W * 0.7, H * 0.85)),
                        hl=[(0.4, 4.0, "n08-nested", (217, 328, 480, 395)), (0.4, 4.0, "n08-nested", (505, 255, 1215, 300)),
                            (4.4, 8.2, "n08-move", (556, 465, 1198, 510)), (6.2, 8.2, "n08-move", (560, 586, 623, 624))]))
    S.append(scene_desk([(0, "n08-filed")], 6.0,
                        [side(["Filed", "Site › Architectural › Older issues › June 2026 issue. Every move can be undone."], 0.4, 5.7)],
                        cam=track(6.0, [(0, D("n08-filed", (217, 250, 1215, 400), z=1.06))], drift=1.03),
                        hl=[(0.4, 5.7, "n08-filed", (505, 255, 1215, 305)), (0.4, 5.7, "n08-filed", (217, 361, 480, 395))]))
    S.append(scene_desk([(0, "n07-home-after")], 6.0,
                        [side(["Projects", "Open Cedar Row Townhomes for the site walk."], 0.4, 5.7)],
                        cam=track(6.0, [(0, D("n07-home-after", (208, 232, 1232, 698), z=1.0))], drift=1.05),
                        cur=(1.6, 3.4, 3.9, "n07-home-after", (W * 0.78, H * 0.8), "cedar"),
                        hl=[(0.4, 5.7, "n07-home-after", (556, 232, 884, 698))]))
    # the walk
    S.append(scene_phone([(0, "p01-field"), (2.4, "p02-walk")], 6.0,
                         [left(["On site,", "open the field tab.", "Start the review."], 0.4, 5.7)],
                         cam=track(6.0, [(0, P("p01-field", (16, 250, 187, 568), z=1.12)), (2.4, P("p02-walk", (31, 659, 359, 819), z=1.2))]),
                         taps=[(1.7, "p01-field"), (4.8, "p02-walk")],
                         hl=[(0.3, 2.3, "p01-field", (16, 523, 187, 568)), (2.5, 5.7, "p02-walk", (31, 659, 359, 819))]))
    S.append(scene_phone([(0, "p03-photo"), (2.6, "p04-where")], 6.5,
                         [left(["Take a photo.", "Say which unit and floor."], 0.3, 6.2)],
                         cam=track(6.5, [(0, P("p03-photo", (31, 659, 359, 844), z=1.2)), (2.6, P("p04-where", (51, 92, 373, 443), z=1.15))]),
                         taps=[(5.3, "p04-where")],
                         hl=[(0.3, 2.5, "p03-photo", (31, 659, 359, 844)), (2.8, 6.2, "p04-where", (51, 92, 373, 137)),
                             (2.8, 6.2, "p04-where", (51, 399, 268, 443))]))
    S.append(scene_phone([(0, "p05-plan")], 5.0,
                         [left(["The plan opens", "on your floor."], 0.3, 4.7)],
                         cam=[pz(0, 1.0), pz(1.2, 1.0), pz(5.0, 1.55, 0.36)],
                         hl=[(0.6, 4.8, "p05-plan", (4, 150, 386, 404))]))
    S.append(scene_phone([(0, "p05-plan"), (1.4, "p06-tapped")], 6.5,
                         [left(["Tap the spot.", "Ask Closeout to write it up."], 2.0, 6.3)],
                         cam=[pz(0, 1.55, 0.36), pz(1.7, 1.55, 0.36), pz(3.1, 1.0)],
                         taps=[(0.9, "p06-tapped"), (5.4, "p06-tapped", "ask")],
                         hl=[(1.5, 3.2, "p06-tapped", (185, 327, 233, 375)), (3.4, 6.3, "p06-tapped", (18, 749, 176, 794))]))
    S.append(scene_phone([(0, "p07-thinking"), (0.9, "p07-suggested"), (5.3, "p08-form")], 7.8,
                         [left(["It reads the photo", "and the drawing,", "and writes it up."], 0.3, 4.6),
                          left(["Check the wording,", "then save."], 4.9, 7.6)],
                         cam=[pz(0, 1.0), pz(0.9, 1.0), pz(2.3, 1.5, 0.5), pz(5.0, 1.5, 0.5), pz(6.0, 1.0)],
                         taps=[(7.0, "p08-form")],
                         hl=[(1.0, 4.8, "p07-suggested", (16, 421, 373, 780)), (5.3, 7.6, "p08-form", (16, 568, 373, 741)),
                             (5.3, 7.6, "p08-form", (17, 783, 148, 827))]))
    S.append(scene_phone([(0, "p07-type1"), (0.4, "p07-type2"), (0.8, "p07-type3"), (1.2, "p07-type4"), (1.9, "p08-typed")], 5.0,
                         [left(["Or type it yourself."], 0.3, 4.8)],
                         cam=track(5.0, [(0, P("p07-type1", (18, 762, 372, 844), z=1.15)), (1.9, P("p08-typed", (18, 696, 371, 827), z=1.15))], move=0.6),
                         taps=[(4.1, "p08-typed")],
                         hl=[(0.2, 1.9, None, (18, 762, 372, 844)), (2.0, 4.8, "p08-typed", (18, 696, 371, 741)),
                             (2.0, 4.8, "p08-typed", (18, 783, 156, 827))]))
    S.append(scene_phone([(0, "p09-saved1"), (2.1, "p09-saved2"), (4.2, "p09-saved3")], 6.5,
                         [left(["AR-01. AR-02. AR-03."], 0.3, 6.2),
                          {"lines": ["Numbered, pinned to the plan,", "filed by unit."], "t0": 1.2, "t1": 6.2, "x": 170, "y": 500,
                           "size": 44, "weight": "Regular", "color": GREY, "same": True}],
                         cam=[pz(0, 1.2, 0.45), pz(6.5, 1.32, 0.42)],
                         hl=[(0.3, 2.1, "p09-saved1", (163, 313, 220, 372)), (2.2, 4.2, "p09-saved2", (60, 203, 122, 257)),
                             (4.3, 6.3, "p09-saved3", (72, 250, 125, 302)), (4.3, 6.3, "p09-saved3", (256, 404, 310, 456))]))
    S.append(scene_phone([(0, "p10-list"), (2.6, "p11-finish-ask")], 7.0,
                         [left(["Finish.", "It asks which units you walked."], 0.3, 6.7)],
                         cam=track(7.0, [(0, P("p10-list", (138, 131, 272, 184), z=1.12)), (2.6, P("p11-finish-ask", (52, 254, 337, 738), z=1.08))]),
                         taps=[(2.0, "p10-list"), (6.0, "p11-finish-ask")],
                         hl=[(0.3, 2.5, "p10-list", (138, 131, 272, 184)), (2.8, 5.3, "p11-finish-ask", (52, 254, 337, 499)),
                             (5.3, 6.8, "p11-finish-ask", (93, 691, 271, 738))]))
    S.append(scene_phone([(0, "p11-drafting"), (0.9, "p12-finished")], 4.8,
                         [left(["Field review 1 finished.", "Closeout drafts the", "message to the contractor."], 0.3, 4.5)],
                         hl=[(0.1, 0.9, "p11-drafting", (22, 45, 380, 100)), (1.0, 4.6, "p12-finished", (17, 250, 187, 419)),
                             (1.0, 4.6, "p12-finished", (97, 758, 293, 819))]))
    split = len(S)        # the office and the contractor: second score from here
    S.append(scene_desk([(0, "d05-deficiencies")], 6.0, [side(["0 of 3 ready to close", "Each item with its unit, floor and sheet."], 0.4, 5.7)],
                        cam=track(6.0, [(0, D("d05-deficiencies", (200, 190, 740, 450), z=1.1)), (3.0, D("d05-deficiencies", (208, 690, 1232, 900), z=1.05))]),
                        hl=[(0.4, 3.0, "d05-deficiencies", (200, 262, 460, 392)), (3.2, 5.8, "d05-deficiencies", (208, 772, 1232, 900))]))
    S.append(scene_report(9.0, [side(["The field review report", "Each item with its photo, plan pin and what closes it."], 0.4, 8.5)]))
    S.append(scene_desk([(0, "d07-messages")], 7.5,
                        [side(["The drafted message", "3 items to close at Cedar Row. Nothing is sent until you send it."], 0.4, 7.2)],
                        cam=track(7.5, [(0, D("d07-messages", (209, 150, 1232, 404), z=1.05)), (3.4, D("d07-messages", (521, 250, 1232, 560), z=1.12))]),
                        hl=[(0.4, 3.4, "d07-messages", (209, 318, 521, 404)), (3.5, 7.2, "d07-messages", (530, 262, 1225, 370))]))
    S.append(scene_phone([(0, "p13-contractor"), (3.4, "p14-contractor-list")], 7.0,
                         [left(["The contractor", "gets one link."], 0.3, 3.4),
                          left(["No account.", "Only their items."], 3.7, 6.8)],
                         cam=track(7.0, [(0, P("p13-contractor", (17, 282, 373, 516), z=1.12)), (3.4, P("p14-contractor-list", (8, 133, 385, 844), z=1.0))]),
                         hl=[(0.3, 3.3, "p13-contractor", (17, 282, 373, 516)), (3.6, 6.8, "p14-contractor-list", (8, 140, 385, 836))]))
    S.append(scene_phone([(0, "p13-contractor"), (2.0, "p16-contractor-filing"), (3.2, "p17-contractor-filed")], 7.0,
                         [left(["They upload AR-01.jpg.", "Closeout files it", "on item AR-01."], 0.3, 6.7)],
                         cam=track(7.0, [(0, P("p13-contractor", (17, 282, 373, 516), z=1.12)), (2.0, P("p16-contractor-filing", (16, 535, 374, 580), z=1.15)),
                                         (3.2, P("p17-contractor-filed", (8, 133, 385, 478), z=1.12))], move=0.7),
                         taps=[(1.2, "p13-contractor")],
                         hl=[(0.2, 1.9, "p13-contractor", (17, 282, 373, 516)), (2.0, 3.2, "p16-contractor-filing", (16, 535, 374, 580)),
                             (3.3, 6.7, "p17-contractor-filed", (8, 133, 385, 478))]))
    S.append(scene_desk([(0, "d08-item-filed")], 8.5,
                        [side(["Location not confirmed", "The photo matches AR-01, but nothing in it shows where it was taken."], 0.4, 8.2)],
                        cam=track(8.5, [(0, D("d08-item-filed", (208, 300, 1232, 600), z=1.08)), (4.2, D("d08-item-filed", (233, 660, 1207, 900), z=1.1))]),
                        hl=[(0.4, 4.2, "d08-item-filed", (228, 360, 690, 440)), (4.4, 8.2, "d08-item-filed", (240, 678, 1205, 840))]))
    S.append(scene_desk([(0, "d09-item-evidence"), (2.6, "d10-item-closed")], 7.0,
                        [side(["The engineer decides", "Ready to close, hold, or not accepted."], 0.4, 6.7)],
                        cam=track(7.0, [(0, D("d09-item-evidence", (228, 300, 900, 600), z=1.1)), (2.6, D("d10-item-closed", (228, 400, 900, 600), z=1.2))]),
                        cur=(0.4, 1.9, 2.3, "d09-item-evidence", (W * 0.7, H * 0.4)),
                        hl=[(0.3, 2.5, "d09-item-evidence", (228, 420, 607, 480)), (2.7, 6.7, "d10-item-closed", (228, 420, 390, 480)),
                            (2.7, 6.7, "d10-item-closed", (228, 545, 570, 572))]))
    S.append(scene_desk([(0, "d12-ask"), (2.2, "d12-ask-thinking"), (3.1, "d13-answer")], 8.4,
                        [side(["Ask about the project", "“What does the contractor still need to send?”"], 0.4, 8.1)],
                        cam=[(0, 0.5, 0.5, 0.77), (2.4, 0.5, 0.5, 0.77), (4.2, 0.8, 0.37, 1.45), (8.4, 0.8, 0.37, 1.5)],
                        cur=(0.5, 1.7, 2.0, "d12-ask", (W * 0.5, H * 0.5)),
                        hl=[(0.3, 2.2, "d12-ask", (1023, 835, 1428, 892)), (3.6, 8.1, "d13-answer", (1028, 140, 1398, 295))]))
    S.append(scene_dual(7.0, [side(["Office and contractor", "The same list, on both screens."], 0.4, 6.7)]))
    S.append(scene_black(["Closeout"], 6.0))
    return S, split


def audio_len(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout
    return float(out.strip())


def render(scenes, split, preview=None):
    XF = 0.6
    starts, t = [], 0.0
    for dur, _ in scenes:
        starts.append(t)
        t += dur - XF
    total = t + XF
    if preview is not None:
        (HERE / "output" / "preview").mkdir(parents=True, exist_ok=True)
        for i, ((dur, fn), s) in enumerate(zip(scenes, starts)):
            for frac in (0.3, 0.85):
                fn(dur * frac).save(HERE / "output" / "preview" / f"s{i:02d}-{int(frac * 100)}.jpg", quality=85)
        print("preview scenes", len(scenes), "total", round(total, 1), "s; second score from", round(starts[split], 1), "s")
        return
    # two scores: the calm one under the site walk, the brighter one from the office on, crossfaded over XA seconds.
    # The second is trimmed from its start so its resolved ending lands on the end card.
    XA, fade_out = 2.0, 2.5
    t2 = starts[split] - XA / 2
    len2 = total - t2
    off2 = max(0.0, audio_len(SCORE2) - len2 - 0.5)
    af = (f"[1:a]atrim=0:{t2 + XA:.2f},afade=t=in:d=1.2,afade=t=out:st={t2:.2f}:d={XA}[a1];"
          f"[2:a]atrim={off2:.2f}:{off2 + len2:.2f},asetpts=PTS-STARTPTS,afade=t=in:d={XA},"
          f"afade=t=out:st={len2 - fade_out:.2f}:d={fade_out},adelay={round(t2 * 1000)}:all=1[a2];"
          f"[a1][a2]amix=inputs=2:normalize=0:duration=longest,loudnorm=I=-16:TP=-1.5:LRA=11[a]")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-i", str(SCORE), "-i", str(SCORE2), "-filter_complex", af,
           "-map", "0:v", "-map", "[a]", "-t", f"{total:.2f}",
           "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "192k", str(OUT)]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = round(total * FPS)
    for f in range(n):
        tt = f / FPS
        active = [i for i, s in enumerate(starts) if s <= tt < s + scenes[i][0]]
        i = active[-1]
        fr = scenes[i][1](tt - starts[i])
        if len(active) > 1:
            j = active[-2]
            prev = scenes[j][1](tt - starts[j])
            fr = Image.blend(prev, fr, ease((tt - starts[i]) / XF))
        if tt > total - 1.5:
            fr = Image.blend(fr, Image.new("RGB", (W, H)), ramp(tt, total - 1.5, total))
        ff.stdin.write(fr.tobytes())
        if f % 300 == 0:
            print(f"{f}/{n}", flush=True)
    ff.stdin.close()
    ff.wait()
    print("wrote", OUT, round(total, 1), "s")


if __name__ == "__main__":
    BG = stage_bg()
    sc, split = build()
    render(sc, split, preview=0 if "--preview" in sys.argv else None)

#!/usr/bin/env python3
"""Cut the Closeout demo film: stills from capture.py in device frames, camera moves, taps, captions, score.

    python3 demo-video/assemble.py                 # full film -> output/closeout-demo.mp4
    python3 demo-video/assemble.py --preview 7     # one still per scene at its midpoint -> output/preview/
"""
import json
import math
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
CAP = HERE / "output" / "cap"
PHOTOS = Path.home() / "Documents/New project/PunchPilot/demo-assets/photos"
SCORE = HERE / "audio" / "score.mp3"
OUT = HERE / "output" / "closeout-demo.mp4"
W, H, FPS, SS = 1920, 1080, 30, 2          # output size; stage is rendered SS times larger for crisp zooms
SW, SH = W * SS, H * SS
META = json.loads((CAP / "shots.json").read_text())
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
    return Image.open(CAP / f"{name}.png").convert("RGB")


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
    """items: dicts with text lines, t0, t1, x, y, align, size."""
    d = ImageDraw.Draw(frame, "RGBA")
    for c in items:
        t0, t1 = c["t0"], c.get("t1", 1e9)
        a_in, a_out = ramp(t, t0, t0 + 0.7), 1 - ramp(t, t1 - 0.5, t1)
        a = min(a_in, a_out)
        if a <= 0:
            continue
        if c.get("band"):
            band = Image.new("RGBA", (W, 250), STAGE + (0,))
            mask = Image.linear_gradient("L").rotate(180).resize((W, 250)).point(lambda v: round(min(255, v * 1.6) * a))
            frame.paste(Image.new("RGB", (W, 250), STAGE), (0, 0), mask)
            d = ImageDraw.Draw(frame, "RGBA")
        rise = (1 - a_in) * 26
        y = c["y"] + rise
        col = c.get("color", INK)
        f = font(c.get("size", 72), c.get("weight", "Semibold"))
        for i, line in enumerate(c["lines"]):
            li = ramp(t, t0 + i * 0.18, t0 + i * 0.18 + 0.7) if a_out >= 1 else a
            fill = col if i == 0 or c.get("same") else c.get("sub", GREY)
            ff = f if i == 0 or c.get("same") else font(round(c.get("size", 72) * 0.5), "Regular")
            anchor = {"l": "la", "c": "ma", "r": "ra"}[c.get("align", "l")]
            d.text((c["x"], y), line, font=ff, fill=fill + (round(255 * min(li, a)),), anchor=anchor)
            y += ff.size * (1.14 if i == 0 or c.get("same") else 1.45) + (18 if i == 0 else 0)


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


@lru_cache(4)
def desk_stage(name, top=0.2, width=0.8):
    st = BG.copy()
    img = shot(name)
    m = browser(img, SW / 2, round(SH * top), round(SW * width), st, crop_h=min(img.height, 1800))
    return st, m


def dual_stage(dname, pname):
    st = BG.copy()
    img = shot(dname)
    md = browser(img, SW * 0.41, round(SH * 0.2), round(SW * 0.66), st, crop_h=1800)
    mp = phone(shot(pname), SW * 0.82, SH * 0.6, round(SH * 0.72), st)
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


def scene_phone(beats, dur, cap, cam=None, taps=()):
    """beats: [(t_start, still)]; cap: caption dicts; cam: camera keys; taps: [(t, still)] uses shots.json tap."""
    cam = cam or [pz(0, 1.0), pz(dur, 1.06)]

    def draw(t):
        name = [b for b in beats if b[0] <= t][-1][1]
        st, m = phone_stage(name)
        fr, to_out = camera(st, cam, t)
        for tt, nm in taps:
            p = META[nm].get("tap")
            if p:
                sx, sy = phone_stage(nm)[1](p[0] * META[nm]["dsf"], p[1] * META[nm]["dsf"])
                tap(fr, *to_out(sx, sy), t, tt)
        captions(fr, cap, t)
        return fr
    return dur, draw


def scene_desk(beats, dur, cap, cam=None, cur=None):
    """cur: (t0, t1, click_t, still, start_out_xy) moves the cursor to that still's tap point."""
    cam = cam or [(0, 0.5, 0.5, 1.0)]

    def draw(t):
        name = [b for b in beats if b[0] <= t][-1][1]
        st, m = desk_stage(name)
        fr, to_out = camera(st, cam, t)
        captions(fr, cap, t)
        if cur:
            t0, t1, ct, nm, start = cur
            p = META[nm]["tap"]
            sx, sy = desk_stage(nm)[1](p[0] * META[nm]["dsf"], p[1] * META[nm]["dsf"])
            if t >= t0 - 0.4:
                cursor(fr, start, to_out(sx, sy), t, t0, t1, click=ct)
        return fr
    return dur, draw


def scene_report(dur, cap):
    img = shot("d06-report")
    vis = 1800
    top = round(SH * 0.2)
    width = round(SW * 0.62)
    max_scroll = max(0, img.height - vis)

    def draw(t):
        st = BG.copy()
        scroll = max_scroll * 0.62 * ramp(t, 2.2, dur - 1.0)
        browser(img, SW / 2, top, width, st, crop_h=vis, scroll=scroll)
        fr = st.resize((W, H), Image.BILINEAR)
        captions(fr, cap, t)
        return fr
    return dur, draw


def scene_dual(dur, cap):
    def draw(t):
        st, md, mp = _dual()
        fr, _ = camera(st, [(0, 0.5, 0.5, 1.0)], t)
        captions(fr, cap, t)
        return fr
    return dur, draw


@lru_cache(1)
def _dual():
    return dual_stage("d11-overview-closed", "p15-contractor-closed")


def pz(t, z, cy=0.5, px=0.70):
    """Camera key that enlarges the phone around its own spot, leaving the caption column clear."""
    return (t, px - (px - 0.5) / z, cy, z)


def left(lines, t0, t1, y=380, size=76):
    return {"lines": lines, "t0": t0, "t1": t1, "x": 170, "y": y, "size": size, "weight": "Bold"}


def top(lines, t0, t1, size=60):
    return {"lines": lines, "t0": t0, "t1": t1, "x": W / 2, "y": 62, "size": size, "align": "c", "weight": "Bold", "band": True}


def end_extra(fr, t):
    d = ImageDraw.Draw(fr, "RGBA")
    a = round(255 * ramp(t, 3.0, 4.0))
    d.text((W / 2, H - 150), "Built with Strands Agents SDK on Amazon Bedrock", font=font(30, "Medium"), fill=(161, 161, 166, a), anchor="mm")
    d.text((W / 2, H - 105), "AWS Agents for Humans Hackathon", font=font(24, "Regular"), fill=(110, 110, 115, a), anchor="mm")


def build():
    P = lambda nm, i=None: nm
    S = []
    S.append(scene_photo("site-access-2.jpg", ["A site walk ends with a list.", "Then the chasing starts."], 7.0))
    S.append(scene_black(["Closeout", "From the walk to the last item closed."], 6.5))
    S.append(scene_desk([(0, "d01-projects"), (3.6, "d02-docs")], 9.0,
                        [top(["One project. Every drawing, filed."], 0.5, 8.6)],
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
                         [left(["The list is frozen", "for the contractor."], 0.3, 5.2)]))
    S.append(scene_desk([(0, "d04-overview")], 7.0, [top(["The overview always shows the next step."], 0.4, 6.7, size=56)],
                        cam=[(0, 0.5, 0.5, 1.0), (7.0, 0.5, 0.42, 1.12)]))
    S.append(scene_desk([(0, "d05-deficiencies")], 7.0, [top(["Every item, where it is, and what it needs."], 0.4, 6.7, size=56)],
                        cam=[(0, 0.5, 0.5, 1.0), (7.0, 0.5, 0.8, 1.3)]))
    S.append(scene_report(12.0, [top(["The report is ready.", "Photos, plan pins, what closes each item."], 0.4, 10.6, size=56)]))
    S.append(scene_phone([(0, "p13-contractor"), (5.0, "p14-contractor-list")], 11.0,
                         [left(["The contractor", "gets one link."], 0.3, 5.0),
                          left(["No account.", "Just their items, and", "what closes each one."], 5.4, 10.7)]))
    S.append(scene_desk([(0, "d08-item"), (4.0, "d09-item-evidence")], 9.5,
                        [top(["What they send lands on the item it belongs to."], 0.4, 9.2, size=54)],
                        cur=(0.6, 2.2, 2.6, "d08-item", (W * 0.6, H * 0.9))))
    S.append(scene_desk([(0, "d09-item-evidence"), (2.7, "d10-item-closed")], 7.5,
                        [top(["The engineer decides what closes."], 0.4, 7.2)],
                        cam=[(0, 0.5, 0.5, 1.0), (7.5, 0.36, 0.62, 1.35)],
                        cur=(0.4, 2.0, 2.4, "d09-item-evidence", (W * 0.7, H * 0.4))))
    S.append(scene_dual(8.0, [top(["The office and the contractor", "see the same list."], 0.4, 7.7, size=56)]))
    S.append(scene_black(["Closeout", "Walk it. Send it. Close it."], 12.0, extra=end_extra))
    return S


def render(scenes, preview=None):
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
        print("preview scenes", len(scenes), "total", round(total, 1), "s")
        return
    fade_out = 2.5
    af = f"afade=t=in:d=1.2,afade=t=out:st={total - fade_out:.2f}:d={fade_out},loudnorm=I=-16:TP=-1.5:LRA=11"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-i", str(SCORE), "-filter:a", af, "-map", "0:v", "-map", "1:a", "-t", f"{total:.2f}",
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
    sc = build()
    render(sc, preview=0 if "--preview" in sys.argv else None)

"""Pilot, not wired into the pages: one venue's card drawn in that venue's own visual language.

Today only Temple. The look follows Temple's public app (app.templedigitalgroup.com/stats, read
2026-10-07): a near-black page, warm off-white ink, IBM Plex Mono at light weights, uppercase
tracked labels, square hairline boxes, a coral accent. Only open-licensed type is used (IBM Plex
Mono, OFL, the same family Temple's app loads); none of Temple's own font files or marks.

It stays our card: our mark and name sit at the top, the strip at the foot names
cantonvenues.com as the source and says the card is independent of the venue.

    python venue_style_pilot.py --api ./api --out temple-venue-style.png
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import venue_pages as vp

# Temple's app palette. Sources: app.templedigitalgroup.com/_next/static/chunks/*.css (the most
# used colours: #f2f1e9 ink, #e07a5a coral, #27b48b green), templedigitalgroup.com
# 3s9vsnsj2tc9d.css `.dark { --background: 0 0% 8.6% }` (#161616), and pixels of the stats page
# (#0e0e0e boxes, #222222 box borders).
TEMPLE = {"bg": "#161616", "box": "#0e0e0e", "edge": "#2a2a2a", "ink": "#f2f1e9", "muted": "#9a9a9a",
          "label": "#8a8a8a", "coral": "#e07a5a", "green": "#27b48b"}


def _mono(weight: str, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(str(vp.FONTS / f"IBMPlexMono-{weight}.ttf"), size)


def _tracked(d, xy, text: str, font, fill, track: float) -> float:
    """Draw ``text`` with extra letter spacing (``track`` x size, as CSS ``letter-spacing: .2em``)."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill, anchor="ls")
        x += d.textlength(ch, font=font) + track * font.size
    return x


def render(f: dict, head: dict, t: int, path: Path) -> None:
    from PIL import Image, ImageDraw

    c = {k: vp._hex(v) for k, v in TEMPLE.items()}
    S = 2
    W, H = 1200 * S, 630 * S
    M = 56 * S
    img = Image.new("RGB", (W, H), c["bg"])
    d = ImageDraw.Draw(img)
    width = W - 2 * M

    # top rail: our mark and name on the left, the read time on the right, in Temple's mono
    vp.draw_logo(d, M, 40 * S, 30 * S, c["ink"], c["bg"])
    d.text((M + 42 * S, 55 * S), "Canton Venues", font=vp._font("SemiBold", 24 * S), fill=c["ink"], anchor="lm")
    lab = _mono("Regular", 15 * S)
    when = vp.stamp(t).upper()
    tw = sum(d.textlength(ch, font=lab) + 0.2 * lab.size for ch in when)
    d.ellipse((W - M - tw - 22 * S, 51 * S, W - M - tw - 12 * S, 61 * S), fill=c["green"])
    _tracked(d, (W - M - tw, 61 * S), when, lab, c["label"], 0.2)
    d.line((M, 96 * S, W - M, 96 * S), fill=c["edge"], width=S)

    # eyebrow and the lead, light mono as Temple's own headings
    title = head["title"]
    tfont, lines = None, None
    for size in (56, 52, 48, 44):
        tfont = _mono("Light", size * S)
        if d.textlength(title, font=tfont) <= width:
            lines = [title]
            break
        lines = vp._balanced(d, title, tfont, width)
        if lines:
            break
    lines = lines or vp._wrap(d, title, tfont, width, 2)
    step = round(tfont.size * 1.18)
    block = 22 * S + 24 * S + len(lines) * step + 10 * S + 28 * S
    y = 96 * S + max(30 * S, (270 * S - block) // 2)
    _tracked(d, (M, y + 18 * S), f"{f['name']} / {f['kind']}".upper(), _mono("Regular", 18 * S), c["coral"], 0.24)
    y += 22 * S + 24 * S
    for line in lines:
        d.text((M, y), line, font=tfont, fill=c["ink"], anchor="lt")
        y += step
    d.text((M, y + 10 * S), head["sub"], font=_fit_mono(d, head["sub"], "Light", 22 * S, width, 15 * S),
           fill=c["muted"], anchor="lt")

    # square hairline boxes, uppercase tracked label over a light number
    panels = vp.stats(f)
    if panels:
        gap, top, ph = 14 * S, 384 * S, 124 * S
        pw = (width - gap * (len(panels) - 1)) // len(panels)
        for n, p in enumerate(panels):
            x0 = M + n * (pw + gap)
            d.rectangle((x0, top, x0 + pw, top + ph), fill=c["box"], outline=c["edge"], width=S)
            pad = 22 * S
            _tracked(d, (x0 + pad, top + 34 * S), p["k"].upper(), _mono("Regular", 13 * S), c["label"], 0.18)
            d.text((x0 + pad, top + 50 * S), p["v"], font=_fit_mono(d, p["v"], "Light", 40 * S, pw - 2 * pad, 24 * S),
                   fill=c["ink"], anchor="lt")
            first = p["n"].startswith("#1 ")
            d.text((x0 + pad, top + 100 * S), p["n"],
                   font=_fit_mono(d, p["n"], "Regular", 14 * S, pw - 2 * pad, 11 * S),
                   fill=c["green"] if first else c["muted"], anchor="lt")

    # our strip: the source, and that this is not the venue's own material
    st = 544 * S
    d.rectangle((0, st, W, H), fill=c["ink"])
    sf = vp._font("SemiBold", 22 * S)
    x = M
    for part, col in (("Source: ", vp._hex("#0d1421")), ("cantonvenues.com", vp._hex("#3861fb")),
                      (" · live Canton DEX data", vp._hex("#0d1421"))):
        d.text((x, st + 30 * S), part, font=sf, fill=col, anchor="lm")
        x += d.textlength(part, font=sf)
    note = f"Independent data, not affiliated with {f['name']}. {vp.METHOD.get(head['rule'], '')}".strip()
    d.text((M, st + 62 * S), note, font=vp._fit(d, note, "Regular", 17 * S, width, 13 * S),
           fill=vp._hex("#58667e"), anchor="lm")

    out = img.resize((1200, 630), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    out.save(tmp, "PNG", optimize=True)
    os.replace(tmp, path)


def _fit_mono(d, text: str, weight: str, size: int, width: int, floor: int):
    while size > floor and d.textlength(text, font=_mono(weight, size)) > width:
        size -= 2
    return _mono(weight, size)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", type=Path, required=True, help="directory holding venues.json and the rest")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--venue", default="temple")
    args = ap.parse_args()
    data = {n: json.loads((args.api / f"{n}.json").read_text())
            for n in ("venues", "tokens", "execution", "lp", "perps")}
    f = vp.facts(data)[args.venue]
    render(f, vp.headline(f), data["venues"]["t"], args.out)


if __name__ == "__main__":
    main()

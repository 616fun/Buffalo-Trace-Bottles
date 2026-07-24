#!/usr/bin/env python3
"""
build_og_image.py — Regenerate og-image.png with TODAY's availability.

The social-share card is a pipeline artifact: whenever the link is shared
(text, Reddit, forums), the preview shows the actual day's gift shop
status instead of a static logo. Called from build_data_json.py
(non-fatal — if Pillow is missing the previous image simply remains).

Usage:
    python build_og_image.py [--data data.json] [--out og-image.png]
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

BOTTLES = [
    ("blantons",    "Blanton's"),
    ("weller107",   "Weller 107"),
    ("ehtaylor_sb", "E.H. Taylor"),
    ("eagle_rare",  "Eagle Rare"),
]

FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu/",           # ubuntu runners
    "/usr/share/fonts/dejavu/",                     # some distros
]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def _font(name, size):
    from PIL import ImageFont
    for d in FONT_DIRS:
        try:
            return ImageFont.truetype(d + name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build(data, out_path: Path):
    from PIL import Image, ImageDraw

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), "#1A0F05")
    # amber glow, top-left
    glow = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-500, -450, 700, 500], fill=55)
    img = Image.composite(Image.new("RGB", (W, H), "#C07328"), img, glow)
    d = ImageDraw.Draw(img)

    serif_b  = lambda s: _font("DejaVuSerif-Bold.ttf", s)
    sans     = lambda s: _font("DejaVuSans.ttf", s)
    sans_b   = lambda s: _font("DejaVuSans-Bold.ttf", s)

    date_iso = data["meta"]["last_updated"]
    date_lbl = datetime.date.fromisoformat(date_iso).strftime("%A, %B %d, %Y").replace(" 0", " ")

    t1 = "Buffalo Trace "
    d.text((70, 56), t1, font=serif_b(58), fill="#F7F3ED")
    w1 = d.textlength(t1, font=serif_b(58))
    d.text((70 + w1, 56), "Bottle Drops", font=serif_b(58), fill="#F0C060")
    d.text((72, 136), f"Gift shop availability — {date_lbl}", font=sans(30), fill="#D9C9B2")

    # status tiles
    tile_w, tile_h, gap, x0, y0 = 258, 240, 24, 70, 210
    today_avail = {b["key"]: b["available_today"] == 1 for b in data["bottles"]}
    for i, (key, label) in enumerate(BOTTLES):
        x = x0 + i * (tile_w + gap)
        avail = today_avail.get(key, False)
        d.rounded_rectangle([x, y0, x + tile_w, y0 + tile_h], radius=20,
                            fill="#241505", outline="#3A2410", width=2)
        sym, col = ("✓", "#4ADE80") if avail else ("✗", "#EF5350")
        d.text((x + tile_w/2, y0 + 78), sym, font=sans_b(84), fill=col, anchor="mm")
        d.text((x + tile_w/2, y0 + 158), label, font=sans_b(28), fill="#F7F3ED", anchor="mm")
        d.text((x + tile_w/2, y0 + 198), "In stock" if avail else "Out today",
               font=sans(22), fill=col, anchor="mm")

    # special release strip (if any today)
    special = (data.get("today") or {}).get("special_release")
    if special:
        d.rounded_rectangle([70, 480, 1130, 540], radius=14, fill="#F0C060")
        d.text((600, 510), f"★  Special release today: {special}",
               font=sans_b(28), fill="#1A0F05", anchor="mm")
        y_foot = 570
    else:
        y_foot = 520

    d.text((70, y_foot), "buffalotracebottledrops.com — updated every morning, 7am EST",
           font=sans_b(26), fill="#9C8B75")

    img.save(out_path, optimize=True)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data.json")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    data_path = Path(args.data)
    out = Path(args.out) if args.out else data_path.parent / "og-image.png"
    data = json.loads(data_path.read_text())
    build(data, out)
    log(f"[og-image] wrote {out}")
    print(json.dumps({"success": True}))


if __name__ == "__main__":
    main()

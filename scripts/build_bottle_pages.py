#!/usr/bin/env python3
"""
build_bottle_pages.py — Generate static per-bottle SEO landing pages.

Each tracked bottle gets its own URL (e.g. /blantons/) with today's
availability baked into the static HTML, title, and meta description —
fully crawlable with no JavaScript required. Regenerated on every pipeline
run by build_data_json.py, so the pages are as fresh as data.json.

Usage:
    python build_bottle_pages.py [--data data.json] [--out-dir .]

Called automatically from build_data_json.py (non-fatal there).
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

SITE = "https://buffalotracebottledrops.com"

# slug → bottle key + static copy (facts only; no invented pricing claims)
PAGES = {
    "blantons": {
        "key": "blantons",
        "h1": "Blanton's Single Barrel",
        "series": "Blanton's",
        "about": ("Blanton's Single Barrel — the original single barrel bourbon, "
                  "first bottled in 1984 and instantly recognizable by its round "
                  "grenade-shaped bottle and horse-and-jockey stopper. Allocated "
                  "nearly everywhere, but a regular at the Buffalo Trace "
                  "Distillery gift shop in Frankfort, Kentucky."),
        "notes": ["Citrus & honey", "Vanilla", "Baking spice", "Caramel corn"],
    },
    "weller-antique-107": {
        "key": "weller107",
        "h1": "Weller Antique 107",
        "series": "W.L. Weller",
        "about": ("Weller Antique 107 is the higher-proof expression of Buffalo "
                  "Trace's wheated bourbon line — the same wheated mashbill "
                  "family as Pappy Van Winkle, bottled at 107 proof. One of the "
                  "most hunted shelf bottles in America and a frequent sight at "
                  "the distillery gift shop."),
        "notes": ["Cherry & red fruit", "Cinnamon", "Brown sugar", "Long spicy finish"],
    },
    "eh-taylor-small-batch": {
        "key": "ehtaylor_sb",
        "h1": "E.H. Taylor Small Batch",
        "series": "E.H. Taylor, Jr.",
        "about": ("Colonel E.H. Taylor, Jr. Small Batch is a bottled-in-bond "
                  "bourbon (100 proof) honoring the founding father of the "
                  "bonded whiskey movement. The most reliably available of the "
                  "four rotating gift shop bottles — but far from guaranteed."),
        "notes": ["Caramel & toffee", "Ripe apple", "Tobacco", "Toasted oak"],
    },
    "eagle-rare": {
        "key": "eagle_rare",
        "h1": "Eagle Rare 10-Year",
        "series": "Eagle Rare",
        "about": ("Eagle Rare is a 10-year age-stated single barrel bourbon and "
                  "arguably the best value in American whiskey at retail price. "
                  "Its scarcity makes it the hardest of the four rotating "
                  "bottles to catch at the Buffalo Trace gift shop."),
        "notes": ["Toffee & orange peel", "Herbs", "Leather", "Dry oak finish"],
    },
}

SLUGS = list(PAGES.keys())


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fmt_date(iso):
    d = datetime.date.fromisoformat(iso)
    return d.strftime("%B %-d, %Y") if sys.platform != "win32" else d.strftime("%B %d, %Y")


def fmt_short(iso):
    return datetime.date.fromisoformat(iso).strftime("%b %-d")


def build_page(slug, cfg, data):
    b = next(x for x in data["bottles"] if x["key"] == cfg["key"])
    meta, today, cal = data["meta"], data["today"], data["calendar"]
    updated = fmt_date(meta["last_updated"])
    avail = b["available_today"] == 1
    status_word = "Available Today" if avail else "Not Available Today"
    status_sym = "✓" if avail else "✗"
    open_days = [d for d in cal if not d.get("is_closure")]
    days_avail = round(b["overall_pct"] / 100 * len(open_days))
    pred = round(b.get("prediction_tomorrow_pct") or 0)
    streak_txt = (f"available {b['streak']} day{'s' if b['streak']!=1 else ''} in a row"
                  if b["streak_direction"] == "available"
                  else f"out of stock for {b['streak']} day{'s' if b['streak']!=1 else ''}")
    price = b.get("gift_shop_price")
    price_html = (f'<div class="stat"><div class="v">${price:.2f}</div>'
                  f'<div class="l">Gift shop price</div></div>') if price else ""
    last14 = [d for d in cal][-14:]
    dots = "".join(
        f'<span class="dot {"on" if d.get(cfg["key"])==1 else ("cl" if d.get("is_closure") else "off")}" '
        f'title="{d["date"]}"></span>' for d in last14)
    notes = "".join(f'<span class="chip">{n}</span>' for n in cfg["notes"])
    others = " · ".join(
        f'<a href="/{s}/">{PAGES[s]["h1"]}</a>' for s in SLUGS if s != slug)

    title = f"Is {cfg['h1']} Available at Buffalo Trace Today? {status_sym} {status_word} ({updated})"
    desc = (f"{cfg['h1']} is {'AVAILABLE' if avail else 'not available'} at the Buffalo Trace "
            f"Distillery gift shop today ({updated}) — {streak_txt}. Seen {days_avail} of "
            f"{len(open_days)} tracked days; {pred}% predicted for tomorrow. Updated every morning.")

    faq_json = json.dumps({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question",
             "name": f"Is {cfg['h1']} available at the Buffalo Trace gift shop today?",
             "acceptedAnswer": {"@type": "Answer",
                 "text": f"As of {updated}: {cfg['h1']} is {'available' if avail else 'not available'} at the Buffalo Trace Distillery gift shop ({streak_txt}). This page updates every morning after the 7am EST check."}},
            {"@type": "Question",
             "name": f"How often does {cfg['h1']} show up at the Buffalo Trace gift shop?",
             "acceptedAnswer": {"@type": "Answer",
                 "text": f"{cfg['h1']} has been available {days_avail} of {len(open_days)} tracked open days ({b['overall_pct']:.0f}%) since March 2026, averaging one appearance every {b.get('avg_days_between_releases') or '—'} days."}},
        ],
    }, separators=(",", ":"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{title}</title>
<meta name="description" content="{desc}"/>
<link rel="canonical" href="{SITE}/{slug}/"/>
<link rel="icon" type="image/svg+xml" href="/favicon.svg"/>
<meta property="og:title" content="{title}"/>
<meta property="og:description" content="{desc}"/>
<meta property="og:type" content="website"/>
<meta property="og:url" content="{SITE}/{slug}/"/>
<meta property="og:image" content="{SITE}/og-image.png"/>
<meta name="twitter:card" content="summary_large_image"/>
<script type="application/ld+json">{faq_json}</script>
<style>
:root{{--bg:#F7F3ED;--surface:#fff;--text:#1A0F05;--text2:#5C4A32;--muted:#9C8B75;--amber:#C07328;--gold:#F0C060;--green:#166534;--red:#991B1B;--border:rgba(0,0,0,.08)}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:var(--text);line-height:1.6}}
a{{color:var(--amber)}}
header{{background:#1A0F05;padding:16px 24px}}
header a{{color:#F7F3ED;text-decoration:none;font-weight:600}}
header .gold{{color:var(--gold)}}
main{{max-width:760px;margin:0 auto;padding:36px 24px 80px}}
.status{{background:var(--surface);border-radius:14px;padding:28px;box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:24px;border-left:6px solid {('#4CAF50' if avail else '#EF5350')}}}
.status h1{{font-size:1.5rem;margin-bottom:6px}}
.badge{{display:inline-block;font-weight:700;padding:4px 14px;border-radius:20px;font-size:.95rem;
 background:{('#F0FDF4' if avail else '#FEF2F2')};color:{('#166534' if avail else '#991B1B')};border:1px solid {('#BBF7D0' if avail else '#FECACA')}}}
.upd{{color:var(--muted);font-size:.82rem;margin-top:8px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:24px 0}}
.stat{{background:var(--surface);border-radius:10px;padding:16px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
.stat .v{{font-size:1.3rem;font-weight:700;color:var(--text2)}}
.stat .l{{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-top:4px}}
h2{{font-size:1.05rem;margin:28px 0 10px}}
.dots{{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}}
.dot{{width:16px;height:16px;border-radius:50%;background:#E8DDD0}}
.dot.on{{background:var(--amber)}}
.dot.cl{{background:#1e2535}}
.chip{{display:inline-block;background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:4px 12px;font-size:.8rem;margin:0 6px 6px 0;color:var(--text2)}}
.cta{{display:inline-block;background:var(--amber);color:#fff;text-decoration:none;font-weight:600;padding:12px 22px;border-radius:10px;margin-top:16px}}
footer{{border-top:1px solid var(--border);margin-top:36px;padding-top:18px;font-size:.78rem;color:var(--muted)}}
footer a{{color:var(--text2)}}
</style>
</head>
<body>
<header><a href="/">🥃 Buffalo Trace <span class="gold">Bottle Drops</span></a></header>
<main>
  <div class="status">
    <h1>Is {cfg['h1']} Available at Buffalo Trace Today?</h1>
    <p><span class="badge">{status_sym} {status_word}</span></p>
    <p style="margin-top:10px;color:var(--text2)">As of this morning's check, {cfg['h1']} is
    <strong>{'available' if avail else 'not available'}</strong> at the Buffalo Trace Distillery
    gift shop in Frankfort, KY — {streak_txt}.</p>
    <p class="upd">Updated {updated} · checked daily at 7am EST (8am Sun)</p>
  </div>

  <div class="stats">
    <div class="stat"><div class="v">{b['overall_pct']:.0f}%</div><div class="l">Availability rate</div></div>
    <div class="stat"><div class="v">{days_avail}/{len(open_days)}</div><div class="l">Open days seen</div></div>
    <div class="stat"><div class="v">{pred}%</div><div class="l">Tomorrow's forecast</div></div>
    <div class="stat"><div class="v">{(b.get('avg_days_between_releases') or 0):.1f}d</div><div class="l">Avg days between</div></div>
    {price_html}
  </div>

  <h2>Last 14 days at the gift shop</h2>
  <div class="dots">{dots}</div>
  <p style="font-size:.75rem;color:var(--muted)">Amber = available that day · dark = gift shop closed</p>

  <h2>About {cfg['h1']}</h2>
  <p style="color:var(--text2)">{cfg['about']}</p>
  <p style="margin-top:10px">{notes}</p>

  <a class="cta" href="/">See today's full tracker &amp; predictions →</a>

  <footer>
    <p>Also tracked: {others}</p>
    <p style="margin-top:8px">Buffalo Trace Bottle Drops · availability data collected daily from
    <a href="https://www.buffalotracedistillery.com/visit-us/product-availability/" rel="noopener">buffalotracedistillery.com</a>
    · Not affiliated with Buffalo Trace Distillery</p>
  </footer>
</main>
</body>
</html>
"""


def build_all(data, out_dir: Path):
    written = []
    for slug, cfg in PAGES.items():
        page_dir = out_dir / slug
        page_dir.mkdir(exist_ok=True)
        (page_dir / "index.html").write_text(build_page(slug, cfg, data))
        written.append(f"{slug}/index.html")
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data.json")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    data_path = Path(args.data)
    out_dir = Path(args.out_dir) if args.out_dir else data_path.parent
    data = json.loads(data_path.read_text())
    written = build_all(data, out_dir)
    log(f"[bottle-pages] wrote: {', '.join(written)}")
    print(json.dumps({"success": True, "pages": written}))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
build_home_static.py — Write today's answer into the STATIC HTML of index.html.

index.html is a JS-rendered app: the header date, the hero and the bottle list
are all painted by renderToday() from INLINE_DATA. A crawler that reads only
the raw HTML saw "Loading…" and no bottles. This script rewrites four
marker-delimited regions of index.html on every pipeline run so the raw HTML
carries the same facts the JS will paint:

  <!--bt:hdr-update-->…<!--/bt:hdr-update-->   header "Oct 20 · 6:57am EST"
  <!--bt:hdr-days-->…<!--/bt:hdr-days-->       header "222 days tracked"
  <!--bt:live-answer-->…<!--/bt:live-answer--> "What is Buffalo Trace selling today?" answer
  FAQPage JSON-LD answer for the same question (kept identical in wording)

The header strings are byte-for-byte what renderToday() produces, so the JS
overwrite changes nothing on screen — that is what keeps CLS at 0 (the
2026-07-24 fix). Nothing here is shown that the JS does not already derive
from the same data; special releases are deliberately NOT mentioned (removed
from the public pages 2026-09-22).

Called from build_data_json.py right after refresh_inline_data (non-fatal
there). Also runnable by hand:
    python build_home_static.py [--data data.json] [--index index.html]
"""

import argparse
import datetime
import html
import json
import re
import sys
from pathlib import Path

# bottle key -> (display name, landing page path). Names mirror data.json.
BOTTLE_LINKS = {
    "blantons":    "/blantons/",
    "weller107":   "/weller-antique-107/",
    "ehtaylor_sb": "/eh-taylor-small-batch/",
    "eagle_rare":  "/eagle-rare/",
}

LIVE_QUESTION = "What is Buffalo Trace selling today?"
SCHEDULE_SENTENCE = "We check every morning from 7am EST (8am on Sundays)."


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def _join(names):
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _long_date(iso):
    d = datetime.date.fromisoformat(iso)
    return f"{d:%A, %B} {d.day}, {d.year}"


def header_strings(data):
    """Exactly what renderToday() writes into #hdr-update / #hdr-days."""
    meta, today = data["meta"], data["today"]
    d = datetime.date.fromisoformat(meta["last_updated"])
    upd = f"{d:%b} {d.day}"
    if today.get("last_site_update"):
        upd += " · " + today["last_site_update"]
    n = meta["days_tracked"]
    days = f"{n} day{'' if n == 1 else 's'} tracked"
    return upd, days


def live_answer(data):
    """Return (html, plain_text) for the 'selling today' answer."""
    today, bottles = data["today"], data["bottles"]
    date = _long_date(today["date"])
    notes = today.get("notes") or ""
    is_closure = bool(today.get("is_closure")) or notes.startswith("Gift shop closed")

    def link(b):
        return (f'<a href="{BOTTLE_LINKS[b["key"]]}">{html.escape(b["name"], quote=False)}</a>'
                if b["key"] in BOTTLE_LINKS else html.escape(b["name"], quote=False))

    if is_closure:
        holiday = re.sub(r"^Gift shop closed\s*[—–-]\s*", "", notes).strip() or "a holiday"
        h = (f"On {date} the Buffalo Trace gift shop is closed ({html.escape(holiday, quote=False)}), "
             f"so no bottles are on sale.")
        t = f"On {date} the Buffalo Trace gift shop is closed ({holiday}), so no bottles are on sale."
    else:
        avail = [b for b in bottles if b.get("available_today") == 1]
        out = [b for b in bottles if b.get("available_today") != 1]
        if not avail:
            body_h = body_t = "none of the four tracked bottles"
        else:
            body_h = _join([link(b) for b in avail])
            body_t = _join([b["name"] for b in avail])
            if len(avail) == len(bottles):
                body_h = "all four tracked bottles: " + body_h
                body_t = "all four tracked bottles: " + body_t
        h = f"On {date}, Buffalo Trace's product availability page lists {body_h}."
        t = f"On {date}, Buffalo Trace's product availability page lists {body_t}."
        if out and avail:
            h += f" Not listed: {_join([link(b) for b in out])}."
            t += f" Not listed: {_join([b['name'] for b in out])}."
        if notes.startswith("PROVISIONAL"):
            h += " Provisional capture: Buffalo Trace's own update stamp was stale."
            t += " Provisional capture: Buffalo Trace's own update stamp was stale."

    if today.get("last_site_update") and not is_closure:
        h += f" Buffalo Trace's list was last updated {html.escape(today['last_site_update'], quote=False)}."
        t += f" Buffalo Trace's list was last updated {today['last_site_update']}."
    h += " " + SCHEDULE_SENTENCE
    t += " " + SCHEDULE_SENTENCE
    h += ' <a href="#bottle-grid">See each bottle’s status, streak and odds above.</a>'
    t += " See each bottle's status, streak and odds above."
    return h, t


def _swap(src, name, new):
    """Replace the content between <!--bt:name--> and <!--/bt:name-->."""
    pat = re.compile(rf"(<!--bt:{re.escape(name)}-->).*?(<!--/bt:{re.escape(name)}-->)", re.DOTALL)
    out, n = pat.subn(lambda m: m.group(1) + new + m.group(2), src, count=1)
    if n != 1:
        raise ValueError(f"marker bt:{name} not found exactly once")
    return out


def _swap_jsonld_answer(src, plain):
    pat = re.compile(
        r'("name": ' + re.escape(json.dumps(LIVE_QUESTION)) +
        r',\s*"acceptedAnswer": \{"@type": "Answer", "text": )"(?:[^"\\]|\\.)*"')
    out, n = pat.subn(lambda m: m.group(1) + json.dumps(plain, ensure_ascii=False), src, count=1)
    if n != 1:
        raise ValueError("FAQPage entry for the live question not found exactly once")
    return out


def _validate_jsonld(src):
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', src, re.DOTALL)
    if not blocks:
        raise ValueError("no JSON-LD block")
    faq = 0
    for b in blocks:
        doc = json.loads(b)  # raises if invalid
        for node in doc.get("@graph", [doc]):
            if node.get("@type") == "FAQPage":
                faq += 1
    if faq != 1:
        raise ValueError(f"expected exactly 1 FAQPage, found {faq}")


def refresh_home_static(index_path: Path, data: dict) -> bool:
    """Rewrite the static regions of index.html. Returns True if the file is current."""
    src = index_path.read_text()
    upd, days = header_strings(data)
    ans_html, ans_text = live_answer(data)
    out = _swap(src, "hdr-update", html.escape(upd, quote=False))
    out = _swap(out, "hdr-days", html.escape(days, quote=False))
    out = _swap(out, "live-answer", ans_html)
    out = _swap_jsonld_answer(out, ans_text)
    _validate_jsonld(out)          # never write a page with broken JSON-LD
    if out == src:
        log("  [home-static] Already current")
        return True
    index_path.write_text(out)
    log("  [home-static] Refreshed static date + today's answer in index.html")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data.json")
    ap.add_argument("--index", default="index.html")
    a = ap.parse_args()
    data = json.loads(Path(a.data).read_text())
    ok = refresh_home_static(Path(a.index), data)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

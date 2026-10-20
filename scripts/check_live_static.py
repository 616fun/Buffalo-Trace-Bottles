#!/usr/bin/env python3
"""
check_live_static.py — Watchdog: is the LIVE homepage's static HTML fresh?

build_home_static.py is non-fatal inside the daily pipeline, so if it ever stops
refreshing, the site keeps working but the raw HTML (what search engines read)
quietly goes stale. This job, run by .github/workflows/static-check.yml after the
daily pipeline has finished, fetches the live site and compares:

  1. live data.json last_updated == today (Indianapolis date)   [pipeline ran]
  2. static header date + day count  == data.json                [refresh ran]
  3. static "selling today" answer   == what data.json implies   [refresh correct]
  4. FAQPage JSON-LD: parses, exactly one FAQPage, answer text == the page's  [markup intact]

Any failure -> Pushover priority 1 to Brian's iPhone (same env vars and device as
the pipeline's alerts). Silent on success. Exit code 1 on failure so the Actions
run also shows red.

  python scripts/check_live_static.py [--site URL] [--no-alert] [--force-alert]
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_home_static as bhs  # same code that wrote the page = the expected values

SITE = "https://buffalotracebottledrops.com"


def log(msg):
    print(msg, flush=True)


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            sep = "&" if "?" in url else "?"
            req = urllib.request.Request(f"{url}{sep}cb={int(time.time())}",
                                         headers={"Cache-Control": "no-cache", "Pragma": "no-cache",
                                                  "User-Agent": "bt-static-watchdog/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8")
        except Exception as exc:
            last = exc
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"could not fetch {url}: {last}")


def _marker(html, name):
    m = re.search(rf"<!--bt:{name}-->(.*?)<!--/bt:{name}-->", html, re.S)
    return m.group(1) if m else None


def _plain(s):
    return re.sub(r"<[^>]+>", "", s).replace("&#x27;", "'").replace("&amp;", "&").replace("’", "'")


def check(html, data, today):
    """Return a list of problem strings (empty = healthy)."""
    problems = []
    if data["meta"]["last_updated"] != today:
        problems.append(f"data.json last_updated is {data['meta']['last_updated']}, expected {today} "
                        f"(daily pipeline may not have run)")
    upd, days = bhs.header_strings(data)
    got_upd, got_days = _marker(html, "hdr-update"), _marker(html, "hdr-days")
    if got_upd is None or got_days is None:
        problems.append("static header markers missing from index.html")
    else:
        if got_upd.strip() != upd:
            problems.append(f"static header date is '{got_upd.strip()}', expected '{upd}'")
        if got_days.strip() != days:
            problems.append(f"static day count is '{got_days.strip()}', expected '{days}'")
    ans_html, ans_text = bhs.live_answer(data)
    got = _marker(html, "live-answer")
    if got is None:
        problems.append("live-answer marker missing from index.html")
    elif _plain(got) != _plain(ans_html):
        problems.append("static 'selling today' answer does not match data.json")
    try:
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        faq = []
        for b in blocks:
            doc = json.loads(b)
            faq += [n for n in doc.get("@graph", [doc]) if n.get("@type") == "FAQPage"]
        if len(faq) != 1:
            problems.append(f"expected exactly 1 FAQPage in JSON-LD, found {len(faq)}")
        else:
            q = [x for x in faq[0]["mainEntity"] if x["name"] == bhs.LIVE_QUESTION]
            if len(q) != 1:
                problems.append("live question missing from FAQPage JSON-LD")
            elif q[0]["acceptedAnswer"]["text"] != ans_text:
                problems.append("FAQPage JSON-LD answer does not match data.json")
    except Exception as exc:
        problems.append(f"JSON-LD invalid: {exc}")
    return problems


def pushover(title, body, priority=1):
    token = os.environ.get("PUSHOVER_TOKEN", "").strip()
    user = os.environ.get("PUSHOVER_USER", "").strip()
    device = os.environ.get("PUSHOVER_DEVICE", "BriansPhone").strip()
    if not token or not user:
        log("[Pushover] PUSHOVER_TOKEN / PUSHOVER_USER not set — cannot alert")
        return False
    payload = urllib.parse.urlencode({"token": token, "user": user, "title": title, "message": body[:1000],
                                      "sound": "cashregister", "priority": priority, "device": device}).encode()
    for i in range(2):
        try:
            req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            return True
        except Exception as exc:
            log(f"[Pushover] attempt {i + 1} failed: {exc}")
            time.sleep(3)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--force-alert", action="store_true", help="send a test alert regardless of result")
    a = ap.parse_args()
    today = datetime.datetime.now(ZoneInfo("America/Indiana/Indianapolis")).date().isoformat()
    try:
        html = fetch(a.site + "/")
        data = json.loads(fetch(a.site + "/data.json"))
        problems = check(html, data, today)
    except Exception as exc:
        problems = [f"watchdog could not read the live site: {exc}"]
    if a.force_alert:
        problems = problems or ["TEST ALERT — watchdog is wired up; everything actually checked out"]
    if not problems:
        log(f"OK {today}: static header, answer and FAQPage JSON-LD match data.json")
        return 0
    body = f"BT static-HTML check {today}: " + "; ".join(problems)
    log("PROBLEMS:\n  - " + "\n  - ".join(problems))
    if not a.no_alert:
        log("alert sent" if pushover("Buffalo Trace — Static HTML stale", body) else "ALERT NOT SENT")
    return 1


if __name__ == "__main__":
    sys.exit(main())

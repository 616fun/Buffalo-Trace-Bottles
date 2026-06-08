#!/usr/bin/env python3
"""
Buffalo Trace Availability Scraper
===================================
Fetches and parses the Buffalo Trace gift shop availability page.
Polls until the page shows today's date, then outputs a JSON result.

Usage:
    python scrape_availability.py [--max-poll-minutes 180] [--dry-run]

Output (stdout, JSON):
    {
        "success": true,
        "date": "2026-03-30",
        "last_site_update": "7:38am EST",
        "blantons": 1,
        "weller107": 0,
        "ehtaylor_sb": 1,
        "eagle_rare": 0,
        "special_release": null,
        "polls": 1
    }

On failure (site stale after timeout):
    {
        "success": false,
        "reason": "Site still stale after 180min poll",
        "polls": 18
    }
"""

import sys
import json
import time
import datetime
import argparse
import re

try:
    import requests
except ImportError:
    print(json.dumps({"success": False, "reason": "requests library not installed — run: pip install requests --break-system-packages"}))
    sys.exit(1)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

URL = "https://www.buffalotracedistillery.com/visit-us/product-availability/"

# Cache-busting headers — bypass Vercel CDN (can cache up to 24h)
HEADERS = {
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

POLL_INTERVAL_SECONDS = 600  # 10 minutes

# Bottle name → key mapping (matches CLAUDE.md schema)
# Keys are checked against h4 text from page product blocks
BOTTLE_KEYS = {
    "blantons":    ["blanton"],
    "weller107":   ["weller antique 107"],
    "ehtaylor_sb": ["e.h. taylor small batch"],
    "eagle_rare":  ["eagle rare"],
}


# ─────────────────────────────────────────────
# Fetch and parse
# ─────────────────────────────────────────────

def fetch_page(timeout=30):
    """Fetch the availability page. Returns (html_text, status_code)."""
    try:
        resp = requests.get(URL, headers=HEADERS, timeout=timeout)
        return resp.text, resp.status_code
    except requests.RequestException as e:
        return None, str(e)


def _coerce_date(month, day, year):
    """Build a datetime.date from M/D/Y ints, or None if out of range."""
    try:
        if 1 <= month <= 12 and 1 <= day <= 31:
            return datetime.date(year, month, day)
    except (ValueError, AttributeError):
        pass
    return None


def parse_date_from_page(html):
    """
    Extract the 'Last availability update' date from the page.

    Primary (anchored): read the <div class="date">M.D.YYYY</div> that sits
    inside the class="last_updated" block. This is the authoritative freshness
    stamp. Anchoring matters because the page also contains unrelated
    DD/MM/YYYY dates elsewhere (e.g. event listings) that a naive global regex
    would happily mis-parse as the update date.

    Fallback: first plausible M.D.YYYY / M/D/YYYY match anywhere in the HTML
    (preserves old behavior if BT ever restructures the last_updated block).

    Returns a datetime.date object, or None if not found.
    """
    # Anchored: the date div within the last_updated block.
    m = re.search(
        r'class="last_updated".*?class="date">\s*(\d{1,2})[./](\d{1,2})[./](20\d{2})',
        html, re.IGNORECASE | re.DOTALL
    )
    if m:
        d = _coerce_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d

    # Fallback: first plausible date anywhere on the page.
    for m in re.finditer(r'\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b', html):
        d = _coerce_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d
    return None


def parse_time_from_page(html):
    """
    Extract the update time from the page.
    Returns a formatted string like '7:38am EST', or 'unknown' if not found.
    The site started showing real times in March 2026 — always read from page.
    """
    # First, try to parse the structured last_updated element:
    #   <div class="last_updated">
    #     <div class="time">
    #       <div class="block">8</div>:<div class="block">05</div>
    #     </div>
    #     <div class="tz">am...</div>
    #   </div>
    m = re.search(
        r'class="last_updated".*?'
        r'class="block">(\d{1,2})</div>:<div class="block">(\d{2})</div>.*?'
        r'class="tz">\s*(am|pm)',
        html, re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)}:{m.group(2)}{m.group(3).lower()} EST"

    return "unknown"


def parse_availability(html):
    """
    Parse bottle availability from the page HTML.

    The BT availability page structure (confirmed March 2026):
    - Each product is in a <div class="product"> block
    - Available bottles have <div class="in_stock"> inside
    - Unavailable bottles have <div class="out_of_stock"> inside
    - The bottle name is in the <h4> tag within the same block

    Returns dict: {blantons, weller107, ehtaylor_sb, eagle_rare, special_release}
    All bottle values are 0 or 1.
    """
    result = {
        "blantons": 0,
        "weller107": 0,
        "ehtaylor_sb": 0,
        "eagle_rare": 0,
        "special_release": None,
    }

    # ── Primary strategy: parse <div class="product"> blocks ────────────────
    # Each block ends before the next "discover_link" anchor
    product_blocks = re.findall(
        r'<div class="product">(.*?)<a class="discover_link"',
        html, re.DOTALL
    )

    if not product_blocks:
        # Fallback: try broader product block detection
        product_blocks = re.findall(
            r'<div class=["\']product["\']>(.*?)</div>\s*</div>\s*</div>',
            html, re.DOTALL
        )

    parsed_any = False
    special_candidates = []

    for block in product_blocks:
        # Get bottle name from <h4>
        h4_match = re.search(r'<h4>([^<]+)</h4>', block)
        if not h4_match:
            continue
        name = h4_match.group(1).strip()
        name_lower = name.lower()

        is_available = 'class="in_stock"' in block
        is_unavailable = 'class="out_of_stock"' in block

        # Only process if we can determine availability
        if not is_available and not is_unavailable:
            continue

        available = is_available and not is_unavailable
        parsed_any = True

        # Try to match to one of our 4 tracked bottles
        matched = False
        for key, aliases in BOTTLE_KEYS.items():
            for alias in aliases:
                if alias in name_lower:
                    result[key] = 1 if available else 0
                    matched = True
                    break
            if matched:
                break

        # If not a tracked bottle and it IS available, check for special release
        if not matched and available:
            # Skip the always-available everyday products
            everyday = ['buffalo trace bourbon', 'traveller', 'sazerac rye',
                        'weller special reserve', 'cream', 'vodka', 'century']
            if not any(e in name_lower for e in everyday):
                special_candidates.append(name)

    if special_candidates:
        result["special_release"] = special_candidates[0]

    if not parsed_any:
        # No product blocks found — log warning but don't crash
        import sys
        print("[scrape_availability] WARNING: No product blocks parsed — page structure may have changed", file=sys.stderr)

    return result


# ─────────────────────────────────────────────
# Main polling loop
# ─────────────────────────────────────────────

def _emit_provisional_capture(html, page_date, today, poll_count, stale_reason):
    """
    Emit a SUCCESS result captured from a page whose freshness stamp never
    advanced to today. The pipeline records the day so we never miss one, but
    the result is flagged stale_stamp=True so run_daily can alert for review.
    """
    site_time = parse_time_from_page(html)
    availability = parse_availability(html)
    output = {
        "success": True,
        "date": today.isoformat(),
        "last_site_update": site_time,
        "stale_stamp": True,
        "page_date": str(page_date) if page_date else "unknown",
        "note": (f"Provisional capture — BT freshness stamp did not advance to "
                 f"today ({stale_reason}). Live inventory captured as-is."),
        "polls": poll_count,
    }
    output.update(availability)
    print(f"[scrape_availability] PROVISIONAL CAPTURE ({stale_reason}). "
          f"Availability: {availability}", file=sys.stderr)
    print(json.dumps(output))


def run(max_poll_minutes, dry_run=False):
    """
    Poll until the page shows today's date, then return parsed availability.
    Outputs a single JSON object to stdout.
    """
    today = datetime.date.today()
    max_polls = max(1, max_poll_minutes * 60 // POLL_INTERVAL_SECONDS)
    poll_count = 0

    print(f"[scrape_availability] Starting. Target date: {today}. Max polls: {max_polls} ({max_poll_minutes} min)", file=sys.stderr)

    if dry_run:
        print(f"[scrape_availability] DRY RUN — skipping actual fetch", file=sys.stderr)
        print(json.dumps({
            "success": True,
            "date": today.isoformat(),
            "last_site_update": "7:00am EST",
            "blantons": 1,
            "weller107": 1,
            "ehtaylor_sb": 1,
            "eagle_rare": 0,
            "special_release": None,
            "stale_stamp": False,
            "polls": 1,
            "dry_run": True
        }))
        return

    # Remember the most recent successfully fetched page so that, if the
    # freshness stamp never advances within the poll window, we can still
    # capture live inventory provisionally instead of missing the day.
    last_html = None
    last_page_date = None

    for attempt in range(1, max_polls + 1):
        poll_count = attempt
        print(f"[scrape_availability] Poll {attempt}/{max_polls} at {datetime.datetime.now().strftime('%H:%M:%S')} ...", file=sys.stderr)

        html, status = fetch_page()

        if html is None:
            print(f"[scrape_availability] Fetch failed: {status}", file=sys.stderr)
            if attempt < max_polls:
                print(f"[scrape_availability] Retrying in {POLL_INTERVAL_SECONDS//60} min...", file=sys.stderr)
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            else:
                # Final poll and still no page. If an earlier poll DID return a
                # page, fall back to that (stale stamp) so we don't miss the
                # day. Only hard-fail when we never fetched a page at all.
                if last_html is not None:
                    _emit_provisional_capture(
                        last_html, last_page_date, today, poll_count,
                        stale_reason=f"fetch error on final poll: {status}")
                    return
                print(json.dumps({
                    "success": False,
                    "reason": f"Fetch error after {poll_count} polls: {status}",
                    "polls": poll_count
                }))
                return

        last_html = html
        page_date = parse_date_from_page(html)
        last_page_date = page_date
        print(f"[scrape_availability] Page date: {page_date}", file=sys.stderr)

        if page_date == today:
            # Fresh — parse and return
            site_time = parse_time_from_page(html)
            availability = parse_availability(html)
            print(f"[scrape_availability] SUCCESS. Availability: {availability}", file=sys.stderr)

            output = {
                "success": True,
                "date": today.isoformat(),
                "last_site_update": site_time,
                "stale_stamp": False,
                "polls": poll_count,
            }
            output.update(availability)
            print(json.dumps(output))
            return

        elif page_date is not None and page_date > today:
            # Page is from the future — treat as success (clock skew or timezone issue)
            print(f"[scrape_availability] WARNING: Page date {page_date} is in the future. Using as-is.", file=sys.stderr)
            site_time = parse_time_from_page(html)
            availability = parse_availability(html)
            output = {
                "success": True,
                "date": today.isoformat(),
                "last_site_update": site_time,
                "stale_stamp": False,
                "polls": poll_count,
                "note": f"Page showed future date {page_date}"
            }
            output.update(availability)
            print(json.dumps(output))
            return

        else:
            # Stale — wait and retry
            if attempt < max_polls:
                print(f"[scrape_availability] Page is stale (shows {page_date}). Waiting {POLL_INTERVAL_SECONDS//60} min...", file=sys.stderr)
                time.sleep(POLL_INTERVAL_SECONDS)
            else:
                # Timed out and the stamp never advanced to today. Rather than
                # skip the day, capture the live inventory provisionally and
                # flag it (stale_stamp=True) so run_daily can alert for review.
                print(f"[scrape_availability] Timed out after {max_poll_minutes} min. "
                      f"Page still shows {page_date}. Capturing provisionally.", file=sys.stderr)
                _emit_provisional_capture(
                    html, page_date, today, poll_count,
                    stale_reason=f"page still showed {page_date} after {max_poll_minutes}min poll")
                return


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape Buffalo Trace gift shop availability"
    )
    parser.add_argument(
        "--max-poll-minutes",
        type=int,
        default=180,
        help="Maximum polling time in minutes (default 180; use 240 for Sunday)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip actual fetch and return fake data (for testing)"
    )
    args = parser.parse_args()
    run(args.max_poll_minutes, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

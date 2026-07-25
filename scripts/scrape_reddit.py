#!/usr/bin/env python3
"""
Buffalo Trace Reddit Scraper
=============================
Searches r/bourbon and r/whiskeybuds for recent posts mentioning Buffalo Trace
gift shop activity. Uses Reddit's official OAuth API when REDDIT_CLIENT_ID /
REDDIT_CLIENT_SECRET are set (required on datacenter IPs — unauthenticated
requests get 403-blocked there); falls back to public JSON endpoints otherwise.

Usage:
    python scrape_reddit.py [--days 7] [--max-posts 10] [--dry-run]

Output (stdout, JSON):
    {
        "success": true,
        "posts": [
            {
                "id": "abc123",
                "title": "Got a bottle at BT today!",
                "author": "u/whiskeyFan",
                "subreddit": "r/bourbon",
                "url": "https://www.reddit.com/r/bourbon/comments/abc123/...",
                "score": 42,
                "num_comments": 7,
                "created_utc": 1234567890,
                "age_hours": 3.2,
                "snippet": "First 150 chars of post body..."
            }
        ],
        "total_found": 3,
        "query_count": 4
    }

On failure:
    {
        "success": false,
        "reason": "...",
        "posts": []
    }
"""

import sys
import json
import time
import argparse
import urllib.request
import urllib.parse
import urllib.error


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

# Reddit blocks requests without a meaningful User-Agent
USER_AGENT = (
    "BuffaloTraceMonitor/1.0 "
    "(gift shop availability tracker; contact: brianwulff@yahoo.com)"
)

# r/buffalotrace verified dead (2026-07-24): nothing since a 2021 pinned post
SUBREDDITS = ["bourbon", "whiskeybuds", "whiskey"]

# All queries run across all subreddits; results deduplicated by post ID
SEARCH_QUERIES = [
    "buffalo trace gift shop",
    "BT gift shop",
    "buffalo trace special release",
    "buffalo trace distillery drop",
]

EXTRA_QUERIES = {}

BASE_URL = "https://www.reddit.com"
OAUTH_BASE_URL = "https://oauth.reddit.com"

# OAuth tier allows 100 QPM. RSS mode makes only ONE fetch per subreddit
# (3 total), so a short gap suffices. (Fixed 2026-07-25: the old 15s value
# was slept after all 12 sub×query iterations — 180s of pure sleep against
# run_daily's 120s subprocess budget, guaranteeing a timeout every run.)
RATE_LIMIT_SECONDS = 1.5
RSS_RATE_LIMIT_SECONDS = 5

# Fallback host tried once per subreddit if the primary RSS fetch fails
# (e.g. datacenter-IP 403 on www) — old.reddit is served by a different edge.
RSS_FALLBACK_BASE = "https://old.reddit.com"


# ─────────────────────────────────────────────
# Fetch helpers — official OAuth API (added 2026-07-24)
#
# Reddit 403-blocks unauthenticated *.json requests from datacenter IPs
# (GitHub Actions, cloud runners) — this is why the scraper kept failing.
# The sanctioned fix is Reddit's official API: register a free "script" app
# at https://www.reddit.com/prefs/apps, then set REDDIT_CLIENT_ID and
# REDDIT_CLIENT_SECRET (GitHub repo secrets → workflow env). This uses the
# application-only OAuth flow (client_credentials) for read-only public
# data — no Reddit user password involved. Without creds, falls back to
# the old unauthenticated endpoints (works from residential IPs only).
# ─────────────────────────────────────────────

import os
import base64

_oauth_token = None   # cached for the process lifetime (expires in 1h)
_oauth_failed = False  # cache failure too — never retry the token POST
                       # (before 2026-07-25 a failing attempt could repeat
                       # on every loop iteration, burning up to 20s each)


def _get_oauth_token():
    """App-only OAuth token via client_credentials, or None if no creds."""
    global _oauth_token, _oauth_failed
    if _oauth_token is not None:
        return _oauth_token
    if _oauth_failed:
        return None
    cid = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        return None
    try:
        payload = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            "https://www.reddit.com/api/v1/access_token",
            data=payload, method="POST")
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        req.add_header("Authorization", f"Basic {basic}")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=20) as resp:
            tok = json.loads(resp.read().decode()).get("access_token")
        if tok:
            _oauth_token = tok
            print("[scrape_reddit] OAuth token acquired (official API mode)",
                  file=sys.stderr)
        else:
            _oauth_failed = True
        return _oauth_token
    except Exception as e:
        _oauth_failed = True
        print(f"[scrape_reddit] OAuth token request failed: {e} — "
              f"falling back to unauthenticated", file=sys.stderr)
        return None


def reddit_get(path_and_query: str, timeout: int = 15,
               max_attempts: int = 3) -> dict:
    """
    Fetch a Reddit JSON endpoint. `path_and_query` is the part after the
    host, e.g. "/r/bourbon/search.json?q=...". Uses the OAuth API when
    credentials are configured, else the public endpoint. Retries with
    backoff on 429/5xx.
    """
    token = _get_oauth_token()
    if token:
        # oauth.reddit.com uses the same paths minus the .json suffix
        url = OAUTH_BASE_URL + path_and_query.replace(".json?", "?", 1)
    else:
        url = BASE_URL + path_and_query
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", USER_AGENT)
            req.add_header("Accept", "application/json")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                wait = 5 * attempt
                print(f"[scrape_reddit] HTTP {e.code}, retrying in {wait}s "
                      f"(attempt {attempt}/{max_attempts})", file=sys.stderr)
                time.sleep(wait)
                last_exc = e
                continue
            raise
        except Exception as e:
            last_exc = e
            if attempt < max_attempts:
                time.sleep(5 * attempt)
                continue
            raise
    raise last_exc


def search_subreddit(subreddit: str, query: str, days: int) -> list:
    """
    Search a subreddit for posts matching query.
    Reddit's 't=week' filter covers 7 days; we do our own fine-grained age
    filtering in is_relevant() so changing --days < 7 works correctly.
    Returns list of raw post data dicts (kind=t3 posts only).
    """
    params = urllib.parse.urlencode({
        "q":           query,
        "sort":        "new",
        "t":           "week",    # coarse Reddit filter; fine filter below
        "limit":       25,
        "restrict_sr": 1,         # limit to this subreddit
        "type":        "link",
    })
    path = f"/r/{subreddit}/search.json?{params}"

    try:
        if _get_oauth_token() is None:
            # No API credentials — use Reddit's public RSS syndication feeds,
            # which (unlike the .json endpoints) are served to datacenter IPs.
            return search_subreddit_rss(subreddit, query)
        data = reddit_get(path)
        children = data.get("data", {}).get("children", [])
        return [c["data"] for c in children if c.get("kind") == "t3"]
    except urllib.error.HTTPError as e:
        print(
            f"[scrape_reddit] HTTP {e.code} searching r/{subreddit} '{query}'",
            file=sys.stderr
        )
        return []
    except Exception as e:
        print(
            f"[scrape_reddit] Error searching r/{subreddit} '{query}': {e}",
            file=sys.stderr
        )
        return []


# ─────────────────────────────────────────────
# RSS fallback (added 2026-07-24)
#
# Reddit's Atom feeds (…/search.rss) are public syndication endpoints and
# are NOT subject to the datacenter-IP 403 that blocks the .json API, so
# they work from GitHub Actions without any credentials. They lack score /
# comment counts (returned as 0) but carry id, title, author, link, date,
# and body HTML — everything the pipeline actually uses. Keep request
# volume gentle: ~1 request per 15s (429s appear under ~10s spacing).
# ─────────────────────────────────────────────

import re as _re
import xml.etree.ElementTree as _ET
import datetime as _dt

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


# Local relevance matching for RSS mode: a post must mention Buffalo Trace
# AND something gift-shop-ish. (search.rss ignores its q parameter — verified
# 2026-07-24 — so filtering must happen client-side.)
_RSS_KW_BRAND = ("buffalo trace", "btd", "frankfort")
_RSS_KW_TOPIC = ("gift shop", "giftshop", "visitor center", "distillery",
                 "drop", "pick", "haul", "score")

_rss_cache = {}   # subreddit → parsed posts (fetch each sub once per run)


def search_subreddit_rss(subreddit: str, query: str) -> list:
    """
    RSS mode: fetch r/<sub>/new.rss ONCE per run (cached) and filter locally.
    The `query` argument is ignored — Reddit's search.rss disregards q, so we
    pull the sub's recent posts and keyword-match title+body ourselves.
    """
    if subreddit in _rss_cache:
        return []          # already contributed its posts this run
    posts = _fetch_new_rss(subreddit)
    _rss_cache[subreddit] = True
    matched = []
    for p in posts:
        text = f"{p.get('title','')} {p.get('selftext','')}".lower()
        if any(b in text for b in _RSS_KW_BRAND) and \
           any(t in text for t in _RSS_KW_TOPIC):
            matched.append(p)
    print(f"[scrape_reddit] RSS r/{subreddit}: {len(posts)} recent, "
          f"{len(matched)} relevant", file=sys.stderr)
    return matched


def _fetch_new_rss(subreddit: str) -> list:
    """
    Fetch and parse r/<sub>/new.rss into post dicts.
    Tries the primary host, then RSS_FALLBACK_BASE once (different edge —
    a datacenter-IP 403 on www does not always apply to old.reddit).
    Every failure path returns [] quickly; total worst case ≤ ~25s.
    """
    root = None
    for base in (BASE_URL, RSS_FALLBACK_BASE):
        url = f"{base}/r/{subreddit}/new.rss?limit=100"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", USER_AGENT)
        try:
            with urllib.request.urlopen(req, timeout=15 if base == BASE_URL else 10) as resp:
                root = _ET.fromstring(resp.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as e:
            print(f"[scrape_reddit] RSS HTTP {e.code} fetching {url}",
                  file=sys.stderr)
        except Exception as e:
            print(f"[scrape_reddit] RSS error fetching {url}: {e}",
                  file=sys.stderr)
    if root is None:
        return []

    posts = []
    for entry in root.findall("a:entry", _ATOM_NS):
        def _t(tag, default=""):
            el = entry.find(f"a:{tag}", _ATOM_NS)
            return (el.text or default) if el is not None else default
        link_el = entry.find("a:link", _ATOM_NS)
        link = link_el.get("href", "") if link_el is not None else ""
        author_el = entry.find("a:author/a:name", _ATOM_NS)
        author = (author_el.text or "").lstrip("/u/") if author_el is not None else ""
        created = 0
        try:
            pub = _t("published") or _t("updated")
            created = int(_dt.datetime.fromisoformat(
                pub.replace("Z", "+00:00")).timestamp())
        except Exception:
            pass
        body_html = _t("content")
        body_text = _re.sub(r"<[^>]+>", " ", body_html)
        body_text = _re.sub(r"\s+", " ", body_text).strip()
        posts.append({
            "id":            _t("id").split("_")[-1],
            "title":         _t("title"),
            "author":        author,
            "subreddit":     subreddit,
            "permalink":     link.replace(BASE_URL, ""),
            "url":           link,
            "score":         0,             # not exposed via RSS
            "num_comments":  0,             # not exposed via RSS
            "created_utc":   created,
            "selftext":      body_text[:500],
        })
    return posts


# ─────────────────────────────────────────────
# Filtering and formatting
# ─────────────────────────────────────────────

def is_relevant(post: dict, days: int) -> bool:
    """Return True if post is within the lookback window and not removed."""
    age_seconds = time.time() - post.get("created_utc", 0)
    if age_seconds > days * 86400:
        return False
    if post.get("removed_by_category"):
        return False
    return True


def format_post(post: dict) -> dict:
    """Extract and format the fields we care about from a raw Reddit post dict."""
    age_seconds = time.time() - post.get("created_utc", 0)
    age_hours   = age_seconds / 3600

    body = (post.get("selftext") or "").strip().replace("\n", " ")
    if len(body) > 150:
        body = body[:147] + "..."

    permalink = post.get("permalink", "")
    url = f"{BASE_URL}{permalink}" if permalink else post.get("url", "")

    return {
        "id":           post.get("id", ""),
        "title":        post.get("title", "").strip(),
        "author":       f"u/{post.get('author', '[deleted]')}",
        "subreddit":    f"r/{post.get('subreddit', '')}",
        "url":          url,
        "score":        post.get("score", 0),
        "num_comments": post.get("num_comments", 0),
        "created_utc":  int(post.get("created_utc", 0)),
        "age_hours":    round(age_hours, 1),
        "snippet":      body,
    }


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def run(days: int = 7, max_posts: int = 10, dry_run: bool = False) -> None:
    """Search Reddit and print JSON results to stdout."""

    if dry_run:
        print(json.dumps({
            "success":     True,
            "posts": [
                {
                    "id":           "dryrun1",
                    "title":        "[DRY RUN] Found Blanton's and W107 at BT gift shop today",
                    "author":       "u/testuser",
                    "subreddit":    "r/bourbon",
                    "url":          "https://www.reddit.com/r/bourbon/comments/dryrun1/",
                    "score":        15,
                    "num_comments": 4,
                    "created_utc":  int(time.time()) - 7200,
                    "age_hours":    2.0,
                    "snippet":      "Visited the distillery today and scored Blanton's and Weller 107.",
                }
            ],
            "total_found": 1,
            "query_count": 0,
            "dry_run":     True,
        }))
        return

    seen_ids  = set()
    all_posts = []
    query_count = 0

    for subreddit in SUBREDDITS:
        queries = SEARCH_QUERIES + EXTRA_QUERIES.get(subreddit, [])
        if not _get_oauth_token():
            # RSS mode ignores the query string entirely (search.rss drops q;
            # we keyword-filter locally) — one pass per subreddit is a full
            # scan. Running all queries just multiplied the rate-limit sleeps
            # 4×, which is what blew the 120s budget (fixed 2026-07-25).
            queries = queries[:1]
        for query in queries:
            query_count += 1
            print(
                f"[scrape_reddit] Searching r/{subreddit}: '{query}'",
                file=sys.stderr
            )

            raw = search_subreddit(subreddit, query, days)

            for post in raw:
                post_id = post.get("id", "")
                if not post_id or post_id in seen_ids:
                    continue
                if not is_relevant(post, days):
                    continue
                seen_ids.add(post_id)
                all_posts.append(format_post(post))

            time.sleep(RATE_LIMIT_SECONDS if _get_oauth_token() else RSS_RATE_LIMIT_SECONDS)

    # Newest first, then cap
    all_posts.sort(key=lambda p: p["created_utc"], reverse=True)
    all_posts = all_posts[:max_posts]

    print(
        f"[scrape_reddit] Done. {len(all_posts)} unique posts "
        f"across {query_count} queries.",
        file=sys.stderr
    )

    print(json.dumps({
        "success":     True,
        "posts":       all_posts,
        "total_found": len(all_posts),
        "query_count": query_count,
    }))


def main():
    parser = argparse.ArgumentParser(
        description="Scrape Reddit for Buffalo Trace gift shop posts"
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Lookback window in days (default 7)"
    )
    parser.add_argument(
        "--max-posts", type=int, default=10,
        help="Max posts to return (default 10)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Return fake data without making network calls"
    )
    args = parser.parse_args()
    run(days=args.days, max_posts=args.max_posts, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

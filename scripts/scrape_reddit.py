#!/usr/bin/env python3
"""
Buffalo Trace Reddit Scraper
=============================
Searches r/bourbon and r/whiskeybuds for recent posts mentioning Buffalo Trace
gift shop activity. Uses Reddit's public JSON API — no authentication required.

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

SUBREDDITS = ["bourbon", "whiskeybuds", "whiskey"]

# All queries run across all subreddits; results deduplicated by post ID
SEARCH_QUERIES = [
    "buffalo trace gift shop",
    "BT gift shop",
    "buffalo trace special release",
    "buffalo trace distillery drop",
]

BASE_URL = "https://www.reddit.com"

# Stay well under Reddit's 1 req/sec rate limit
RATE_LIMIT_SECONDS = 1.5


# ─────────────────────────────────────────────
# Fetch helpers
# ─────────────────────────────────────────────

def reddit_get(url: str, timeout: int = 15) -> dict:
    """Fetch a Reddit JSON endpoint. Returns parsed dict. Raises on HTTP errors."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    url = f"{BASE_URL}/r/{subreddit}/search.json?{params}"

    try:
        data = reddit_get(url)
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
        for query in SEARCH_QUERIES:
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

            time.sleep(RATE_LIMIT_SECONDS)

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

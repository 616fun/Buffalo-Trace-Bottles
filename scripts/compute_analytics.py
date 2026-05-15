#!/usr/bin/env python3
"""
compute_analytics.py — Buffalo Trace Tracker analytics engine.

Pure function: reads daily_log + bayesian_priors, returns per-bottle analytics.
No file I/O. No side effects. Called by update_tracker_data.py and build_data_json.py.

Usage as a module:
    from compute_analytics import compute_analytics
    analytics = compute_analytics(tracker["daily_log"], tracker["config"]["bayesian_priors"])

Usage as a script (for validation/debugging):
    python compute_analytics.py --tracker-data /path/to/tracker_data.json
"""

import argparse
import datetime
import json
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BOTTLE_KEYS = ["blantons", "weller107", "ehtaylor_sb", "eagle_rare"]

BOTTLE_DISPLAY_NAMES = {
    "blantons":    "Blanton's Single Barrel",
    "weller107":   "Weller Antique 107",
    "ehtaylor_sb": "E.H. Taylor Small Batch",
    "eagle_rare":  "Eagle Rare 10-Year",
}


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_bottle_stats(
    daily_log: list[dict],
    bottle_key: str,
    prior_aa: float,
    prior_na: float,
    prior_n: int = 15,
) -> dict:
    """
    Compute all statistics for one bottle from the daily_log array.

    Closure days (is_closure=true) are excluded from all calculations — they
    are recorded for the calendar but are not valid observations for the model.

    Arguments:
        daily_log   — list of daily log dicts (each has date, blantons, weller107, etc.)
        bottle_key  — one of: blantons, weller107, ehtaylor_sb, eagle_rare
        prior_aa    — Bayesian prior P(available | was available)
        prior_na    — Bayesian prior P(available | was NOT available)
        prior_n     — effective sample size for the prior (default 15)

    Returns a dict with:
        days_available, total_days,
        overall_pct, rolling_10d_pct, rolling_30d_pct,
        streak, streak_direction,
        avg_gap,          ← mean calendar days between availability events (None if < 2)
        tomorrow_pct,     ← Bayesian Markov prediction %
        confidence,       ← "Prior only" / "Medium" / "High"
        last_seen         ← "YYYY-MM-DD" or "Never"
    """
    # Extract parallel arrays: values (0/1) and dates (datetime.date)
    # Closure days are excluded — they are gaps, not zero-availability observations.
    values: list[int] = []
    dates:  list[datetime.date] = []

    for row in daily_log:
        if row.get("is_closure", False):
            continue
        raw_date = row["date"]
        if isinstance(raw_date, str):
            d = datetime.date.fromisoformat(raw_date)
        else:
            d = raw_date
        values.append(int(row.get(bottle_key, 0) or 0))
        dates.append(d)

    total_days = len(values)

    if total_days == 0:
        return {
            "days_available":   0,
            "total_days":       0,
            "overall_pct":      0.0,
            "rolling_10d_pct":  0.0,
            "rolling_30d_pct":  0.0,
            "streak":           0,
            "streak_direction": "unavailable",
            "avg_gap":          None,
            "tomorrow_pct":     prior_na * 100,
            "confidence":       "Prior only",
            "last_seen":        "Never",
        }

    days_available = sum(v == 1 for v in values)

    # --- Overall availability % ---
    overall_pct = days_available / total_days * 100

    # --- Rolling 10-day and 30-day availability % ---
    n10 = min(10, total_days)
    n30 = min(30, total_days)
    rolling_10d_pct = sum(values[-n10:]) / n10 * 100
    rolling_30d_pct = sum(values[-n30:]) / n30 * 100

    # --- Streak: count consecutive same-value days from the end ---
    streak_val = values[-1]
    streak = 0
    for v in reversed(values):
        if v == streak_val:
            streak += 1
        else:
            break
    streak_direction = "available" if streak_val == 1 else "unavailable"

    # --- Average gap: mean calendar days between consecutive availability events ---
    avail_dates = [dates[i] for i, v in enumerate(values) if v == 1]
    if len(avail_dates) >= 2:
        gaps = [(avail_dates[i + 1] - avail_dates[i]).days
                for i in range(len(avail_dates) - 1)]
        avg_gap: Optional[float] = sum(gaps) / len(gaps)
    else:
        avg_gap = None

    # --- Bayesian Markov transition counts ---
    n_aa = n_an = n_na = n_nn = 0
    for i in range(total_days - 1):
        prev, curr = values[i], values[i + 1]
        if   prev == 1 and curr == 1: n_aa += 1
        elif prev == 1 and curr == 0: n_an += 1
        elif prev == 0 and curr == 1: n_na += 1
        else:                          n_nn += 1

    # --- Posterior probabilities (Beta-Binomial conjugate update) ---
    p_aa = (prior_aa * prior_n + n_aa) / (prior_n + n_aa + n_an)
    p_na = (prior_na * prior_n + n_na) / (prior_n + n_na + n_nn)

    # --- Tomorrow's prediction % based on current (last) state ---
    last_val = values[-1]
    tomorrow_pct = (p_aa if last_val == 1 else p_na) * 100

    # --- Confidence level based on total observed transitions ---
    total_transitions = n_aa + n_an + n_na + n_nn   # == total_days - 1
    if total_transitions < 15:
        confidence = "Prior only"
    elif total_transitions < 30:
        confidence = "Medium"
    else:
        confidence = "High"

    # --- Last seen ---
    last_seen_date: Optional[datetime.date] = None
    for d, v in zip(dates, values):
        if v == 1:
            last_seen_date = d
    last_seen = last_seen_date.strftime("%Y-%m-%d") if last_seen_date else "Never"

    return {
        "days_available":   days_available,
        "total_days":       total_days,
        "overall_pct":      overall_pct,
        "rolling_10d_pct":  rolling_10d_pct,
        "rolling_30d_pct":  rolling_30d_pct,
        "streak":           streak,
        "streak_direction": streak_direction,
        "avg_gap":          avg_gap,
        "tomorrow_pct":     tomorrow_pct,
        "confidence":       confidence,
        "last_seen":        last_seen,
    }


def compute_analytics(daily_log: list[dict], bayesian_priors: dict) -> dict:
    """
    Compute analytics for all four tracked bottles.

    Arguments:
        daily_log        — tracker_data["daily_log"]
        bayesian_priors  — tracker_data["config"]["bayesian_priors"]

    Returns a dict keyed by bottle key, each value is the dict from compute_bottle_stats.

    Example:
        {
            "blantons":    {"days_available": 18, "total_days": 35, "overall_pct": 51.4, ...},
            "weller107":   {...},
            "ehtaylor_sb": {...},
            "eagle_rare":  {...},
        }
    """
    result: dict[str, dict] = {}
    for key in BOTTLE_KEYS:
        priors = bayesian_priors.get(key, {})
        prior_aa = float(priors.get("p_avail_given_avail", 0.5))
        prior_na = float(priors.get("p_avail_given_not",   0.3))
        prior_n  = int(priors.get("effective_n", 15))
        result[key] = compute_bottle_stats(daily_log, key, prior_aa, prior_na, prior_n)
    return result


# ---------------------------------------------------------------------------
# CLI entrypoint (validation / debugging)
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute analytics from tracker_data.json and print to stdout."
    )
    parser.add_argument(
        "--tracker-data",
        default=None,
        help="Path to tracker_data.json. Defaults to ./tracker_data.json"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit full analytics as JSON instead of human-readable table"
    )
    args = parser.parse_args()

    import os
    if args.tracker_data:
        path = args.tracker_data
    else:
        # Try common locations
        candidates = [
            "tracker_data.json",
            os.path.join(os.path.dirname(__file__), "..", "tracker_data.json"),
        ]
        path = next((p for p in candidates if os.path.exists(p)), None)
        if path is None:
            print("ERROR: tracker_data.json not found. Pass --tracker-data <path>",
                  file=sys.stderr)
            sys.exit(1)

    with open(path) as f:
        tracker = json.load(f)

    daily_log       = tracker["daily_log"]
    bayesian_priors = tracker["config"]["bayesian_priors"]
    analytics       = compute_analytics(daily_log, bayesian_priors)

    if args.json:
        print(json.dumps(analytics, indent=2))
        return

    # Human-readable table
    tracked = [r for r in daily_log if not r.get("is_closure", False)]
    closures = len(daily_log) - len(tracked)
    closure_note = f" ({closures} closure day{'s' if closures != 1 else ''} excluded)" if closures else ""
    print(f"\nAnalytics computed from {len(tracked)} tracked rows{closure_note}\n")
    header = (
        f"{'Bottle':<28} {'Avail':>6} {'Days':>5} "
        f"{'Overall':>8} {'10d':>5} {'30d':>5} "
        f"{'Streak':>7} {'AvgGap':>7} "
        f"{'TomPct':>7} {'Conf':>10} {'LastSeen':>12}"
    )
    print(header)
    print("-" * len(header))

    for key in BOTTLE_KEYS:
        s = analytics[key]
        name = BOTTLE_DISPLAY_NAMES[key]
        avg_gap_str = f"{s['avg_gap']:.2f}" if s["avg_gap"] is not None else "   —"
        print(
            f"{name:<28} {s['days_available']:>6} {s['total_days']:>5} "
            f"{s['overall_pct']:>7.1f}% {s['rolling_10d_pct']:>4.0f}% {s['rolling_30d_pct']:>4.0f}% "
            f"{s['streak']:>5}{'↑' if s['streak_direction']=='available' else '↓':>2} "
            f"{avg_gap_str:>7} "
            f"{s['tomorrow_pct']:>6.1f}% {s['confidence']:>10} {s['last_seen']:>12}"
        )

    print()


if __name__ == "__main__":
    main()

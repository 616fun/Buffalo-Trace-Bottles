#!/usr/bin/env python3
"""
build_data_json.py — Build data.json from tracker_data.json.

Reads tracker_data.json, computes analytics, and writes data.json in the
exact schema the website (index.html) expects. Called by run_daily.py after
update_tracker_data.py has written the new daily log row.

Usage:
    python build_data_json.py \\
        --tracker-data /path/to/tracker_data.json \\
        --output /path/to/data.json
        [--date YYYY-MM-DD]   # override "today" (default: most recent log row)

Exit codes: 0 = success, 1 = failure
Stdout:     JSON  {"success": true}  or  {"success": false, "error": "..."}
Stderr:     progress log (human-readable)
"""

import argparse
import datetime
import json
import sys
import os
from pathlib import Path

# Import compute_analytics from the same directory
sys.path.insert(0, os.path.dirname(__file__))
from compute_analytics import compute_analytics, BOTTLE_KEYS, BOTTLE_DISPLAY_NAMES


# ---------------------------------------------------------------------------
# Bottle metadata (display order and prior_frequency for data.json output)
# ---------------------------------------------------------------------------

BOTTLE_META = [
    {"key": "blantons",    "name": "Blanton's Single Barrel",  "prior_frequency": 0.63},
    {"key": "weller107",   "name": "Weller Antique 107",        "prior_frequency": 0.75},
    {"key": "ehtaylor_sb", "name": "E.H. Taylor Small Batch",   "prior_frequency": 0.93},
    {"key": "eagle_rare",  "name": "Eagle Rare 10-Year",        "prior_frequency": 0.17},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit_success() -> None:
    print(json.dumps({"success": True}), flush=True)


def emit_failure(error: str) -> None:
    print(json.dumps({"success": False, "error": error}), flush=True)


def compute_prediction_accuracy(predictions: list[dict]) -> dict:
    """
    Compute the prediction_accuracy block for data.json meta.

    Iterates the predictions array. Counts:
    - total_days: rows where actual_bottles is not None (any list, including empty)
    - correct / partial / incorrect: from 'correct' field ("Yes"/"Partial"/"No")
    - binary: per-bottle correct/total, excluding closure days (correct == "N/A")
    """
    total_days = 0
    correct    = 0
    partial    = 0
    incorrect  = 0

    # Binary accuracy: per bottle, track (correct_count, total_count)
    binary_per_bottle: dict[str, list[int]] = {k: [0, 0] for k in BOTTLE_KEYS}
    binary_correct_total = 0
    binary_grand_total   = 0

    for row in predictions:
        actual = row.get("actual_bottles")
        if actual is None:
            # No actual recorded yet — skip
            continue

        total_days += 1
        correct_val = row.get("correct")

        if correct_val == "N/A":
            # Closure day — count in total_days but exclude from binary
            continue

        if correct_val == "Yes":
            correct += 1
        elif correct_val == "Partial":
            partial += 1
        elif correct_val == "No":
            incorrect += 1

        # Binary accuracy: for each bottle, was the prediction correct?
        predicted_set = set(row.get("predicted_bottles") or [])
        actual_set    = set(actual or [])

        for key in BOTTLE_KEYS:
            pred_val   = 1 if key in predicted_set else 0
            actual_val = 1 if key in actual_set    else 0
            binary_per_bottle[key][1] += 1          # total
            binary_grand_total        += 1
            if pred_val == actual_val:
                binary_per_bottle[key][0] += 1      # correct
                binary_correct_total      += 1

    # Compute percentages
    if binary_grand_total > 0:
        binary_accuracy_pct = round(binary_correct_total / binary_grand_total * 100, 1)
    else:
        binary_accuracy_pct = 0.0

    per_bottle_pct: dict[str, float] = {}
    for key in BOTTLE_KEYS:
        cnt_correct, cnt_total = binary_per_bottle[key]
        if cnt_total > 0:
            per_bottle_pct[key] = round(cnt_correct / cnt_total * 100, 1)
        else:
            per_bottle_pct[key] = 0.0

    return {
        "total_days":          total_days,
        "correct":             correct,
        "partial":             partial,
        "incorrect":           incorrect,
        "binary_total":        binary_grand_total,
        "binary_correct":      binary_correct_total,
        "binary_accuracy_pct": binary_accuracy_pct,
        "per_bottle": {
            "blantons":    per_bottle_pct.get("blantons",    0.0),
            "weller107":   per_bottle_pct.get("weller107",   0.0),
            "ehtaylor_sb": per_bottle_pct.get("ehtaylor_sb", 0.0),
            "eagle_rare":  per_bottle_pct.get("eagle_rare",  0.0),
        }
    }


def build_data_json(tracker: dict, today_str: str | None = None) -> dict:
    """
    Build the full data.json dict from a loaded tracker_data dict.

    Arguments:
        tracker     — the parsed tracker_data.json object
        today_str   — override "today" date (YYYY-MM-DD). Default: last log row.

    Returns the data.json dict (ready for json.dump).
    """
    daily_log       = tracker.get("daily_log",   [])
    predictions     = tracker.get("predictions", [])
    bayesian_priors = tracker["config"]["bayesian_priors"]

    if not daily_log:
        raise ValueError("daily_log is empty — nothing to build from")

    # Determine "today" row
    if today_str:
        today_row = next((r for r in daily_log if r["date"] == today_str), None)
        if today_row is None:
            raise ValueError(f"No daily_log row found for --date {today_str}")
    else:
        today_row = daily_log[-1]
        today_str = today_row["date"]

    # All logged rows (includes closure days like Easter)
    closure_rows = [r for r in daily_log if r.get("is_closure", False)]

    log(f"  Building data.json for date: {today_str}")
    log(f"  Daily log rows: {len(daily_log)} ({len(daily_log)-len(closure_rows)} open, {len(closure_rows)} closures)")
    log(f"  Predictions rows: {len(predictions)}")

    # --- Compute analytics ---
    analytics = compute_analytics(daily_log, bayesian_priors)

    # --- Meta ---
    prediction_accuracy = compute_prediction_accuracy(predictions)
    meta = {
        "last_updated":       today_str,
        "days_tracked":       len(daily_log),      # all logged days including closures
        "site_version":       "1.0",
        "data_source":        "https://www.buffalotracebottledrops.com",
        "prediction_accuracy": prediction_accuracy,
    }

    # --- Today block ---
    # Parse notes: detect closure
    raw_notes = today_row.get("notes", "") or ""
    is_closure = bool(today_row.get("is_closure", False))

    today_block = {
        "date":             today_str,
        "day_of_week":      today_row.get("day_of_week", ""),
        "last_site_update": today_row.get("last_site_update") or "",
        "special_release":  today_row.get("special_release") or None,
        "notes":            raw_notes,
        "is_closure":       is_closure,
    }

    # --- Bottles array ---
    bottles = []
    for bm in BOTTLE_META:
        key  = bm["key"]
        s    = analytics[key]

        # Rounding rules (match existing data.json):
        #   overall_pct, rolling_*: round to nearest int, stored as float (e.g. 51.0)
        #   prediction_tomorrow_pct: round to 1 decimal
        #   avg_gap: round to 2 decimals if not None
        bottles.append({
            "name":                      bm["name"],
            "key":                       key,
            "available_today":           int(today_row.get(key, 0) or 0),
            "streak":                    s["streak"],
            "streak_direction":          s["streak_direction"],
            "overall_pct":               float(round(s["overall_pct"])),
            "rolling_10d_pct":           float(round(s["rolling_10d_pct"])),
            "rolling_30d_pct":           float(round(s["rolling_30d_pct"])),
            "avg_days_between_releases": round(s["avg_gap"], 2) if s["avg_gap"] is not None else None,
            "prediction_tomorrow_pct":   round(s["tomorrow_pct"], 1),
            "confidence":                s["confidence"],
            "last_seen":                 s["last_seen"],
            "prior_frequency":           bm["prior_frequency"],
        })

    # --- Calendar array ---
    calendar = []
    for row in daily_log:
        # Parse closure/notes from the row
        row_notes = row.get("notes", "") or ""
        row_is_closure = bool(row.get("is_closure", False))

        # Normalize closure notes: strip the "Gift shop closed — " prefix.
        # Two cases:
        #   (a) is_closure=true, notes="Gift shop closed — Easter Sunday" → notes="Easter Sunday"
        #   (b) is_closure=false, notes="Gift shop closed — Easter Sunday" → backward compat
        PREFIX = "Gift shop closed \u2014 "
        if row_notes.startswith(PREFIX):
            row_is_closure = True
            row_notes = row_notes[len(PREFIX):]

        calendar.append({
            "date":       row["date"],
            "blantons":   int(row.get("blantons",    0) or 0),
            "weller107":  int(row.get("weller107",   0) or 0),
            "ehtaylor_sb": int(row.get("ehtaylor_sb", 0) or 0),
            "eagle_rare": int(row.get("eagle_rare",  0) or 0),
            "is_closure": row_is_closure,
            "notes":      row_notes if row_is_closure else None,
        })

    return {
        "meta":     meta,
        "today":    today_block,
        "bottles":  bottles,
        "calendar": calendar,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build data.json from tracker_data.json."
    )
    parser.add_argument(
        "--tracker-data",
        default=None,
        help="Path to tracker_data.json. Default: auto-locate."
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output path for data.json. Default: data.json alongside tracker_data.json."
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Override 'today' date (YYYY-MM-DD). Default: last row in daily_log."
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="After writing, print a diff against the existing data.json (requires 'diff' tool)."
    )
    args = parser.parse_args()

    # --- Resolve tracker_data.json path ---
    if args.tracker_data:
        tracker_path = Path(args.tracker_data)
    else:
        candidates = [
            Path("tracker_data.json"),
            Path(__file__).parent.parent / "tracker_data.json",
        ]
        tracker_path = next((p for p in candidates if p.exists()), None)
        if tracker_path is None:
            emit_failure("tracker_data.json not found. Pass --tracker-data <path>")
            sys.exit(1)

    if not tracker_path.exists():
        emit_failure(f"tracker_data.json not found: {tracker_path}")
        sys.exit(1)

    # --- Resolve output path ---
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = tracker_path.parent / "data.json"

    log(f"=== Buffalo Trace build_data_json ===")
    log(f"tracker_data.json : {tracker_path}")
    log(f"output data.json  : {output_path}")

    # --- Load tracker data ---
    try:
        with open(tracker_path) as f:
            tracker = json.load(f)
    except Exception as exc:
        emit_failure(f"Failed to load tracker_data.json: {exc}")
        sys.exit(1)

    # --- Build ---
    try:
        data = build_data_json(tracker, today_str=args.date)
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit_failure(f"Failed to build data.json: {exc}")
        sys.exit(1)

    # --- Write ---
    try:
        # Save backup of existing data.json if it exists
        if output_path.exists() and args.diff:
            backup = output_path.with_suffix(".json.bak")
            import shutil
            shutil.copy2(output_path, backup)
            log(f"  Backed up existing data.json to {backup}")

        with open(output_path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        log(f"  Written: {output_path}")
    except Exception as exc:
        emit_failure(f"Failed to write data.json: {exc}")
        sys.exit(1)

    # --- Optional diff ---
    if args.diff:
        backup = output_path.with_suffix(".json.bak")
        if backup.exists():
            import subprocess
            log("\n--- Diff against previous data.json ---")
            result = subprocess.run(
                ["diff", str(backup), str(output_path)],
                capture_output=True, text=True
            )
            if result.stdout:
                log(result.stdout)
            else:
                log("  No differences (files are identical)")
            backup.unlink(missing_ok=True)
        else:
            log("  No backup to diff against (first run?)")

    log("\n=== Done ===")
    emit_success()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit_failure(str(exc))
        sys.exit(1)

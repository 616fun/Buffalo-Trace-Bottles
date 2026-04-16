#!/usr/bin/env python3
"""
update_tracker_data.py — Buffalo Trace Tracker JSON updater.

Replaces update_spreadsheet.py. Reads tracker_data.json, performs all daily
update operations, and writes it back. No Excel, no openpyxl.

Operations (in order):
  2a. Back-fill today's Predictions actual (find today's row, set actual/correct/result)
  2b. Append today's Daily Log entry (idempotent — skip if today already exists)
  2c. Recompute analytics (via compute_analytics.py)
  2d. Write tomorrow's Predictions row (create or overwrite)

Usage (normal day):
    python update_tracker_data.py \\
        --tracker-data /path/to/tracker_data.json \\
        --data '{"success":true,"blantons":1,"weller107":0,"ehtaylor_sb":1,
                 "eagle_rare":0,"special_release":null,
                 "last_site_update":"7:38am EST","date":"2026-04-17"}'
        [--dry-run]

Usage (closure day):
    python update_tracker_data.py \\
        --tracker-data /path/to/tracker_data.json \\
        --data '{"date":"2026-11-27"}' \\
        --closure "Thanksgiving Day"

Exit codes: 0 = success, 1 = failure
Stdout:     JSON  {"success": true}  or  {"success": false, "error": "..."}
Stderr:     progress log (human-readable)
"""

import argparse
import datetime
import json
import shutil
import sys
import os
from pathlib import Path

# Import compute_analytics from the same directory
sys.path.insert(0, os.path.dirname(__file__))
from compute_analytics import compute_analytics, BOTTLE_KEYS, BOTTLE_DISPLAY_NAMES


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_BOTTLE_NAMES = set(BOTTLE_DISPLAY_NAMES.values())
BOTTLE_DISPLAY_NAMES_REVERSE = {v: k for k, v in BOTTLE_DISPLAY_NAMES.items()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit_success() -> None:
    print(json.dumps({"success": True}), flush=True)


def emit_failure(error: str) -> None:
    print(json.dumps({"success": False, "error": error}), flush=True)


def find_prediction_for_date(predictions: list[dict], target_date: str) -> tuple[int, dict] | tuple[None, None]:
    """Return (index, row) for the predictions row matching target_date, or (None, None)."""
    for i, row in enumerate(predictions):
        if row.get("date") == target_date:
            return i, row
    return None, None


def find_log_row_for_date(daily_log: list[dict], target_date: str) -> tuple[int, dict] | tuple[None, None]:
    """Return (index, row) for the daily_log row matching target_date, or (None, None)."""
    for i, row in enumerate(daily_log):
        if row.get("date") == target_date:
            return i, row
    return None, None


def evaluate_prediction(predicted_bottles: list[str], actual_bottles: list[str]) -> tuple[str, str]:
    """
    Compare predicted bottle keys to actual bottle keys.
    Returns (correct_value, result_value).
    """
    predicted_set = set(predicted_bottles or [])
    actual_set    = set(actual_bottles or [])

    if predicted_set == actual_set:
        return "Yes", "Correct"
    elif predicted_set & actual_set:
        return "Partial", "Partial"
    else:
        return "No", "Incorrect"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update tracker_data.json with today's availability data."
    )
    parser.add_argument(
        "--tracker-data",
        default=None,
        help="Path to tracker_data.json. Default: auto-locate."
    )
    parser.add_argument(
        "--data",
        required=True,
        help="JSON scrape result from scrape_availability.py"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written; do not save."
    )
    parser.add_argument(
        "--backup-path",
        default=None,
        help="If provided, save a backup copy to this path after saving."
    )
    parser.add_argument(
        "--closure",
        metavar="HOLIDAY_NAME",
        default=None,
        help="Closure day holiday name (e.g. 'Easter Sunday'). Forces all bottles to 0."
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

    # --- Parse scrape data ---
    try:
        scrape_data = json.loads(args.data)
    except json.JSONDecodeError as exc:
        emit_failure(f"Invalid JSON in --data: {exc}")
        sys.exit(1)

    # --- Determine run date ---
    date_str = scrape_data.get("date")
    if date_str:
        try:
            today = datetime.date.fromisoformat(date_str)
        except ValueError:
            emit_failure(f"Invalid date in --data: {date_str!r}")
            sys.exit(1)
    else:
        today = datetime.date.today()

    today_str    = today.strftime("%Y-%m-%d")
    tomorrow     = today + datetime.timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")

    # --- Apply closure override ---
    is_closure    = args.closure is not None
    holiday_name  = args.closure if is_closure else None
    closure_notes = f"Gift shop closed — {holiday_name}" if is_closure else None

    if is_closure:
        scrape_data["blantons"]        = 0
        scrape_data["weller107"]       = 0
        scrape_data["ehtaylor_sb"]     = 0
        scrape_data["eagle_rare"]      = 0
        scrape_data["special_release"] = None

    log(f"=== Buffalo Trace Tracker Data Updater ===")
    log(f"tracker_data.json : {tracker_path}")
    log(f"Date              : {today_str}  (tomorrow: {tomorrow_str})")
    log(f"Dry run           : {args.dry_run}")
    if is_closure:
        log(f"CLOSURE           : {holiday_name} — all bottles forced to 0")

    # --- Load tracker data ---
    try:
        with open(tracker_path) as f:
            tracker = json.load(f)
    except Exception as exc:
        emit_failure(f"Failed to load tracker_data.json: {exc}")
        sys.exit(1)

    daily_log       = tracker.get("daily_log",   [])
    predictions     = tracker.get("predictions", [])
    bayesian_priors = tracker["config"]["bayesian_priors"]

    # -----------------------------------------------------------------------
    # Step 2a — Back-fill today's Predictions actual
    # -----------------------------------------------------------------------
    log("\n--- Step 2a: Back-fill today's Predictions actual ---")

    pred_idx, pred_row = find_prediction_for_date(predictions, today_str)

    if pred_idx is None:
        log(f"  No Predictions row found for {today_str} — skipping 2a")
    elif is_closure:
        log(f"  Predictions row found (idx={pred_idx}), closure day:")
        log(f"    actual_bottles = 'Gift shop closed'")
        log(f"    correct        = 'N/A'")
        log(f"    result         = 'Gift shop closed'")
        if not args.dry_run:
            predictions[pred_idx]["actual_bottles"]  = []
            predictions[pred_idx]["correct"]         = "N/A"
            predictions[pred_idx]["result"]          = "Gift shop closed"
        else:
            log("  [DRY RUN] Would update prediction row")
    else:
        # Build actual bottle list (keys, sorted by tomorrow_pct desc for display)
        # For now we sort alphabetically — order doesn't affect correctness evaluation
        actual_bottle_keys = [
            key for key in BOTTLE_KEYS
            if int(scrape_data.get(key, 0) or 0) == 1
        ]

        predicted_bottle_keys = pred_row.get("predicted_bottles") or []
        correct, result = evaluate_prediction(predicted_bottle_keys, actual_bottle_keys)

        log(f"  Predictions row found (idx={pred_idx}):")
        log(f"    predicted = {predicted_bottle_keys}")
        log(f"    actual    = {actual_bottle_keys}")
        log(f"    correct   = {correct}")
        log(f"    result    = {result}")

        if not args.dry_run:
            predictions[pred_idx]["actual_bottles"] = actual_bottle_keys
            predictions[pred_idx]["correct"]        = correct
            predictions[pred_idx]["result"]         = result
        else:
            log("  [DRY RUN] Would update prediction row")

    # -----------------------------------------------------------------------
    # Step 2b — Append today's Daily Log entry (idempotent)
    # -----------------------------------------------------------------------
    log("\n--- Step 2b: Append today's Daily Log entry ---")

    log_idx, _ = find_log_row_for_date(daily_log, today_str)
    appended_today = False

    if log_idx is not None:
        log(f"  Today ({today_str}) already has a log row at index {log_idx} — skipping 2b")
    else:
        new_log_row = {
            "date":             today_str,
            "day_of_week":      today.strftime("%A"),
            "blantons":         int(scrape_data.get("blantons",    0) or 0),
            "weller107":        int(scrape_data.get("weller107",   0) or 0),
            "ehtaylor_sb":      int(scrape_data.get("ehtaylor_sb", 0) or 0),
            "eagle_rare":       int(scrape_data.get("eagle_rare",  0) or 0),
            "special_release":  scrape_data.get("special_release") or None,
            "notes":            closure_notes or scrape_data.get("notes", "") or "",
            "last_site_update": scrape_data.get("last_site_update") or None,
            "is_closure":       is_closure,
        }

        log(f"  Appending new row for {today_str}:")
        log(f"    day_of_week     = {new_log_row['day_of_week']}")
        log(f"    blantons        = {new_log_row['blantons']}")
        log(f"    weller107       = {new_log_row['weller107']}")
        log(f"    ehtaylor_sb     = {new_log_row['ehtaylor_sb']}")
        log(f"    eagle_rare      = {new_log_row['eagle_rare']}")
        log(f"    special_release = {new_log_row['special_release']!r}")
        log(f"    notes           = {new_log_row['notes']!r}")
        log(f"    last_site_update= {new_log_row['last_site_update']!r}")
        log(f"    is_closure      = {new_log_row['is_closure']}")

        if not args.dry_run:
            daily_log.append(new_log_row)
            appended_today = True
        else:
            # Simulate the append so analytics reflects today
            daily_log = list(daily_log) + [new_log_row]
            appended_today = True
            log("  [DRY RUN] Simulating today's row in analytics computation")

    # -----------------------------------------------------------------------
    # Step 2c — Recompute Analytics
    # -----------------------------------------------------------------------
    log("\n--- Step 2c: Recompute Analytics ---")
    log(f"  Total daily log rows: {len(daily_log)}")

    analytics = compute_analytics(daily_log, bayesian_priors)

    for key in BOTTLE_KEYS:
        s = analytics[key]
        log(
            f"  {BOTTLE_DISPLAY_NAMES[key]:30s}  "
            f"avail={s['days_available']}/{s['total_days']}  "
            f"overall={s['overall_pct']:.1f}%  "
            f"10d={s['rolling_10d_pct']:.0f}%  "
            f"streak={s['streak']} ({s['streak_direction']})  "
            f"tomorrow={s['tomorrow_pct']:.1f}%  "
            f"conf={s['confidence']}  "
            f"last={s['last_seen']}"
        )

    # -----------------------------------------------------------------------
    # Step 2d — Write tomorrow's Predictions row
    # -----------------------------------------------------------------------
    log("\n--- Step 2d: Write tomorrow's Predictions row ---")

    # Per-bottle prediction percentages (always computed)
    per_bottle_pct = {
        key: round(analytics[key]["tomorrow_pct"], 1)
        for key in BOTTLE_KEYS
    }

    # Predicted bottles = those at >= 50%, sorted descending by pct
    predicted_tomorrow = [
        key for key in BOTTLE_KEYS
        if analytics[key]["tomorrow_pct"] >= 50.0
    ]
    predicted_tomorrow.sort(key=lambda k: analytics[k]["tomorrow_pct"], reverse=True)

    # Overall confidence = average pct of predicted bottles (None if none predicted)
    if predicted_tomorrow:
        overall_confidence_pct = round(
            sum(analytics[k]["tomorrow_pct"] for k in predicted_tomorrow)
            / len(predicted_tomorrow),
            1
        )
    else:
        overall_confidence_pct = None

    # Confidence label: use the most common confidence across predicted bottles,
    # or fallback to the most restrictive one present
    if predicted_tomorrow:
        conf_values = [analytics[k]["confidence"] for k in predicted_tomorrow]
        if "Prior only" in conf_values:
            confidence_label = "Prior only"
        elif "Medium" in conf_values:
            confidence_label = "Medium"
        else:
            confidence_label = "High"
    else:
        confidence_label = analytics[BOTTLE_KEYS[0]]["confidence"]  # fallback

    log(f"  Tomorrow ({tomorrow_str}):")
    log(f"    predicted bottles   = {predicted_tomorrow}")
    log(f"    per_bottle_pct      = {per_bottle_pct}")
    log(f"    overall_confidence  = {overall_confidence_pct}%")
    log(f"    confidence_label    = {confidence_label}")

    tomorrow_pred_row = {
        "date":                  tomorrow_str,
        "predicted_bottles":     predicted_tomorrow,
        "overall_confidence_pct": overall_confidence_pct,
        "per_bottle_pct":        per_bottle_pct,
        "confidence":            confidence_label,
        "actual_bottles":        None,
        "correct":               None,
        "result":                None,
        "model_used":            "Bayesian Markov",
        "notes":                 "",
        "missing_prediction":    False,
    }

    # Find existing tomorrow row and overwrite, or append new
    tom_idx, _ = find_prediction_for_date(predictions, tomorrow_str)
    if tom_idx is not None:
        log(f"  Overwriting existing Predictions row at index {tom_idx} for {tomorrow_str}")
        if not args.dry_run:
            predictions[tom_idx] = tomorrow_pred_row
        else:
            log("  [DRY RUN] Would overwrite existing row")
    else:
        log(f"  Appending new Predictions row for {tomorrow_str}")
        if not args.dry_run:
            predictions.append(tomorrow_pred_row)
        else:
            log("  [DRY RUN] Would append new prediction row")

    # -----------------------------------------------------------------------
    # Update meta and save
    # -----------------------------------------------------------------------
    if not args.dry_run:
        tracker["daily_log"]   = daily_log
        tracker["predictions"] = predictions
        tracker["meta"]["last_updated"]       = today_str
        tracker["meta"]["last_run_by"]        = "update_tracker_data.py"
        tracker["meta"]["total_days_tracked"] = len(daily_log)

        log("\n--- Saving tracker_data.json ---")
        try:
            with open(tracker_path, "w") as f:
                json.dump(tracker, f, indent=2)
                f.write("\n")
            log(f"  Saved: {tracker_path}")
        except Exception as exc:
            emit_failure(f"Failed to save tracker_data.json: {exc}")
            sys.exit(1)

        if args.backup_path:
            try:
                shutil.copy2(tracker_path, args.backup_path)
                log(f"  Backup: {args.backup_path}")
            except Exception as exc:
                log(f"  WARNING: backup failed: {exc}")
    else:
        log("\n[DRY RUN] tracker_data.json NOT saved.")

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

#!/usr/bin/env python3
"""
run_weekly_qc.py — GitHub Actions weekly quality check for Buffalo Trace tracker.

Checks:
  1. No gaps/duplicates in daily_log dates
  2. All past predictions have actual_bottles filled
  3. data.json in sync with tracker_data.json
  4. GitHub Pages data.json not stale
  5. Required env vars present

Environment variables (GitHub Secrets):
  RESEND_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
  TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER, TWILIO_ENABLED
  DRY_RUN — "true" to skip email/SMS/git push

Usage:
  python scripts/run_weekly_qc.py [--dry-run]
"""

import argparse
import datetime
import email.message
import json
import os
import smtplib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import base64
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
REPO_ROOT   = SCRIPTS_DIR.parent

TRACKER_DATA_PATH = REPO_ROOT / "tracker_data.json"
DATA_JSON_PATH    = REPO_ROOT / "data.json"
BUILD_DATA_JSON   = SCRIPTS_DIR / "build_data_json.py"

REPORT_FROM = "Buffalo Trace Daily <drops@buffalotracebottledrops.com>"
REPORT_TO   = "brianwulff@yahoo.com"

BOTTLE_KEYS = ["blantons", "weller107", "ehtaylor_sb", "eagle_rare"]
BOTTLE_DISPLAY = {
    "blantons":    "Blanton's",
    "weller107":   "Weller 107",
    "ehtaylor_sb": "E.H. Taylor SB",
    "eagle_rare":  "Eagle Rare",
}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# SMTP retry helper
# ---------------------------------------------------------------------------

def smtp_send_with_retry(msg, api_key, max_attempts=2, wait_seconds=900):
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            with smtplib.SMTP_SSL("smtp.resend.com", 465) as s:
                s.login("resend", api_key)
                s.send_message(msg)
            return
        except smtplib.SMTPAuthenticationError:
            raise
        except (smtplib.SMTPException, OSError) as e:
            last_exc = e
            if attempt < max_attempts:
                log(f"[SMTP] Attempt {attempt} failed: {e}. Waiting {wait_seconds // 60} min...")
                time.sleep(wait_seconds)
            else:
                log(f"[SMTP] All {max_attempts} attempts failed.")
    raise last_exc


# ---------------------------------------------------------------------------
# Twilio SMS helper
# ---------------------------------------------------------------------------

def twilio_send_sms(body, max_attempts=2, wait_seconds=900):
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "")
    to_number   = os.environ.get("TWILIO_TO_NUMBER", "")
    enabled     = os.environ.get("TWILIO_ENABLED", "true").strip().lower()

    if enabled != "true" or not account_sid:
        log(f"[SMS] Disabled or unconfigured — skipping: {body[:80]}")
        return

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    auth_header = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()

    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            payload = urllib.parse.urlencode({
                "From": from_number, "To": to_number, "Body": body
            }).encode()
            req = urllib.request.Request(url, data=payload, method="POST")
            req.add_header("Authorization", f"Basic {auth_header}")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            log(f"[SMS] Sent: {body[:80]}")
            return
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            if 400 <= e.code < 500:
                raise Exception(f"Twilio error {e.code}: {err_body}") from e
            last_exc = Exception(f"Twilio transient {e.code}: {err_body}")
        except Exception as e:
            last_exc = e
        if attempt < max_attempts:
            log(f"[SMS] Attempt {attempt} failed: {last_exc}. Waiting {wait_seconds // 60} min...")
            time.sleep(wait_seconds)
        else:
            log(f"[SMS] All {max_attempts} attempts failed.")
    raise last_exc


def send_sms_safe(body):
    try:
        twilio_send_sms(body)
    except Exception as exc:
        log(f"[SMS] Failed (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# Git helper
# ---------------------------------------------------------------------------

def git_commit_and_push(commit_message, dry_run=False):
    if dry_run:
        log("[GIT] DRY RUN — skipping commit and push")
        return
    try:
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            check=True, cwd=REPO_ROOT
        )
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            check=True, cwd=REPO_ROOT
        )
        subprocess.run(
            ["git", "add", "tracker_data.json", "data.json"],
            check=True, cwd=REPO_ROOT
        )
        result = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=REPO_ROOT, capture_output=True
        )
        if result.returncode == 0:
            log("[GIT] No changes to commit")
            return
        subprocess.run(
            ["git", "commit", "-m", commit_message],
            check=True, cwd=REPO_ROOT
        )
        subprocess.run(["git", "push"], check=True, cwd=REPO_ROOT)
        log(f"[GIT] Pushed: {commit_message}")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Git operation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Check 1: No gaps or duplicates in daily_log dates
# ---------------------------------------------------------------------------

def check_1_no_gaps(daily_log):
    """Returns list of issue strings."""
    issues = []
    dates = [row["date"] for row in daily_log]

    # Check duplicates
    seen = set()
    for d in dates:
        if d in seen:
            issues.append(f"Duplicate date in daily_log: {d}")
        seen.add(d)

    # Check gaps
    sorted_dates = sorted(set(dates))
    for i in range(1, len(sorted_dates)):
        prev = datetime.date.fromisoformat(sorted_dates[i - 1])
        curr = datetime.date.fromisoformat(sorted_dates[i])
        delta = (curr - prev).days
        if delta > 1:
            missing = [
                (prev + datetime.timedelta(days=j)).strftime("%Y-%m-%d")
                for j in range(1, delta)
            ]
            issues.append(
                f"Gap between {sorted_dates[i-1]} and {sorted_dates[i]}: "
                f"{delta - 1} missing date(s): {', '.join(missing)}"
            )

    return issues


# ---------------------------------------------------------------------------
# Check 2: All past predictions have actual_bottles filled
# ---------------------------------------------------------------------------

def check_2_predictions_filled(tracker, today):
    """Returns (issues, modified_tracker_or_None)."""
    issues = []
    today_str = today.strftime("%Y-%m-%d")
    daily_map = {row["date"]: row for row in tracker.get("daily_log", [])}
    modified = False

    for pred in tracker.get("predictions", []):
        if pred["date"] >= today_str:
            continue  # Future — skip

        # Check if actual is filled
        if (pred.get("actual_bottles") is not None and
                pred.get("result") is not None):
            continue

        # Missing actual — try to back-fill from daily_log
        daily_row = daily_map.get(pred["date"])
        if daily_row:
            actual = [k for k in BOTTLE_KEYS if daily_row.get(k, 0) == 1]
            pred["actual_bottles"] = actual
            predicted_set = set(pred.get("predicted_bottles") or [])
            actual_set    = set(actual)

            if daily_row.get("is_closure", False):
                pred["correct"] = False
                pred["result"]  = "Gift shop closed"
            elif predicted_set == actual_set:
                pred["correct"] = True
                pred["result"]  = "Correct"
            elif predicted_set & actual_set:
                pred["correct"] = False
                pred["result"]  = "Partial"
            else:
                pred["correct"] = False
                pred["result"]  = "Incorrect"

            issues.append(
                f"Back-filled {pred['date']}: actual={actual}, result={pred['result']}"
            )
            modified = True
        else:
            issues.append(
                f"Missing actual for {pred['date']} — no matching daily_log row"
            )

    return issues, (tracker if modified else None)


# ---------------------------------------------------------------------------
# Check 3: data.json in sync
# ---------------------------------------------------------------------------

def check_3_data_json_sync():
    """Rebuilds data.json via subprocess. Returns list of issue strings."""
    issues = []
    try:
        result = subprocess.run(
            [sys.executable, str(BUILD_DATA_JSON),
             "--tracker-data", str(TRACKER_DATA_PATH),
             "--output", str(DATA_JSON_PATH)],
            capture_output=True, text=True, timeout=60
        )
        out = json.loads(result.stdout)
        if not out.get("success"):
            issues.append(f"build_data_json.py failed: {out.get('error', 'unknown')}")
        else:
            log("[QC3] data.json rebuilt OK")
    except Exception as e:
        issues.append(f"data.json sync check error: {e}")
    return issues


# ---------------------------------------------------------------------------
# Check 4: GitHub Pages freshness
# ---------------------------------------------------------------------------

def check_4_github_pages_fresh(today):
    """Returns list of warning strings."""
    issues = []
    try:
        url = ("https://raw.githubusercontent.com/616fun/Buffalo-Trace-Bottles"
               "/main/data.json")
        req = urllib.request.Request(url)
        req.add_header("Cache-Control", "no-cache")
        with urllib.request.urlopen(req, timeout=15) as resp:
            live = json.loads(resp.read())
        last_updated = live.get("meta", {}).get("last_updated", "")
        if last_updated:
            d = datetime.date.fromisoformat(last_updated)
            delta = (today - d).days
            if delta > 2:
                issues.append(
                    f"GitHub Pages data.json stale: last_updated={last_updated} "
                    f"({delta} days ago)"
                )
            else:
                log(f"[QC4] GitHub Pages fresh: last_updated={last_updated}")
        else:
            issues.append("GitHub Pages data.json missing meta.last_updated")
    except Exception as e:
        issues.append(f"Could not fetch GitHub Pages data.json: {e}")
    return issues


# ---------------------------------------------------------------------------
# Check 5: Required env vars
# ---------------------------------------------------------------------------

def check_5_env_vars():
    issues = []
    for var in ["RESEND_API_KEY", "TWILIO_ACCOUNT_SID"]:
        if not os.environ.get(var, "").strip():
            issues.append(f"Required env var not set: {var}")
    return issues


# ---------------------------------------------------------------------------
# HTML email builder
# ---------------------------------------------------------------------------

def build_email_html(today, all_results, tracker):
    date_label  = today.strftime("%A, %B %-d, %Y")
    hard_issues = [i for r in all_results if not r["warning_only"] for i in r["issues"]]
    warn_issues = [i for r in all_results if r["warning_only"]     for i in r["issues"]]

    if not hard_issues and not warn_issues:
        summary_text  = "All checks passed — no issues found."
        summary_color = "#2d6a2d"
        summary_icon  = "✅"
    elif hard_issues:
        summary_text  = f"{len(hard_issues)} issue(s) found and fixed."
        summary_color = "#b35c00"
        summary_icon  = "⚠️"
    else:
        summary_text  = f"All checks passed — {len(warn_issues)} warning(s)."
        summary_color = "#b35c00"
        summary_icon  = "⚠️"

    checks_html = ""
    for r in all_results:
        if not r["issues"]:
            row_color = "#2d6a2d"; icon = "✅"; label = "Pass"; detail = ""
        elif r["warning_only"]:
            row_color = "#b35c00"; icon = "⚠️"; label = "Warning"
            detail = "<br><small style='color:#666'>" + "<br>".join(r["issues"]) + "</small>"
        else:
            row_color = "#b35c00"; icon = "⚠️"; label = "Fixed"
            detail = "<br><small style='color:#666'>" + "<br>".join(r["issues"]) + "</small>"

        checks_html += f"""
        <tr style="border-bottom:1px solid #e0d5c1;">
            <td style="padding:10px 16px;font-size:14px;">{r['name']}</td>
            <td style="padding:10px 16px;color:{row_color};font-weight:bold;white-space:nowrap;">{icon} {label}</td>
            <td style="padding:10px 16px;font-size:13px;color:#555;">{r['description']}{detail}</td>
        </tr>"""

    # Stats + last 7 days
    daily_log = tracker.get("daily_log", [])
    dates = sorted(r["date"] for r in daily_log)
    date_range   = f"{dates[0]} → {dates[-1]}" if dates else "—"
    last_updated = tracker.get("meta", {}).get("last_updated", "—")
    recent = daily_log[-7:]
    grid_rows = ""
    for row in recent:
        cells = "".join(
            f'<td style="padding:6px 10px;text-align:center;">{"✅" if row.get(k) else "❌"}</td>'
            for k in BOTTLE_KEYS
        )
        grid_rows += f'<tr><td style="padding:6px 10px;font-size:12px;">{row["date"]}</td>{cells}</tr>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Buffalo Trace Quality Check — {date_label}</title></head>
<body style="margin:0;padding:0;background-color:#f5f0e8;font-family:Georgia,'Times New Roman',serif;">
<div style="max-width:700px;margin:0 auto;background-color:#fffcf5;border:1px solid #d4c5a0;">

  <div style="background:linear-gradient(135deg,#2c1810 0%,#4a2c17 100%);color:#f5e6c8;padding:24px 32px;text-align:center;">
    <h1 style="margin:0;font-size:22px;">🥃 Buffalo Trace Quality Check</h1>
    <p style="margin:8px 0 0;font-size:14px;color:#d4b896;">{date_label}</p>
  </div>

  <div style="padding:24px 32px;">
    <div style="background:#f0ead8;border-left:4px solid {summary_color};padding:16px;border-radius:4px;">
      <strong style="font-size:16px;color:{summary_color};">{summary_icon} {summary_text}</strong>
    </div>
  </div>

  <div style="padding:0 32px 24px;">
    <h2 style="color:#2c1810;font-size:16px;border-bottom:2px solid #d4c5a0;padding-bottom:8px;">Check Results</h2>
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="background:#2c1810;color:#f5e6c8;">
          <th style="padding:10px 16px;text-align:left;width:35%;">Check</th>
          <th style="padding:10px 16px;text-align:left;width:15%;">Status</th>
          <th style="padding:10px 16px;text-align:left;">Details</th>
        </tr>
      </thead>
      <tbody>{checks_html}</tbody>
    </table>
  </div>

  <div style="padding:0 32px 24px;">
    <h2 style="color:#2c1810;font-size:16px;border-bottom:2px solid #d4c5a0;padding-bottom:8px;">Data Snapshot</h2>
    <p style="font-size:14px;color:#444;margin:0 0 12px;">
      <strong>Total days tracked:</strong> {len(daily_log)}<br>
      <strong>Date range:</strong> {date_range}<br>
      <strong>Last updated:</strong> {last_updated}
    </p>
    <h3 style="font-size:14px;color:#2c1810;margin:16px 0 8px;">Last 7 Days</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#4a2c17;color:#f5e6c8;">
          <th style="padding:6px 10px;text-align:left;">Date</th>
          <th style="padding:6px 10px;text-align:center;">Blanton's</th>
          <th style="padding:6px 10px;text-align:center;">Weller 107</th>
          <th style="padding:6px 10px;text-align:center;">EHT SB</th>
          <th style="padding:6px 10px;text-align:center;">Eagle Rare</th>
        </tr>
      </thead>
      <tbody>{grid_rows}</tbody>
    </table>
  </div>

  <div style="background:#2c1810;color:#d4b896;padding:16px 32px;text-align:center;font-size:12px;">
    <p style="margin:0;">Buffalo Trace Daily Tracker · Weekly QC ·
      <a href="https://616fun.github.io/Buffalo-Trace-Bottles/" style="color:#f5e6c8;">Live Dashboard</a>
    </p>
  </div>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    dry_run = args.dry_run or os.environ.get("DRY_RUN", "false").lower() == "true"

    today     = datetime.date.today()
    today_str = today.strftime("%b %-d")
    date_label = today.strftime("%A, %B %-d, %Y")
    resend_api_key = os.environ.get("RESEND_API_KEY", "")

    if dry_run:
        log("=== DRY RUN MODE — email, SMS, and git push will be skipped ===")

    try:
        log("=== Buffalo Trace Weekly QC ===")
        log(f"Date: {today}")

        tracker   = json.loads(TRACKER_DATA_PATH.read_text())
        daily_log = tracker.get("daily_log", [])
        all_results = []
        any_repair  = False

        # ── Check 1 ──────────────────────────────────────────────────────────
        log("\n[QC1] Checking daily_log for gaps and duplicates...")
        issues1 = check_1_no_gaps(daily_log)
        all_results.append({
            "name": "No gaps/duplicates in daily_log",
            "description": "All dates consecutive, no skips or repeats",
            "issues": issues1, "warning_only": False,
        })
        log(f"  {'✅ Pass' if not issues1 else f'⚠️ {len(issues1)} issue(s)'}")

        # ── Check 2 ──────────────────────────────────────────────────────────
        log("\n[QC2] Checking predictions for missing actuals...")
        issues2, repaired = check_2_predictions_filled(tracker, today)
        if repaired is not None:
            tracker    = repaired
            any_repair = True
        all_results.append({
            "name": "Predictions actuals filled",
            "description": "All past prediction rows have actual_bottles and result",
            "issues": issues2, "warning_only": False,
        })
        log(f"  {'✅ Pass' if not issues2 else f'⚠️ {len(issues2)} item(s) repaired/flagged'}")

        # Write repaired tracker before check 3 (so build_data_json sees it)
        if any_repair and not dry_run:
            log("  Writing repaired tracker_data.json...")
            TRACKER_DATA_PATH.write_text(json.dumps(tracker, indent=2))
            log("  ✅ Saved")

        # ── Check 3 ──────────────────────────────────────────────────────────
        log("\n[QC3] Rebuilding data.json from tracker_data.json...")
        if dry_run:
            issues3 = []
            log("  [DRY RUN] Skipped")
        else:
            issues3 = check_3_data_json_sync()
        all_results.append({
            "name": "data.json in sync with tracker_data.json",
            "description": "build_data_json.py runs clean and output is current",
            "issues": issues3, "warning_only": False,
        })
        log(f"  {'✅ Pass' if not issues3 else f'⚠️ {issues3}'}")

        # ── Check 4 ──────────────────────────────────────────────────────────
        log("\n[QC4] Checking GitHub Pages data.json freshness...")
        issues4 = check_4_github_pages_fresh(today)
        all_results.append({
            "name": "GitHub Pages data.json not stale",
            "description": "Live site last_updated within 2 days of today",
            "issues": issues4, "warning_only": True,
        })
        log(f"  {'✅ Pass' if not issues4 else f'⚠️ {issues4}'}")

        # ── Check 5 ──────────────────────────────────────────────────────────
        log("\n[QC5] Checking required env vars...")
        issues5 = check_5_env_vars()
        all_results.append({
            "name": "Required env vars present",
            "description": "RESEND_API_KEY and TWILIO_ACCOUNT_SID are set",
            "issues": issues5, "warning_only": False,
        })
        log(f"  {'✅ Pass' if not issues5 else f'⚠️ {issues5}'}")

        # ── Git push if repairs made ──────────────────────────────────────────
        if any_repair and not dry_run:
            log("\n=== Git: pushing repaired files ===")
            try:
                git_commit_and_push(f"Weekly QC repair {today.strftime('%Y-%m-%d')}")
            except RuntimeError as e:
                log(f"  Git push failed (non-fatal): {e}")

        # ── Email ─────────────────────────────────────────────────────────────
        log("\n=== Building and sending QC email ===")
        html_body = build_email_html(today, all_results, tracker)

        month_folder = REPO_ROOT / today.strftime("%B %Y")
        month_folder.mkdir(exist_ok=True)
        html_path = month_folder / f"Buffalo Trace Quality Check - {today.strftime('%b %d %Y')}.html"
        html_path.write_text(html_body)
        log(f"  HTML saved: {html_path.name}")

        if not dry_run and resend_api_key:
            msg = email.message.EmailMessage()
            msg["Subject"] = f"Buffalo Trace Quality Check — {date_label}"
            msg["From"]    = REPORT_FROM
            msg["To"]      = REPORT_TO
            msg.set_content("This email requires an HTML-capable client.")
            msg.add_alternative(html_body, subtype="html")
            smtp_send_with_retry(msg, resend_api_key)
            log(f"  Email sent to {REPORT_TO}")
        else:
            log("  [DRY RUN or no API key] Email skipped")

        # ── SMS ───────────────────────────────────────────────────────────────
        log("\n=== Sending SMS ===")
        hard_issues = [i for r in all_results if not r["warning_only"] for i in r["issues"]]
        if hard_issues:
            sms_body = f"⚠️ BT QC {today_str}: {len(hard_issues)} issue(s) fixed. Details emailed."
        else:
            sms_body = f"✅ BT QC {today_str}: All checks passed."

        if not dry_run:
            send_sms_safe(sms_body)
        else:
            log(f"  [DRY RUN] SMS would be: {sms_body}")

        log("\n=== Weekly QC complete ===")

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            send_sms_safe(
                f"❌ BT QC {today_str}: Task failed — "
                f"{type(e).__name__}: {str(e)[:60]}. Manual check required."
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()

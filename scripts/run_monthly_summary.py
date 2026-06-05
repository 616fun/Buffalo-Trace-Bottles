#!/usr/bin/env python3
"""
run_monthly_summary.py — Monthly summary report for Buffalo Trace tracker.

Reads tracker_data.json. Summarizes the current calendar month's data.
Sends HTML email + SMS. No git writes.

Environment variables (GitHub Secrets):
  RESEND_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
  TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER, TWILIO_ENABLED
  DRY_RUN — "true" to skip email/SMS

Usage:
  python scripts/run_monthly_summary.py [--dry-run] [--month YYYY-MM]
  (--month overrides the auto-detected month, useful for testing)
"""

import argparse
import calendar
import datetime
import email.message
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import base64
from pathlib import Path

SCRIPTS_DIR       = Path(__file__).parent
REPO_ROOT         = SCRIPTS_DIR.parent
TRACKER_DATA_PATH = REPO_ROOT / "tracker_data.json"

REPORT_FROM = "Buffalo Trace Daily <drops@buffalotracebottledrops.com>"
REPORT_TO   = "brianwulff@yahoo.com"

BOTTLE_KEYS    = ["blantons", "weller107", "ehtaylor_sb", "eagle_rare"]
BOTTLE_DISPLAY = {
    "blantons":    "Blanton's Single Barrel",
    "weller107":   "Weller Antique 107",
    "ehtaylor_sb": "E.H. Taylor Small Batch",
    "eagle_rare":  "Eagle Rare 10-Year",
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
# Pushover push-notification helper
#   This task's notifications were moved from Twilio SMS to Pushover on
#   2026-06-05. Reads PUSHOVER_TOKEN / PUSHOVER_USER from the environment.
# ---------------------------------------------------------------------------

def pushover_send(title, body, priority=0, max_attempts=2):
    """Send a Pushover notification. Retries once on transient (5xx/network)
    errors. Raises on permanent (4xx) failure."""
    token   = os.environ.get("PUSHOVER_TOKEN", "").strip()
    user    = os.environ.get("PUSHOVER_USER", "").strip()
    enabled = os.environ.get("PUSHOVER_ENABLED", "true").strip().lower()
    # Target only Brian's iPhone (not Nancy's). Override via PUSHOVER_DEVICE.
    device  = os.environ.get("PUSHOVER_DEVICE", "BriansPhone").strip()

    if enabled != "true" or not token or not user:
        log(f"[Pushover] Disabled or unconfigured — skipping: {body[:80]}")
        return

    url = "https://api.pushover.net/1/messages.json"
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            payload = urllib.parse.urlencode({
                "token":    token,
                "user":     user,
                "title":    title,
                "message":  body,
                "sound":    "cashregister",
                "priority": priority,
                "device":   device,
            }).encode()
            req = urllib.request.Request(url, data=payload, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            log(f"[Pushover] Sent (p{priority}): {title} — {body[:80]}")
            return
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            if 400 <= e.code < 500:
                raise Exception(f"Pushover error {e.code}: {err_body}") from e
            last_exc = Exception(f"Pushover transient {e.code}: {err_body}")
        except Exception as e:
            last_exc = e
        if attempt < max_attempts:
            time.sleep(3)
        else:
            raise last_exc


def pushover_send_safe(title, body, priority=0):
    """Send a Pushover notification, logging but not raising on failure."""
    try:
        pushover_send(title, body, priority)
    except Exception as exc:
        log(f"[Pushover] Failed (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def filter_by_month(rows, year, month, date_key="date"):
    prefix = f"{year:04d}-{month:02d}-"
    return [r for r in rows if r[date_key].startswith(prefix)]


def availability_stats(rows):
    """Returns {key: (available_days, total_days, pct)}."""
    stats = {}
    for k in BOTTLE_KEYS:
        avail = sum(1 for r in rows if r.get(k, 0) == 1)
        total = len(rows)
        pct   = (avail / total * 100) if total > 0 else 0.0
        stats[k] = (avail, total, pct)
    return stats


def prediction_accuracy(pred_rows):
    """Returns (correct, partial, incorrect, total)."""
    correct = partial = incorrect = 0
    for p in pred_rows:
        result = p.get("result", "")
        if result == "Correct":
            correct += 1
        elif result == "Partial":
            partial += 1
        elif result in ("Incorrect", "No"):
            incorrect += 1
    total = correct + partial + incorrect
    return correct, partial, incorrect, total


# ---------------------------------------------------------------------------
# HTML email builder
# ---------------------------------------------------------------------------

def build_email_html(today, year, month, month_rows, prev_month_rows,
                     pred_rows, special_releases, price_rows):

    month_label = datetime.date(year, month, 1).strftime("%B %Y")
    date_label  = today.strftime("%A, %B %-d, %Y")
    month_days  = calendar.monthrange(year, month)[1]

    # Availability stats
    stats = availability_stats(month_rows)
    prev_stats = availability_stats(prev_month_rows) if prev_month_rows else None
    total_tracked = len(month_rows)
    closure_days  = sum(1 for r in month_rows if r.get("is_closure", False))

    # Bottle rows HTML
    bottle_rows_html = ""
    for k in BOTTLE_KEYS:
        avail, total, pct = stats[k]
        if prev_stats:
            prev_avail, prev_total, prev_pct = prev_stats[k]
            delta = pct - prev_pct
            delta_str = f" ({'+' if delta >= 0 else ''}{delta:.1f}pp vs prior month)"
        else:
            delta_str = ""
        bar_width = int(pct)
        bar_color = "#2d6a2d" if pct >= 50 else ("#b35c00" if pct >= 20 else "#8b0000")
        bottle_rows_html += f"""
        <tr style="border-bottom:1px solid #e0d5c1;">
          <td style="padding:10px 16px;font-weight:600;">{BOTTLE_DISPLAY[k]}</td>
          <td style="padding:10px 16px;text-align:center;">{avail}/{total}</td>
          <td style="padding:10px 16px;text-align:center;">
            <div style="background:#e0d5c1;border-radius:4px;height:12px;width:100%;max-width:120px;display:inline-block;">
              <div style="background:{bar_color};border-radius:4px;height:12px;width:{bar_width}%;"></div>
            </div>
            <span style="margin-left:6px;font-weight:600;color:{bar_color};">{pct:.1f}%</span>
          </td>
          <td style="padding:10px 16px;font-size:13px;color:#666;">{delta_str or '—'}</td>
        </tr>"""

    # Prediction accuracy
    correct, partial, incorrect, total_preds = prediction_accuracy(pred_rows)
    if total_preds > 0:
        correct_pct  = correct  / total_preds * 100
        partial_pct  = partial  / total_preds * 100
        incorrect_pct = incorrect / total_preds * 100
        pred_summary = (f"{correct} correct ({correct_pct:.0f}%), "
                        f"{partial} partial ({partial_pct:.0f}%), "
                        f"{incorrect} incorrect ({incorrect_pct:.0f}%) "
                        f"out of {total_preds} days")
    else:
        pred_summary = "No prediction data for this month."

    # Special releases
    if special_releases:
        sr_html = "".join(
            f'<li style="margin:4px 0;">{r["date"]} — {r["name"]}</li>'
            for r in special_releases
        )
        sr_section = f'<ul style="margin:8px 0;padding-left:20px;">{sr_html}</ul>'
    else:
        sr_section = '<p style="color:#666;font-style:italic;">No special releases this month.</p>'

    # Price changes
    month_prefix = f"{year:04d}-{month:02d}-"
    month_prices = [r for r in price_rows if r.get("date", "").startswith(month_prefix)]
    if month_prices:
        price_rows_html = ""
        for pr in month_prices:
            price_rows_html += f'<p style="margin:4px 0;font-size:13px;"><strong>{pr["date"]}:</strong> '
            price_rows_html += ", ".join(
                f'{k.replace("_", " ").title()}: ${v}'
                for k, v in pr.items()
                if k != "date" and v is not None
            ) + "</p>"
        price_section = price_rows_html
    else:
        price_section = (
            '<p style="color:#666;font-style:italic;">No price changes recorded this month.</p>'
        )

    # Full availability grid (monthly calendar)
    date_map = {r["date"]: r for r in month_rows}
    calendar_rows = ""
    for day in range(1, month_days + 1):
        d = datetime.date(year, month, day)
        d_str = d.strftime("%Y-%m-%d")
        row = date_map.get(d_str)
        if row:
            is_closure = row.get("is_closure", False)
            if is_closure:
                cells = '<td colspan="4" style="padding:4px 8px;text-align:center;color:#888;font-style:italic;">Closed</td>'
            else:
                cells = "".join(
                    f'<td style="padding:4px 8px;text-align:center;">{"✅" if row.get(k) else "❌"}</td>'
                    for k in BOTTLE_KEYS
                )
            bg = "background:#f0ead8;" if is_closure else ""
        else:
            cells = '<td colspan="4" style="padding:4px 8px;text-align:center;color:#ccc;">—</td>'
            bg = "background:#fafafa;"
        calendar_rows += f'<tr style="{bg}"><td style="padding:4px 8px;font-size:12px;">{d_str} ({d.strftime("%a")})</td>{cells}</tr>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Buffalo Trace Monthly Summary — {month_label}</title></head>
<body style="margin:0;padding:0;background-color:#f5f0e8;font-family:Georgia,'Times New Roman',serif;">
<div style="max-width:700px;margin:0 auto;background-color:#fffcf5;border:1px solid #d4c5a0;">

  <div style="background:linear-gradient(135deg,#2c1810 0%,#4a2c17 100%);color:#f5e6c8;padding:24px 32px;text-align:center;">
    <h1 style="margin:0;font-size:22px;">🥃 Buffalo Trace Monthly Summary</h1>
    <p style="margin:8px 0 0;font-size:16px;font-weight:bold;color:#d4b896;">{month_label}</p>
    <p style="margin:4px 0 0;font-size:13px;color:#c4a886;">Generated {date_label}</p>
  </div>

  <div style="padding:24px 32px;">
    <p style="font-size:14px;color:#444;margin:0;">
      <strong>Days tracked:</strong> {total_tracked} of {month_days}
      {f' ({closure_days} closure day{"s" if closure_days != 1 else ""})' if closure_days else ''}
    </p>
  </div>

  <div style="padding:0 32px 24px;">
    <h2 style="color:#2c1810;font-size:16px;border-bottom:2px solid #d4c5a0;padding-bottom:8px;">
      Monthly Availability Rates
    </h2>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <thead>
        <tr style="background:#2c1810;color:#f5e6c8;">
          <th style="padding:10px 16px;text-align:left;">Bottle</th>
          <th style="padding:10px 16px;text-align:center;">Days Available</th>
          <th style="padding:10px 16px;text-align:center;">Rate</th>
          <th style="padding:10px 16px;text-align:left;">vs. Prior Month</th>
        </tr>
      </thead>
      <tbody>{bottle_rows_html}</tbody>
    </table>
  </div>

  <div style="padding:0 32px 24px;">
    <h2 style="color:#2c1810;font-size:16px;border-bottom:2px solid #d4c5a0;padding-bottom:8px;">
      Prediction Accuracy
    </h2>
    <p style="font-size:14px;color:#444;margin:0;">{pred_summary}</p>
  </div>

  <div style="padding:0 32px 24px;">
    <h2 style="color:#2c1810;font-size:16px;border-bottom:2px solid #d4c5a0;padding-bottom:8px;">
      Special Releases
    </h2>
    {sr_section}
  </div>

  <div style="padding:0 32px 24px;">
    <h2 style="color:#2c1810;font-size:16px;border-bottom:2px solid #d4c5a0;padding-bottom:8px;">
      Price Changes
    </h2>
    {price_section}
  </div>

  <div style="padding:0 32px 24px;">
    <h2 style="color:#2c1810;font-size:16px;border-bottom:2px solid #d4c5a0;padding-bottom:8px;">
      Full Month Calendar
    </h2>
    <table style="width:100%;border-collapse:collapse;font-size:12px;">
      <thead>
        <tr style="background:#4a2c17;color:#f5e6c8;">
          <th style="padding:4px 8px;text-align:left;">Date</th>
          <th style="padding:4px 8px;text-align:center;">Blanton's</th>
          <th style="padding:4px 8px;text-align:center;">Weller 107</th>
          <th style="padding:4px 8px;text-align:center;">EHT SB</th>
          <th style="padding:4px 8px;text-align:center;">Eagle Rare</th>
        </tr>
      </thead>
      <tbody>{calendar_rows}</tbody>
    </table>
  </div>

  <div style="background:#2c1810;color:#d4b896;padding:16px 32px;text-align:center;font-size:12px;">
    <p style="margin:0;">Buffalo Trace Daily Tracker · Monthly Summary ·
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
    parser.add_argument("--month", help="YYYY-MM override")
    args = parser.parse_args()
    dry_run = args.dry_run or os.environ.get("DRY_RUN", "false").lower() == "true"

    today = datetime.date.today()

    if args.month:
        year, month = map(int, args.month.split("-"))
    else:
        year, month = today.year, today.month

    month_label = datetime.date(year, month, 1).strftime("%b %Y")
    resend_api_key = os.environ.get("RESEND_API_KEY", "")

    if dry_run:
        log("=== DRY RUN MODE — email and SMS will be skipped ===")

    try:
        log(f"=== Buffalo Trace Monthly Summary — {month_label} ===")
        log(f"Date: {today}")

        tracker   = json.loads(TRACKER_DATA_PATH.read_text())
        daily_log = tracker.get("daily_log", [])
        predictions = tracker.get("predictions", [])
        special_log = tracker.get("special_releases_log", [])
        prices_log  = tracker.get("prices", [])

        # Filter to target month
        month_prefix = f"{year:04d}-{month:02d}-"
        month_rows = [r for r in daily_log if r["date"].startswith(month_prefix)]
        pred_rows  = [p for p in predictions
                      if p["date"].startswith(month_prefix)
                      and p.get("actual_bottles") is not None]
        special_releases = [r for r in special_log
                            if r["date"].startswith(month_prefix)]

        log(f"  Month rows: {len(month_rows)}, pred rows: {len(pred_rows)}")

        # Prior month for comparison
        if month == 1:
            prev_year, prev_month = year - 1, 12
        else:
            prev_year, prev_month = year, month - 1
        prev_prefix = f"{prev_year:04d}-{prev_month:02d}-"
        prev_month_rows = [r for r in daily_log if r["date"].startswith(prev_prefix)]
        log(f"  Prior month rows: {len(prev_month_rows)}")

        # Build and send email
        html_body = build_email_html(
            today, year, month, month_rows, prev_month_rows,
            pred_rows, special_releases, prices_log
        )

        # Save HTML
        month_folder = REPO_ROOT / today.strftime("%B %Y")
        month_folder.mkdir(exist_ok=True)
        html_path = month_folder / f"Buffalo Trace Monthly Summary - {datetime.date(year, month, 1).strftime('%b %Y')}.html"
        html_path.write_text(html_body)
        log(f"  HTML saved: {html_path.name}")

        log(f"\n=== Sending email ===")
        if not dry_run and resend_api_key:
            msg = email.message.EmailMessage()
            msg["Subject"] = f"Buffalo Trace Monthly Summary — {month_label}"
            msg["From"]    = REPORT_FROM
            msg["To"]      = REPORT_TO
            msg.set_content("This email requires an HTML-capable client.")
            msg.add_alternative(html_body, subtype="html")
            smtp_send_with_retry(msg, resend_api_key)
            log(f"  Email sent to {REPORT_TO}")
        else:
            log("  [DRY RUN or no API key] Email skipped")

        log(f"\n=== Sending SMS ===")
        sms_body = f"✅ BT Monthly Summary: {month_label} sent."
        if not dry_run:
            pushover_send_safe("Buffalo Trace Monthly Summary", sms_body)
        else:
            log(f"  [DRY RUN] Pushover would be: {sms_body}")

        log("\n=== Monthly Summary complete ===")

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            pushover_send_safe(
                "Buffalo Trace Monthly Summary — Failure",
                f"❌ BT Monthly Summary: Task failed — "
                f"{type(e).__name__}: {str(e)[:60]}.",
                priority=1,
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()

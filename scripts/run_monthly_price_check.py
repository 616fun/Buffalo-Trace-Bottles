#!/usr/bin/env python3
"""
run_monthly_price_check.py — Monthly price scrape for Buffalo Trace tracker.

Scrapes current prices from the BT availability page, compares with the last
tracker_data.json prices row, appends a new row only if any price changed,
rebuilds data.json, pushes to GitHub, sends email + SMS.

Environment variables (GitHub Secrets):
  RESEND_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
  TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER, TWILIO_ENABLED
  DRY_RUN — "true" to skip email/SMS/git push

Usage:
  python scripts/run_monthly_price_check.py [--dry-run]
"""

import argparse
import datetime
import email.message
import json
import os
import re
import smtplib
import subprocess
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
DATA_JSON_PATH    = REPO_ROOT / "data.json"
BUILD_DATA_JSON   = SCRIPTS_DIR / "build_data_json.py"

PRICE_PAGE_URL = "https://www.buffalotracedistillery.com/visit-us/product-availability/"

PRICE_FIELDS = [
    "blantons_single_barrel",
    "weller_antique_107",
    "eh_taylor_small_batch",
    "eagle_rare_10-year",
    "weller_special_reserve",
    "eh_taylor_straight_rye",
    "sazerac_rye",
    "buffalo_trace_bourbon",
    "wheatley_vodka",
    "buffalo_trace_bourbon_cream",
]

PRICE_DISPLAY = {
    "blantons_single_barrel":     "Blanton's Single Barrel",
    "weller_antique_107":         "Weller Antique 107",
    "eh_taylor_small_batch":      "E.H. Taylor Small Batch",
    "eagle_rare_10-year":         "Eagle Rare 10-Year",
    "weller_special_reserve":     "Weller Special Reserve",
    "eh_taylor_straight_rye":     "E.H. Taylor Straight Rye",
    "sazerac_rye":                "Sazerac Rye",
    "buffalo_trace_bourbon":      "Buffalo Trace Bourbon",
    "wheatley_vodka":             "Wheatley Vodka",
    "buffalo_trace_bourbon_cream": "Buffalo Trace Bourbon Cream",
}

# Maps price field → substrings to match against product h4 text (lowercase)
PRICE_NAME_MAP = {
    "blantons_single_barrel":     ["blanton"],
    "weller_antique_107":         ["weller antique 107"],
    "eh_taylor_small_batch":      ["e.h. taylor small batch"],
    "eagle_rare_10-year":         ["eagle rare"],
    "weller_special_reserve":     ["weller special reserve"],
    "eh_taylor_straight_rye":     ["e.h. taylor straight rye", "taylor straight rye"],
    "sazerac_rye":                ["sazerac rye"],
    "buffalo_trace_bourbon":      ["buffalo trace kentucky", "buffalo trace bourbon"],
    "wheatley_vodka":             ["wheatley vodka", "wheatley"],
    "buffalo_trace_bourbon_cream": ["bourbon cream"],
}

REPORT_FROM = "Buffalo Trace Daily <drops@buffalotracebottledrops.com>"
REPORT_TO   = "brianwulff@yahoo.com"

HEADERS = {
    "Cache-Control": "no-cache",
    "Pragma":        "no-cache",
    "User-Agent":    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
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
# Price scraping
# ---------------------------------------------------------------------------

def fetch_html(max_attempts=3, wait_seconds=60):
    """Fetch the BT availability page with retries. Returns HTML string."""
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(PRICE_PAGE_URL)
            for k, v in HEADERS.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_exc = e
            log(f"[FETCH] Attempt {attempt} failed: {e}")
            if attempt < max_attempts:
                time.sleep(wait_seconds)
    raise RuntimeError(f"Failed to fetch price page after {max_attempts} attempts: {last_exc}")


def parse_price_from_block(block):
    """
    Extract a dollar price from an HTML product block.
    Looks for patterns like $74.99, $74, 74.99, etc.
    Returns a float or None.
    """
    # Try to find price in common patterns:
    # <p class="price">$74.99</p>
    # <span class="price">$74.99</span>
    # data-price="74.99"
    # >$74.99<
    patterns = [
        r'class="[^"]*price[^"]*"[^>]*>\s*\$?([\d]+\.[\d]{2})',  # class=price
        r'data-price="([\d]+\.[\d]{0,2})"',                        # data-price attr
        r'\$\s*([\d]+\.[\d]{2})',                                   # $XX.XX anywhere
        r'\$\s*([\d]+)',                                            # $XX (integer)
    ]
    for pat in patterns:
        m = re.search(pat, block, re.IGNORECASE)
        if m:
            try:
                return round(float(m.group(1)), 2)
            except ValueError:
                continue
    return None


def scrape_prices(html):
    """
    Parse prices from the BT availability page HTML.
    Returns dict of PRICE_FIELDS → float|None.
    """
    result = {field: None for field in PRICE_FIELDS}

    # Extract product blocks (same strategy as scrape_availability.py)
    product_blocks = re.findall(
        r'<div class="product">(.*?)<a class="discover_link"',
        html, re.DOTALL
    )
    if not product_blocks:
        product_blocks = re.findall(
            r'<div class=["\']product["\']>(.*?)</div>\s*</div>\s*</div>',
            html, re.DOTALL
        )

    log(f"[PRICES] Found {len(product_blocks)} product block(s)")

    for block in product_blocks:
        h4_match = re.search(r'<h4>([^<]+)</h4>', block)
        if not h4_match:
            continue
        name_lower = h4_match.group(1).strip().lower()

        price = parse_price_from_block(block)

        # Match to price field
        for field, aliases in PRICE_NAME_MAP.items():
            for alias in aliases:
                if alias in name_lower:
                    result[field] = price
                    log(f"[PRICES]   {field} = {price} (matched '{alias}' in '{name_lower[:40]}')")
                    break

    return result


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

def prices_differ(scraped, last_row):
    """
    Returns list of changed fields: [(field, old_val, new_val), ...]
    Compares to last_row dict (which has a 'date' key too).
    """
    changes = []
    for field in PRICE_FIELDS:
        new_val = scraped.get(field)
        old_val = last_row.get(field)

        # Normalize
        if new_val is not None:
            new_val = round(float(new_val), 2)
        if old_val is not None:
            try:
                old_val = round(float(old_val), 2)
            except (TypeError, ValueError):
                old_val = None

        if new_val != old_val:
            # Treat null→null as no change
            if new_val is None and old_val is None:
                continue
            changes.append((field, old_val, new_val))
    return changes


# ---------------------------------------------------------------------------
# HTML email builder
# ---------------------------------------------------------------------------

def build_email_html(today, scraped_prices, last_row, changes):
    date_label = today.strftime("%A, %B %-d, %Y")
    today_str  = today.strftime("%b %-d")
    last_change_date = last_row.get("date", "unknown") if last_row else "unknown"

    if not changes:
        summary_html = f"""
      <div style="background:#f0ead8;border-left:4px solid #2d6a2d;padding:16px;border-radius:4px;">
        <strong style="font-size:16px;color:#2d6a2d;">✅ No price changes detected.</strong><br>
        <span style="font-size:14px;color:#555;">All prices remain as of {last_change_date}.</span>
      </div>"""
    else:
        change_rows = ""
        for field, old_val, new_val in changes:
            old_str = f"${old_val:.2f}" if old_val is not None else "—"
            new_str = f"${new_val:.2f}" if new_val is not None else "—"
            arrow   = "↑" if (new_val or 0) > (old_val or 0) else "↓"
            color   = "#8b0000" if arrow == "↑" else "#2d6a2d"
            change_rows += f"""
            <tr style="border-bottom:1px solid #e0d5c1;">
              <td style="padding:10px 16px;">{PRICE_DISPLAY.get(field, field)}</td>
              <td style="padding:10px 16px;text-align:center;">{old_str}</td>
              <td style="padding:10px 16px;text-align:center;font-weight:bold;color:{color};">{new_str} {arrow}</td>
            </tr>"""
        summary_html = f"""
      <div style="background:#fff3cd;border-left:4px solid #b35c00;padding:16px;border-radius:4px;margin-bottom:16px;">
        <strong style="font-size:16px;color:#b35c00;">⚠️ {len(changes)} price change(s) detected.</strong>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#2c1810;color:#f5e6c8;">
            <th style="padding:10px 16px;text-align:left;">Product</th>
            <th style="padding:10px 16px;text-align:center;">Previous</th>
            <th style="padding:10px 16px;text-align:center;">New Price</th>
          </tr>
        </thead>
        <tbody>{change_rows}</tbody>
      </table>"""

    # Full price table
    price_rows = ""
    for field in PRICE_FIELDS:
        val = scraped_prices.get(field)
        price_str = f"${val:.2f}" if val is not None else "—"
        changed = any(f == field for f, _, _ in changes)
        style = "font-weight:bold;color:#b35c00;" if changed else ""
        price_rows += f"""
        <tr style="border-bottom:1px solid #e0d5c1;">
          <td style="padding:8px 16px;font-size:14px;">{PRICE_DISPLAY.get(field, field)}</td>
          <td style="padding:8px 16px;text-align:center;font-size:14px;{style}">{price_str}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Buffalo Trace Price Check — {date_label}</title></head>
<body style="margin:0;padding:0;background-color:#f5f0e8;font-family:Georgia,'Times New Roman',serif;">
<div style="max-width:700px;margin:0 auto;background-color:#fffcf5;border:1px solid #d4c5a0;">

  <div style="background:linear-gradient(135deg,#2c1810 0%,#4a2c17 100%);color:#f5e6c8;padding:24px 32px;text-align:center;">
    <h1 style="margin:0;font-size:22px;">🥃 Buffalo Trace Price Check</h1>
    <p style="margin:8px 0 0;font-size:14px;color:#d4b896;">{date_label}</p>
  </div>

  <div style="padding:24px 32px;">
    {summary_html}
  </div>

  <div style="padding:0 32px 24px;">
    <h2 style="color:#2c1810;font-size:16px;border-bottom:2px solid #d4c5a0;padding-bottom:8px;">
      Current Prices
    </h2>
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="background:#2c1810;color:#f5e6c8;">
          <th style="padding:8px 16px;text-align:left;">Product</th>
          <th style="padding:8px 16px;text-align:center;">Price</th>
        </tr>
      </thead>
      <tbody>{price_rows}</tbody>
    </table>
  </div>

  <div style="background:#2c1810;color:#d4b896;padding:16px 32px;text-align:center;font-size:12px;">
    <p style="margin:0;">Buffalo Trace Daily Tracker · Price Check ·
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
    resend_api_key = os.environ.get("RESEND_API_KEY", "")

    if dry_run:
        log("=== DRY RUN MODE — email, SMS, and git push will be skipped ===")

    try:
        log(f"=== Buffalo Trace Monthly Price Check ===")
        log(f"Date: {today}")

        # Load tracker
        tracker    = json.loads(TRACKER_DATA_PATH.read_text())
        prices_log = tracker.get("prices", [])
        last_row   = prices_log[-1] if prices_log else {}
        log(f"  Last price row: {last_row.get('date', 'none')}")

        # Fetch current prices
        log("\n=== Fetching current prices from BT website ===")
        html = fetch_html()
        scraped = scrape_prices(html)
        log(f"  Scraped: {scraped}")

        # Compare
        changes = prices_differ(scraped, last_row)
        log(f"\n  Price changes: {len(changes)}")
        for field, old_val, new_val in changes:
            log(f"    {PRICE_DISPLAY.get(field, field)}: {old_val} → {new_val}")

        # Update tracker if changed
        if changes and not dry_run:
            log("\n=== Writing new prices row to tracker_data.json ===")
            new_price_row = {"date": today.strftime("%Y-%m-%d")}
            new_price_row.update(scraped)
            tracker["prices"].append(new_price_row)
            TRACKER_DATA_PATH.write_text(json.dumps(tracker, indent=2))
            log("  ✅ tracker_data.json updated")

            # Rebuild data.json
            log("=== Rebuilding data.json ===")
            result = subprocess.run(
                [sys.executable, str(BUILD_DATA_JSON),
                 "--tracker-data", str(TRACKER_DATA_PATH),
                 "--output", str(DATA_JSON_PATH)],
                capture_output=True, text=True, timeout=60
            )
            out = json.loads(result.stdout)
            if not out.get("success"):
                raise RuntimeError(f"build_data_json.py failed: {out.get('error')}")
            log("  ✅ data.json rebuilt")

            # Git push
            log("=== Git: pushing price update ===")
            try:
                git_commit_and_push(
                    f"Price update {today.strftime('%Y-%m-%d')} — "
                    f"{len(changes)} change(s)"
                )
            except RuntimeError as e:
                log(f"  Git push failed (non-fatal): {e}")
        elif changes and dry_run:
            log("  [DRY RUN] Would write new price row and push to GitHub")
        else:
            log("  No changes — tracker_data.json not modified")

        # Build and send email
        log("\n=== Building and sending price check email ===")
        html_body = build_email_html(today, scraped, last_row, changes)

        month_folder = REPO_ROOT / today.strftime("%B %Y")
        month_folder.mkdir(exist_ok=True)
        html_path = month_folder / f"Buffalo Trace Price Check - {today.strftime('%b %d %Y')}.html"
        html_path.write_text(html_body)
        log(f"  HTML saved: {html_path.name}")

        if not dry_run and resend_api_key:
            msg = email.message.EmailMessage()
            msg["Subject"] = f"Buffalo Trace Price Check — {today.strftime('%A, %B %-d, %Y')}"
            msg["From"]    = REPORT_FROM
            msg["To"]      = REPORT_TO
            msg.set_content("This email requires an HTML-capable client.")
            msg.add_alternative(html_body, subtype="html")
            smtp_send_with_retry(msg, resend_api_key)
            log(f"  Email sent to {REPORT_TO}")
        else:
            log("  [DRY RUN or no API key] Email skipped")

        # SMS
        log("\n=== Sending SMS ===")
        if changes:
            sms_body = f"⚠️ BT Price Check {today_str}: {len(changes)} change(s). Details emailed."
        else:
            sms_body = f"✅ BT Price Check {today_str}: No price changes."

        if not dry_run:
            send_sms_safe(sms_body)
        else:
            log(f"  [DRY RUN] SMS would be: {sms_body}")

        log("\n=== Monthly Price Check complete ===")

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            send_sms_safe(
                f"❌ BT Price Check {today_str}: Task failed — "
                f"{type(e).__name__}: {str(e)[:60]}."
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()

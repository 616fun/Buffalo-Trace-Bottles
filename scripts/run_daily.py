#!/usr/bin/env python3
"""
run_daily.py — GitHub Actions orchestrator for Buffalo Trace daily report.

Pipeline (mirrors CLAUDE.md Canonical Daily Task Procedure):
  0. Holiday check
  1. Scrape availability          (subprocess → scrape_availability.py)
  2. Update tracker_data.json     (subprocess → update_tracker_data.py)
  3. Build data.json              (subprocess → build_data_json.py)
  4. Git commit + push
  5. Send HTML email report       (Resend SMTP)
  6. Send SMS notification        (Twilio)

Closure days follow Steps C1–C9 from CLAUDE.md (skip scrape, all bottles=0).

Environment variables (GitHub Secrets):
  RESEND_API_KEY          — Resend API key
  REPORT_FROM_EMAIL       — Optional, default: "Buffalo Trace Daily <drops@buffalotracebottledrops.com>"
  REPORT_TO_EMAIL         — Optional, default: "brianwulff@yahoo.com"
  TWILIO_ACCOUNT_SID      — Twilio Account SID
  TWILIO_AUTH_TOKEN       — Twilio Auth Token
  TWILIO_FROM_NUMBER      — From phone number (E.164 format)
  TWILIO_TO_NUMBER        — To phone number (E.164 format)
  DRY_RUN                 — "true" to skip email, SMS, and git push (for testing)

Usage:
  python scripts/run_daily.py [--max-poll-minutes 180] [--dry-run] [--date YYYY-MM-DD]
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


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).parent
REPO_ROOT   = SCRIPTS_DIR.parent

TRACKER_DATA_PATH = REPO_ROOT / "tracker_data.json"
DATA_JSON_PATH    = REPO_ROOT / "data.json"

SCRAPER_SCRIPT          = SCRIPTS_DIR / "scrape_availability.py"
UPDATE_TRACKER_SCRIPT   = SCRIPTS_DIR / "update_tracker_data.py"
BUILD_DATA_JSON_SCRIPT  = SCRIPTS_DIR / "build_data_json.py"
REDDIT_SCRAPER_SCRIPT   = SCRIPTS_DIR / "scrape_reddit.py"

BOTTLE_KEYS = ["blantons", "weller107", "ehtaylor_sb", "eagle_rare"]
BOTTLE_DISPLAY = {
    "blantons":    "Blanton's Single Barrel",
    "weller107":   "Weller Antique 107",
    "ehtaylor_sb": "E.H. Taylor Small Batch",
    "eagle_rare":  "Eagle Rare 10-Year",
}
BOTTLE_SHORT = {
    "blantons":    "Bl",
    "weller107":   "W107",
    "ehtaylor_sb": "EHT",
    "eagle_rare":  "ER",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Holiday detection (Butcher's algorithm + Thanksgiving)
# ---------------------------------------------------------------------------

def easter_sunday(year: int) -> datetime.date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day   = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def thanksgiving(year: int) -> datetime.date:
    nov1 = datetime.date(year, 11, 1)
    days_to_thursday = (3 - nov1.weekday()) % 7
    return nov1 + datetime.timedelta(days=days_to_thursday + 21)


def is_gift_shop_closed(date: datetime.date) -> tuple[bool, str]:
    """Return (is_closed, holiday_name)."""
    fixed = {
        (1,  1):  "New Year's Day",
        (12, 24): "Christmas Eve",
        (12, 25): "Christmas Day",
    }
    key = (date.month, date.day)
    if key in fixed:
        return True, fixed[key]
    if date == easter_sunday(date.year):
        return True, "Easter Sunday"
    if date == thanksgiving(date.year):
        return True, "Thanksgiving Day"
    return False, ""


# ---------------------------------------------------------------------------
# Credential helpers (read from env vars)
# ---------------------------------------------------------------------------

def get_resend_creds() -> dict:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise ValueError("RESEND_API_KEY environment variable is not set")
    return {
        "api_key":    api_key,
        "from":       os.environ.get("REPORT_FROM_EMAIL",
                                     "Buffalo Trace Daily <drops@buffalotracebottledrops.com>"),
        "to":         os.environ.get("REPORT_TO_EMAIL", "brianwulff@yahoo.com"),
    }


def get_twilio_creds() -> dict:
    # TWILIO_ENABLED defaults to true; set to "false" to suppress SMS
    # (e.g. while Twilio phone number verification is pending)
    enabled_str = os.environ.get("TWILIO_ENABLED", "true").strip().lower()
    enabled = enabled_str not in ("false", "0", "no", "off")
    # Support TWILIO_TO_NUMBERS (comma-separated) or single TWILIO_TO_NUMBER
    to_numbers_str = os.environ.get("TWILIO_TO_NUMBERS", "").strip()
    if to_numbers_str:
        to_numbers = [n.strip() for n in to_numbers_str.split(",") if n.strip()]
    else:
        single = os.environ.get("TWILIO_TO_NUMBER", "").strip()
        to_numbers = [single] if single else []
    return {
        "account_sid":  os.environ.get("TWILIO_ACCOUNT_SID", "").strip(),
        "auth_token":   os.environ.get("TWILIO_AUTH_TOKEN",  "").strip(),
        "from_number":  os.environ.get("TWILIO_FROM_NUMBER", "").strip(),
        "to_numbers":   to_numbers,
        "enabled":      enabled,
    }


# ---------------------------------------------------------------------------
# SMTP retry helper (mirrors CLAUDE.md canonical helper)
# ---------------------------------------------------------------------------

def smtp_send_with_retry(msg, api_key: str, max_attempts: int = 2,
                         wait_seconds: int = 900) -> None:
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
                log(f"[SMTP] Attempt {attempt} failed: {e}. "
                    f"Waiting {wait_seconds // 60} min before retry...")
                time.sleep(wait_seconds)
            else:
                log(f"[SMTP] All {max_attempts} attempts failed.")
    raise last_exc


# ---------------------------------------------------------------------------
# Twilio SMS helper (mirrors CLAUDE.md canonical helper)
# ---------------------------------------------------------------------------

def twilio_send_sms(body: str, creds: dict,
                    max_attempts: int = 2, wait_seconds: int = 900) -> None:
    if not creds.get("enabled", True):
        log(f"[SMS] Disabled — skipping: {body[:80]}")
        return

    if not creds.get("account_sid") or not creds.get("auth_token"):
        log("[SMS] Twilio credentials not configured — skipping")
        return

    # Support to_numbers list (preferred) or legacy to_number string
    to_numbers = creds.get("to_numbers") or []
    if not to_numbers and creds.get("to_number"):
        to_numbers = [creds["to_number"]]

    url = (f"https://api.twilio.com/2010-04-01/Accounts/"
           f"{creds['account_sid']}/Messages.json")
    auth_header = base64.b64encode(
        f"{creds['account_sid']}:{creds['auth_token']}".encode()
    ).decode()

    for to_number in to_numbers:
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                payload = urllib.parse.urlencode({
                    "From": creds["from_number"],
                    "To":   to_number,
                    "Body": body,
                }).encode()
                req = urllib.request.Request(url, data=payload, method="POST")
                req.add_header("Authorization", f"Basic {auth_header}")
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
                log(f"[SMS] Sent to {to_number}: {body[:80]}")
                break  # success for this number; move to next recipient
            except urllib.error.HTTPError as e:
                err_body = e.read().decode(errors="replace")
                if 400 <= e.code < 500:
                    raise Exception(f"Twilio error {e.code}: {err_body}") from e
                last_exc = Exception(f"Twilio transient {e.code}: {err_body}")
            except Exception as e:
                last_exc = e

            if attempt < max_attempts:
                log(f"[SMS] Attempt {attempt} to {to_number} failed: {last_exc}. "
                    f"Waiting {wait_seconds // 60} min before retry...")
                time.sleep(wait_seconds)
            else:
                log(f"[SMS] All {max_attempts} attempts failed for {to_number}.")
                raise last_exc


def send_sms_safe(body: str, creds: dict) -> None:
    """Send SMS, logging but not raising on failure."""
    try:
        twilio_send_sms(body, creds)
    except Exception as exc:
        log(f"[SMS] Failed (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# Pushover push-notification helper
#   Failure / error alerts are delivered via Pushover (not Twilio) as of
#   2026-06-05. Daily / closure success notifications still use Twilio SMS.
#   Reads PUSHOVER_TOKEN / PUSHOVER_USER from the environment.
# ---------------------------------------------------------------------------

def pushover_send(title: str, body: str, priority: int = 0,
                  max_attempts: int = 2) -> None:
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


def pushover_send_safe(title: str, body: str, priority: int = 0) -> None:
    """Send a Pushover notification, logging but not raising on failure."""
    try:
        pushover_send(title, body, priority)
    except Exception as exc:
        log(f"[Pushover] Failed (non-fatal): {exc}")


def send_failure_alert(body: str) -> None:
    """Route pipeline failure / error alerts to Pushover at high priority
    (priority=1 bypasses Pushover quiet hours)."""
    pushover_send_safe("Buffalo Trace — Failure", body, priority=1)


# ---------------------------------------------------------------------------
# Reddit community intel
# ---------------------------------------------------------------------------

def fetch_reddit_posts(dry_run: bool = False) -> list:
    """
    Call scrape_reddit.py as a subprocess and return the posts list.
    Non-critical — returns [] on any failure so the pipeline is never blocked.
    """
    try:
        cmd = [sys.executable, str(REDDIT_SCRAPER_SCRIPT), "--days", "7", "--max-posts", "8"]
        if dry_run:
            cmd.append("--dry-run")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.stderr:
            log(result.stderr.strip())
        data = json.loads(result.stdout)
        if data.get("success"):
            posts = data.get("posts", [])
            log(f"  Reddit: {len(posts)} post(s) found")
            return posts
        else:
            log(f"  Reddit scrape returned success=false: {data.get('reason', 'unknown')}")
            return []
    except Exception as exc:
        log(f"  Reddit scrape failed (non-fatal): {exc}")
        return []


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def git_commit_and_push(commit_message: str, dry_run: bool = False) -> None:
    """Commit tracker_data.json + data.json and push to origin."""
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
        # Check if there are staged changes
        result = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=REPO_ROOT, capture_output=True
        )
        if result.returncode == 0:
            log("[GIT] No changes to commit — tracker already up to date")
            return
        subprocess.run(
            ["git", "commit", "-m", commit_message],
            check=True, cwd=REPO_ROOT
        )
        subprocess.run(
            ["git", "push"],
            check=True, cwd=REPO_ROOT
        )
        log(f"[GIT] Pushed: {commit_message}")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Git operation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# HTML email generator (matches existing report format exactly)
# ---------------------------------------------------------------------------

def _age_label(hours: float) -> str:
    """Human-readable age: '45m ago', '3h ago', '2d ago'."""
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    elif hours < 24:
        return f"{int(hours)}h ago"
    else:
        return f"{int(hours / 24)}d ago"


def _build_reddit_section(reddit_posts: list) -> str:
    """Return the Community Intel HTML section, or '' if no posts."""
    if not reddit_posts:
        return ""

    rows = ""
    for post in reddit_posts:
        age     = _age_label(post.get("age_hours", 0))
        sub     = post.get("subreddit", "")
        title   = post.get("title", "").replace("<", "&lt;").replace(">", "&gt;")
        url     = post.get("url", "#")
        score   = post.get("score", 0)
        n_cmts  = post.get("num_comments", 0)
        snippet = post.get("snippet", "").replace("<", "&lt;").replace(">", "&gt;")
        author  = post.get("author", "")

        snippet_html = (
            f'<br><span style="font-size: 12px; color: #666; font-style: italic;">'
            f'{snippet}'
            f'</span>'
        ) if snippet else ""

        rows += f"""
    <tr style="border-bottom: 1px solid #e8dfc8;">
        <td style="padding: 10px 12px;">
            <a href="{url}" style="color: #2c1810; font-weight: 600; font-size: 13px; text-decoration: none;"
               target="_blank">{title}</a>
            {snippet_html}
            <br>
            <span style="font-size: 11px; color: #888;">{author} · {sub} · {age} · ▲{score} · {n_cmts} comments</span>
        </td>
    </tr>"""

    return f"""
<div style="padding: 16px 32px;">
    <h2 style="color: #2c1810; font-size: 18px; border-bottom: 2px solid #d4c5a0; padding-bottom: 8px;">
        🗨️ Community Intel <span style="font-size: 13px; font-weight: normal; color: #888;">(r/bourbon · r/whiskeybuds · last 7 days)</span>
    </h2>
    <table style="width: 100%; border-collapse: collapse;">
        <tbody>{rows}
        </tbody>
    </table>
    <p style="font-size: 11px; color: #aaa; margin: 8px 0 0;">
        Unverified community reports — always confirm at the gift shop.
    </p>
</div>"""


def generate_email_html(data: dict, today: datetime.date,
                        reddit_posts: list = None) -> str:
    """Build HTML report from data.json content."""

    today_info  = data.get("today", {})
    bottles     = data.get("bottles", [])
    calendar    = data.get("calendar", [])
    meta        = data.get("meta", {})
    accuracy    = meta.get("prediction_accuracy", {})
    is_closure  = today_info.get("is_closure", False)

    date_str     = today.strftime("%B %d, %Y").replace(" 0", " ")  # "April 16, 2026"
    day_name     = today.strftime("%A")                              # "Thursday"
    last_updated = today_info.get("last_site_update") or "—"
    day_number   = meta.get("days_tracked", "?")

    # Tomorrow's predicted bottles (those with pct >= 50%)
    tomorrow      = today + datetime.timedelta(days=1)
    tomorrow_name = tomorrow.strftime("%A, %B %d").replace(" 0", " ")
    predicted = [
        b for b in bottles if (b.get("prediction_tomorrow_pct") or 0) >= 50.0
    ]
    predicted_sorted = sorted(predicted,
                               key=lambda b: b.get("prediction_tomorrow_pct", 0),
                               reverse=True)
    predicted_names = [b["name"] for b in predicted_sorted]

    if predicted_sorted:
        avg_conf = sum(b["prediction_tomorrow_pct"] for b in predicted_sorted) / len(predicted_sorted)
        prediction_block = f"""
<div style="padding: 16px 32px;">
    <div style="background: linear-gradient(135deg, #1a3a1a 0%, #2d5a2d 100%); color: white; border-radius: 8px; padding: 20px;">
        <h3 style="margin: 0 0 8px; font-size: 16px;">🔮 Tomorrow's Prediction ({tomorrow_name})</h3>
        <p style="margin: 0; font-size: 18px; font-weight: bold;">
            {', '.join(predicted_names)}
        </p>
        <p style="margin: 8px 0 0; font-size: 13px; opacity: 0.8;">
            Avg confidence: {avg_conf:.1f}% · Model: Bayesian Markov
        </p>
    </div>
</div>"""
    else:
        prediction_block = f"""
<div style="padding: 16px 32px;">
    <div style="background: linear-gradient(135deg, #3a1a1a 0%, #5a2d2d 100%); color: white; border-radius: 8px; padding: 20px;">
        <h3 style="margin: 0 0 8px; font-size: 16px;">🔮 Tomorrow's Prediction ({tomorrow_name})</h3>
        <p style="margin: 0; font-size: 18px; font-weight: bold;">None predicted (all below 50%)</p>
        <p style="margin: 8px 0 0; font-size: 13px; opacity: 0.8;">Model: Bayesian Markov</p>
    </div>
</div>"""

    # Bottle rows
    bottle_rows = ""
    for b in bottles:
        avail    = int(b.get("available_today") or 0)
        streak   = int(b.get("streak") or 0)
        s_dir    = b.get("streak_direction", "unavailable")
        s_label  = f"{streak} day{'s' if streak != 1 else ''} {s_dir}"
        overall  = b.get("overall_pct",    0)
        d10      = b.get("rolling_10d_pct", 0)
        d30      = b.get("rolling_30d_pct", 0)
        tom_pct  = b.get("prediction_tomorrow_pct", 0)
        conf     = b.get("confidence", "")

        if avail:
            status_html = (
                '<span style="display: inline-block; padding: 4px 12px; border-radius: 4px; '
                'background-color: #2d6a2d; color: white; font-weight: 600; font-size: 13px;">'
                '✅ AVAILABLE</span>'
            )
        else:
            status_html = (
                '<span style="display: inline-block; padding: 4px 12px; border-radius: 4px; '
                'background-color: #8b0000; color: white; font-weight: 600; font-size: 13px;">'
                '❌ NOT AVAILABLE</span>'
            )

        tom_color = "#2d6a2d" if tom_pct >= 50 else "#8b0000"

        bottle_rows += f"""
    <tr style="border-bottom: 1px solid #e0d5c1;">
        <td style="padding: 12px 16px; font-weight: 600; font-size: 15px;">{b['name']}</td>
        <td style="padding: 12px 16px; text-align: center;">{status_html}</td>
        <td style="padding: 12px 16px; text-align: center; font-size: 14px;">{s_label}</td>
        <td style="padding: 12px 16px; text-align: center; font-size: 14px;">{overall:.1f}%</td>
        <td style="padding: 12px 16px; text-align: center; font-size: 14px;">{d10:.1f}%</td>
        <td style="padding: 12px 16px; text-align: center; font-size: 14px;">{d30:.1f}%</td>
        <td style="padding: 12px 16px; text-align: center;">
            <span style="color: {tom_color}; font-weight: 600; font-size: 14px;">{tom_pct:.1f}%</span>
            <br><span style="font-size: 11px; color: #666;">({conf})</span>
        </td>
    </tr>"""

    availability_section = f"""
<div style="padding: 24px 32px;">
    <h2 style="color: #2c1810; font-size: 18px; border-bottom: 2px solid #d4c5a0; padding-bottom: 8px; margin-top: 0;">
        Today's Rotating Bottle Availability
    </h2>
    <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
        <thead>
            <tr style="background-color: #2c1810; color: #f5e6c8;">
                <th style="padding: 10px 16px; text-align: left;">Bottle</th>
                <th style="padding: 10px 16px; text-align: center;">Status</th>
                <th style="padding: 10px 16px; text-align: center;">Streak</th>
                <th style="padding: 10px 16px; text-align: center;">Overall</th>
                <th style="padding: 10px 16px; text-align: center;">10-Day</th>
                <th style="padding: 10px 16px; text-align: center;">30-Day</th>
                <th style="padding: 10px 16px; text-align: center;">Tomorrow</th>
            </tr>
        </thead>
        <tbody>{bottle_rows}
        </tbody>
    </table>
</div>"""

    # 7-day history
    recent = [e for e in calendar if not e.get("is_closure", False)][-7:]
    history_rows = ""
    today_str = today.strftime("%Y-%m-%d")
    for entry in recent:
        d = datetime.date.fromisoformat(entry["date"])
        label = d.strftime("%b %d (%a)")
        bold_style = "background-color: #f0e6d2; font-weight: bold;" if entry["date"] == today_str else ""
        cells = "".join(
            f'<td style="padding: 8px 12px; text-align: center;">{"✅" if entry.get(k) else "❌"}</td>'
            for k in BOTTLE_KEYS
        )
        history_rows += f"""
    <tr style="{bold_style}">
        <td style="padding: 8px 12px; font-size: 13px;">{label}</td>{cells}
    </tr>"""

    history_section = f"""
<div style="padding: 16px 32px;">
    <h2 style="color: #2c1810; font-size: 18px; border-bottom: 2px solid #d4c5a0; padding-bottom: 8px;">
        7-Day History
    </h2>
    <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
        <thead>
            <tr style="background-color: #4a2c17; color: #f5e6c8;">
                <th style="padding: 8px 12px; text-align: left;">Date</th>
                <th style="padding: 8px 12px; text-align: center;">Blanton's</th>
                <th style="padding: 8px 12px; text-align: center;">Weller 107</th>
                <th style="padding: 8px 12px; text-align: center;">EHT SB</th>
                <th style="padding: 8px 12px; text-align: center;">Eagle Rare</th>
            </tr>
        </thead>
        <tbody>{history_rows}
        </tbody>
    </table>
</div>"""

    # Community Intel (Reddit) — optional
    reddit_section = _build_reddit_section(reddit_posts or [])

    # Prediction accuracy
    total_days  = accuracy.get("total_days", 0)
    correct     = accuracy.get("correct", 0)
    partial     = accuracy.get("partial", 0)
    incorrect   = accuracy.get("incorrect", 0)
    bin_acc     = accuracy.get("binary_accuracy_pct", 0)
    bin_total   = accuracy.get("binary_total", 0)
    bin_correct = accuracy.get("binary_correct", 0)
    per_bottle  = accuracy.get("per_bottle", {})
    bl_acc  = per_bottle.get("blantons",    0)
    w_acc   = per_bottle.get("weller107",   0)
    eht_acc = per_bottle.get("ehtaylor_sb", 0)
    er_acc  = per_bottle.get("eagle_rare",  0)

    accuracy_section = f"""
<div style="padding: 16px 32px;">
    <h2 style="color: #2c1810; font-size: 18px; border-bottom: 2px solid #d4c5a0; padding-bottom: 8px;">
        Prediction Model Performance
    </h2>
    <p style="font-size: 14px; color: #444; margin: 8px 0;">
        <strong>Set-level:</strong> {correct} correct, {partial} partial, {incorrect} incorrect out of {total_days} days<br>
        <strong>Per-bottle binary accuracy:</strong> {bin_acc:.1f}% ({bin_correct}/{bin_total})<br>
        <span style="font-size: 12px; color: #888;">
            Blanton's {bl_acc:.1f}% · Weller 107 {w_acc:.1f}% ·
            EHT SB {eht_acc:.1f}% · Eagle Rare {er_acc:.1f}%
        </span>
    </p>
</div>"""

    # Closure banner (shown instead of availability if closed)
    if is_closure:
        closure_note = today_info.get("notes", "Gift shop closed")
        availability_section = f"""
<div style="padding: 24px 32px;">
    <div style="background: linear-gradient(135deg, #4a2c17 0%, #7a4c27 100%); color: #f5e6c8; border-radius: 8px; padding: 24px; text-align: center;">
        <h2 style="margin: 0 0 8px; font-size: 22px;">🔒 Gift Shop Closed</h2>
        <p style="margin: 0; font-size: 16px; opacity: 0.9;">{closure_note}</p>
        <p style="margin: 8px 0 0; font-size: 14px; opacity: 0.7;">No bottles were available today. The tracker has been updated.</p>
    </div>
</div>"""
        header_note = "Gift shop closed today"
    else:
        header_note = f"Last updated {last_updated}"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Buffalo Trace Bottle Drops — {day_name}, {date_str}</title></head>
<body style="margin: 0; padding: 0; background-color: #f5f0e8; font-family: Georgia, 'Times New Roman', serif;">
<div style="max-width: 700px; margin: 0 auto; background-color: #fffcf5; border: 1px solid #d4c5a0;">

<!-- Header -->
<div style="background: linear-gradient(135deg, #2c1810 0%, #4a2c17 100%); color: #f5e6c8; padding: 24px 32px; text-align: center;">
    <h1 style="margin: 0; font-size: 24px; letter-spacing: 1px;">🥃 Buffalo Trace Bottle Drops</h1>
    <p style="margin: 8px 0 0; font-size: 15px; color: #d4b896;">
        {day_name}, {date_str} · {header_note}
    </p>
</div>

{availability_section}

{prediction_block}

{history_section}

{accuracy_section}

{reddit_section}

<!-- Footer -->
<div style="background-color: #2c1810; color: #d4b896; padding: 16px 32px; text-align: center; font-size: 12px;">
    <p style="margin: 0;">
        Buffalo Trace Daily Tracker · Day {day_number} ·
        <a href="https://616fun.github.io/Buffalo-Trace-Bottles/" style="color: #f5e6c8;">Live Dashboard</a>
    </p>
</div>

</div>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Buffalo Trace daily report pipeline")
    parser.add_argument("--max-poll-minutes", type=int, default=180,
                        help="Max minutes to poll scraper (default 180; 240 for Sunday)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip email, SMS, and git push (safe for testing)")
    parser.add_argument("--date", default=None,
                        help="Override today's date (YYYY-MM-DD)")
    args = parser.parse_args()

    # DRY_RUN can also come from env var (set by workflow_dispatch input)
    dry_run = args.dry_run or os.environ.get("DRY_RUN", "false").lower() == "true"

    if dry_run:
        log("=== DRY RUN MODE — email, SMS, and git push will be skipped ===")

    # Determine today's date
    if args.date:
        today = datetime.date.fromisoformat(args.date)
    else:
        today = datetime.date.today()

    today_str = today.strftime("%Y-%m-%d")
    mon_dd    = today.strftime("%b %-d")  # e.g. "Apr 16"

    log(f"=== Buffalo Trace Daily Pipeline ===")
    log(f"Date: {today_str} ({today.strftime('%A')})")
    log(f"Repo root: {REPO_ROOT}")
    log(f"Dry run: {dry_run}")

    # -----------------------------------------------------------------------
    # Idempotency guard — exit cleanly if today's row already exists.
    #
    # Prevents duplicate runs when both the repository_dispatch trigger (fired
    # by the Claude dispatcher task at 7 AM) AND the cron backup both execute
    # on the same day.  The first run to finish writes the row; the second hits
    # this guard and exits without scraping, emailing, or double-appending data.
    # Skipped in dry-run mode so tests always proceed regardless.
    # -----------------------------------------------------------------------
    if not dry_run and TRACKER_DATA_PATH.exists():
        try:
            tracker = json.loads(TRACKER_DATA_PATH.read_text())
            if any(row.get("date") == today_str
                   for row in tracker.get("daily_log", [])):
                log(f"[SKIP] Row for {today_str} already exists in "
                    f"tracker_data.json — a previous run completed successfully.")
                log(f"[SKIP] Exiting cleanly. No scrape, no email, no duplicate data.")
                sys.exit(0)
        except Exception as e:
            log(f"[WARN] Could not check idempotency guard: {e} — proceeding normally.")

    # Load Twilio creds early so we can send failure SMSes
    try:
        sms_creds = get_twilio_creds()
    except Exception:
        sms_creds = {}

    # -----------------------------------------------------------------------
    # Step 0 — Holiday check
    # -----------------------------------------------------------------------
    log("\n=== Step 0: Holiday check ===")
    closed, holiday_name = is_gift_shop_closed(today)

    if closed:
        log(f"  CLOSED today: {holiday_name}")
        _run_closure_day(today, today_str, mon_dd, holiday_name, dry_run, sms_creds)
        return

    log(f"  Open — proceeding with normal pipeline")

    # -----------------------------------------------------------------------
    # Step 1 — Scrape availability
    # -----------------------------------------------------------------------
    log(f"\n=== Step 1: Scrape (max {args.max_poll_minutes} min) ===")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRAPER_SCRIPT),
             "--max-poll-minutes", str(args.max_poll_minutes)],
            capture_output=True, text=True, timeout=14400  # 4 hours
        )
        scrape_data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        msg = f"❌ BT {mon_dd}: Task crashed at scrape — subprocess timeout. Check logs."
        send_failure_alert(msg)
        sys.exit(1)
    except (json.JSONDecodeError, Exception) as exc:
        msg = f"❌ BT {mon_dd}: Task crashed at scrape — {str(exc)[:60]}. Check logs."
        send_failure_alert(msg)
        raise

    if not scrape_data.get("success"):
        reason = scrape_data.get("reason", "unknown")
        polls  = scrape_data.get("polls", 0)
        log(f"  Scrape failed: {reason} (polls={polls})")
        n_min = args.max_poll_minutes
        msg = f"⚠️ BT {mon_dd}: Site still stale after {n_min}min poll. No row written today."
        send_failure_alert(msg)
        sys.exit(1)

    scrape_data["date"] = today_str
    log(f"  Scrape OK — blantons={scrape_data['blantons']} weller107={scrape_data['weller107']} "
        f"ehtaylor_sb={scrape_data['ehtaylor_sb']} eagle_rare={scrape_data['eagle_rare']} "
        f"last_site_update={scrape_data.get('last_site_update')} polls={scrape_data.get('polls')}")

    # -----------------------------------------------------------------------
    # Step 2 — Update tracker_data.json
    # -----------------------------------------------------------------------
    log("\n=== Step 2: Update tracker_data.json ===")
    try:
        cmd = [sys.executable, str(UPDATE_TRACKER_SCRIPT),
               "--tracker-data", str(TRACKER_DATA_PATH),
               "--data", json.dumps(scrape_data)]
        if dry_run:
            cmd.append("--dry-run")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = json.loads(result.stdout)
        if result.stderr:
            log(result.stderr)
    except Exception as exc:
        msg = f"❌ BT {mon_dd}: STOPPED — spreadsheet write failed. No downstream steps ran."
        send_failure_alert(msg)
        raise

    if not output.get("success"):
        err = output.get("error", "unknown")
        log(f"  Update failed: {err}")
        msg = f"❌ BT {mon_dd}: STOPPED — spreadsheet write failed. No downstream steps ran."
        send_failure_alert(msg)
        sys.exit(1)

    log("  tracker_data.json updated OK")

    # -----------------------------------------------------------------------
    # Step 3 — Build data.json
    # -----------------------------------------------------------------------
    log("\n=== Step 3: Build data.json ===")
    try:
        cmd = [sys.executable, str(BUILD_DATA_JSON_SCRIPT),
               "--tracker-data", str(TRACKER_DATA_PATH),
               "--output", str(DATA_JSON_PATH)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = json.loads(result.stdout)
        if result.stderr:
            log(result.stderr)
    except Exception as exc:
        msg = f"❌ BT {mon_dd}: data.json failed. Spreadsheet OK. GitHub/site NOT updated."
        send_failure_alert(msg)
        raise

    if not output.get("success"):
        err = output.get("error", "unknown")
        log(f"  data.json failed: {err}")
        msg = f"❌ BT {mon_dd}: data.json failed. Spreadsheet OK. GitHub/site NOT updated."
        send_failure_alert(msg)
        sys.exit(1)

    log("  data.json built OK")

    # Load data.json for email / SMS content
    with open(DATA_JSON_PATH) as f:
        data = json.load(f)

    # -----------------------------------------------------------------------
    # Step 4 — Git commit + push
    # -----------------------------------------------------------------------
    log("\n=== Step 4: Git commit + push ===")
    avail_names = [BOTTLE_DISPLAY[k] for k in BOTTLE_KEYS
                   if int(scrape_data.get(k) or 0) == 1]
    commit_msg = (f"Daily update {today_str} — "
                  + (", ".join(avail_names) if avail_names else "none available"))
    try:
        git_commit_and_push(commit_msg, dry_run=dry_run)
    except RuntimeError as exc:
        log(f"  Git push failed: {exc}")
        msg = f"⚠️ BT {mon_dd}: GitHub push failed. Spreadsheet+data.json OK. Site may be stale."
        send_failure_alert(msg)
        # Non-fatal — continue to email + SMS

    # -----------------------------------------------------------------------
    # Step 4.5 — Fetch Reddit community intel (non-critical)
    # -----------------------------------------------------------------------
    log("\n=== Step 4.5: Reddit community intel ===")
    reddit_posts = fetch_reddit_posts(dry_run=dry_run)

    # -----------------------------------------------------------------------
    # Step 5 — Send HTML email
    # -----------------------------------------------------------------------
    log("\n=== Step 5: Send email ===")
    try:
        resend_creds = get_resend_creds()
        html_body    = generate_email_html(data, today, reddit_posts=reddit_posts)

        msg = email.message.EmailMessage()
        msg["Subject"] = f"Buffalo Trace Bottle Drops — {today.strftime('%A, %B %-d, %Y')}"
        msg["From"]    = resend_creds["from"]
        msg["To"]      = resend_creds["to"]
        msg.set_content("This email requires an HTML-capable client.")
        msg.add_alternative(html_body, subtype="html")

        if not dry_run:
            smtp_send_with_retry(msg, resend_creds["api_key"])
            log(f"  Email sent to {resend_creds['to']}")
        else:
            log("  [DRY RUN] Email skipped")
    except Exception as exc:
        log(f"  Email failed: {exc}")
        fail_msg = f"⚠️ BT {mon_dd}: Report email failed. Data steps completed OK."
        send_failure_alert(fail_msg)
        # Non-fatal — continue to SMS

    # -----------------------------------------------------------------------
    # Step 6 — Send SMS
    # -----------------------------------------------------------------------
    log("\n=== Step 6: Send SMS ===")
    bottles_status = " ".join(
        f"{BOTTLE_SHORT[k]}:{'✓' if int(scrape_data.get(k) or 0) else '✗'}"
        for k in BOTTLE_KEYS
    )
    # Tomorrow's predictions
    predicted_keys = sorted(
        [b["key"] for b in data.get("bottles", [])
         if (b.get("prediction_tomorrow_pct") or 0) >= 50.0],
        key=lambda k: next(b["prediction_tomorrow_pct"] for b in data["bottles"] if b["key"] == k),
        reverse=True
    )
    pred_labels = [BOTTLE_SHORT[k] for k in predicted_keys] if predicted_keys else ["None"]
    sms_body = (f"✅ BT {mon_dd}: {bottles_status} | "
                f"Tomorrow's Prediction(s): {', '.join(pred_labels)} | "
                f"https://buffalotracebottledrops.com")

    if not dry_run:
        send_sms_safe(sms_body, sms_creds)
    else:
        log(f"  [DRY RUN] SMS would be: {sms_body}")

    log("\n=== Pipeline complete ===")


# ---------------------------------------------------------------------------
# Closure day pipeline
# ---------------------------------------------------------------------------

def _run_closure_day(today: datetime.date, today_str: str, mon_dd: str,
                     holiday_name: str, dry_run: bool, sms_creds: dict) -> None:
    """Execute closure day procedure (Steps C1–C9)."""

    log(f"  Running closure day procedure for: {holiday_name}")

    # Step C1–C5: Update tracker_data.json with --closure flag
    log("\n=== Step C2: Update tracker_data.json (closure) ===")
    try:
        cmd = [sys.executable, str(UPDATE_TRACKER_SCRIPT),
               "--tracker-data", str(TRACKER_DATA_PATH),
               "--data", json.dumps({"date": today_str}),
               "--closure", holiday_name]
        if dry_run:
            cmd.append("--dry-run")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = json.loads(result.stdout)
        if result.stderr:
            log(result.stderr)
    except Exception as exc:
        msg = f"❌ BT {mon_dd}: STOPPED — spreadsheet write failed. No downstream steps ran."
        send_failure_alert(msg)
        raise

    if not output.get("success"):
        err = output.get("error", "unknown")
        log(f"  Closure update failed: {err}")
        msg = f"❌ BT {mon_dd}: STOPPED — spreadsheet write failed. No downstream steps ran."
        send_failure_alert(msg)
        sys.exit(1)

    log("  tracker_data.json updated (closure)")

    # Step C6: Build data.json
    log("\n=== Step C3: Build data.json (closure) ===")
    try:
        cmd = [sys.executable, str(BUILD_DATA_JSON_SCRIPT),
               "--tracker-data", str(TRACKER_DATA_PATH),
               "--output", str(DATA_JSON_PATH)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = json.loads(result.stdout)
        if result.stderr:
            log(result.stderr)
    except Exception as exc:
        msg = f"❌ BT {mon_dd}: data.json failed. Spreadsheet OK. GitHub/site NOT updated."
        send_failure_alert(msg)
        raise

    if not output.get("success"):
        err = output.get("error", "unknown")
        log(f"  data.json failed: {err}")
        msg = f"❌ BT {mon_dd}: data.json failed. Spreadsheet OK. GitHub/site NOT updated."
        send_failure_alert(msg)
        sys.exit(1)

    log("  data.json built OK (closure)")

    with open(DATA_JSON_PATH) as f:
        data = json.load(f)

    # Step C7: Git push
    log("\n=== Step C4: Git commit + push (closure) ===")
    commit_msg = f"Daily update {today_str} — Gift shop closed ({holiday_name})"
    try:
        git_commit_and_push(commit_msg, dry_run=dry_run)
    except RuntimeError as exc:
        log(f"  Git push failed: {exc}")
        msg = f"⚠️ BT {mon_dd}: GitHub push failed. Spreadsheet+data.json OK. Site may be stale."
        send_failure_alert(msg)

    # Step C7.5: Reddit community intel (non-critical)
    log("\n=== Step C4.5: Reddit community intel ===")
    reddit_posts = fetch_reddit_posts(dry_run=dry_run)

    # Step C8: Closure email
    log("\n=== Step C5: Send closure email ===")
    try:
        resend_creds = get_resend_creds()
        html_body    = generate_email_html(data, today, reddit_posts=reddit_posts)

        msg = email.message.EmailMessage()
        msg["Subject"] = f"Buffalo Trace Bottle Drops — {today.strftime('%A, %B %-d, %Y')}"
        msg["From"]    = resend_creds["from"]
        msg["To"]      = resend_creds["to"]
        msg.set_content("This email requires an HTML-capable client.")
        msg.add_alternative(html_body, subtype="html")

        if not dry_run:
            smtp_send_with_retry(msg, resend_creds["api_key"])
            log(f"  Closure email sent to {resend_creds['to']}")
        else:
            log("  [DRY RUN] Closure email skipped")
    except Exception as exc:
        log(f"  Closure email failed: {exc}")
        fail_msg = f"⚠️ BT {mon_dd}: Report email failed. Data steps completed OK."
        send_failure_alert(fail_msg)

    # Step C9: Closure SMS
    log("\n=== Step C6: Send closure SMS ===")
    # Tomorrow's prediction from data.json
    predicted_keys = sorted(
        [b["key"] for b in data.get("bottles", [])
         if (b.get("prediction_tomorrow_pct") or 0) >= 50.0],
        key=lambda k: next(b["prediction_tomorrow_pct"] for b in data["bottles"] if b["key"] == k),
        reverse=True
    )
    pred_labels = [BOTTLE_SHORT[k] for k in predicted_keys] if predicted_keys else ["None"]
    sms_body = (f"ℹ️ BT {mon_dd}: Closed ({holiday_name}). "
                f"Tomorrow's Prediction(s): {', '.join(pred_labels)} | "
                f"https://buffalotracebottledrops.com")

    if not dry_run:
        send_sms_safe(sms_body, sms_creds)
    else:
        log(f"  [DRY RUN] SMS would be: {sms_body}")

    log("\n=== Closure pipeline complete ===")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    today_fallback = datetime.date.today()
    mon_dd_fallback = today_fallback.strftime("%b %-d")
    try:
        sms_creds_fallback = get_twilio_creds()
    except Exception:
        sms_creds_fallback = {}

    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
        step = "unknown step"
        err_short = str(exc)[:60]
        fail_msg = f"❌ BT {mon_dd_fallback}: Task crashed at {step} — {err_short}. Check logs."
        send_failure_alert(fail_msg)
        sys.exit(1)

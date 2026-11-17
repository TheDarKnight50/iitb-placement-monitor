#!/usr/bin/env python3
import os
import re
import json
import base64
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from common import (get_gspread_client, parse_jwt, send_notifications, send_discord_embed,
                    desktop_notify, check_webhook, mask_webhook)
from job_changes import diff_jobs, format_changes, is_important
from deadlines import compute_deadline_alerts, format_items as format_deadline_items
from calendar_export import build_ics, placement_events

# Load environment configuration (prefer config.env, fallback to .env)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_ENV_FILE = os.path.join(BASE_DIR, "config.env")
ENV_FILE = os.path.join(BASE_DIR, ".env")

if os.path.exists(CONFIG_ENV_FILE):
    load_dotenv(CONFIG_ENV_FILE)
elif os.path.exists(ENV_FILE):
    load_dotenv(ENV_FILE)

TOKEN_FILE = os.path.join(BASE_DIR, "token.txt")
USER_DATA_DIR = os.path.join(BASE_DIR, "chrome_data")
KNOWN_JOBS_FILE = os.path.join(BASE_DIR, "known_jobs.json")
KNOWN_BLOGS_FILE = os.path.join(BASE_DIR, "known_blogs.json")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
# Channel-specific webhooks (fall back to the generic webhook if not set)
DISCORD_WEBHOOK_NEW_JAFS = os.getenv("DISCORD_WEBHOOK_NEW_JAFS", "").strip() or DISCORD_WEBHOOK_URL
DISCORD_WEBHOOK_TESTS = os.getenv("DISCORD_WEBHOOK_TESTS", "").strip() or DISCORD_WEBHOOK_URL
DISCORD_WEBHOOK_REMINDERS = os.getenv("DISCORD_WEBHOOK_REMINDERS", "").strip() or DISCORD_WEBHOOK_URL
DISCORD_WEBHOOK_FORMS = os.getenv("DISCORD_WEBHOOK_FORMS", "").strip() or DISCORD_WEBHOOK_URL
DISCORD_WEBHOOK_SHORTLISTS = os.getenv("DISCORD_WEBHOOK_SHORTLISTS", "").strip() or DISCORD_WEBHOOK_URL
# Deadline digests/warnings: own channel if set, otherwise the reminders channel
DISCORD_WEBHOOK_DEADLINES = os.getenv("DISCORD_WEBHOOK_DEADLINES", "").strip() or DISCORD_WEBHOOK_REMINDERS
# Non-test blog posts: own channel if set (keeps the general channel quiet), otherwise generic
DISCORD_WEBHOOK_BLOG = os.getenv("DISCORD_WEBHOOK_BLOG", "").strip() or DISCORD_WEBHOOK_URL
# Bot health problems (expired tokens, dead webhooks): own channel if set, otherwise generic
DISCORD_WEBHOOK_ALERTS = os.getenv("DISCORD_WEBHOOK_ALERTS", "").strip() or DISCORD_WEBHOOK_URL

def _int_env(name, default):
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default

DEADLINE_WARN_HOURS = _int_env("DEADLINE_WARN_HOURS", 3)
DEADLINE_DIGEST_HOUR = _int_env("DEADLINE_DIGEST_HOUR", 9)        # IST, 0-23
DEADLINE_DIGEST_WINDOW_HOURS = _int_env("DEADLINE_DIGEST_WINDOW_HOURS", 48)
ALERT_ON_JOB_CHANGES = os.getenv("ALERT_ON_JOB_CHANGES", "true").strip().lower() not in ("0", "false", "no")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1O96KAd3FrBUikER4fo3IkrLcibPjVnZcXrIziUYKKHY")
TARGET_PROGRAM = os.getenv("TARGET_PROGRAM", "B.Tech.")
SHEET_ALL_JOBS = "All Jobs"
SHEET_SIGNED_JOBS = "Signed JAFs"
SHEET_SUMMARY = "Summary"

BASE_API = "https://campus.placements.iitb.ac.in/api/v1"
BLOG_API = "https://gymkhana.iitb.ac.in/instiapp/api/placement-blog"
INSTI_TOKEN_FILE = os.path.join(BASE_DIR, "insti_token.txt")
TESTS_FILE = os.path.join(BASE_DIR, "tests.json")
FORMS_FILE = os.path.join(BASE_DIR, "forms.json")
DAILY_CLOSING_STATE_FILE = os.path.join(BASE_DIR, ".daily_closing_state")
DEADLINE_STATE_FILE = os.path.join(BASE_DIR, "deadline_state.json")
HEALTH_STATE_FILE = os.path.join(BASE_DIR, "health_state.json")
CALENDAR_FILE = os.path.join(BASE_DIR, os.getenv("CALENDAR_FILE", "placements.ics"))

# Cache currency conversion rates to INR
RATES_TO_INR = {
    "INR": 1.0,
    "USD": 88.0,
    "JPY": 0.58,
    "EUR": 95.0,
    "GBP": 110.0,
    "SGD": 65.0,
    "AED": 24.0,
    "CAD": 62.0
}

def update_exchange_rates():
    """Fetches live exchange rates relative to INR."""
    global RATES_TO_INR
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        if r.status_code == 200:
            rates = r.json().get("rates", {})
            usd_inr = rates.get("INR", 85.0)
            for cur, val in rates.items():
                if val > 0:
                    RATES_TO_INR[cur] = usd_inr / val
    except Exception as e:
        print(f"[!] Using fallback currency exchange rates: {e}")

update_exchange_rates()

PROGRAMS_MAP = {}

def is_token_valid(token):
    """Checks JWT expiry (with a 1-hour buffer). Unparseable tokens are passed through."""
    payload = parse_jwt(token)
    exp = payload.get("exp")
    if not exp:
        return True
    return exp > (datetime.now(timezone.utc).timestamp() + 3600)

def ensure_jwt_token():
    """Returns a valid (non-expired) JWT: env -> token.txt -> refresh from browser session."""
    candidates = []
    env_token = os.getenv("PORTAL_TOKEN", "").strip()
    if env_token:
        candidates.append(env_token)
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            file_token = f.read().strip()
        if file_token:
            candidates.append(file_token)

    for token in candidates:
        if is_token_valid(token):
            return token

    if os.path.exists(USER_DATA_DIR):
        print("[*] JWT expired/missing - attempting refresh from persistent browser session...")
        try:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=USER_DATA_DIR,
                    headless=True
                )
                page = context.new_page()
                page.goto("https://campus.placements.iitb.ac.in/applicant/home", wait_until="networkidle", timeout=30000)
                token = page.evaluate("() => localStorage.getItem('token')")
                context.close()

                if token and is_token_valid(token.strip()):
                    with open(TOKEN_FILE, "w") as f:
                        f.write(token.strip())
                    print("[+] JWT refreshed and saved from browser session.")
                    return token.strip()
        except Exception as e:
            print(f"[!] Warning on auto-refresh via browser: {e}")

    return candidates[0] if candidates else None

def load_programs(headers):
    """Loads degree/program mappings from the portal."""
    global PROGRAMS_MAP
    if PROGRAMS_MAP:
        return PROGRAMS_MAP
    try:
        r = requests.get(f"{BASE_API}/program", headers=headers, timeout=10)
        if r.status_code == 200:
            for p in r.json():
                PROGRAMS_MAP[p["id"]] = p.get("name") or p.get("description", f"Prog {p['id']}")
    except Exception as e:
        print(f"[!] Could not load programs: {e}")
    return PROGRAMS_MAP

def get_btech_program_id(prog_map):
    """Returns programId corresponding to B.Tech."""
    for pid, name in prog_map.items():
        if "B.Tech" in name or name.strip() == TARGET_PROGRAM:
            return pid
    return 1

def ordinal(n):
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

def format_date_clean(utc_str):
    """Converts UTC ISO date string into clean '26th Oct' format in IST."""
    if not utc_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        ist_dt = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
        return f"{ordinal(ist_dt.day)} {ist_dt.strftime('%b')}"
    except Exception:
        return utc_str

def format_inr(amount_inr):
    """Formats INR amount cleanly as LPA or Cr."""
    if not amount_inr or amount_inr <= 0:
        return "N/A"
    lakhs = amount_inr / 100000.0
    if lakhs >= 100:
        crores = lakhs / 100.0
        return f"₹{crores:.2f} Cr"
    return f"₹{lakhs:.2f} LPA"

def to_inr_value(amount, currency):
    """Numeric INR value (for sorting/stats). 0 when unknown."""
    try:
        return float(amount or 0) * RATES_TO_INR.get((currency or "INR").upper(), 1.0)
    except (TypeError, ValueError):
        return 0.0

def convert_to_inr_formatted(amount, currency):
    """Converts amount in any foreign currency to INR and returns formatted string."""
    if not amount:
        return "N/A"
    try:
        amt = float(amount)
        if amt <= 0:
            return "N/A"
        cur = (currency or "INR").upper()
        rate = RATES_TO_INR.get(cur, 1.0)
        inr_val = amt * rate
        
        if cur != "INR":
            return f"{format_inr(inr_val)} ({cur} {int(amt):,})"
        return format_inr(inr_val)
    except Exception:
        return "N/A"

def fetch_single_job_salary(args):
    """Fetches detailed salary distribution + selection processes for a job ID."""
    job_id, headers = args
    try:
        r = requests.get(f"{BASE_API}/job/{job_id}", headers=headers, timeout=10)
        if r.status_code == 200:
            d = r.json()
            processes = d.get("processes") or []
            test_names = [p.get("name", "") for p in processes
                          if p.get("name") and "test" in p.get("name", "").lower()]
            return job_id, d.get("salaries", []), d.get("currency", "INR"), test_names
    except Exception:
        pass
    return job_id, [], "INR", []

def fetch_jobs_data(token):
    """Fetches jobs, resolves B.Tech Base Pay & CTC in INR, eligibility, and signed status."""
    headers = {"Authorization": f"Bearer {token}"}
    payload = parse_jwt(token)
    
    applicant_id = payload.get("id")
    service = payload.get("service", "placement")
    year = str(payload.get("batch", "2026"))
    season = payload.get("season", "autumn")
    
    prog_map = load_programs(headers)
    btech_prog_id = get_btech_program_id(prog_map)
    
    params = {
        "sendEligibility": "true",
        "service": service,
        "year": year,
        "season": season
    }
    
    print("[*] Fetching all jobs from placement portal...")
    r_jobs = requests.get(f"{BASE_API}/job", headers=headers, params=params, timeout=20)
    if r_jobs.status_code != 200:
        raise RuntimeError(f"Failed to fetch jobs API: {r_jobs.status_code} {r_jobs.text}")
    all_jobs = r_jobs.json()
    
    # Sort chronologically by opensAt descending (newest jobs first)
    all_jobs = sorted(all_jobs, key=lambda x: x.get("opensAt") or "", reverse=True)
    
    signed_job_ids = set()
    signed_apps_map = {}
    if applicant_id:
        try:
            r_apps = requests.get(f"{BASE_API}/job/application", headers=headers, params={"applicantId": applicant_id}, timeout=20)
            if r_apps.status_code == 200:
                apps = r_apps.json()
                for app in apps:
                    jid = app.get("jobId") or (app.get("job", {}).get("id"))
                    if jid:
                        signed_job_ids.add(jid)
                        signed_apps_map[jid] = app
        except Exception as e:
            print(f"[!] Warning fetching job applications: {e}")

    job_ids = [j["id"] for j in all_jobs if "id" in j]
    eligibility_map = {}
    batch_size = 100
    for i in range(0, len(job_ids), batch_size):
        chunk = job_ids[i:i + batch_size]
        try:
            r_elig = requests.get(
                f"{BASE_API}/job/sign-eligibility",
                headers=headers,
                params={"jobIds": ",".join(map(str, chunk))},
                timeout=20
            )
            if r_elig.status_code == 200:
                for item in r_elig.json():
                    eligibility_map[item.get("jobId")] = item
        except Exception as e:
            print(f"[!] Warning fetching sign eligibility batch: {e}")

    print(f"[*] Fetching compensation details for {len(job_ids)} jobs in parallel...")
    job_salary_map = {}
    job_tests_map = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(fetch_single_job_salary, [(jid, headers) for jid in job_ids])
        for jid, salaries, currency, test_names in results:
            job_salary_map[jid] = (salaries, currency)
            if test_names:
                job_tests_map[jid] = test_names

    processed_jobs = []
    for j in all_jobs:
        job_id = j.get("id")
        company_name = j.get("company", {}).get("name", "Unknown Company")
        title = j.get("title", "Unknown Role")
        
        last_day_str = format_date_clean(j.get("closesAt"))

        elig_info = eligibility_map.get(job_id)
        is_eligible = elig_info.get("eligible", False) if elig_info else bool(j.get("eligible", False))

        # Salary resolution
        salaries, currency = job_salary_map.get(job_id, ([], "INR"))
        base_pay_str = "N/A"
        ctc_str = "N/A"
        raw_base = raw_ctc = None

        if salaries:
            if is_eligible:
                # 1. If eligible, find exact B.Tech program salary entry
                btech_sals = [s for s in salaries if s.get("programId") == btech_prog_id]
                chosen_sals = btech_sals if btech_sals else salaries

                # For eligible jobs, collapse multiple options → pick the highest CTC as single value
                best_sal = max(chosen_sals, key=lambda s: float(s.get("ctc") or s.get("amount") or 0))
                raw_base = best_sal.get("amount")
                raw_ctc  = best_sal.get("ctc")

                base_pay_str = convert_to_inr_formatted(raw_base, currency) if raw_base and float(raw_base) > 0 else "N/A"
                ctc_str      = convert_to_inr_formatted(raw_ctc,  currency) if raw_ctc  and float(raw_ctc)  > 0 else base_pay_str
            else:
                # 2. If ineligible, find the HIGHEST paying salary offer across ALL programs
                best_sal = max(salaries, key=lambda s: float(s.get("ctc") or s.get("amount") or 0))
                raw_base = best_sal.get("amount")
                raw_ctc  = best_sal.get("ctc")

                base_pay_str = convert_to_inr_formatted(raw_base, currency) if raw_base and float(raw_base) > 0 else "N/A"
                ctc_str      = convert_to_inr_formatted(raw_ctc,  currency) if raw_ctc  and float(raw_ctc)  > 0 else base_pay_str
                # Last resort fallback: if still N/A, use whatever non-zero field exists
                if ctc_str == "N/A" and base_pay_str == "N/A":
                    for s in sorted(salaries, key=lambda s: float(s.get("ctc") or s.get("amount") or 0), reverse=True):
                        amt = s.get("ctc") or s.get("amount")
                        if amt and float(amt) > 0:
                            ctc_str = convert_to_inr_formatted(amt, currency)
                            base_pay_str = ctc_str
                            raw_ctc = amt
                            break

        has_signed = job_id in signed_job_ids
        signed_at_time = signed_apps_map.get(job_id, {}).get("createdAt", "")

        processed_jobs.append({
            "id": job_id,
            "company": company_name,
            "role": title,
            "base_pay": base_pay_str,
            "ctc": ctc_str,
            "last_day": last_day_str,
            "closes_at": j.get("closesAt") or "",
            "opens_at": j.get("opensAt", ""),
            # Raw (unconverted) salary, so exchange-rate moves don't look like CTC changes
            "salary_key": f"{currency} {raw_base or 0} {raw_ctc or 0}",
            "ctc_inr": round(to_inr_value(raw_ctc or raw_base, currency)),
            "signed_at": signed_at_time,
            "eligible": "Eligible" if is_eligible else "Not Eligible",
            "signed": "Yes" if has_signed else "No",
            "tests": job_tests_map.get(job_id, [])
        })

    return processed_jobs

def apply_sheet_formatting_and_colors(sh, worksheet, num_rows):
    """
    Applies whole-row conditional formatting on a freshly created sheet.
    Sheet is always recreated before this is called - no cleanup needed.
    """
    sheet_id = worksheet.id

    # Now add clean rules (Column F is index 5: "Eligible or Not", Column G is index 6: "Have I Signed")
    rules = [
        # 1. Header row design
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.12, "green": 0.22, "blue": 0.38},
                        "horizontalAlignment": "CENTER",
                        "textFormat": {"foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "fontSize": 10, "bold": True}
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
            }
        },
        # 2. Entire Row Green if Eligible ($F2 = "Eligible") -> Distinct Soft Mint Green (#D4EDDA)
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": num_rows, "startColumnIndex": 0, "endColumnIndex": 8}],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": '=$F2="Eligible"'}]
                        },
                        "format": {
                            "backgroundColor": {"red": 0.83, "green": 0.93, "blue": 0.85}
                        }
                    }
                },
                "index": 0
            }
        },
        # 3. Entire Row Red if Not Eligible ($F2 = "Not Eligible") -> Distinct Soft Rose Red (#F8D7DA)
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": num_rows, "startColumnIndex": 0, "endColumnIndex": 8}],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": '=$F2="Not Eligible"'}]
                        },
                        "format": {
                            "backgroundColor": {"red": 0.97, "green": 0.84, "blue": 0.85}
                        }
                    }
                },
                "index": 1
            }
        },
        # 4. Highlight Signed column "Yes" with bold blue badge (#CCE5FF)
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": num_rows, "startColumnIndex": 6, "endColumnIndex": 7}],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": "Yes"}]
                        },
                        "format": {
                            "backgroundColor": {"red": 0.75, "green": 0.88, "blue": 1.0},
                            "textFormat": {"foregroundColor": {"red": 0.05, "green": 0.28, "blue": 0.70}, "bold": True}
                        }
                    }
                },
                "index": 2
            }
        },
        # 5. Column sizing: sensible fixed width for 'Roles' (col index 2), auto for rest
        {
            "autoResizeDimensions": {
                "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 8}
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 2,
                    "endIndex": 3
                },
                "properties": {
                    "pixelSize": 180
                },
                "fields": "pixelSize"
            }
        }
    ]
    try:
        sh.batch_update({"requests": rules})
    except Exception as e:
        print(f"[!] Warning applying styles: {e}")


def update_google_sheet(jobs):
    """Updates 'All Jobs' and 'Signed JAFs' by clearing and rewriting data each run."""
    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    headers = ["Sr No", "Company Name", "Roles", "Base Pay (INR)", "CTC (INR)", "Eligible or Not", "Have I Signed", "Last Date to Sign"]

    # 1. Update All Jobs sheet
    try:
        ws_all = sh.worksheet(SHEET_ALL_JOBS)
    except Exception:
        ws_all = sh.add_worksheet(title=SHEET_ALL_JOBS, rows="500", cols="10")

    rows_all = [headers]
    for idx, j in enumerate(jobs, start=1):
        rows_all.append([idx, j["company"], j["role"], j["base_pay"], j["ctc"], j["eligible"], j["signed"], j["last_day"]])

    ws_all.clear()
    ws_all.update(values=rows_all, range_name="A1")
    apply_sheet_formatting_and_colors(sh, ws_all, len(rows_all))
    print(f"[+] '{SHEET_ALL_JOBS}' updated with {len(jobs)} jobs.")

    # 2. Update Signed JAFs sheet
    signed_jobs = sorted(
        [j for j in jobs if j["signed"] == "Yes"],
        key=lambda x: x.get("signed_at") or x.get("opens_at") or "",
        reverse=True
    )
    try:
        ws_signed = sh.worksheet(SHEET_SIGNED_JOBS)
    except Exception:
        ws_signed = sh.add_worksheet(title=SHEET_SIGNED_JOBS, rows="200", cols="10")

    rows_signed = [headers]
    for idx, j in enumerate(signed_jobs, start=1):
        rows_signed.append([idx, j["company"], j["role"], j["base_pay"], j["ctc"], j["eligible"], j["signed"], j["last_day"]])

    ws_signed.clear()
    ws_signed.update(values=rows_signed, range_name="A1")
    apply_sheet_formatting_and_colors(sh, ws_signed, len(rows_signed))
    print(f"[+] '{SHEET_SIGNED_JOBS}' updated with {len(signed_jobs)} signed jobs.")

    # 3. Summary tab (stats + upcoming deadlines + last update time)
    try:
        update_summary_sheet(sh, jobs)
    except Exception as e:
        print(f"[!] Warning updating '{SHEET_SUMMARY}' tab: {e}")


def build_summary_rows(jobs, tests, now=None):
    """Rows for the Summary tab. Kept separate from the Sheets call so it's testable."""
    now = now or datetime.now(timezone.utc)
    eligible = [j for j in jobs if j["eligible"] == "Eligible"]
    signed = [j for j in jobs if j["signed"] == "Yes"]
    open_unsigned = []
    for j in eligible:
        if j["signed"] == "Yes" or not j.get("closes_at"):
            continue
        try:
            closes = datetime.fromisoformat(j["closes_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if closes > now:
            open_unsigned.append((closes, j))
    open_unsigned.sort(key=lambda p: p[0])

    rows = [
        ["Placement Monitor - Summary", ""],
        ["Last updated", now.astimezone(IST_TZ).strftime("%d %b %Y, %I:%M %p IST")],
        ["", ""],
        ["Total JAFs", len(jobs)],
        ["Eligible", len(eligible)],
        ["Signed", len(signed)],
        ["Eligible & still open (unsigned)", len(open_unsigned)],
        ["", ""],
        ["Closing soon (eligible, unsigned)", "Closes (IST)", "CTC"],
    ]
    for closes, j in open_unsigned[:10]:
        rows.append([f"{j['company']} - {j['role']}", closes.astimezone(IST_TZ).strftime("%d %b, %I:%M %p"), j["ctc"]])
    if not open_unsigned:
        rows.append(["(none)", "", ""])

    rows += [["", ""], ["Top eligible CTCs", "CTC", "Signed?"]]
    for j in sorted(eligible, key=lambda j: j.get("ctc_inr", 0), reverse=True)[:10]:
        rows.append([f"{j['company']} - {j['role']}", j["ctc"], j["signed"]])

    upcoming_tests = []
    for t in tests:
        try:
            dt = datetime.fromisoformat(t["test_time"]) if t.get("test_time") else None
        except ValueError:
            dt = None
        if dt and dt > now:
            upcoming_tests.append((dt, t))
    upcoming_tests.sort(key=lambda p: p[0])
    rows += [["", ""], ["Upcoming tests", "Starts (IST)", "Link"]]
    for dt, t in upcoming_tests[:10]:
        rows.append([t.get("company") or t["title"][:60], dt.astimezone(IST_TZ).strftime("%d %b, %I:%M %p"), t.get("link", "")])
    if not upcoming_tests:
        rows.append(["(none)", "", ""])
    return rows


def update_summary_sheet(sh, jobs):
    rows = build_summary_rows(jobs, load_json(TESTS_FILE, []))
    try:
        ws = sh.worksheet(SHEET_SUMMARY)
    except Exception:
        ws = sh.add_worksheet(title=SHEET_SUMMARY, rows="100", cols="4")
    width = max(len(r) for r in rows)
    ws.clear()
    ws.update(values=[r + [""] * (width - len(r)) for r in rows], range_name="A1")
    try:
        ws.format("A1", {"textFormat": {"bold": True, "fontSize": 13}})
        bold_rows = [i + 1 for i, r in enumerate(rows) if r and r[0] in (
            "Closing soon (eligible, unsigned)", "Top eligible CTCs", "Upcoming tests")]
        for r in bold_rows:
            ws.format(f"A{r}:C{r}", {"textFormat": {"bold": True},
                                     "backgroundColor": {"red": 0.87, "green": 0.91, "blue": 0.97}})
    except Exception:
        pass
    print(f"[+] '{SHEET_SUMMARY}' tab updated.")

def check_jobs(token=None):
    """Checks for new jobs, updates sheets, and alerts on newly added ones."""
    token = token or ensure_jwt_token()
    if not token:
        print("[!] No JWT token available. Please run set_token.py first.")
        return

    jobs = fetch_jobs_data(token)
    
    known_jobs = {}
    if os.path.exists(KNOWN_JOBS_FILE):
        try:
            with open(KNOWN_JOBS_FILE, "r") as f:
                known_jobs = json.load(f)
        except Exception:
            known_jobs = {}

    current_jobs_map = {str(j["id"]): j for j in jobs}
    
    new_jobs = []
    if known_jobs:
        for j_id, j_data in current_jobs_map.items():
            if j_id not in known_jobs:
                new_jobs.append(j_data)

    if new_jobs:
        msg = f"Found {len(new_jobs)} new job(s):\n"
        for nj in new_jobs[:5]:
            test_hint = f" | Test round: {', '.join(nj['tests'])}" if nj.get("tests") else ""
            msg += f"• {nj['company']} - {nj['role']} (Base: {nj['base_pay']} | CTC: {nj['ctc']}) [{nj['eligible']}{test_hint}]\n"
        if len(new_jobs) > 5:
            msg += f"...and {len(new_jobs) - 5} more."
        send_notifications("🆕 New JAF Opening!", msg, DISCORD_WEBHOOK_NEW_JAFS, color=5763719)

    # Changes to jobs we already knew about (deadline extended, CTC revised,
    # became eligible, test round added...)
    if known_jobs and ALERT_ON_JOB_CHANGES:
        changes = diff_jobs(known_jobs, current_jobs_map)
        if changes:
            important = [c for c in changes if is_important(c)]
            for c in changes:
                print(f"[+] JAF changed: {c['company']} - {c['role']}: {c['changes']}")
            title = (f"✏️ {len(changes)} JAF(s) updated" +
                     (f" - {len(important)} need your attention" if important else ""))
            body = format_changes(sorted(changes, key=lambda c: not is_important(c)))
            if important:
                send_notifications(title, body, DISCORD_WEBHOOK_NEW_JAFS, color=15105570)
            else:
                # Minor edits: Discord only, no desktop pop-up
                send_discord_embed(DISCORD_WEBHOOK_NEW_JAFS, title, body, color=9807270)

    try:
        update_google_sheet(jobs)
    except Exception as e:
        print(f"[!] Error updating Google Sheet: {e}")

    with open(KNOWN_JOBS_FILE, "w") as f:
        json.dump(current_jobs_map, f, indent=2)
    return jobs

TEST_TITLE_RE = re.compile(r"\b(test|aptitude|assessment|coding\s+round|online\s+test)\b", re.I)
FORM_TITLE_RE = re.compile(r"\b(mandatory\s+form|preference\s+form|google\s+form|fill\s+the\s+form|mandatory.*form)\b", re.I)
SHORTLIST_TITLE_RE = re.compile(r"\b(shortlist|short-list|waitlist|wait-list)\b", re.I)
IST_TZ = timezone(timedelta(hours=5, minutes=30))
MONTHS_MAP = {m[:3].lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

def extract_form_links(content_html, fallback_text=""):
    """Extracts form / survey links from HTML content or text."""
    links = []
    if content_html:
        soup = BeautifulSoup(content_html, "html.parser")
        for a in soup.find_all("a"):
            href = (a.get("href") or "").strip()
            if href.startswith("http") and "campus.placements.iitb.ac.in/blog" not in href:
                links.append(href)
    if not links and fallback_text:
        raw_links = re.findall(r"https?://[^\s\"<>]+", fallback_text)
        for rl in raw_links:
            if "campus.placements.iitb.ac.in/blog" not in rl and rl not in links:
                links.append(rl)
    return list(dict.fromkeys(links))

def parse_deadline(text):
    """Extracts a deadline date+time from text (IST assumed). Returns ISO string or None."""
    if not text:
        return None
    t = text[:6000]
    now = datetime.now(IST_TZ)

    deadline_idx = t.lower().find("deadline")
    search_text = t[deadline_idx:deadline_idx+500] if deadline_idx != -1 else t

    hour = minute = None
    m = re.search(r"\b(\d{1,2})\s*(?:[:.]\s*(\d{2}))?\s*([ap])\.?\s*m\.?\b", search_text, re.I)
    if not m and search_text != t:
        m = re.search(r"\b(\d{1,2})\s*(?:[:.]\s*(\d{2}))?\s*([ap])\.?\s*m\.?\b", t, re.I)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if m.group(3).lower() == "p" and hour != 12:
            hour += 12
        if m.group(3).lower() == "a" and hour == 12:
            hour = 0
    else:
        m = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\s*(?:hrs|hours)?\b", search_text)
        if not m and search_text != t:
            m = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\s*(?:hrs|hours)?\b", t)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
    if hour is None or not (0 <= hour <= 23):
        hour, minute = 23, 59

    day = month = year = None
    m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?[\s,-]+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s*(\d{4})?\b", search_text, re.I)
    if not m and search_text != t:
        m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?[\s,-]+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s*(\d{4})?\b", t, re.I)
    if not m:
        m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\s*(?:st|nd|rd|th)?,?\s*(\d{4})?\b", search_text, re.I)
        if not m and search_text != t:
            m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\s*(?:st|nd|rd|th)?,?\s*(\d{4})?\b", t, re.I)
        if m:
            month = MONTHS_MAP.get(m.group(1)[:3].lower())
            day, year = int(m.group(2)), m.group(3)
    else:
        day = int(m.group(1))
        month = MONTHS_MAP.get(m.group(2)[:3].lower())
        year = m.group(3)

    if not day or not month:
        return None
    year = int(year) if year else now.year
    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=IST_TZ)
        return dt.isoformat()
    except Exception:
        return None

def parse_test_datetime(text):
    """Best-effort extraction of a test date+time from blog text (IST assumed).
    Returns an ISO datetime string, or None if no reliable date+time found."""
    if not text:
        return None
    t = text[:6000]
    now = datetime.now(IST_TZ)

    # ---- Time (required for reminders) ----
    hour = minute = None
    m = re.search(r"\b(\d{1,2})\s*(?:[:.]\s*(\d{2}))?\s*([ap])\.?\s*m\.?\b", t, re.I)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if m.group(3).lower() == "p" and hour != 12:
            hour += 12
        if m.group(3).lower() == "a" and hour == 12:
            hour = 0
    else:
        m = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\s*(?:hrs|hours)?\b", t)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
    if hour is None or not (0 <= hour <= 23):
        return None

    # ---- Date ----
    day = month = year = None
    m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?[\s,-]+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s*(\d{4})?\b", t, re.I)
    if not m:
        m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\s*(?:st|nd|rd|th)?,?\s*(\d{4})?\b", t, re.I)
        if m:
            month = MONTHS_MAP.get(m.group(1)[:3].lower())
            day, year = int(m.group(2)), m.group(3)
    else:
        day = int(m.group(1))
        month = MONTHS_MAP.get(m.group(2)[:3].lower())
        year = m.group(3)
    if not m or not month:
        # dd/mm/yyyy fallback (Indian convention)
        m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?\b", t)
        if not m:
            return None
        day, month = int(m.group(1)), int(m.group(2))
        year = m.group(3)
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None

    if year:
        year = int(year)
        if year < 100:
            year += 2000
    else:
        year = now.year
        try:
            if datetime(year, month, day, hour, minute, tzinfo=IST_TZ) < now - timedelta(days=1):
                year += 1
        except ValueError:
            return None

    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=IST_TZ)
    except ValueError:
        return None
    if dt < now - timedelta(days=1):
        return None  # stale/past date - don't schedule reminders
    if dt > now + timedelta(days=60):
        return None  # implausible horizon - likely a misparse
    return dt.isoformat()

def match_job_for_test(title, token):
    """Matches a blog post title against portal jobs. Returns (company_guess, [job_ids])."""
    company_guess, job_ids = "", []
    if not token:
        return company_guess, job_ids
    try:
        headers = {"Authorization": f"Bearer {token}"}
        payload = parse_jwt(token)
        params = {
            "service": payload.get("service", "placement"),
            "year": str(payload.get("batch", "2026")),
            "season": payload.get("season", "autumn")
        }
        r = requests.get(f"{BASE_API}/job", headers=headers, params=params, timeout=20)
        if r.status_code != 200:
            return company_guess, job_ids
        tl = title.lower()
        for j in r.json():
            comp = (j.get("company") or {}).get("name", "")
            if comp and len(comp) >= 4 and comp.lower() in tl:
                company_guess = company_guess or comp
                job_ids.append(j.get("id"))
    except Exception as e:
        print(f"[!] Warning matching test to jobs: {e}")
    return company_guess, job_ids

def get_insti_session():
    """InstiApp session id: env var first, then insti_token.txt."""
    session = os.getenv("INSTI_SESSION_ID", "").strip()
    if session:
        return session
    if os.path.exists(INSTI_TOKEN_FILE):
        with open(INSTI_TOKEN_FILE, "r") as f:
            return f.read().strip()
    return ""

def fetch_insti_posts(session, num=60):
    """Fetches recent placement blog posts from the InstiApp API (public mirror, no SSO)."""
    r = requests.get(BLOG_API, headers={"Cookie": f"sessionid={session}"}, params={"num": num}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"InstiApp blog API returned {r.status_code}: {r.text[:200]}")
    data = r.json()
    raw_posts = data if isinstance(data, list) else data.get("results", [])
    posts = []
    for p in raw_posts:
        title = (p.get("title") or "").strip()
        if not title:
            continue
        content_html = p.get("content") or ""
        body = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True) if content_html else ""
        posts.append({
            "title": title,
            "link": p.get("link") or f"https://www.insti.app/blog/{p.get('id', '')}",
            "body": body,
            "content_html": content_html,
            "published": p.get("published") or ""
        })
    return posts

def check_blog():
    """Checks the placement blog via InstiApp, alerts test-related posts on the
    tests channel and stores them for reminders."""
    print("[*] Checking placement blog (insti.app)...")
    session = get_insti_session()
    flag_file = os.path.join(BASE_DIR, ".blog_session_expired")
    if not session:
        warn_msg = "InstiApp session missing! Run 'python3 set_insti_token.py'. Blog/test monitoring is PAUSED."
        print(f"[!] {warn_msg}")
        if not os.path.exists(flag_file):
            open(flag_file, "w").close()
            send_notifications("⚠️ Blog Monitor Stopped", warn_msg, DISCORD_WEBHOOK_ALERTS)
        return
    try:
        posts = fetch_insti_posts(session)
    except Exception as e:
        print(f"[!] Error fetching InstiApp blog: {e}")
        if any(code in str(e) for code in ("returned 401", "returned 403")):
            health_alert("insti_session",
                         "⚠️ InstiApp session expired",
                         "The placement blog rejected your InstiApp session, so blog and test "
                         "alerts are PAUSED. Run 'python3 set_insti_token.py' with a fresh sessionid.")
        return
    if os.path.exists(flag_file):
        os.remove(flag_file)
        print("[+] Blog monitoring restored.")
    clear_health_alert("insti_session")

    if not posts:
        return

    known_blogs = []
    if os.path.exists(KNOWN_BLOGS_FILE):
        try:
            with open(KNOWN_BLOGS_FILE, "r") as f:
                known_blogs = json.load(f)
        except Exception:
            known_blogs = []

    known_links = {b.get("link") for b in known_blogs if "link" in b}
    new_blogs = [p for p in posts if p["link"] not in known_links]

    if new_blogs:
        form_posts = [p for p in new_blogs if FORM_TITLE_RE.search(p["title"])]
        rem1 = [p for p in new_blogs if p not in form_posts]
        shortlist_posts = [p for p in rem1 if SHORTLIST_TITLE_RE.search(p["title"])]
        rem2 = [p for p in rem1 if p not in shortlist_posts]
        test_posts = [p for p in rem2 if TEST_TITLE_RE.search(p["title"] + "\n" + p["body"])]
        other_posts = [p for p in rem2 if p not in test_posts]

        token = ensure_jwt_token()

        if form_posts:
            known_forms = []
            if os.path.exists(FORMS_FILE):
                try:
                    with open(FORMS_FILE, "r") as f:
                        known_forms = json.load(f)
                except Exception:
                    known_forms = []
            known_form_links = {fm.get("link") for fm in known_forms}

            for fp in form_posts:
                company, job_ids = match_job_for_test(fp["title"], token)
                form_links = extract_form_links(fp.get("content_html", ""), fp["body"])
                deadline_iso = parse_deadline(fp["body"])
                dl_str = "No deadline detected" if not deadline_iso \
                    else f"Deadline: {datetime.fromisoformat(deadline_iso).strftime('%d %b %Y, %I:%M %p IST')}"
                links_str = "\n".join(f"🔗 Form Link: {fl}" for fl in form_links) if form_links else "🔗 Form Link: see post"
                desc = f"**{fp['title']}**\n{dl_str}\n{links_str}\n\n[Open Blog Post]({fp['link']})"

                print(f"[+] Mandatory Form update: {fp['title']} ({dl_str})")
                send_discord_embed(
                    DISCORD_WEBHOOK_FORMS,
                    f"📋 Mandatory Form: {company or fp['title'][:80]}",
                    desc,
                    color=15105570
                )
                if fp["link"] not in known_form_links:
                    known_forms.append({
                        "title": fp["title"],
                        "link": fp["link"],
                        "company": company,
                        "job_ids": job_ids,
                        "deadline": deadline_iso,
                        "form_links": form_links,
                        "added_at": datetime.now(timezone.utc).isoformat(),
                        "reminded_1h": False
                    })

            with open(FORMS_FILE, "w") as f:
                json.dump(known_forms, f, indent=2)

        if shortlist_posts:
            # Check applicant roll number if token available
            my_roll = (parse_jwt(token).get("username") or "").upper() if token else ""
            for sp in shortlist_posts:
                company, job_ids = match_job_for_test(sp["title"], token)
                external_links = extract_form_links(sp.get("content_html", ""), sp["body"])
                links_str = "\n".join(f"🔗 External Link: {el}" for el in external_links) if external_links else ""
                
                # Check if user's roll number is mentioned in the post body
                is_mentioned = my_roll and (my_roll in sp["body"].upper())
                status_badge = "🎉 **YOU ARE SHORTLISTED / MENTIONED IN THIS POST!**\n" if is_mentioned else ""

                desc = f"{status_badge}**{sp['title']}**\n"
                if links_str:
                    desc += f"{links_str}\n"
                desc += f"\n[Open Blog Post]({sp['link']})"

                color = 3066993 if is_mentioned else 3447003  # Green if selected, blue otherwise
                print(f"[+] Shortlist update: {sp['title']}")
                send_discord_embed(
                    DISCORD_WEBHOOK_SHORTLISTS,
                    f"🎯 Shortlist: {company or sp['title'][:80]}",
                    desc,
                    color=color
                )

        if test_posts:
            known_tests = []
            if os.path.exists(TESTS_FILE):
                try:
                    with open(TESTS_FILE, "r") as f:
                        known_tests = json.load(f)
                except Exception:
                    known_tests = []
            known_test_links = {t.get("link") for t in known_tests}

            for tp in test_posts:
                dt_iso = parse_test_datetime(tp["title"] + "\n" + tp["body"])
                company, job_ids = match_job_for_test(tp["title"], token)
                when_str = "Time: see post (could not parse exact time - no reminders set)" if not dt_iso \
                    else f"Starts: {datetime.fromisoformat(dt_iso).strftime('%d %b %Y, %I:%M %p IST')}"
                print(f"[+] Test update: {tp['title']} ({when_str})")
                send_discord_embed(
                    DISCORD_WEBHOOK_TESTS,
                    f"📝 Test Update: {company or tp['title'][:80]}",
                    f"**{tp['title']}**\n{when_str}\n{tp['link']}",
                    color=15844367
                )
                if tp["link"] not in known_test_links:
                    known_tests.append({
                        "title": tp["title"],
                        "link": tp["link"],
                        "company": company,
                        "job_ids": job_ids,
                        "test_time": dt_iso,
                        "added_at": datetime.now(timezone.utc).isoformat(),
                        "reminded_1h": False,
                        "reminded_30m": False
                    })

            with open(TESTS_FILE, "w") as f:
                json.dump(known_tests, f, indent=2)

        if other_posts and known_blogs:
            msg = f"Found {len(other_posts)} new blog post(s):\n"
            for nb in other_posts[:5]:
                msg += f"• {nb['title']}\n{nb['link']}\n"
            send_notifications("New Placement Blog Update!", msg, DISCORD_WEBHOOK_BLOG)

    with open(KNOWN_BLOGS_FILE, "w") as f:
        json.dump([{"title": p["title"], "link": p["link"]} for p in posts], f, indent=2)

def test_is_for_signed_job(t, signed_map):
    """Is this test for a job I signed? signed_map = {job_id: company_name}.
    Matches by stored job_ids, full company name, or distinctive first token
    (e.g. "Hyde Inc." matches "Hyde AI")."""
    title_and_company = f"{t.get('company', '')} {t.get('title', '')}".lower()
    signed_companies = [c.lower() for c in signed_map.values() if c]
    first_tokens = [re.split(r"[\s.,()\[\]]+", c)[0] for c in signed_companies]
    return any(jid in (t.get("job_ids") or []) for jid in signed_map) or \
        any(len(c) >= 4 and c in title_and_company for c in signed_companies) or \
        any(len(tok) >= 4 and tok in title_and_company for tok in first_tokens)

def check_reminders(token):
    """Sends 1-hour and 30-minute reminders for upcoming tests of signed JAFs,
    and 1-hour reminders for mandatory-form deadlines of signed JAFs."""
    if not token:
        print("[*] No token - skipping test reminders.")
        return
    tests = load_json(TESTS_FILE, [])
    forms = load_json(FORMS_FILE, [])

    now = datetime.now(timezone.utc)
    pending = [t for t in tests
               if t.get("test_time") and not (t.get("reminded_1h") and t.get("reminded_30m"))]
    pending_forms = [fm for fm in forms if fm.get("deadline") and not fm.get("reminded_1h")]
    # Only return early when there is nothing to remind about at all (tests OR forms);
    # previously form reminders were skipped whenever no test was pending.
    if not pending and not pending_forms:
        return

    # Which jobs have I signed? (applications API includes the job object)
    signed_map = {}
    try:
        headers = {"Authorization": f"Bearer {token}"}
        payload = parse_jwt(token)
        applicant_id = payload.get("id")
        r = requests.get(f"{BASE_API}/job/application", headers=headers,
                         params={"applicantId": applicant_id}, timeout=20)
        if r.status_code == 200:
            for app in r.json():
                jid = app.get("jobId") or (app.get("job") or {}).get("id")
                comp = ((app.get("job") or {}).get("company") or {}).get("name", "")
                if jid:
                    signed_map[jid] = comp
    except Exception as e:
        print(f"[!] Warning fetching applications for reminders: {e}")
        return
    if not signed_map:
        return

    changed = False
    for t in pending:
        try:
            test_dt = datetime.fromisoformat(t["test_time"])
        except Exception:
            continue
        delta_min = (test_dt - now).total_seconds() / 60
        if delta_min <= 0:
            t["reminded_1h"] = True
            t["reminded_30m"] = True
            changed = True
            continue

        if not test_is_for_signed_job(t, signed_map):
            continue

        if delta_min <= 60 and not t.get("reminded_1h"):
            send_notifications(
                "⏰ Test Reminder (1 hour)",
                f"{t.get('company') or 'Company'} - {t['title']}\nStarts in ~{int(delta_min)} min ({test_dt.astimezone(IST_TZ).strftime('%I:%M %p IST')})\n{t['link']}",
                DISCORD_WEBHOOK_REMINDERS, color=16776960)
            t["reminded_1h"] = True
            changed = True
        if delta_min <= 30 and not t.get("reminded_30m"):
            send_notifications(
                "🚨 Test Reminder (30 minutes) - HURRY!",
                f"{t.get('company') or 'Company'} - {t['title']}\nStarts in {int(delta_min)} min ({test_dt.astimezone(IST_TZ).strftime('%I:%M %p IST')})\n{t['link']}",
                DISCORD_WEBHOOK_REMINDERS, color=15158332)
            t["reminded_30m"] = True
            changed = True

    if changed:
        with open(TESTS_FILE, "w") as f:
            json.dump(tests, f, indent=2)

    # Check mandatory form reminders (1 hour before deadline)
    if pending_forms:
        forms_changed = False
        for fm in pending_forms:
            try:
                deadline_dt = datetime.fromisoformat(fm["deadline"])
            except Exception:
                continue
            delta_min = (deadline_dt - now).total_seconds() / 60
            if delta_min <= 0:
                fm["reminded_1h"] = True
                forms_changed = True
                continue

            # Only for forms of jobs I signed (same matching as test reminders)
            if not test_is_for_signed_job(fm, signed_map):
                continue

            if delta_min <= 60 and not fm.get("reminded_1h"):
                links_text = "\n".join(f"🔗 {fl}" for fl in fm.get("form_links", [])) if fm.get("form_links") else fm["link"]
                send_notifications(
                    "⏰ Mandatory Form Reminder (1 hour)",
                    f"{fm.get('company') or 'Company'} - {fm['title']}\nDeadline in ~{int(delta_min)} min ({deadline_dt.astimezone(IST_TZ).strftime('%I:%M %p IST')})\n{links_text}",
                    DISCORD_WEBHOOK_REMINDERS, color=16776960)
                fm["reminded_1h"] = True
                forms_changed = True

        if forms_changed:
            with open(FORMS_FILE, "w") as f:
                json.dump(forms, f, indent=2)

def check_daily_closing_jafs():
    """Every day at 11 PM IST, sends a summary reminder for all JAFs closing today."""
    now_ist = datetime.now(IST_TZ)
    today_str = now_ist.strftime("%Y-%m-%d")

    # Only fire during the 11 PM hour (23:00 to 23:59 IST)
    if now_ist.hour != 23:
        return

    # Check if we already alerted today
    if os.path.exists(DAILY_CLOSING_STATE_FILE):
        try:
            with open(DAILY_CLOSING_STATE_FILE, "r") as f:
                if f.read().strip() == today_str:
                    return
        except Exception:
            pass

    if not os.path.exists(KNOWN_JOBS_FILE):
        return

    try:
        with open(KNOWN_JOBS_FILE, "r") as f:
            known_jobs = json.load(f)
    except Exception:
        return

    # Find jobs whose deadline is today (by closes_at or last_day string)
    today_day_str = f"{now_ist.day}th {now_ist.strftime('%b')}"
    if now_ist.day in (1, 21, 31):
        today_day_str = f"{now_ist.day}st {now_ist.strftime('%b')}"
    elif now_ist.day in (2, 22):
        today_day_str = f"{now_ist.day}nd {now_ist.strftime('%b')}"
    elif now_ist.day in (3, 23):
        today_day_str = f"{now_ist.day}rd {now_ist.strftime('%b')}"

    closing_today = []
    for j in known_jobs.values():
        closes_at = j.get("closes_at")
        matched = False
        if closes_at:
            try:
                c_dt = datetime.fromisoformat(closes_at.replace("Z", "+00:00")).astimezone(IST_TZ)
                if c_dt.strftime("%Y-%m-%d") == today_str:
                    matched = True
            except Exception:
                pass
        if not matched and j.get("last_day"):
            if today_day_str.lower() in j["last_day"].lower():
                matched = True

        if matched:
            closing_today.append(j)

    if not closing_today:
        print("[*] No JAFs closing today at 11 PM check.")
        with open(DAILY_CLOSING_STATE_FILE, "w") as f:
            f.write(today_str)
        return

    # Split into eligible vs not eligible or signed
    msg = f"⏳ **{len(closing_today)} JAF(s) are closing TONIGHT ({now_ist.strftime('%d %b %Y')})!**\n\n"
    for cj in closing_today:
        signed_badge = "✅ Signed" if cj.get("signed") == "Yes" else "❌ Not Signed"
        elig_badge = cj.get("eligible", "Unknown")
        msg += f"• **{cj['company']}** - {cj['role']}\n  CTC: {cj.get('ctc', 'N/A')} | [{elig_badge}] | [{signed_badge}]\n"

    print(f"[+] Sending 11 PM daily reminder for {len(closing_today)} JAF(s) closing today.")
    send_notifications(
        "⏳ JAFs Closing Tonight Reminder (11 PM)",
        msg,
        DISCORD_WEBHOOK_REMINDERS,
        color=15158332
    )

    with open(DAILY_CLOSING_STATE_FILE, "w") as f:
        f.write(today_str)
# ------------------------------------------------------------------ helpers / state

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ------------------------------------------------------------------ health

HEALTH_REALERT_HOURS = 12
TOKEN_WARN_HOURS = _int_env("TOKEN_WARN_HOURS", 24)

def health_alert(key, title, message):
    """Alerts about a bot problem (desktop + Discord) at most once every
    HEALTH_REALERT_HOURS, instead of failing silently every 15 minutes."""
    state = load_json(HEALTH_STATE_FILE, {})
    alerts = state.setdefault("alerts", {})
    now = datetime.now(timezone.utc)
    last = alerts.get(key)
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(hours=HEALTH_REALERT_HOURS):
                print(f"[!] {title} (already alerted)")
                return
        except ValueError:
            pass
    send_notifications(title, message, DISCORD_WEBHOOK_ALERTS, color=15158332)
    alerts[key] = now.isoformat()
    save_json(HEALTH_STATE_FILE, state)

def clear_health_alert(key):
    state = load_json(HEALTH_STATE_FILE, {})
    if state.get("alerts", {}).pop(key, None):
        save_json(HEALTH_STATE_FILE, state)
        print(f"[+] Health issue '{key}' resolved.")

def check_token_health(token):
    """Warns before (and after) the portal JWT expires - without it nothing works."""
    if not token:
        health_alert("portal_token", "⚠️ Portal token missing",
                     "No placement-portal token found, so JAF tracking is PAUSED. Run 'python3 set_token.py'.")
        return
    exp = parse_jwt(token).get("exp")
    if not exp:
        return
    hours_left = (exp - datetime.now(timezone.utc).timestamp()) / 3600
    if hours_left <= 0:
        health_alert("portal_token", "⚠️ Portal token expired",
                     "Your placement-portal login token has expired, so JAF tracking, deadlines and "
                     "reminders are PAUSED. Run 'python3 set_token.py' with a fresh token.")
    elif hours_left <= TOKEN_WARN_HOURS and not os.path.exists(USER_DATA_DIR):
        # (with chrome_data/ present, ensure_jwt_token() refreshes it automatically)
        health_alert("portal_token", "⏳ Portal token expires soon",
                     f"Your placement-portal token expires in ~{int(hours_left)}h. "
                     "Refresh it with 'python3 set_token.py' to avoid missing alerts.")
    else:
        clear_health_alert("portal_token")

def record_run(ok, jobs_count=None, errors=None):
    state = load_json(HEALTH_STATE_FILE, {})
    now = datetime.now(timezone.utc).isoformat()
    state["last_run"] = now
    if ok:
        state["last_success"] = now
        state["jobs_count"] = jobs_count
    state["last_errors"] = errors or []
    save_json(HEALTH_STATE_FILE, state)

# ------------------------------------------------------------------ deadlines

def check_deadlines(jobs):
    """Daily digest of eligible-but-unsigned JAFs closing soon, plus a last-call
    warning DEADLINE_WARN_HOURS before each one closes."""
    if not jobs:
        return
    state = load_json(DEADLINE_STATE_FILE, {})
    digest, warnings, new_state = compute_deadline_alerts(
        jobs, state, datetime.now(timezone.utc),
        warn_hours=DEADLINE_WARN_HOURS,
        digest_hour=DEADLINE_DIGEST_HOUR,
        digest_window_hours=DEADLINE_DIGEST_WINDOW_HOURS)

    if digest:
        send_notifications(
            f"📅 {len(digest)} eligible JAF(s) closing in the next {DEADLINE_DIGEST_WINDOW_HOURS}h",
            format_deadline_items(digest) + "\n\nYou haven't signed these yet.",
            DISCORD_WEBHOOK_DEADLINES, color=3447003)
    elif digest is not None:
        print("[*] Deadline digest: nothing eligible closing soon.")

    if warnings:
        send_notifications(
            f"🚨 Last call: {len(warnings)} JAF(s) close within {DEADLINE_WARN_HOURS}h",
            format_deadline_items(warnings) + "\n\nSign now if you want to apply!",
            DISCORD_WEBHOOK_DEADLINES, color=15158332)

    save_json(DEADLINE_STATE_FILE, new_state)

# ------------------------------------------------------------------ calendar

def export_calendar(jobs):
    """Writes placements.ics: deadlines for eligible unsigned JAFs + tests for signed JAFs."""
    tests = load_json(TESTS_FILE, [])
    signed_map = {j["id"]: j["company"] for j in jobs if j.get("signed") == "Yes"}
    events = placement_events(jobs, tests, lambda t: test_is_for_signed_job(t, signed_map))
    with open(CALENDAR_FILE, "w", newline="") as f:
        f.write(build_ics(events))
    print(f"[+] Calendar written: {CALENDAR_FILE} ({len(events)} events)")

# ------------------------------------------------------------------ webhooks

def configured_webhooks():
    return {
        "DISCORD_WEBHOOK_URL (generic / fallback)": DISCORD_WEBHOOK_URL,
        "DISCORD_WEBHOOK_NEW_JAFS": DISCORD_WEBHOOK_NEW_JAFS,
        "DISCORD_WEBHOOK_TESTS": DISCORD_WEBHOOK_TESTS,
        "DISCORD_WEBHOOK_REMINDERS": DISCORD_WEBHOOK_REMINDERS,
        "DISCORD_WEBHOOK_DEADLINES": DISCORD_WEBHOOK_DEADLINES,
        "DISCORD_WEBHOOK_BLOG": DISCORD_WEBHOOK_BLOG,
        "DISCORD_WEBHOOK_ALERTS": DISCORD_WEBHOOK_ALERTS,
    }

def check_webhooks_cli():
    """python3 scraper.py --check-webhooks : verifies every webhook WITHOUT posting a message."""
    print("Checking Discord webhooks (no messages are sent)...\n")
    results = {}
    all_ok = True
    for name, url in configured_webhooks().items():
        if url not in results:
            results[url] = check_webhook(url)
        ok, detail = results[url]
        fallback = "" if not url or os.getenv(name.split(" ")[0], "").strip() else "  [falls back]"
        mark = "✓" if ok else ("-" if not url else "✗")
        shown = mask_webhook(url) if url else ""
        print(f" {mark} {name:<42} {detail} {shown}{fallback}")
        if url and not ok:
            all_ok = False
    print("\nAll configured webhooks are working." if all_ok else
          "\nSome webhooks are dead: create a new webhook in Discord (Edit Channel → Integrations → "
          "Webhooks) and update config.env.")
    return all_ok

# ------------------------------------------------------------------ main loop

def run_all():
    print("----------------------------------------------------------------")
    print(f"Running Placement Portal & Blog check at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}...")
    token = ensure_jwt_token()
    errors = []
    jobs = None

    try:
        check_token_health(token)
    except Exception as e:
        print(f"[!] Error during token health check: {e}")
    try:
        jobs = check_jobs(token)
    except Exception as e:
        errors.append(f"jobs: {e}")
        print(f"[!] Error during job check: {e}")
    try:
        check_blog()
    except Exception as e:
        errors.append(f"blog: {e}")
        print(f"[!] Error during blog check: {e}")
    try:
        check_reminders(token)
    except Exception as e:
        errors.append(f"reminders: {e}")
        print(f"[!] Error during reminder check: {e}")
    try:
        check_daily_closing_jafs()
    except Exception as e:
        errors.append(f"daily closing: {e}")
        print(f"[!] Error during daily closing JAF check: {e}")
    try:
        check_deadlines(jobs)
    except Exception as e:
        errors.append(f"deadlines: {e}")
        print(f"[!] Error during deadline check: {e}")
    if jobs:
        try:
            export_calendar(jobs)
        except Exception as e:
            print(f"[!] Error writing calendar: {e}")

    record_run(ok=jobs is not None, jobs_count=len(jobs) if jobs else None, errors=errors)
    print("Check complete.")
    print("----------------------------------------------------------------")

def backfill_tests(days=2):
    """One-shot: delivers test-related blog posts from the last `days` days
    (plus upcoming ones) to Discord AND desktop notifications, storing them for reminders."""
    print(f"[*] Backfilling test updates from the last {days} day(s)...")
    session = get_insti_session()
    if not session:
        print("[!] InstiApp session missing. Run 'python3 set_insti_token.py' first, then retry.")
        return
    try:
        posts = fetch_insti_posts(session, num=100)
    except Exception as e:
        print(f"[!] Error fetching InstiApp blog: {e}")
        return

    token = ensure_jwt_token()
    cutoff = datetime.now(IST_TZ) - timedelta(days=days)
    known_blogs = []
    if os.path.exists(KNOWN_BLOGS_FILE):
        try:
            with open(KNOWN_BLOGS_FILE, "r") as f:
                known_blogs = json.load(f)
        except Exception:
            known_blogs = []
    known_tests = []
    if os.path.exists(TESTS_FILE):
        try:
            with open(TESTS_FILE, "r") as f:
                known_tests = json.load(f)
        except Exception:
            known_tests = []
    known_test_links = {t.get("link") for t in known_tests}

    delivered = 0
    for post in posts:
        if not TEST_TITLE_RE.search(post["title"] + "\n" + post["body"]):
            continue
        # Skip posts published before the requested window (when a date is available)
        pub_cutoff = None
        if post["published"]:
            try:
                pub_cutoff = datetime.fromisoformat(post["published"].replace("Z", "+00:00"))
            except Exception:
                pub_cutoff = None
        dt_iso = parse_test_datetime(post["title"] + "\n" + post["body"])
        if pub_cutoff and pub_cutoff.astimezone(IST_TZ) < cutoff:
            continue
        if not pub_cutoff and dt_iso and datetime.fromisoformat(dt_iso) < cutoff:
            continue
        company, job_ids = match_job_for_test(post["title"], token)
        when_str = "Time: see post (could not parse exact time - no reminders set)" if not dt_iso \
            else f"Starts: {datetime.fromisoformat(dt_iso).strftime('%d %b %Y, %I:%M %p IST')}"
        send_notifications(
            f"📝 Test Update: {company or post['title'][:60]}",
            f"{post['title']}\n{when_str}\n{post['link']}",
            DISCORD_WEBHOOK_TESTS, color=15844367)
        delivered += 1
        if post["link"] not in known_test_links:
            known_tests.append({
                "title": post["title"],
                "link": post["link"],
                "company": company,
                "job_ids": job_ids,
                "test_time": dt_iso,
                "added_at": datetime.now(timezone.utc).isoformat(),
                "reminded_1h": False,
                "reminded_30m": False
            })
        if post["link"] not in {b.get("link") for b in known_blogs}:
            known_blogs.append({"title": post["title"], "link": post["link"]})

    with open(TESTS_FILE, "w") as f:
        json.dump(known_tests, f, indent=2)
    with open(KNOWN_BLOGS_FILE, "w") as f:
        json.dump(known_blogs, f, indent=2)
    print(f"[+] Delivered {delivered} test update(s).")

def backfill_forms_and_shortlists(days=7):
    """Delivers recent mandatory forms and shortlists to their Discord channels."""
    print(f"[*] Backfilling forms & shortlists from the last {days} day(s)...")
    session = get_insti_session()
    if not session:
        print("[!] InstiApp session missing.")
        return
    try:
        posts = fetch_insti_posts(session, num=100)
    except Exception as e:
        print(f"[!] Error fetching InstiApp blog: {e}")
        return

    token = ensure_jwt_token()
    my_roll = (parse_jwt(token).get("username") or "").upper() if token else ""
    cutoff = datetime.now(IST_TZ) - timedelta(days=days)

    known_forms = []
    if os.path.exists(FORMS_FILE):
        try:
            with open(FORMS_FILE, "r") as f:
                known_forms = json.load(f)
        except Exception:
            known_forms = []
    known_form_links = {fm.get("link") for fm in known_forms}

    delivered_forms = 0
    delivered_shortlists = 0

    for p in reversed(posts):
        pub_cutoff = None
        if p.get("published"):
            try:
                pub_cutoff = datetime.fromisoformat(p["published"].replace("Z", "+00:00")).astimezone(IST_TZ)
            except Exception:
                pub_cutoff = None
        if pub_cutoff and pub_cutoff < cutoff:
            continue

        title = p["title"]
        # Check Form
        if FORM_TITLE_RE.search(title):
            company, job_ids = match_job_for_test(title, token)
            form_links = extract_form_links(p.get("content_html", ""), p["body"])
            deadline_iso = parse_deadline(p["body"])
            dl_str = "No deadline detected" if not deadline_iso \
                else f"Deadline: {datetime.fromisoformat(deadline_iso).strftime('%d %b %Y, %I:%M %p IST')}"
            links_str = "\n".join(f"🔗 Form Link: {fl}" for fl in form_links) if form_links else "🔗 Form Link: see post"
            desc = f"**{title}**\n{dl_str}\n{links_str}\n\n[Open Blog Post]({p['link']})"

            print(f"[+] Delivering Form: {title}")
            send_discord_embed(
                DISCORD_WEBHOOK_FORMS,
                f"📋 Mandatory Form: {company or title[:80]}",
                desc,
                color=15105570
            )
            delivered_forms += 1
            if p["link"] not in known_form_links:
                known_forms.append({
                    "title": title,
                    "link": p["link"],
                    "company": company,
                    "job_ids": job_ids,
                    "deadline": deadline_iso,
                    "form_links": form_links,
                    "added_at": datetime.now(timezone.utc).isoformat(),
                    "reminded_1h": False
                })
                known_form_links.add(p["link"])

        # Check Shortlist
        elif SHORTLIST_TITLE_RE.search(title):
            company, job_ids = match_job_for_test(title, token)
            external_links = extract_form_links(p.get("content_html", ""), p["body"])
            links_str = "\n".join(f"🔗 External Link: {el}" for el in external_links) if external_links else ""
            is_mentioned = my_roll and (my_roll in p["body"].upper())
            status_badge = "🎉 **YOU ARE SHORTLISTED / MENTIONED IN THIS POST!**\n" if is_mentioned else ""

            desc = f"{status_badge}**{title}**\n"
            if links_str:
                desc += f"{links_str}\n"
            desc += f"\n[Open Blog Post]({p['link']})"

            color = 3066993 if is_mentioned else 3447003
            print(f"[+] Delivering Shortlist: {title}")
            send_discord_embed(
                DISCORD_WEBHOOK_SHORTLISTS,
                f"🎯 Shortlist: {company or title[:80]}",
                desc,
                color=color
            )
            delivered_shortlists += 1

    with open(FORMS_FILE, "w") as f:
        json.dump(known_forms, f, indent=2)

    print(f"[✓] Delivered {delivered_forms} form(s) and {delivered_shortlists} shortlist(s).")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--backfill":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 2
        backfill_tests(days)
    elif len(sys.argv) > 1 and sys.argv[1] == "--backfill-all":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        backfill_forms_and_shortlists(days)
    elif len(sys.argv) > 1 and sys.argv[1] == "--check-webhooks":
        sys.exit(0 if check_webhooks_cli() else 1)
    else:
        run_all()

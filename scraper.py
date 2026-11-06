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

from common import get_gspread_client, parse_jwt, send_notifications, send_discord_embed

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
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1O96KAd3FrBUikER4fo3IkrLcibPjVnZcXrIziUYKKHY")
TARGET_PROGRAM = os.getenv("TARGET_PROGRAM", "B.Tech.")
SHEET_ALL_JOBS = "All Jobs"
SHEET_SIGNED_JOBS = "Signed JAFs"

BASE_API = "https://campus.placements.iitb.ac.in/api/v1"
BLOG_API = "https://gymkhana.iitb.ac.in/instiapp/api/placement-blog"
INSTI_TOKEN_FILE = os.path.join(BASE_DIR, "insti_token.txt")
TESTS_FILE = os.path.join(BASE_DIR, "tests.json")

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
            "opens_at": j.get("opensAt", ""),
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

    try:
        update_google_sheet(jobs)
    except Exception as e:
        print(f"[!] Error updating Google Sheet: {e}")

    with open(KNOWN_JOBS_FILE, "w") as f:
        json.dump(current_jobs_map, f, indent=2)

TEST_TITLE_RE = re.compile(r"\b(test|aptitude|assessment|coding\s+round|online\s+test)\b", re.I)
IST_TZ = timezone(timedelta(hours=5, minutes=30))
MONTHS_MAP = {m[:3].lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

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
            send_notifications("⚠️ Blog Monitor Stopped", warn_msg, DISCORD_WEBHOOK_URL)
        return
    try:
        posts = fetch_insti_posts(session)
    except Exception as e:
        print(f"[!] Error fetching InstiApp blog: {e}")
        return
    if os.path.exists(flag_file):
        os.remove(flag_file)
        print("[+] Blog monitoring restored.")

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
        test_posts = [p for p in new_blogs if TEST_TITLE_RE.search(p["title"] + "\n" + p["body"])]
        other_posts = [p for p in new_blogs if p not in test_posts]

        if test_posts:
            token = ensure_jwt_token()
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
            send_notifications("New Placement Blog Update!", msg, DISCORD_WEBHOOK_URL)

    with open(KNOWN_BLOGS_FILE, "w") as f:
        json.dump([{"title": p["title"], "link": p["link"]} for p in posts], f, indent=2)

def check_reminders(token):
    """Sends 1-hour and 30-minute reminders for upcoming tests of signed JAFs."""
    if not token:
        print("[*] No token - skipping test reminders.")
        return
    tests = []
    if os.path.exists(TESTS_FILE):
        try:
            with open(TESTS_FILE, "r") as f:
                tests = json.load(f)
        except Exception:
            tests = []
    if not tests:
        return

    now = datetime.now(timezone.utc)
    pending = [t for t in tests
               if t.get("test_time") and not (t.get("reminded_1h") and t.get("reminded_30m"))]
    if not pending:
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

        # Is this test for a job I signed? (by stored job_ids or company-name match)
        title_and_company = f"{t.get('company', '')} {t.get('title', '')}".lower()
        signed_companies = [c.lower() for c in signed_map.values()]
        # full name, distinctive first token (e.g. "Hyde Inc." matches "Hyde AI")
        first_tokens = [re.split(r"[\s.,()\[\]]+", c)[0] for c in signed_companies]
        is_signed = any(jid in (t.get("job_ids") or []) for jid in signed_map) or \
            any(len(c) >= 4 and c in title_and_company for c in signed_companies) or \
            any(len(tok) >= 4 and tok in title_and_company for tok in first_tokens)
        if not is_signed:
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

def run_all():
    print("----------------------------------------------------------------")
    print(f"Running Placement Portal & Blog check at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}...")
    token = ensure_jwt_token()
    try:
        check_jobs(token)
    except Exception as e:
        print(f"[!] Error during job check: {e}")
    try:
        check_blog()
    except Exception as e:
        print(f"[!] Error during blog check: {e}")
    try:
        check_reminders(token)
    except Exception as e:
        print(f"[!] Error during reminder check: {e}")
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

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--backfill":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 2
        backfill_tests(days)
    else:
        run_all()

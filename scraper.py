#!/usr/bin/env python3
import os
import json
import base64
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from common import get_gspread_client, parse_jwt, send_notifications

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
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1O96KAd3FrBUikER4fo3IkrLcibPjVnZcXrIziUYKKHY")
TARGET_PROGRAM = os.getenv("TARGET_PROGRAM", "B.Tech.")
SHEET_ALL_JOBS = "All Jobs"
SHEET_SIGNED_JOBS = "Signed JAFs"

BASE_API = "https://campus.placements.iitb.ac.in/api/v1"
BLOG_URL = "https://campus.placements.iitb.ac.in/blog/placement/"

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

def ensure_jwt_token():
    """Reads the JWT token from env, token.txt, or refreshes from Playwright session."""
    env_token = os.getenv("PORTAL_TOKEN", "").strip()
    if env_token:
        return env_token

    token = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            token = f.read().strip()
            
    if token:
        return token

    if os.path.exists(USER_DATA_DIR):
        print("[*] Attempting token refresh from persistent browser session...")
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
                
                if token:
                    with open(TOKEN_FILE, "w") as f:
                        f.write(token.strip())
                    return token.strip()
        except Exception as e:
            print(f"[!] Warning on auto-refresh via browser: {e}")

    return token

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
    """Fetches detailed salary distribution for a job ID."""
    job_id, headers = args
    try:
        r = requests.get(f"{BASE_API}/job/{job_id}", headers=headers, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return job_id, d.get("salaries", []), d.get("currency", "INR")
    except Exception:
        pass
    return job_id, [], "INR"

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
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(fetch_single_job_salary, [(jid, headers) for jid in job_ids])
        for jid, salaries, currency in results:
            job_salary_map[jid] = (salaries, currency)

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
            "signed": "Yes" if has_signed else "No"
        })

    return processed_jobs

def apply_sheet_formatting_and_colors(sh, worksheet, num_rows):
    """
    Applies whole-row conditional formatting:
    - Entire Row Clean Mint Green if Eligible ($F2 = "Eligible")
    - Entire Row Clean Soft Rose Red if Not Eligible ($F2 = "Not Eligible")
    - Signed column "Yes" highlighted in bold royal blue
    - Navy blue header
    """
    sheet_id = worksheet.id
    
    # 1a. Clear any old conditional formatting rules on this sheet to avoid duplicates
    try:
        sheet_meta = sh.fetch_sheet_metadata()
        for s in sheet_meta.get("sheets", []):
            if s["properties"]["sheetId"] == sheet_id:
                rules = s.get("conditionalFormats", [])
                del_reqs = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}} for i in range(len(rules) - 1, -1, -1)]
                if del_reqs:
                    sh.batch_update({"requests": del_reqs})
                break
    except Exception as e:
        print(f"[!] Warning clearing old conditional format rules: {e}")

    # 1b. Clear ALL data validation from every cell (removes any residual dropdowns)
    try:
        sh.batch_update({"requests": [{
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": max(num_rows + 10, 500),
                    "startColumnIndex": 0,
                    "endColumnIndex": 10
                },
                "rule": None  # None = clear all validation on this range
            }
        }]})
    except Exception as e:
        print(f"[!] Warning clearing data validation: {e}")

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
    """Updates 'All Jobs' and creates/updates a separate 'Signed JAFs' worksheet."""
    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    # 1. Update All Jobs Sheet
    try:
        ws_all = sh.worksheet(SHEET_ALL_JOBS)
    except Exception:
        try:
            ws_all = sh.worksheet("Sheet1")
            ws_all.update_title(SHEET_ALL_JOBS)
        except Exception:
            ws_all = sh.add_worksheet(title=SHEET_ALL_JOBS, rows="500", cols="10")

    headers = ["Sr No", "Company Name", "Roles", "Base Pay (INR)", "CTC (INR)", "Eligible or Not", "Have I Signed", "Last Date to Sign"]
    
    rows_all = [headers]
    for idx, j in enumerate(jobs, start=1):
        rows_all.append([
            idx,
            j["company"],
            j["role"],
            j["base_pay"],
            j["ctc"],
            j["eligible"],
            j["signed"],
            j["last_day"]
        ])

    ws_all.clear()
    ws_all.update(values=rows_all, range_name="A1")
    apply_sheet_formatting_and_colors(sh, ws_all, len(rows_all))
    print(f"[+] '{SHEET_ALL_JOBS}' updated with {len(jobs)} jobs.")

    # 2. Update Separate 'Signed JAFs' Sheet (sorted by signed time descending)
    signed_jobs = [j for j in jobs if j["signed"] == "Yes"]
    signed_jobs.sort(key=lambda x: x.get("signed_at") or x.get("opens_at") or "", reverse=True)
    
    try:
        ws_signed = sh.worksheet(SHEET_SIGNED_JOBS)
    except Exception:
        ws_signed = sh.add_worksheet(title=SHEET_SIGNED_JOBS, rows="200", cols="10")

    rows_signed = [headers]
    for idx, j in enumerate(signed_jobs, start=1):
        rows_signed.append([
            idx,
            j["company"],
            j["role"],
            j["base_pay"],
            j["ctc"],
            j["eligible"],
            j["signed"],
            j["last_day"]
        ])

    ws_signed.clear()
    ws_signed.update(values=rows_signed, range_name="A1")
    apply_sheet_formatting_and_colors(sh, ws_signed, len(rows_signed))
    print(f"[+] '{SHEET_SIGNED_JOBS}' updated with {len(signed_jobs)} signed jobs.")

def check_jobs():
    """Checks for new jobs, updates sheets, and alerts on newly added ones."""
    token = ensure_jwt_token()
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
            msg += f"• {nj['company']} - {nj['role']} (Base: {nj['base_pay']} | CTC: {nj['ctc']}) [{nj['eligible']}]\n"
        if len(new_jobs) > 5:
            msg += f"...and {len(new_jobs) - 5} more."
        send_notifications("New Placement Job Alert!", msg, DISCORD_WEBHOOK_URL)

    try:
        update_google_sheet(jobs)
    except Exception as e:
        print(f"[!] Error updating Google Sheet: {e}")

    with open(KNOWN_JOBS_FILE, "w") as f:
        json.dump(current_jobs_map, f, indent=2)

def check_blog():
    """Scrapes the placement blog using the persistent browser session and checks for new posts."""
    print("[*] Checking placement blog...")
    html_content = ""
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=True
            )
            page = context.new_page()
            page.goto(BLOG_URL, wait_until="networkidle", timeout=30000)
            html_content = page.content()
            context.close()
    except Exception as e:
        print(f"[!] Error accessing blog: {e}")
        return

    if not html_content:
        return

    soup = BeautifulSoup(html_content, "html.parser")
    posts = []
    
    articles = soup.find_all(["article", "div"], class_=lambda c: c and any(k in c for k in ["post", "entry", "item"]))
    if not articles:
        articles = soup.find_all("a", href=lambda h: h and "/blog/" in h)

    for item in articles:
        title_el = item.find(["h1", "h2", "h3", "h4", "a"])
        link_el = item if item.name == "a" else item.find("a")
        
        title = title_el.get_text(strip=True) if title_el else ""
        link = link_el.get("href", "") if link_el else ""
        
        if title and len(title) > 3 and link:
            posts.append({"title": title, "link": link})

    known_blogs = []
    if os.path.exists(KNOWN_BLOGS_FILE):
        try:
            with open(KNOWN_BLOGS_FILE, "r") as f:
                known_blogs = json.load(f)
        except Exception:
            known_blogs = []

    known_links = {b.get("link") for b in known_blogs if "link" in b}
    new_blogs = [p for p in posts if p["link"] not in known_links]

    if known_blogs and new_blogs:
        msg = f"Found {len(new_blogs)} new blog post(s):\n"
        for nb in new_blogs[:5]:
            msg += f"• {nb['title']}\n{nb['link']}\n"
        send_notifications("New Placement Blog Update!", msg, DISCORD_WEBHOOK_URL)

    if posts:
        with open(KNOWN_BLOGS_FILE, "w") as f:
            json.dump(posts, f, indent=2)

def run_all():
    print("----------------------------------------------------------------")
    print(f"Running Placement Portal & Blog check at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}...")
    try:
        check_jobs()
    except Exception as e:
        print(f"[!] Error during job check: {e}")
    try:
        check_blog()
    except Exception as e:
        print(f"[!] Error during blog check: {e}")
    print("Check complete.")
    print("----------------------------------------------------------------")

if __name__ == "__main__":
    run_all()

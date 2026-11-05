#!/usr/bin/env python3
"""
One-time fix script: deletes and recreates the Google Sheet tabs so all
residual data validation (dropdowns) and old formatting are wiped clean.
Run this ONCE, then never again.

Usage:
    python3 fix_sheet.py
"""
import os
from dotenv import load_dotenv
from common import get_gspread_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
for f in ["config.env", ".env"]:
    path = os.path.join(BASE_DIR, f)
    if os.path.exists(path):
        load_dotenv(path)
        break

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1O96KAd3FrBUikER4fo3IkrLcibPjVnZcXrIziUYKKHY")
SHEETS_TO_RESET = ["All Jobs", "Signed JAFs"]
TEMP = "__fix_tmp__"


def recreate_sheet(sh, title, rows=600, cols=10):
    # Create a temp sheet so we can delete the target without hitting min-sheet limit
    try:
        tmp = sh.add_worksheet(title=TEMP, rows="5", cols="2")
    except Exception:
        tmp = None

    try:
        old = sh.worksheet(title)
        sh.del_worksheet(old)
        print(f"  [✓] Deleted old '{title}'")
    except Exception:
        print(f"  [~] '{title}' didn't exist, will create fresh")

    new_ws = sh.add_worksheet(title=title, rows=str(rows), cols=str(cols))
    print(f"  [✓] Created fresh '{title}'")

    if tmp:
        try:
            sh.del_worksheet(tmp)
        except Exception:
            pass

    return new_ws


def main():
    print("[*] Connecting to Google Sheets...")
    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    print(f"[*] Opened spreadsheet: {sh.title}")
    print("[*] Recreating sheets to wipe all old data validation and formatting...")

    for sheet_title in SHEETS_TO_RESET:
        recreate_sheet(sh, sheet_title)

    print("\n[✓] Done! Both sheets are now fresh with zero dropdowns or old formatting.")
    print("    Run scraper.py now to repopulate them cleanly.")


if __name__ == "__main__":
    main()

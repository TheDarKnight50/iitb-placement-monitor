#!/usr/bin/env python3
"""One-time helper to (re-)establish the IITB SSO session for blog monitoring.

Opens a VISIBLE browser window using the same persistent profile (chrome_data/)
that scraper.py uses. Log in with your IITB credentials; the session cookies
are saved automatically. Then verify the blog is accessible.
"""
import sys
from playwright.sync_api import sync_playwright

BASE_DIR = "/home/aryan/Desktop/Imp/Placements/scripts"
USER_DATA_DIR = f"{BASE_DIR}/chrome_data"
BLOG_URL = "https://campus.placements.iitb.ac.in/blog/placement/"


def main():
    print("Opening browser for IITB SSO login...")
    print("Log in, and wait until you can see the placement blog page.")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-software-rasterizer"
            ]
        )
        page = context.new_page()
        page.goto(BLOG_URL, wait_until="domcontentloaded", timeout=60000)

        print("\nWaiting for you to complete SSO login (up to 5 minutes)...")
        print("Keep this window open - it closes itself once login is detected.")
        print("Current URL:", page.url)
        logged_in = False
        try:
            for _ in range(60):
                page.wait_for_timeout(5000)
                current = page.url
                print("Current URL:", current)
                if "campus.placements.iitb.ac.in/blog" in current:
                    logged_in = True
                    break
        except Exception:
            print("[!] Browser was closed before login completed. Run this again when ready.")
            sys.exit(1)
        if not logged_in:
            print("[!] Timed out waiting for login.")
            context.close()
            sys.exit(1)

        print("[+] Login detected! Verifying blog access...")
        page.goto(BLOG_URL, wait_until="networkidle", timeout=30000)
        if "sso.iitb.ac.in" in page.url:
            print("[!] Still redirected to SSO - login may not have persisted. Try again.")
            context.close()
            sys.exit(1)

        print("[+] Blog session saved to chrome_data/. You can close the browser.")
        context.close()
    print("Done! Blog monitoring will work again on the next check.")


if __name__ == "__main__":
    main()

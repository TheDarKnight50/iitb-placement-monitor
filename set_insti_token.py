#!/usr/bin/env python3
"""Setup helper for the InstiApp session (used to read the placement blog without SSO).

1. Open https://www.insti.app/blog/ in your normal browser (where you are logged in).
2. Press F12 -> Console tab, paste and run:
       copy(localStorage.getItem('sessionid'))
3. Run this script and paste the copied value.
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "insti_token.txt")


def main():
    print("===================================================================")
    print("   INSTIAPP SESSION SETUP (for placement blog / test monitoring)   ")
    print("===================================================================")
    print("1. Open https://www.insti.app/blog/ in your browser (logged in).")
    print("2. F12 -> Console, run:  copy(localStorage.getItem('sessionid'))")
    print("3. Paste the value below.")
    print("===================================================================")

    session = input("\nPaste the sessionid here and press ENTER:\n").strip()
    if session.startswith('"') and session.endswith('"'):
        session = session[1:-1]
    if not session or len(session) < 10:
        print("[!] That doesn't look like a valid session id. Try again.")
        sys.exit(1)

    with open(TOKEN_FILE, "w") as f:
        f.write(session)

    print(f"\n[+] Saved to {TOKEN_FILE}")
    import requests
    try:
        r = requests.get(
            "https://gymkhana.iitb.ac.in/instiapp/api/placement-blog",
            headers={"Cookie": f"sessionid={session}"},
            params={"num": 1},
            timeout=15
        )
        if r.status_code == 200:
            print("[+] Verified! InstiApp accepted the session.")
        else:
            print(f"[!] Warning: API returned {r.status_code}. Session may be invalid/expired.")
    except Exception as e:
        print(f"[!] Connection test failed: {e}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import sys
import os

TOKEN_FILE = "/home/aryan/Desktop/Imp/Placements/scripts/token.txt"

def main():
    print("===================================================================")
    print("   MANUAL IITB PORTAL TOKEN & COOKIE SETUP (NO AUTOMATED BROWSER)  ")
    print("===================================================================")
    print("Follow these 3 quick steps in your normal Chrome / Firefox browser:")
    print("1. Open your browser where you are ALREADY logged into:")
    print("   https://campus.placements.iitb.ac.in/applicant/home")
    print("2. Press F12 (or Right Click -> Inspect) and go to the 'Console' tab.")
    print("3. Paste and run this exact one-line command:\n")
    print("   copy(localStorage.getItem('token'))\n")
    print("   (This copies your portal login JWT token directly to your clipboard!)")
    print("===================================================================")
    
    token = input("\nPaste the copied token here and press ENTER:\n").strip()
    
    # Strip any extra quotes if copied with quotes
    if token.startswith('"') and token.endswith('"'):
        token = token[1:-1]
        
    if not token or len(token) < 20:
        print("[!] That doesn't look like a valid token. Please try again.")
        sys.exit(1)
        
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
        
    print(f"\n[+] Success! Token saved to {TOKEN_FILE}")
    print("[+] Now testing your token against the placement API...")
    
    import requests
    try:
        r = requests.get(
            "https://campus.placements.iitb.ac.in/api/v1/auth/slt",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        if r.status_code == 200:
            print("[✓] Verified! Portal API accepted your token successfully.")
        else:
            print(f"[!] Warning: Portal returned status {r.status_code}. (You can still run the scraper).")
    except Exception as e:
        print(f"[!] Connection test: {e}")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import os
import json
import base64
import shutil
import platform
import subprocess
from datetime import datetime, timezone
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# All paths are relative to this repo folder, so the project runs from any location.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
TOKEN_FILE = os.path.join(BASE_DIR, "google_token.json")
DEAD_WEBHOOKS_FILE = os.path.join(BASE_DIR, ".dead_webhooks.json")

def get_gspread_client():
    """
    Returns an authorized gspread client.
    Supports either:
    1. Service account json (if 'type': 'service_account' in credentials.json)
    2. OAuth2 client_secret (generates or refreshes google_token.json)
    """
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, "r") as f:
                creds_data = json.load(f)
            
            # Case 1: Service Account JSON
            if creds_data.get("type") == "service_account":
                creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, SCOPES)
                return gspread.authorize(creds)
        except Exception as e:
            print(f"[!] Warning reading service account: {e}")

    # Case 2: OAuth2 Client Credentials
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"Neither valid token nor {CREDENTIALS_FILE} found. "
                    "Please place your Google Cloud OAuth2 or Service Account JSON file as 'credentials.json' "
                    "in the scripts folder."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
            
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return gspread.authorize(creds)

def parse_jwt(token_str):
    """Parses payload from JWT string without external cryptographic dependencies."""
    try:
        parts = token_str.strip().split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1]
            # Add base64 padding
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            decoded = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
            return json.loads(decoded)
    except Exception as e:
        print(f"[!] Error decoding JWT: {e}")
    return {}

def mask_webhook(url):
    """Shows only the webhook id (never the secret token) for logs and alerts."""
    try:
        parts = url.rstrip("/").split("/")
        return f".../webhooks/{parts[-2]}/****"
    except Exception:
        return "(invalid webhook URL)"


def _load_dead_webhooks():
    try:
        with open(DEAD_WEBHOOKS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_dead_webhooks(data):
    try:
        if data:
            with open(DEAD_WEBHOOKS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        elif os.path.exists(DEAD_WEBHOOKS_FILE):
            os.remove(DEAD_WEBHOOKS_FILE)
    except Exception as e:
        print(f"[!] Could not update {DEAD_WEBHOOKS_FILE}: {e}")


def _mark_webhook(url, dead, status=None):
    """Remembers dead webhooks so we alert ONCE (desktop) instead of failing silently
    every run, and forgets them as soon as a send succeeds again."""
    data = _load_dead_webhooks()
    key = mask_webhook(url)
    if dead and key not in data:
        data[key] = {"status": status, "since": datetime.now(timezone.utc).isoformat()}
        _save_dead_webhooks(data)
        desktop_notify(
            "⚠️ Discord webhook is dead",
            f"Discord returned {status} for {key}. The channel or webhook was probably deleted. "
            "Update config.env and run: python3 scraper.py --check-webhooks")
    elif not dead and key in data:
        data.pop(key)
        _save_dead_webhooks(data)
        print(f"[+] Discord webhook {key} is working again.")


def send_discord_embed(webhook_url, title, description, color=3447003):
    """Posts a single embed to a Discord webhook. Returns True on success."""
    if not webhook_url or not webhook_url.strip():
        return False
    try:
        payload = {
            "embeds": [{
                "title": title[:256],
                "description": description[:4000],
                "color": color
            }]
        }
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code == 429:
            # Rate limited: wait the time Discord asks for (capped) and retry once.
            import time
            try:
                retry_after = float(r.json().get("retry_after", 1))
            except Exception:
                retry_after = 1.0
            time.sleep(min(retry_after, 10))
            r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code in (401, 403, 404):
            print(f"[!] Discord webhook {mask_webhook(webhook_url)} is dead ({r.status_code}): {r.text[:200]}")
            _mark_webhook(webhook_url, dead=True, status=r.status_code)
            return False
        if r.status_code not in (200, 204):
            print(f"[!] Discord webhook returned {r.status_code}: {r.text[:200]}")
            return False
        _mark_webhook(webhook_url, dead=False)
        return True
    except Exception as e:
        print(f"[!] Discord notification error: {e}")
        return False


def check_webhook(url):
    """GET on a webhook URL returns its metadata without posting a message.
    Returns (ok: bool, detail: str)."""
    if not url or not url.strip():
        return False, "not set"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            info = r.json()
            return True, f"OK - '{info.get('name', 'webhook')}' in channel {info.get('channel_id', '?')}"
        if r.status_code in (401, 403, 404):
            return False, f"DEAD ({r.status_code}) - channel or webhook was deleted"
        return False, f"unexpected status {r.status_code}"
    except Exception as e:
        return False, f"error: {e}"


def desktop_notify(title, message):
    """Desktop notification on Linux (notify-send) or macOS (osascript).
    Arguments are passed as a list - never through a shell - so text from blog posts
    (quotes, $(...), backticks) cannot break or inject commands."""
    message = message.replace("**", "")  # Discord markdown doesn't render on the desktop
    try:
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "--", title, message], timeout=10, check=False)
        elif platform.system() == "Darwin" and shutil.which("osascript"):
            script = ["-e", "on run argv",
                      "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                      "-e", "end run"]
            subprocess.run(["osascript", *script, title, message[:250]], timeout=10, check=False)
    except Exception as e:
        print(f"[!] Desktop notification error: {e}")


def send_notifications(title, message, discord_webhook_url=None, color=3447003):
    """Sends desktop notification and Discord webhook if configured."""
    print(f"[NOTIFICATION] {title}: {message}")
    desktop_notify(title, message)
    send_discord_embed(discord_webhook_url, title, message, color)

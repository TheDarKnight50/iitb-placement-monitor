#!/usr/bin/env python3
import os
import json
import base64
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

CREDENTIALS_FILE = "/home/aryan/Desktop/Imp/Placements/scripts/credentials.json"
TOKEN_FILE = "/home/aryan/Desktop/Imp/Placements/scripts/google_token.json"

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
        if r.status_code not in (200, 204):
            print(f"[!] Discord webhook returned {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[!] Discord notification error: {e}")
        return False

def send_notifications(title, message, discord_webhook_url=None, color=3447003):
    """Sends desktop notification and Discord webhook if configured."""
    print(f"[NOTIFICATION] {title}: {message}")

    # 1. Desktop notification via notify-send
    try:
        os.system(f'notify-send "{title}" "{message}"')
    except Exception as e:
        print(f"[!] notify-send error: {e}")

    # 2. Discord Webhook
    send_discord_embed(discord_webhook_url, title, message, color)

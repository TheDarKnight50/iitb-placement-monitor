# IIT Bombay Placement Portal & Blog Auto-Tracker 🎯

An automated monitoring suite for the IIT Bombay campus placement portal and placement blog. It runs continuously in the background, extracts newly opened jobs and blog notices every 15 minutes, synchronizes with a Google Sheet, and delivers real-time notifications via Desktop alerts (`notify-send`) and Discord webhooks.

---

## 🌟 Key Features

1. **Automated Job Tracking & Diffs**:
   - Fetches active placement openings from `campus.placements.iitb.ac.in`.
   - Identifies new jobs and sends instant alerts.
2. **Personalized Salary & Eligibility Filtering**:
   - Resolves compensation details specifically for your target degree/program (default: `B.Tech.`).
   - Ineligible jobs automatically display `N/A`.
   - International offers (JPY, USD, EUR, etc.) are converted to **INR (LPA / Cr)** using real-time exchange rates.
3. **Clean Google Sheets Sync**:
   - **`All Jobs` tab**: Full list of placement openings with whole-row conditional colors (Soft Green for **Eligible**, Soft Red for **Not Eligible**).
   - **`Signed JAFs` tab**: Dedicated sheet showing exclusively the JAFs you have signed.
   - Simplified date formatting for deadlines (e.g., `26th Oct`, `31st Oct`).
4. **Placement Blog & Test Alerts**:
   - Monitors the placement blog via the public InstiApp mirror (`gymkhana.iitb.ac.in/instiapp`) - no SSO login required.
   - Test-related posts are routed to a dedicated Discord channel, with test date/time auto-parsed from the post text.
5. **Test Reminders for Signed JAFs**:
   - Tests are stored locally, matched against your signed JAFs, and you get 2 reminders on Discord: 1 hour and 30 minutes before each test.
6. **Instant Terminal Trigger (`fetch-placement`)**:
   - Update your sheet and check for alerts anytime on demand with a single short command.
7. **Zero-Window Background Daemon**:
   - Runs silently via `systemd` user service without popping up browser windows or interrupting your workflow.
8. **Deadline Reminders**:
   - A daily digest (default 9 AM IST) of JAFs you're **eligible for but haven't signed** that close in the next 48 hours.
   - A last-call warning 3 hours before each of those JAFs closes (re-sent if the deadline is extended).
9. **JAF Change Detection**:
   - Alerts when an existing JAF changes: deadline extended, CTC revised, eligibility changed, test round added.
   - Becoming eligible or a deadline/test change on an eligible JAF triggers a desktop pop-up; minor edits go to Discord only.
   - Ignores exchange-rate noise on foreign-currency offers.
10. **Self-Monitoring**:
    - Alerts (desktop + Discord) when the portal token is about to expire or has expired, when the InstiApp session is rejected, or when a Discord webhook dies (e.g. its channel was deleted). Each problem is alerted at most once every 12 hours.
    - `python3 scraper.py --check-webhooks` verifies every webhook without posting anything.
11. **Summary Tab & Calendar Export**:
    - A `Summary` sheet tab with totals, eligible JAFs closing soon, top eligible CTCs, upcoming tests and the last update time.
    - `placements.ics` with deadlines for eligible unsigned JAFs and tests for signed JAFs (with alarms) - import it into Google / Apple Calendar to get reminders on your phone.

---

## 📁 Repository Structure

```
├── common.py             # Shared utilities (Google Sheets client, JWT decoding, notifications, webhook checks)
├── scraper.py            # Core engine (API scraper, salary extractor, blog checker, sheets updater)
├── job_changes.py        # Detects changes to existing JAFs between runs
├── deadlines.py          # Deadline digest + last-call warning logic
├── calendar_export.py    # Builds the placements.ics calendar file
├── tests/                # Unit tests (python -m unittest discover -s tests)
├── set_token.py          # Quick setup helper to paste your portal JWT session token
├── set_insti_token.py    # Setup helper for the InstiApp session (blog/test monitoring)
├── blog_login.py         # Optional: re-establish portal SSO via browser (for JWT auto-refresh)
├── run_monitor.sh        # Background loop runner (every 15 minutes)
├── config.env.example    # Configuration template (fill this in as config.env)
├── requirements.txt      # Python dependencies
└── README.md             # Setup guide
```

> **Security Note**: All secrets (`credentials.json`, `token.txt`, `insti_token.txt`, `config.env`, `chrome_data/`) are strictly git-ignored so your private credentials and tokens are never pushed to version control.

---

## 🚀 Quick Setup Guide

### 1. Prerequisites
- Python 3.10+
- Linux (Ubuntu / Debian / Arch / Fedora)

Install the dependencies:
```bash
pip install -r requirements.txt
playwright install chromium
```

---

### 2. Configure Your Settings (`config.env`)

Copy `config.env.example` to `config.env`:
```bash
cp config.env.example config.env
```

Open `config.env` in any text editor and fill in your details:
```env
# 1. Google Sheets Spreadsheet ID (from your sheet URL: /spreadsheets/d/<ID>/edit)
SPREADSHEET_ID=your_spreadsheet_id_here

# 2. Path to your Google Cloud service account JSON key (keep in root directory)
GOOGLE_CREDENTIALS_FILE=credentials.json

# 3. Target Degree for salary extraction (Default: B.Tech.)
TARGET_PROGRAM=B.Tech.

# 4. Discord Webhook URL for alerts (Optional - leave blank if not used)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

---

### 3. Google Sheets Access (Free Google Cloud Account)

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. Enable the **Google Sheets API**.
3. Create a **Service Account** under **APIs & Services > Credentials**.
4. Download the JSON key file, rename it to `credentials.json`, and place it in this repository directory.
5. Open your Google Sheet, click **Share**, and give **Editor** access to the service account email (e.g., `sheet-bot@...iam.gserviceaccount.com`).

---

### 4. Capture Placement Portal Login Token

1. Open your regular browser where you are logged into:
   `https://campus.placements.iitb.ac.in/applicant/home`
2. Press **F12** (Developer Tools) and open the **Console** tab.
3. Paste and run this command:
   ```javascript
   copy(localStorage.getItem('token'))
   ```
4. In your terminal, run:
   ```bash
   python3 set_token.py
   ```
   Paste the token and hit Enter. The token will be safely stored in `token.txt` (git-ignored).

### 4b. Capture InstiApp Session (for blog & test monitoring)

1. Open your normal browser where you are logged into [insti.app](https://www.insti.app/blog/) (the placement blog's public mirror).
2. Press **F12**, open the **Console** tab, paste and run:
   ```javascript
   copy(localStorage.getItem('sessionid'))
   ```
3. In your terminal, run:
   ```bash
   python3 set_insti_token.py
   ```
   Paste the value and hit Enter. It will be saved to `insti_token.txt` (git-ignored).

### 4c. Discord Channel Webhooks (Optional)

For per-channel routing, create one webhook per channel
(*Edit Channel → Integrations → Webhooks → New Webhook → Copy URL*) and fill in `config.env`:

| Variable | What goes there | Falls back to |
|---|---|---|
| `DISCORD_WEBHOOK_NEW_JAFS` | New JAFs + JAF changes | `DISCORD_WEBHOOK_URL` |
| `DISCORD_WEBHOOK_TESTS` | Test announcements | `DISCORD_WEBHOOK_URL` |
| `DISCORD_WEBHOOK_REMINDERS` | 1h / 30m test reminders, form reminders, 11 PM "closing tonight" | `DISCORD_WEBHOOK_URL` |
| `DISCORD_WEBHOOK_FORMS` | Mandatory forms | `DISCORD_WEBHOOK_URL` |
| `DISCORD_WEBHOOK_SHORTLISTS` | Interview / test shortlists | `DISCORD_WEBHOOK_URL` |
| `DISCORD_WEBHOOK_DEADLINES` | Deadline digest + last-call warnings | `DISCORD_WEBHOOK_REMINDERS` |
| `DISCORD_WEBHOOK_BLOG` | Other blog posts | `DISCORD_WEBHOOK_URL` |
| `DISCORD_WEBHOOK_ALERTS` | Bot health (expired tokens, dead webhooks) | `DISCORD_WEBHOOK_URL` |

> ⚠️ Deleting a Discord channel also deletes its webhooks. After changing channels, run
> `python3 scraper.py --check-webhooks` to confirm every webhook still works (it posts nothing).

---

### 5. Run Immediate Sync

Test the scraper manually:
```bash
python3 scraper.py
```
Check your Google Sheet! The **`All Jobs`**, **`Signed JAFs`** and **`Summary`** tabs will be updated.

Other commands:
```bash
python3 scraper.py --check-webhooks   # verify all Discord webhooks (sends nothing)
python3 scraper.py --backfill 2       # re-send test posts from the last 2 days
python -m unittest discover -s tests  # run the unit tests
```

To get deadlines and tests in your phone's calendar, import `placements.ics`
(Google Calendar → Settings → Import & export). Re-importing updates existing events instead of duplicating them.

---

### 6. Set Up On-Demand Terminal Command (`fetch-placement`)

To run an instant update whenever you want by typing `fetch-placement` in any terminal:
```bash
mkdir -p ~/.local/bin
cat << 'EOF' > ~/.local/bin/fetch-placement
#!/bin/bash
python3 /path/to/repo/scraper.py
EOF
chmod +x ~/.local/bin/fetch-placement
```
*(Make sure `~/.local/bin` is in your `$PATH`, or add an alias `alias fetch-placement="python3 /path/to/repo/scraper.py"` to `~/.bashrc`)*.

---

### 7. Run Continuously in the Background (Systemd)

To automatically monitor every 15 minutes whenever your laptop is running:

1. Create a user service file at `~/.config/systemd/user/placement-monitor.service`:
   ```ini
   [Unit]
   Description=IITB Placement & Blog Monitor Service
   After=network.target

   [Service]
   Type=simple
   ExecStart=/bin/bash /absolute/path/to/repo/run_monitor.sh
   WorkingDirectory=/absolute/path/to/repo
   Restart=always
   RestartSec=60
   Environment="DISPLAY=:0"
   Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"

   [Install]
   WantedBy=default.target
   ```

2. Enable and start the service:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now placement-monitor.service
   ```

3. View live logs:
   ```bash
   tail -f monitor.log
   ```

---

## 🛡️ License
Distributed under the MIT License.

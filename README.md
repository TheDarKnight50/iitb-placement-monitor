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
4. **Placement Blog Alerts**:
   - Monitors `https://campus.placements.iitb.ac.in/blog/placement/` for newly posted announcements.
5. **Instant Terminal Trigger (`fetch-placement`)**:
   - Update your sheet and check for alerts anytime on demand with a single short command.
6. **Zero-Window Background Daemon**:
   - Runs silently via `systemd` user service without popping up browser windows or interrupting your workflow.

---

## 📁 Repository Structure

```
├── common.py             # Shared utilities (Google Sheets client, JWT decoding, notifications)
├── scraper.py            # Core engine (API scraper, salary extractor, blog checker, sheets updater)
├── set_token.py          # Quick setup helper to paste your portal JWT session token
├── run_monitor.sh        # Background loop runner (every 15 minutes)
├── config.env.example    # Configuration template (fill this in as config.env)
├── requirements.txt      # Python dependencies
└── README.md             # Setup guide
```

> **Security Note**: All secrets (`credentials.json`, `token.txt`, `config.env`, `chrome_data/`) are strictly git-ignored so your private credentials and tokens are never pushed to version control.

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

---

### 5. Run Immediate Sync

Test the scraper manually:
```bash
python3 scraper.py
```
Check your Google Sheet! Both **`All Jobs`** and **`Signed JAFs`** tabs will be updated.

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
   ExecStart=/bin/bash /absolute/path/to/run_monitor.sh
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

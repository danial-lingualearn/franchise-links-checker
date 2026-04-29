# Franchise Links Dashboard

Automated daily franchise link scanning with a web-based dashboard for viewing results and downloading reports.

## Features

- **Daily Automated Scans** — Runs at 00:00 UTC via GitHub Actions
- **Link Classification** — Detects redirects, errors, maintenance pages, parked domains, bot blocks
- **Web Dashboard** — View scan history, filter by status/country, download CSVs
- **Email Notifications** — Optional email reports after each scan
- **Free Hosting** — Dashboard hosted on Streamlit Cloud, data stored in GitHub repo

---

## Quick Start

### 1. Deploy Dashboard on Streamlit Cloud (Free)

1. Go to [share.streamlit.io](https://share.streamlit.io)
2. Click **New App**
3. Connect your GitHub repository
4. Configure:
   - **Main file path:** `dashboard.py`
   - **Python version:** 3.10+
5. Click **Deploy!**

Your dashboard will be live at `https://your-repo-name.streamlit.app`

### 2. Configure Email Notifications (Optional)

In Streamlit Cloud dashboard settings, add these secrets:

| Secret | Description |
|--------|-------------|
| `EMAIL_USER` | Your Gmail address |
| `EMAIL_PASS` | Gmail App Password (not regular password) |
| `EMAIL_TO` | Recipient email address |
| `SMTP_HOST` | `smtp.gmail.com` (default) |
| `SMTP_PORT` | `587` (default) |

**Gmail App Password setup:**
1. Enable 2FA on your Google Account
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Create app password for "Mail"
4. Use this 16-character password in `EMAIL_PASS`

---

## Local Development

### Prerequisites

- Python 3.10+
- pip

### Setup

```bash
# Clone repository
git clone https://github.com/your-username/franchise-links-checker.git
cd franchise-links-checker

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
playwright install-deps chromium
```

### Run Scan Locally

```bash
python main.py --use-browser
```

### Run Dashboard Locally

```bash
streamlit run dashboard.py
```

Dashboard opens at `http://localhost:8501`

---

## Project Structure

```
franchise-links-checker/
├── .github/
│   └── workflows/
│       ├── daily-scan.yml      # Daily scan + auto-commit
│       └── daily-links-check.yml # Legacy workflow (can remove)
├── .streamlit/
│   ├── config.toml             # Streamlit theme config
│   └── secrets.toml.example    # Email config template
├── data/                       # Scan results (auto-created)
│   └── Franchise_Links_Report_*.csv
├── dashboard.py                # Streamlit dashboard
├── main.py                     # Link scanner
└── requirements.txt            # Python dependencies
```

---

## How It Works

### Daily Scan Workflow

1. **GitHub Actions** triggers at 00:00 UTC
2. **main.py** extracts links from franchise page
3. Each link is checked and classified:
   - **OK** — Working link
   - **REDIRECT_MAIN** — Redirects to .com domain
   - **REDIRECT_OTHER** — Redirects to other domain
   - **MAINTENANCE** — Site under maintenance
   - **PARKED** — Domain for sale/parked
   - **BOT_BLOCKED** — CAPTCHA or bot detection
   - **NOT_FOUND** — 404 error
   - **TIMEOUT** — Request timed out
   - **EMPTY_PAGE** — Page has no content
   - **BRAND_MISMATCH** — Title doesn't match brand
4. CSV saved to `data/` folder
5. Results committed and pushed to repository
6. Email sent (if configured)

### Dashboard

- Reads all CSV files from `data/` folder
- Displays summary metrics and detailed results
- Filter by status and country
- Download individual scan reports

---

## GitHub Actions Configuration

The workflow file `.github/workflows/daily-scan.yml` handles:

- Daily scheduled runs (cron: `0 0 * * *`)
- Manual trigger via GitHub Actions UI
- Python environment setup
- Playwright browser installation
- Scan execution
- Auto-commit of results to `data/` folder

**To change scan time:** Edit the cron expression in `daily-scan.yml`

| Schedule | Cron Expression |
|----------|-----------------|
| Midnight UTC | `0 0 * * *` |
| 3 AM UTC | `0 3 * * *` |
| 9 AM EST | `0 14 * * *` |
| Every 6 hours | `0 */6 * * *` |

---

## Status Definitions

| Status | Description |
|--------|-------------|
| `OK` | Link working, valid content |
| `COMING_SOON` | Placeholder, no link yet |
| `REDIRECT_MAIN` | Redirects to .com domain |
| `REDIRECT_OTHER` | Redirects to other domain |
| `MAINTENANCE` | Site temporarily down for maintenance |
| `PARKED` | Domain parked/for sale |
| `BOT_BLOCKED` | CAPTCHA or bot detection page |
| `NOT_FOUND` | 404 error |
| `FORBIDDEN` | 403 error |
| `TIMEOUT` | Request timed out |
| `CONNECTION_ERROR` | Network/SSL error |
| `EMPTY_PAGE` | Page returned but no content |
| `BRAND_MISMATCH` | Title doesn't contain brand keywords |
| `HTTP_429` | Rate limited |
| `SERVER_ERROR_*` | 5xx server errors |

---

## Troubleshooting

### Dashboard shows "No scan results found"

- Ensure at least one scan has run successfully
- Check that `data/` folder exists and contains CSV files
- Verify file naming: `Franchise_Links_Report_YYYYMMDD_HHMMSS.csv`

### Email not sending

- Verify all email secrets are set in Streamlit Cloud
- Use Gmail App Password, not regular password
- Check 2FA is enabled on Gmail account

### Scan failing in GitHub Actions

- Check Action logs for specific error
- Ensure Playwright dependencies installed (`playwright install-deps`)
- Increase timeout if scan takes >30 min (current: 45 min)

### Streamlit Cloud deployment fails

- Verify `requirements.txt` includes all dependencies
- Ensure `dashboard.py` has no syntax errors
- Check file paths are correct (relative to repo root)

---

## License

Private — Internal use only

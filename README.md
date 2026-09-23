# ASTRA (Automated System for Trading Risk & Analysis)

ASTRA is a lightweight, fully automated, local trading telemetry and alerting system engineered specifically for Indian Equity Markets (NSE). It scans budget-aligned equities in real time, evaluates technical indicators (RSI and MACD), verifies trade feasibility against live cash liquidity in Angel One, and dispatches real-time **BUY** and **SELL** alerts via Telegram while maintaining live performance dashboards in Google Sheets.

---

## 1. System Architecture & Core Workflow

ASTRA operates as a headless loop that adapts dynamically to market hours (9:15 AM – 3:30 PM IST, Monday–Friday).

```
 ┌─────────────────────────────────────────────────────────┐
 │                   Angel One SmartAPI                    │
 │         (TOTP Auth -> Fetch Liquidity & Holdings)       │
 └────────────────────────────┬────────────────────────────┘
                              │
                              ▼
 ┌─────────────────────────────────────────────────────────┐
 │                   Dynamic Ticker Sweep                  │
 │   (Downloads official NSE EQUITY_L.csv -> Filters Price)│
 └────────────────────────────┬────────────────────────────┘
                              │
                              ▼
 ┌─────────────────────────────────────────────────────────┐
 │              Technical Analysis Engine                  │
 │            (yfinance -> RSI + MACD Signal)              │
 └────────────────────────────┬────────────────────────────┘
                              │
                              ▼
 ┌─────────────────────────────────────────────────────────┐
 │              Triple-Bound Capital Validation            │
 │     (Upper Bound <= Price <= Lower Bound <= Cash)       │
 └────────────────────────────┬────────────────────────────┘
                              │
       ┌──────────────────────┴──────────────────────┐
       ▼                                             ▼
┌──────────────┐                             ┌──────────────┐
│ Telegram Bot │                             │ Google Sheets│
│(BUY/SELL Msg)│                             │ (3-Tab Sync) │
└──────────────┘                             └──────────────┘

```

### Execution Phases:

1. **Authentication & Telemetry Sync**: On every loop, ASTRA authenticates with Angel One SmartAPI using TOTP (`pyotp`) to fetch live available trading cash and active Demat holdings.
2. **Portfolio Health & SELL Evaluation**: If you hold active positions, ASTRA strips broker-specific tags (e.g., `-EQ`), fetches live market price action via `yfinance`, and evaluates if any positions hit profit-taking targets (RSI $\ge 65$, bearish MACD cross) or the strict -5.0% stop-loss threshold.
3. **NSE Market Sweep & BUY Evaluation**: During live market hours, it fetches the official NSE stock list (`EQUITY_L.csv`), filters symbols matching your `.env` budget allocation window, and evaluates them for bullish oversold setups (RSI $\le 35$ with MACD crossover).
4. **Triple-Bound Capital Safety Check**: A candidate BUY alert is **only** generated if:
   * Stock Price $\le$ `MAX_TRADE_ALLOCATION`
   * Stock Price $\ge$ `MIN_TRADE_ALLOCATION`
   * Calculated Trade Capital $\le$ Live Angel One Available Cash
5. **Non-Destructive Dashboard Update**: Updates your Google Sheets dashboard across three tabs (`Market Scan`, `Raw Data`, `Wallet_and_Holdings`). If the market is closed or `yfinance` returns empty datasets, previous scan data is preserved untouched.

---

## 2. Directory Structure & Module Breakdown

```text
ASTRA/
├── .env                       # Environment variables & secrets
├── .eod_state.json            # Local transaction state persistence
├── service_account.json       # Google Cloud Service Account credentials
├── requirements.txt           # Python dependency manifest
└── src/
    ├── __init__.py
    ├── main.py                # Core execution loop & pipeline orchestrator
    ├── decision_engine.py     # RSI & MACD mathematical analysis engine
    ├── portfolio_fetcher.py   # Non-destructive Google Sheets telemetry sync
    ├── smartapi.py            # Angel One SmartAPI client (TOTP & Portfolio)
    ├── telegram.py            # Telegram alert dispatch & command handler
    ├── mailer.py              # EOD / EOW / EOM email reporting module
    └── tickers.py             # Dynamic NSE stock list sweeper

```

### Key Modules:

* **`src/main.py`**: The entry point. Manages ANSI terminal logging, controls the adaptive loop execution (5-minute cycle during live market, 15-minute sleep off-market), enforces triple-bound capital checks, and triggers alerts.
* **`src/decision_engine.py`**: Calculates 14-period RSI and MACD (12, 26, 9) signal line crossovers using `pandas` and `numpy`.
* **`src/smartapi.py`**: Interacts with Angel One SmartAPI via `smartapi-python` to log in using TOTP and fetch RMS net available funds and active Demat holdings.
* **`src/portfolio_fetcher.py`**: Interfaces with Google Sheets via `gspread`. Flattens nested metric lists to prevent Google API `400` errors and preserves historical data when off-market.
* **`src/telegram.py`**: Sends structured HTML BUY/SELL alerts and handles polling commands (e.g., `/status`, `/portfolio`, `/help`). Includes an 8-hour anti-spam cooldown window per ticker.
* **`src/tickers.py`**: Downloads official equity lists directly from `nsearvh.com` / `nseindia.com`, caching `EQUITY_L.csv` locally to dynamically discover budget-aligned symbols.
* **`src/mailer.py`**: Compiles EOD/EOW/EOM performance summaries and emails them via SMTP.

---

## 3. Environment Configuration (`.env`)

Create a `.env` file in the project root directory with the following variables:

```ini
# ==========================================
# ANGEL ONE SMARTAPI CREDENTIALS
# ==========================================
ANGEL_API_KEY=your_angel_one_api_key
ANGEL_CLIENT_CODE=your_client_code
ANGEL_PIN=your_account_pin
ANGEL_TOTP_SECRET=your_base32_totp_secret

# ==========================================
# TELEGRAM BOT CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

# ==========================================
# GOOGLE SHEETS TELEMETRY
# ==========================================
GOOGLE_SHEETS_SPREADSHEET_ID=your_google_sheet_id_here
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json

# ==========================================
# SMTP MAILER SETTINGS
# ==========================================
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SENDER_EMAIL=your_email@gmail.com
SENDER_PASSWORD=your_app_passcode
RECEIVER_EMAIL=your_email@gmail.com
MAILER_TIME=EOD   # Options: EOD, EOW, EOM

# ==========================================
# RISK MANAGEMENT & CAPITAL BOUNDS
# ==========================================
MIN_TRADE_ALLOCATION=100.0       # Lower price bound (INR)
MAX_TRADE_ALLOCATION=2000.0      # Upper price bound (INR)
PORTFOLIO_ALLOCATION_PCT=0.10    # Allocate 10% of available cash per trade
RSI_LOWER_THRESHOLD=35.0         # Oversold threshold
RSI_UPPER_THRESHOLD=65.0         # Overbought threshold
TICKERS_COUNT=50                 # Number of stocks to evaluate per cycle
ASTRA_CYCLE_BUFFER=5             # Execution loop interval in minutes

```

---

## 4. External Services & Infrastructure Setup

Before executing the system, follow these steps to configure your third-party integrations:

### 4.1. Google SMTP Setup (Email Dispatcher)

To allow `src/mailer.py` to dispatch EOD summaries via Gmail without storing your raw password:

1. Log into your Google Account and navigate to **Security** settings.
2. Ensure **2-Step Verification** is enabled.
3. Search for **App Passwords** in the Google Account search bar.
4. Create a new App Password (name it `ASTRA-Mailer`).
5. Copy the generated 16-character string and paste it into `SENDER_PASSWORD` in your `.env`.

### 4.2. Angel One SmartAPI Setup

To grant ASTRA programmatic access to your live cash liquidity and Demat holdings:

1. Register on the [Angel One SmartAPI Developer Portal](https://smartapi.angelbroking.com/?utm_source=gemini).
2. Click **Add App**, select **Historical API** (if needed) or Standard Trading API, name your application `ASTRA`, and set any placeholder URL for Redirect/Postback.
3. Save the generated **API Key** (`ANGEL_API_KEY`).
4. Enable **TOTP 2FA** on your Angel One trading account via the mobile app or website.
5. Record the **Base32 QR Code / Secret Key** shown during setup—this is your `ANGEL_TOTP_SECRET`.

### 4.3. Google Cloud Service Account Setup (Sheets Telemetry)

To allow ASTRA to read/write performance dashboards without manual authorization:

1. Go to the [Google Cloud Console](https://console.cloud.google.com/?utm_source=gemini).
2. Create a new project (e.g., `ASTRA-Telemetry`).
3. Navigate to **APIs & Services > Library** and enable the **Google Sheets API** and **Google Drive API**.
4. Go to **IAM & Admin > Service Accounts**, click **Create Service Account**, name it `astra-bot`, and click **Done**.
5. Click on your newly created Service Account email, navigate to the **Keys** tab, click **Add Key > Create New Key**, select **JSON**, and download it.
6. Rename this downloaded file to `service_account.json` and place it directly in the root directory of ASTRA.
7. Open `service_account.json`, copy the `client_email` address inside it.
8. Create a blank Google Sheet, click **Share**, paste the service account `client_email`, assign it **Editor** permissions, and copy the Spreadsheet ID from the URL (`https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`).

---

## 5. Setup & Usage Instructions

### Step 1: Virtual Environment Creation (Arch Linux)

```bash
# Clone or navigate to the repository
cd ~/Desktop/Projects/ASTRA

# Create isolated Python virtual environment
python3 -m venv .

# Activate virtual environment
source bin/activate

```

### Step 2: Install Dependencies

```bash
pip install -r requirements.txt

```

### Step 3: Configure Environment Variables

Edit `.env` file following the instructions below as a reference.

#### Detailed `.env` Variable Map:

* **Angel One Block**:
* `ANGEL_API_KEY`: API Key generated from SmartAPI portal.
* `ANGEL_CLIENT_CODE`: Your Angel One account ID/Client ID.
* `ANGEL_PIN`: Your 4-digit MPIN.
* `ANGEL_TOTP_SECRET`: Base32 secret key retrieved during 2FA setup.


* **Telegram Block**:
* `TELEGRAM_BOT_TOKEN`: Token obtained from Telegram `@BotFather`.
* `TELEGRAM_CHAT_ID`: Your numerical Telegram Chat ID (obtainable via `@userinfobot`).


* **Google Telemetry Block**:
* `GOOGLE_SHEETS_SPREADSHEET_ID`: Unique key string extracted from your sheet URL.
* `GOOGLE_SERVICE_ACCOUNT_FILE`: Relative path to your service account key (`service_account.json`).


* **SMTP Mailer Block**:
* `SENDER_EMAIL` / `RECEIVER_EMAIL`: Email addresses for automated summaries.
* `SENDER_PASSWORD`: 16-character Google App Password.


* **Risk & Capital Management**:
* `MIN_TRADE_ALLOCATION` / `MAX_TRADE_ALLOCATION`: Lower and upper per-stock price limits (in INR).
* `PORTFOLIO_ALLOCATION_PCT`: Fractional cap of available liquid capital allocated per trade (e.g., `0.10` = 10%).


### Step 4: Execute ASTRA

```bash
python src/main.py

```

---

## 6. Telegram Commands Reference

You can interact with ASTRA in real time directly from your Telegram chat:

* `/status` — Output system status and cycle info
* `/sheet` or `/spreadsheet` — Get live Google Sheets telemetry URL
* `/config` or `/cfg` — Read raw .env configuration
* `/set KEY VALUE` — Set or overwrite any .env variable
* `/restore_default` or `/restore` — Restore .env from .env.bak
* `/suspend DURATION` — Pause system (e.g., /suspend 5m or /suspend 2h)
* `/portfolio` — Output live portfolio feed

---

## 7. Operational Safety Features

* **No Hardcoded Stock Lists**: Dynamically adjusts stock selection based on live market pricing and `.env` bounds.
* **Cooldown Mechanism**: Prevents repeated alerts for the same stock within 8 hours.
* **Non-Destructive Sheet Updates**: Preserves dashboard historical records when market scanning is paused or offline.
* **Graceful Exit**: Handles `KeyboardInterrupt` (`Ctrl+C`) cleanly, ensuring logging streams and connections close gracefully.

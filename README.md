# ASTRA (Automated System for Trading & Risk Analysis)

ASTRA is a lightweight, fully automated, local trading telemetry and alerting system engineered specifically for Indian Equity Markets (NSE). It scans budget-aligned equities in real time, evaluates technical indicators (RSI and MACD), verifies trade feasibility against live cash liquidity in Angel One, persists exact cost prices to prevent loss-tracking drift, and dispatches real-time **BUY** and **SELL** alerts via Telegram while maintaining live performance dashboards in Google Sheets.

---

## 1. System Architecture & Core Workflow

ASTRA operates as a headless loop that adapts dynamically to market hours (9:15 AM – 3:30 PM IST, Monday–Friday).


```

 ┌─────────────────────────────────────────────────────────┐
 │                   Angel One SmartAPI                    │
 │          (TOTP Auth -> Fetch Liquidity & Holdings)      │
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
 │               Technical Analysis Engine                 │
 │             (yfinance -> RSI + MACD Signal)             │
 └────────────────────────────┬────────────────────────────┘
                              │
                              ▼
 ┌─────────────────────────────────────────────────────────┐
 │             Triple-Bound Capital Validation             │
 │      (Upper Bound <= Price <= Lower Bound <= Cash)      │
 └────────────────────────────┬────────────────────────────┘
                              │
                              ▼
 ┌─────────────────────────────────────────────────────────┐
 │             Cost Price Persistence Engine               │
 │ (Reads & Locks Initial Purchase Price in Sheets/SQLite) │
 └────────────────────────────┬────────────────────────────┘
                              │
       ┌──────────────────────┴──────────────────────┐
       ▼                                             ▼
┌──────────────┐                              ┌──────────────┐
│ Telegram Bot │                              │ Google Sheets│
│(BUY/SELL Msg)│                              │ (3-Tab Sync) │
└──────────────┘                              └──────────────┘

```

### Execution Phases:

1. **Authentication & Telemetry Sync**: On every loop, ASTRA authenticates with Angel One SmartAPI using TOTP (`pyotp`) to fetch live available trading cash and active Demat holdings.
2. **Cost Price Preservation & Position Tracking**: Tracks holdings dynamically using a non-destructive state model. When a new stock is acquired, its initial buy price is locked into Google Sheets (`Wallet_and_Holdings`) and local SQLite state (`position_tracker`). Subsequent cycles update **only** live price and live P&L metrics, protecting historical cost basis.
3. **Portfolio Health & SELL Evaluation**: Evaluates active positions using stored cost price baselines instead of fluctuating session averages. Triggers exit alerts if positions hit profit targets (RSI $\ge 65$, bearish MACD cross) or the strict -5.0% stop-loss threshold.
4. **NSE Market Sweep & BUY Evaluation**: During live market hours, it fetches the official NSE stock list (`EQUITY_L.csv`), filters symbols matching your `.env` budget allocation window, and evaluates them for bullish oversold setups (RSI $\le 35$ with MACD crossover).
5. **Triple-Bound Capital Safety Check**: A candidate BUY alert is **only** generated if:
   * Stock Price $\le$ `MAX_TRADE_ALLOCATION`
   * Stock Price $\ge$ `MIN_TRADE_ALLOCATION`
   * Calculated Trade Capital $\le$ Live Angel One Available Cash
6. **Non-Destructive Dashboard Telemetry**: Updates your Google Sheets dashboard across three tabs (`Market Scan`, `Raw Data`, `Wallet_and_Holdings`). If the market is closed or external data feeds return empty datasets, previous scan data and static portfolio fields remain untouched.

---

## 2. Directory Structure & Module Breakdown

```text
ASTRA/
├── .env                         # Environment variables & secrets
├── .eod_state.json              # Local transaction state persistence
├── service_account.json         # Google Cloud Service Account credentials
├── requirements.txt             # Python dependency manifest
├── logs/
│   └── astra_data.db            # SQLite persistent position & snapshot store
└── src/
    ├── __init__.py
    ├── main.py                  # Core execution loop & pipeline orchestrator
    ├── decision_engine.py       # RSI & MACD mathematical analysis engine
    ├── portfolio_fetcher.py     # Non-destructive Google Sheets telemetry & cost price sync
    ├── mng_db.py                # SQLite database manager for position & snapshot tracking
    ├── smartapi.py              # Angel One SmartAPI client (TOTP & Portfolio)
    ├── telegram_bot.py          # Telegram alert dispatch & command handler
    ├── mailer.py                # EOD / EOW / EOM email reporting module
    ├── tickers.py               # Dynamic NSE stock list sweeper
    └── utils.py                 # Symbol sanitization & failure alerting utilities

```

### Key Modules:

* **`src/main.py`**: The entry point. Manages ANSI terminal logging, controls adaptive loop execution (5-minute cycle during live market, 15-minute sleep off-market), enforces triple-bound capital checks, and orchestrates trade signals.
* **`src/portfolio_fetcher.py`**: Interfaces with Google Sheets via `gspread`. Reads existing sheet data to preserve fixed initial buy prices, quantities, and invested amounts while updating live prices and P&L dynamically. Includes `get_cost_price_from_sheet()` for direct cost lookups.
* **`src/mng_db.py`**: Manages persistent SQLite storage (`position_tracker` and `portfolio_snapshots`), locking in buy prices at execution and recording portfolio equity over time.
* **`src/decision_engine.py`**: Calculates 14-period RSI and MACD (12, 26, 9) signal line crossovers using `pandas` and `numpy`.
* **`src/smartapi.py`**: Interacts with Angel One SmartAPI via `smartapi-python` to log in using TOTP and fetch RMS net available funds and active Demat holdings.
* **`src/telegram_bot.py`**: Dispatches structured HTML BUY/SELL alerts and handles polling commands (`/status`, `/portfolio`, `/help`, `/config`).
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
MAX_TRADE_ALLOCATION=2000.0       # Upper price bound (INR)
PORTFOLIO_ALLOCATION_PCT=0.10    # Allocate 10% of available cash per trade
RSI_LOWER_THRESHOLD=35.0         # Oversold threshold
RSI_UPPER_THRESHOLD=65.0         # Overbought threshold
TICKERS_COUNT=50                 # Number of stocks to evaluate per cycle
ASTRA_CYCLE_BUFFER=5             # Execution loop interval in minutes

# ==========================================
# TRAILING STOP LOSS (TSL) CONFIGURATION
# ==========================================
ENABLE_TRAILING_STOP=true
STOP_LOSS_PCT=0.05
TRAILING_STOP_PCT=0.04
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
2. Click **Add App**, select Standard Trading API, name your application `ASTRA`, and set any placeholder URL for Redirect/Postback.
3. Save the generated **API Key** (`ANGEL_API_KEY`).
4. Enable **TOTP 2FA** on your Angel One trading account via the mobile app or website.
5. Record the **Base32 QR Code / Secret Key** shown during setup—this is your `ANGEL_TOTP_SECRET`.

### 4.3. Google Cloud Service Account Setup (Sheets Telemetry)

To allow ASTRA to read/write performance dashboards without manual authorization:

1. Go to the [Google Cloud Console](https://console.cloud.google.com/?utm_source=gemini).
2. Create a new project (e.g., `ASTRA-Telemetry`).
3. Navigate to **APIs & Services > Library** and enable **Google Sheets API** and **Google Drive API**.
4. Go to **IAM & Admin > Service Accounts**, click **Create Service Account**, name it `astra-bot`, and click **Done**.
5. Click on your newly created Service Account email, navigate to the **Keys** tab, click **Add Key > Create New Key**, select **JSON**, and download it.
6. Rename this downloaded file to `service_account.json` and place it directly in the root directory of ASTRA.
7. Open `service_account.json`, copy the `client_email` address inside it.
8. Create a blank Google Sheet, click **Share**, paste the service account `client_email`, assign it **Editor** permissions, and copy the Spreadsheet ID from the URL (`https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`).

---

## 5. Setup & Usage Instructions

### Step 1: Git Clone and Virtual Environment Creation

```bash
# Clone this repository
git clone https://github.com/srijan-76448/ASTRA-V1

# Navigate to the repository
cd ASTRA

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

Create and edit `.env` following the sample template provided above in Section 3.

### Step 4: Execute ASTRA

```bash
python src/main.py

```

To run a single execution cycle and send an immediate debug email report:

```bash
python src/main.py --email-now

```

---

## 6. Telegram Commands Reference

You can interact with ASTRA in real time directly from your Telegram chat:

* `/status` — Output current system operational state and loop cycle info
* `/sheet` or `/spreadsheet` — Get direct link to live Google Sheets telemetry dashboard
* `/config` or `/cfg` — Read active `.env` configuration values
* `/set KEY VALUE` — Set or overwrite any `.env` configuration variable dynamically
* `/restore_default` or `/restore` — Restore `.env` settings from `.env.bak`
* `/suspend DURATION` — Pause execution loop (e.g., `/suspend 5m` or `/suspend 2h`)
* `/portfolio` — Fetch and display real-time Angel One portfolio holdings & liquidity

---

## 7. Operational Safety Features

* **Cost Price Preservation Engine**: Retains initial buy price in Sheets and SQLite, preventing moving-average inflation or P&L miscalculations on subsequent runs.
* **No Hardcoded Stock Lists**: Dynamically adjusts stock selection based on live market pricing and `.env` bounds using official NSE datasets.
* **Cooldown Mechanism**: Prevents repeated alerts for the same stock within an 8-hour window.
* **Non-Destructive Sheet Updates**: Preserves dashboard historical records when market scanning is paused or offline.
* **Graceful Exit**: Handles `KeyboardInterrupt` (`Ctrl+C`) cleanly, ensuring logging streams, database handles, and API connections close gracefully.

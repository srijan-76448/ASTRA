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
MAX_TRADE_ALLOCATION=2000.0       # Upper price bound (INR)
PORTFOLIO_ALLOCATION_PCT=0.10    # Allocate 10% of available cash per trade
RSI_LOWER_THRESHOLD=35.0         # Oversold threshold
RSI_UPPER_THRESHOLD=65.0         # Overbought threshold
TICKERS_COUNT=50                 # Number of stocks to evaluate per cycle
ASTRA_CYCLE_BUFFER=5             # Execution loop interval in minutes

```

---

## 4. Google Sheets Integration (`Google Sheets Dashboard`)

ASTRA syncs real-time telemetry into three separate tabs within your Google Sheet:

1. **`Market Scan`**: Contains filtered market tickers, current live prices, RSI values, MACD indicators, and trade signal statuses (`BUY`, `NEUTRAL`, `SELL`).
2. **`Raw Data`**: Stores raw unparsed technical metrics across all scanned stocks for auditing and charting.
3. **`Wallet_and_Holdings`**: Displays live Angel One account statistics:
* Total Available Cash / RMS Net Liquidity
* Total Invested Capital
* Detailed Holdings Table (Ticker, Quantity, Avg Price, Live Price, Un-realized P&L)



*Note: Requires `service_account.json` in the root directory with Google Sheets API access enabled.*

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

### Step 3: Run ASTRA

```bash
python src/main.py

```

---

## 6. Telegram Commands Reference

You can interact with ASTRA in real time directly from your Telegram chat:

* `/status` — Displays current loop execution status, market state (LIVE/CLOSED), and capital bounds.
* `/portfolio` — Returns live Angel One available cash liquidity and active Demat holdings with real-time P&L breakdown.
* `/help` — Lists all available Telegram commands.

---

## 7. Operational Safety Features

* **No Hardcoded Stock Lists**: Dynamically adjusts stock selection based on live market pricing and `.env` bounds.
* **Cooldown Mechanism**: Prevents repeated alerts for the same stock within 8 hours.
* **Non-Destructive Sheet Updates**: Preserves dashboard historical records when market scanning is paused or offline.
* **Graceful Exit**: Handles `KeyboardInterrupt` (`Ctrl+C`) cleanly, ensuring logging streams and connections close gracefully.

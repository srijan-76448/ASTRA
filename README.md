# ASTRA (Autonomous System for Trading & Risk Analysis)

![version](https://img.shields.io/badge/ASTRA-V3-brightgreen?style=plastic)

ASTRA is a lightweight, local **market-analysis, risk-analysis, telemetry, and alerting system** designed primarily for Indian Equity Markets (NSE).

ASTRA continuously analyzes market data, evaluates technical indicators, monitors portfolio state, tracks risk metrics, generates advisory BUY/SELL signals, manages persistent telemetry, and communicates system status through Telegram, email, and Google Sheets.

> **Important:** ASTRA is intentionally designed as a **human-in-the-loop advisory system**. It does **not** autonomously execute trades. Trade signals and recommendations are generated for human review and decision-making.

---

## 1. System Architecture & Core Workflow

ASTRA operates as a persistent headless service with a wall-clock-aligned scheduler.

During market hours it performs active market analysis, while outside market hours it performs passive/off-market processing according to the configured cycle buffers.

The primary NSE market session is:

```text
09:15 AM – 03:30 PM IST
Monday – Friday
```

The scheduler aligns cycles to actual minute boundaries rather than simply sleeping for a fixed number of seconds after each cycle.

For example, if a cycle begins at:

```text
13:05:40
```

the next scheduled cycle can begin at the configured boundary:

```text
13:10:00
```

rather than:

```text
13:10:40
```

---

### High-Level Architecture

```text
                         ┌───────────────────────────┐
                         │       Angel One            │
                         │        SmartAPI            │
                         │   Authentication / Wallet  │
                         │      / Holdings Data       │
                         └─────────────┬─────────────┘
                                       │
                                       ▼
                         ┌───────────────────────────┐
                         │      ASTRA Main Loop       │
                         │        main.py             │
                         │ Scheduler / Orchestrator   │
                         └─────────────┬─────────────┘
                                       │
             ┌─────────────────────────┼─────────────────────────┐
             │                         │                         │
             ▼                         ▼                         ▼
   ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
   │  Normal Market   │      │ Intraday Engine  │      │    SIP Engine    │
   │     Analysis     │      │   intraday_bot   │      │   sip_engine.py  │
   └────────┬─────────┘      └────────┬─────────┘      └────────┬─────────┘
            │                         │                         │
            └─────────────────────────┼─────────────────────────┘
                                      ▼
                         ┌───────────────────────────┐
                         │     Decision Engine       │
                         │ RSI / MACD / Risk / TSL   │
                         └─────────────┬─────────────┘
                                       │
             ┌─────────────────────────┼─────────────────────────┐
             │                         │                         │
             ▼                         ▼                         ▼
   ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
   │   SQLite State   │      │ Google Sheets    │      │ Telegram / Email │
   │    mng_db.py     │      │   Telemetry      │      │ Alerts & Reports │
   └──────────────────┘      └──────────────────┘      └──────────────────┘

                       ┌─────────────────────────┐
                       │ Gold / Silver Analysis  │
                       │       gold.py           │
                       │ Advisory Hedge Signals  │
                       └─────────────────────────┘
```

---

## 2. Core Execution Phases

### 2.1 Authentication & Wallet Telemetry

ASTRA connects to Angel One SmartAPI using the configured credentials and TOTP authentication.

The SmartAPI layer provides:

- Available trading cash
- Wallet/RMS information
- Current holdings
- Instrument information
- Authentication/session management

Credentials remain in `.env`.

Runtime/cache configuration is controlled through `settings.json`.

ASTRA does **not** treat an unavailable wallet as `₹0`. Unknown wallet state remains explicitly distinguishable from a genuine zero balance.

---

### 2.2 Dynamic NSE Ticker Discovery

ASTRA dynamically discovers NSE equity instruments rather than relying on a permanently hardcoded stock list.

The ticker engine can:

- Retrieve the current NSE equity universe
- Filter instruments according to configured requirements
- Apply ticker-count limits
- Support custom ticker universes
- Randomize selection when configured
- Use a deterministic random seed when required
- Normalize symbols for downstream APIs

Ticker selection is controlled through:

```json
{
    "NORMAL_TRADING": {
        "TICKERS_COUNT": 50
    },
    "INTRADAY": {
        "TICKERS_COUNT": 50
    }
}
```

Optional ticker customization is controlled through the `TICKERS` settings section.

---

## 3. Normal Trading Analysis Engine

ASTRA's normal market-analysis pipeline evaluates equities using technical indicators and capital/risk constraints.

### Technical Indicators

The decision engine uses:

- RSI
- MACD
- MACD signal crossover
- Cost price
- Current market price
- Historical peak price
- Stop-loss levels
- Trailing stop levels

Default RSI configuration:

```text
RSI Lower Threshold : 35
RSI Upper Threshold : 65
```

### BUY Analysis

A bullish setup is generally associated with:

```text
RSI <= RSI_LOWER_THRESHOLD
+
Bullish MACD condition
```

The final advisory signal is also evaluated against configured allocation and capital constraints.

### SELL / EXIT Analysis

Exit analysis can consider:

1. Hard stop-loss
2. Trailing stop-loss
3. Technical overbought conditions
4. MACD bearish conditions
5. Current portfolio state

---

## 4. Persistent Cost & Peak Price Tracking

ASTRA maintains persistent portfolio state using SQLite.

The database stores information such as:

- Position state
- Cost price
- Highest observed price
- Position telemetry
- Snapshot information
- Runtime state

The historical peak is particularly important for the Trailing Stop-Loss engine.

Example:

```text
Buy Price       = ₹100
Historical Peak = ₹125
Trailing Stop   = 4%

Trailing threshold:

₹125 × (1 - 0.04)
= ₹120
```

If the market price subsequently falls to or below the calculated trailing threshold, ASTRA can generate an advisory exit signal.

The historical peak survives application restarts because it is persisted locally.

---

## 5. Dynamic Trailing Stop-Loss Engine

The TSL engine is controlled through runtime settings.

Current normal-trading configuration:

```json
{
    "NORMAL_TRADING": {
        "ENABLE_TRAILING_STOP": true,
        "STOP_LOSS_PCT": 0.05,
        "TRAILING_STOP_PCT": 0.04
    }
}
```

This represents:

```text
Hard Stop Loss       = 5%
Trailing Stop Loss   = 4%
Trailing Stop        = Enabled
```

### Hard Stop

If:

```text
Current Price <= Cost Price × (1 - STOP_LOSS_PCT)
```

ASTRA generates an advisory exit condition.

### Trailing Stop

If:

```text
Current Price <= Historical Peak × (1 - TRAILING_STOP_PCT)
```

ASTRA generates an advisory trailing-stop exit condition.

ASTRA does not automatically submit the resulting trade to the broker.

---

# 6. Intraday Analysis Engine

ASTRA contains a dedicated intraday analysis engine implemented in:

```text
src/intraday_bot.py
```

The intraday engine is intentionally separate from the normal market-analysis pipeline.

It supports runtime configuration for:

```json
{
    "INTRADAY": {
        "TRADING_ENGINE": false,
        "LIVE_TRADING": false,
        "TICKERS_COUNT": 50,
        "SCAN_INTERVAL_SECONDS": 180,
        "RSI_LOWER_THRESHOLD": 35,
        "RSI_UPPER_THRESHOLD": 65,
        "MIN_ALLOCATION": 100,
        "MAX_ALLOCATION": 500,
        "MAX_POSITIONS": 5,
        "MAX_DAILY_LOSS": 0.02,
        "STOP_LOSS_PCT": 0.03,
        "TRAILING_STOP_PCT": 0.02,
        "FORCE_EXIT_TIME": "15:30"
    }
}
```

The intraday engine maintains its own session/risk state.

The main loop reports an intraday state snapshot similar to:

```text
Intraday state: active=False | allowed=False | killed=False | session_date=None
```

or, when active:

```text
Intraday state: active=True | allowed=True | killed=False | session_date=2026-10-05
```

The intraday engine also supports:

- Session gating
- Maximum position limits
- Daily loss limits
- Intraday stop-loss
- Intraday trailing stop-loss
- Force-exit time
- Configurable scan interval
- Advisory execution mode

### Advisory-Only Execution

The intraday configuration contains:

```json
"TRADING_ENGINE": false,
"LIVE_TRADING": false
```

and the engine operates in:

```text
ADVISORY
```

mode.

The human-in-the-loop design is intentional and must be preserved.

---

# 7. SIP Engine

ASTRA includes a dedicated SIP engine:

```text
src/sip_engine.py
```

The SIP subsystem maintains recurring investment targets and their lifecycle state.

Runtime configuration includes:

```json
{
    "SIP": {
        "DEFAULT_FREQUENCY": "MONTHLY",
        "DEFAULT_DURATION_DAYS": 365,
        "MIN_SIP_AMOUNT": 1.0,
        "DECISION_HISTORY_LIMIT": 5000
    }
}
```

The SIP engine provides:

- SIP lifecycle management
- Target state tracking
- Contribution accounting
- Decision history
- Active/paused target state
- SIP telemetry
- Runtime refresh

The main pipeline exposes SIP telemetry such as:

```text
SIP state: active=True | targets=3 | active_targets=2 | paused_targets=1
```

The SIP subsystem remains advisory and analytical rather than an autonomous trade executor.

---

# 8. Gold & Silver Analysis

ASTRA also contains a dedicated precious-metals analysis module:

```text
src/gold.py
```

It provides advisory analysis for:

- Gold
- Silver
- MCX contracts
- Hedge allocation
- Market/value conditions

Runtime parameters include:

```text
GOLD.BASE_GOLD_24K
GOLD.BASE_SILVER_1KG

GOLD.GOLD_BULLISH_THRESHOLD
GOLD.GOLD_VALUE_THRESHOLD

GOLD.SILVER_CAUTION_THRESHOLD
GOLD.SILVER_VALUE_THRESHOLD

GOLD.HEDGE_MIN_PCT
GOLD.HEDGE_MAX_PCT
```

The gold engine can dynamically resolve relevant MCX instruments using the configured official scrip-master source.

It does not autonomously place commodity orders.

---

# 9. Runtime Configuration Architecture

ASTRA now separates **secrets/infrastructure configuration** from **runtime application configuration**.

## `.env`

`.env` contains sensitive credentials and infrastructure-level configuration.

Examples include:

```ini
ANGEL_API_KEY=your_angel_api_key
ANGEL_CLIENT_CODE=your_client_code
ANGEL_PASSWORD=your_account_password
ANGEL_TOTP_SECRET=your_totp_secret

TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
TELEGRAM_PASSWORD=your_admin_password

SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_SENDER_EMAIL=your_email@gmail.com
SMTP_PASSWORD=your_app_password
SMTP_RECEIVER_EMAIL=your_email@gmail.com
```

Actual credentials must never be committed to Git.

---

## `settings.json`

Non-secret runtime configuration is stored in:

```text
settings.json
```

Current structure:

```json
{
    "ASTRA_FUNCTIONS": {
        "ACTIVE_CYCLE_BUFFER": 5,
        "PASSIVE_CYCLE_BUFFER": 15,
        "NOTIFICATION_REPEAT_BUFFER": 6,
        "PASSIVE_WALLET_REFRESH_BUFFER": 30,
        "KILL": false
    },

    "NORMAL_TRADING": {
        "TICKERS_COUNT": 50,
        "MIN_TRADE_ALLOCATION": 100,
        "MAX_TRADE_ALLOCATION": 500,
        "PORTFOLIO_ALLOCATION_PCT": 0.10,
        "RSI_LOWER_THRESHOLD": 35,
        "RSI_UPPER_THRESHOLD": 65,
        "ENABLE_TRAILING_STOP": true,
        "STOP_LOSS_PCT": 0.05,
        "TRAILING_STOP_PCT": 0.04
    },

    "INTRADAY": {
        "TRADING_ENGINE": false,
        "LIVE_TRADING": false,
        "TICKERS_COUNT": 50,
        "SCAN_INTERVAL_SECONDS": 180,
        "RSI_LOWER_THRESHOLD": 35,
        "RSI_UPPER_THRESHOLD": 65,
        "MIN_ALLOCATION": 100,
        "MAX_ALLOCATION": 500,
        "MAX_POSITIONS": 5,
        "MAX_DAILY_LOSS": 0.02,
        "STOP_LOSS_PCT": 0.03,
        "TRAILING_STOP_PCT": 0.02,
        "FORCE_EXIT_TIME": "15:30"
    },

    "mailer": {
        "TIME": "EOD"
    },

    "SIP": {},

    "IPO": {}
}
```

Additional module-specific settings can be added without exposing secrets.

---

# 10. Persistent Settings Controller

`src/utils.py` provides the persistent settings controller.

Important functions include:

```python
load_settings()
reload_settings()
get_settings()
get_setting()
has_setting()

save_settings()
update_settings()
update_setting()
set_setting()
delete_setting()
reset_settings_cache()
```

The settings controller supports:

- Nested/dotted setting paths
- Runtime reads
- Persistent writes
- Recursive dictionary updates
- Thread-safe access
- Cached settings
- Deep-copy protection
- Atomic JSON writes

Modules should use:

```python
from utils import get_setting
```

instead of directly reading runtime configuration from environment variables.

---

# 11. Telegram Control Interface

ASTRA exposes operational controls through Telegram.

The Telegram interface is responsible for:

- Authentication
- Authorized-user validation
- Command handling
- Runtime configuration
- System status
- Portfolio telemetry
- Analysis requests
- Suspend/kill controls

### Main Commands

```text
/help
/status
/sheet
/spreadsheet
/config
/cfg
/set
/restore
/restore_default
/suspend
/portfolio
/analyze
```

### Configuration

`/config` and `/cfg` display the structured runtime configuration from:

```text
settings.json
```

They do **not** expose `.env` secrets.

### Runtime Modification

Privileged users can modify runtime settings with:

```text
/set KEY VALUE
```

Examples:

```text
/set NORMAL_TRADING.RSI_LOWER_THRESHOLD 35
/set NORMAL_TRADING.RSI_UPPER_THRESHOLD 65
/set INTRADAY.SCAN_INTERVAL_SECONDS 180
/set ASTRA_FUNCTIONS.KILL true
```

Values are converted to appropriate types where possible:

```text
boolean
integer
float
string
```

Critical credentials remain outside this mechanism.

---

# 12. Main Scheduler

`src/main.py` is the central ASTRA orchestrator.

It controls:

- Startup
- Authentication
- Database initialization
- Telegram runtime
- SIP processing
- Normal market analysis
- Intraday processing
- Portfolio synchronization
- TSL evaluation
- Mail reporting
- Off-market processing
- Cycle scheduling
- Graceful shutdown

The scheduler distinguishes between:

```text
ACTIVE MARKET CYCLE
```

and:

```text
PASSIVE / OFF-MARKET CYCLE
```

The configured buffers are:

```json
{
    "ASTRA_FUNCTIONS": {
        "ACTIVE_CYCLE_BUFFER": 5,
        "PASSIVE_CYCLE_BUFFER": 15
    }
}
```

The scheduler aligns execution to wall-clock boundaries.

---

# 13. Market / Off-Market Behaviour

During market hours ASTRA performs active market processing.

Outside market hours ASTRA continues operating in passive mode rather than terminating.

Typical off-market output resembles:

```text
=== ASTRA OFF-MARKET CYCLE | 2026-10-04 20:38:22 IST ===
```

The system can continue to perform:

- Portfolio state synchronization
- Wallet refresh
- SIP telemetry
- Intraday session checks
- TSL analysis against available/latest data
- Database maintenance
- Telegram monitoring
- Scheduled reporting logic

Weekend processing is handled separately so that scheduled EOD reports are not incorrectly treated as normal trading-day reports.

---

# 14. Terminal Logging

ASTRA uses structured terminal logging with ANSI formatting.

The cycle header uses a bold gold/yellow RGB format:

```text
=== ASTRA ACTIVE CYCLE | YYYY-MM-DD HH:MM:SS IST ===
```

and:

```text
=== ASTRA OFF-MARKET CYCLE | YYYY-MM-DD HH:MM:SS IST ===
```

System telemetry includes separate SIP and intraday state snapshots.

Example:

```text
SIP state: active=False | targets=0 | active_targets=0 | paused_targets=0

Intraday state: active=False | allowed=False | killed=False | session_date=None
```

This makes it possible to distinguish the state of each subsystem without interpreting the detailed internal logs.

---

# 15. Directory Structure

```text
ASTRA/
│
├── .env                         # Secrets & infrastructure configuration
├── .env.bak                     # Optional environment backup
├── settings.json                # Runtime application configuration
├── .eod_state.json              # EOD/reporting state where applicable
├── service_account.json         # Google Cloud service-account credentials
├── requirements.txt             # Python dependencies
├── astra.db                     # SQLite persistent state database
├── logs/
│   └── ...                      # Runtime logs
│
└── src/
    ├── __init__.py
    ├── main.py                  # Main scheduler & system orchestrator
    ├── decision_engine.py       # RSI / MACD / risk / TSL analysis
    ├── intraday_bot.py          # Intraday analysis engine
    ├── sip_engine.py            # SIP lifecycle & decision engine
    ├── gold.py                  # Gold / silver advisory analysis
    ├── portfolio_fetcher.py     # Google Sheets portfolio telemetry
    ├── mng_db.py                # SQLite persistence layer
    ├── smartapi.py              # Angel One SmartAPI integration
    ├── telegram_bot.py          # Telegram control & alert interface
    ├── mailer.py                # SMTP reporting and alerting
    ├── tickers.py               # Dynamic NSE ticker discovery
    └── utils.py                 # Runtime settings & utility layer
```

---

# 16. Module Breakdown

### `src/main.py`

The primary entry point.

Responsibilities:

- Initialize ASTRA
- Manage scheduler
- Run market cycles
- Run passive cycles
- Coordinate SIP
- Coordinate intraday
- Coordinate portfolio telemetry
- Coordinate TSL analysis
- Manage reporting
- Handle graceful shutdown

---

### `src/decision_engine.py`

The mathematical analysis engine.

Responsibilities:

- RSI calculation
- MACD analysis
- BUY evaluation
- SELL evaluation
- Exit evaluation
- Stop-loss calculation
- Trailing-stop calculation
- Technical exit analysis
- Runtime threshold resolution

---

### `src/intraday_bot.py`

Dedicated intraday analysis subsystem.

Responsibilities:

- Intraday session gating
- Intraday ticker analysis
- Position limits
- Daily loss limits
- Intraday risk parameters
- Stop-loss / TSL analysis
- Force-exit logic
- Intraday telemetry

---

### `src/sip_engine.py`

SIP subsystem.

Responsibilities:

- SIP creation
- SIP lifecycle management
- Target state
- Contributions
- Decision history
- Active/paused state
- Runtime configuration

---

### `src/gold.py`

Precious-metal analysis subsystem.

Responsibilities:

- Gold analysis
- Silver analysis
- MCX instrument discovery
- Hedge recommendations
- Gold/silver threshold analysis

---

### `src/mng_db.py`

Persistent SQLite database manager.

Runtime database settings include:

```text
DATABASE.PATH
DATABASE.SQLITE_TIMEOUT
DATABASE.BUSY_TIMEOUT_MS
DATABASE.JOURNAL_MODE
DATABASE.SYNCHRONOUS
```

The database is used for persistent state such as:

- Position tracking
- Cost prices
- Historical peaks
- Snapshots
- Telemetry

---

### `src/smartapi.py`

Angel One SmartAPI integration.

Responsibilities:

- Authentication
- TOTP
- Wallet retrieval
- Holdings retrieval
- Instrument information
- Session management
- Retry/cooldown handling
- Wallet caching

Runtime cache settings include:

```text
SMARTAPI.AUTH_RETRY_COOLDOWN
SMARTAPI.WALLET_CACHE_TTL
SMARTAPI.WALLET_RETRY_COOLDOWN
SMARTAPI.WALLET_REFRESH_SPACING
SMARTAPI.INSTRUMENT_CACHE_TTL
```

---

### `src/tickers.py`

NSE ticker discovery and selection.

Supports:

- Dynamic ticker discovery
- Configurable ticker counts
- Custom ticker universe
- Randomized selection
- Deterministic random seeds
- Symbol normalization

---

### `src/portfolio_fetcher.py`

Google Sheets telemetry layer.

Responsible for synchronizing:

- Market prices
- Portfolio information
- Cost prices
- P&L
- Dashboard data

The update model is intentionally non-destructive.

---

### `src/telegram_bot.py`

Telegram control and alert layer.

Responsible for:

- Authorized access
- Commands
- Runtime settings
- Alerts
- System status
- Portfolio status
- Analysis requests
- Kill/suspend controls

---

### `src/mailer.py`

SMTP reporting layer.

Responsible for:

- EOD reporting
- EOW reporting
- EOM reporting
- Critical failure notifications
- Advisory email delivery

SMTP credentials remain in `.env`.

Mailer runtime scheduling is controlled through:

```text
mailer.TIME
```

---

### `src/utils.py`

Common utility and persistent settings layer.

Responsible for:

- Runtime configuration
- JSON persistence
- Symbol utilities
- Failure alerts
- Shared helper functions

---

# 17. External Services

ASTRA integrates with several external services.

### Angel One SmartAPI

Used for:

- Authentication
- Wallet information
- Holdings
- Instrument information

ASTRA does not use SmartAPI to autonomously execute trades.

### NSE / Market Data Sources

Used for:

- Equity universe
- Market prices
- Instrument discovery

### Yahoo Finance

Used by the technical-analysis pipeline for market-history/price data where applicable.

### Google Sheets

Used as a live telemetry/dashboard layer.

### Telegram

Used for:

- Alerts
- System control
- Status
- Configuration

### SMTP

Used for:

- EOD/EOW/EOM reports
- Critical alerts

---

# 18. Google Sheets Telemetry

ASTRA can maintain a Google Sheets dashboard containing information such as:

```text
Market Scan
Raw Data
Wallet_and_Holdings
```

The telemetry model is designed to be non-destructive.

When external market data is unavailable or the market is closed, ASTRA should avoid replacing valid historical dashboard information with meaningless empty values.

---

# 19. Environment Setup

## 19.1 Angel One SmartAPI

Configure the following in `.env`:

```ini
ANGEL_API_KEY=your_api_key
ANGEL_CLIENT_CODE=your_client_code
ANGEL_PASSWORD=your_password
ANGEL_TOTP_SECRET=your_totp_secret
```

Generate the API credentials through the Angel One SmartAPI developer portal.

TOTP must be configured for the account used by ASTRA.

---

## 19.2 Telegram

Configure:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
TELEGRAM_PASSWORD=your_admin_password
```

Only authorized users should be allowed to access privileged commands.

---

## 19.3 Google Sheets

Create a Google Cloud project and enable:

```text
Google Sheets API
Google Drive API
```

Create a service account and download its JSON credentials.

Place the credential file in the configured location and share the target spreadsheet with the service-account email.

The spreadsheet ID and service-account path should be treated as infrastructure configuration and must not be exposed publicly.

---

## 19.4 SMTP

For Gmail SMTP:

1. Enable 2-Step Verification.
2. Create an App Password.
3. Configure the SMTP credentials in `.env`.

Example:

```ini
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_SENDER_EMAIL=your_email@gmail.com
SMTP_PASSWORD=your_app_password
SMTP_RECEIVER_EMAIL=your_email@gmail.com
```

---

# 20. Installation

## Step 1 — Clone

```bash
git clone https://github.com/srijan-76448/ASTRA-V1
cd ASTRA-V1
```

---

## Step 2 — Create Virtual Environment

```bash
python3 -m venv .
```

Activate it:

```bash
source bin/activate
```

On Windows:

```powershell
.\Scripts\activate
```

---

## Step 3 — Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Step 4 — Configure Secrets

Create:

```text
.env
```

and configure the required credentials.

---

## Step 5 — Configure Runtime Settings

Create or modify:

```text
settings.json
```

using the current configuration structure.

---

## Step 6 — Start ASTRA

```bash
python src/main.py
```

---

# 21. Operational Safety

ASTRA contains several layers of operational protection.

### Human-in-the-Loop

ASTRA generates:

```text
Signals
Recommendations
Risk Analysis
Telemetry
Alerts
```

It does **not** autonomously execute trades.

---

### Persistent State

Important state is persisted so that application restarts do not erase:

- Cost prices
- Historical peaks
- Position information
- Runtime state

---

### Configurable Risk

Risk parameters can be adjusted through `settings.json` or privileged Telegram commands.

---

### Kill Switch

The global kill state is:

```json
{
    "ASTRA_FUNCTIONS": {
        "KILL": false
    }
}
```

When activated, ASTRA can suppress appropriate processing according to the subsystem's kill/safety logic.

---

### Intraday Risk Controls

Intraday analysis supports:

```text
Maximum positions
Maximum daily loss
Stop-loss
Trailing stop
Force-exit time
Session gating
```

---

### Wallet Validation

ASTRA distinguishes between:

```text
₹0 available cash
```

and:

```text
UNKNOWN wallet state
```

This prevents a temporary API failure from being incorrectly interpreted as zero available capital.

---

### Non-Destructive Telemetry

External data failures should not unnecessarily destroy previously valid telemetry.

---

### Graceful Shutdown

ASTRA handles:

```text
Ctrl+C
KeyboardInterrupt
```

and attempts to close resources cleanly.

---

# 22. Runtime Configuration Reference

### ASTRA Functions

```text
ASTRA_FUNCTIONS.ACTIVE_CYCLE_BUFFER
ASTRA_FUNCTIONS.PASSIVE_CYCLE_BUFFER
ASTRA_FUNCTIONS.NOTIFICATION_REPEAT_BUFFER
ASTRA_FUNCTIONS.PASSIVE_WALLET_REFRESH_BUFFER
ASTRA_FUNCTIONS.KILL
```

### Normal Trading

```text
NORMAL_TRADING.TICKERS_COUNT
NORMAL_TRADING.MIN_TRADE_ALLOCATION
NORMAL_TRADING.MAX_TRADE_ALLOCATION
NORMAL_TRADING.PORTFOLIO_ALLOCATION_PCT
NORMAL_TRADING.RSI_LOWER_THRESHOLD
NORMAL_TRADING.RSI_UPPER_THRESHOLD
NORMAL_TRADING.ENABLE_TRAILING_STOP
NORMAL_TRADING.STOP_LOSS_PCT
NORMAL_TRADING.TRAILING_STOP_PCT
```

### Intraday

```text
INTRADAY.TRADING_ENGINE
INTRADAY.LIVE_TRADING
INTRADAY.TICKERS_COUNT
INTRADAY.SCAN_INTERVAL_SECONDS
INTRADAY.RSI_LOWER_THRESHOLD
INTRADAY.RSI_UPPER_THRESHOLD
INTRADAY.MIN_ALLOCATION
INTRADAY.MAX_ALLOCATION
INTRADAY.MAX_POSITIONS
INTRADAY.MAX_DAILY_LOSS
INTRADAY.STOP_LOSS_PCT
INTRADAY.TRAILING_STOP_PCT
INTRADAY.FORCE_EXIT_TIME
```

### Mailer

```text
mailer.TIME
```

### SIP

```text
SIP.DEFAULT_FREQUENCY
SIP.DEFAULT_DURATION_DAYS
SIP.MIN_SIP_AMOUNT
SIP.DECISION_HISTORY_LIMIT
```

### SmartAPI

```text
SMARTAPI.AUTH_RETRY_COOLDOWN
SMARTAPI.WALLET_CACHE_TTL
SMARTAPI.WALLET_RETRY_COOLDOWN
SMARTAPI.WALLET_REFRESH_SPACING
SMARTAPI.INSTRUMENT_CACHE_TTL
```

### Database

```text
DATABASE.PATH
DATABASE.SQLITE_TIMEOUT
DATABASE.BUSY_TIMEOUT_MS
DATABASE.JOURNAL_MODE
DATABASE.SYNCHRONOUS
```

### Tickers

```text
TICKERS.CUSTOM_UNIVERSE
TICKERS.RANDOMIZE
TICKERS.RANDOM_SEED
```

### Gold

```text
GOLD.BASE_GOLD_24K
GOLD.BASE_SILVER_1KG
GOLD.SCRIP_MASTER_URL
GOLD.SCRIP_MASTER_TIMEOUT
GOLD.GOLD_BULLISH_THRESHOLD
GOLD.GOLD_VALUE_THRESHOLD
GOLD.SILVER_CAUTION_THRESHOLD
GOLD.SILVER_VALUE_THRESHOLD
GOLD.HEDGE_MIN_PCT
GOLD.HEDGE_MAX_PCT
```

---

# 23. Development Principles

ASTRA follows several architectural principles.

### 1. Human-in-the-Loop

ASTRA remains an advisory system.

### 2. Secrets Stay in `.env`

Credentials and sensitive infrastructure values are not stored in `settings.json`.

### 3. Runtime Configuration Stays in `settings.json`

Non-secret settings should be dynamically configurable without changing source code.

### 4. Persistent State Must Survive Restarts

Important trading-analysis state should be persisted.

### 5. Unknown Is Not Zero

External API failures must not be converted into misleading financial values.

### 6. Non-Destructive Telemetry

Temporary data-source failures should not erase valid historical dashboard information.

### 7. Modular Subsystems

Normal trading, intraday analysis, SIP, precious-metals analysis, telemetry, database management, and communication layers remain independently structured.

### 8. Advisory Before Execution

ASTRA's analytical capabilities may become increasingly sophisticated while retaining explicit human approval before any real-world trade action.

---

# 24. Current Runtime State Example

A healthy ASTRA startup may contain telemetry similar to:

```text
=== ASTRA OFF-MARKET CYCLE | 2026-10-04 20:38:22 IST ===

Cycle configuration:
allocation ₹100.00-₹500.00
TSL=True
trail=4.00%
mailer=EOD

SIP state:
active=False
targets=0
active_targets=0
paused_targets=0

Intraday state:
active=False
allowed=False
killed=False
session_date=None
```

During active market processing, ASTRA performs the corresponding live-market analysis and continues according to the configured cycle schedule.

---

# 25. Important Security Notes

Never commit the following to Git:

```text
.env
service_account.json
Telegram bot tokens
Telegram passwords
Angel One API keys
Angel One passwords/PINs
TOTP secrets
SMTP passwords
Private credentials
```

Recommended `.gitignore` entries:

```gitignore
.env
.env.*
!.env.example

service_account.json

*.db
*.sqlite
*.sqlite3

logs/
__pycache__/
*.pyc

.eod_state.json
```

If a secret is ever accidentally exposed, rotate the credential rather than simply deleting it from the repository.

---

# 26. Project Status

ASTRA currently contains the following major subsystems:

```text
✓ Angel One SmartAPI integration
✓ TOTP authentication
✓ Wallet telemetry
✓ Holdings telemetry
✓ Dynamic NSE ticker discovery
✓ RSI analysis
✓ MACD analysis
✓ BUY/SELL advisory engine
✓ Persistent cost-price tracking
✓ Persistent peak-price tracking
✓ Dynamic trailing stop-loss
✓ Intraday analysis engine
✓ Intraday session state telemetry
✓ SIP engine
✓ Gold/Silver advisory analysis
✓ SQLite persistence
✓ Google Sheets telemetry
✓ Telegram control interface
✓ SMTP reporting
✓ Runtime settings.json configuration
✓ Telegram runtime configuration
✓ Global kill/suspend controls
✓ Active/off-market scheduling
✓ Wall-clock cycle alignment
✓ Graceful shutdown
✓ Human-in-the-loop architecture
```

---

# 27. Related Projects

Also check:

- [ASTRA BIT](https://github.com/srijan-76448/ASTRA-BIT) — Crypto-mining-related ASTRA project

---

# 28. Repository

Main repository:

https://github.com/srijan-76448/ASTRA-V1

---

## License

Add the project's applicable license information here if/when a formal license is selected.

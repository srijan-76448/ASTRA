import sys
import logging
import datetime
import json
import os
import time
from pathlib import Path
import yfinance as yf
from dotenv import load_dotenv

# Path Setup
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
sys.path.append(str(BASE_DIR / "src"))

# Internal Modules
from decision_engine import analyze_ticker_data
from portfolio_fetcher import sync_dashboard_data
from telegram import send_investment_suggestion, process_telegram_commands
from mailer import send_eod_email_report
from tickers import get_dynamic_tickers
from smartapi import AngelOneClient

# Terminal ANSI Color Codes
CLR_RESET = "\033[0m"
CLR_BOLD = "\033[1m"
CLR_RED = "\033[91m"
CLR_GREEN = "\033[92m"
CLR_YELLOW = "\033[93m"
CLR_BLUE = "\033[94m"
CLR_MAGENTA = "\033[95m"
CLR_CYAN = "\033[96m"

class ColoredFormatter(logging.Formatter):
    FORMATS = {
        logging.DEBUG: CLR_BLUE + "%(asctime)s - [%(levelname)s] - %(message)s" + CLR_RESET,
        logging.INFO: CLR_CYAN + "%(asctime)s" + CLR_RESET + " - " + CLR_GREEN + "[%(levelname)s]" + CLR_RESET + " - %(message)s",
        logging.WARNING: CLR_YELLOW + "%(asctime)s - [%(levelname)s] - %(message)s" + CLR_RESET,
        logging.ERROR: CLR_RED + "%(asctime)s - [%(levelname)s] - %(message)s" + CLR_RESET,
        logging.CRITICAL: CLR_RED + CLR_BOLD + "%(asctime)s - [%(levelname)s] - %(message)s" + CLR_RESET,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt="%H:%M:%S")
        return formatter.format(record)

handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter())
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(handler)

STATE_FILE = BASE_DIR / ".eod_state.json"
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
CREDENTIALS_FILE = BASE_DIR / os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")

# Threshold & Config Bounds
MIN_ALLOCATION = float(os.getenv("MIN_TRADE_ALLOCATION", 100.0))
MAX_ALLOCATION = float(os.getenv("MAX_TRADE_ALLOCATION", 500.0))
ALLOCATION_PCT = float(os.getenv("PORTFOLIO_ALLOCATION_PCT", 0.10))

RSI_LOWER = float(os.getenv("RSI_LOWER_THRESHOLD", 35.0))
RSI_UPPER = float(os.getenv("RSI_UPPER_THRESHOLD", 65.0))
TICKERS_COUNT = int(os.getenv("TICKERS_COUNT", 50))

env_cycle_buffer = int(os.getenv("ASTRA_CYCLE_BUFFER", 5))
LIVE_LOOP_INTERVAL = max(3, env_cycle_buffer) * 60
CLOSED_LOOP_INTERVAL = 15 * 60

MAILER_TIME_MODE = os.getenv("MAILER_TIME", "EOD").upper()

def is_market_open():
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_start <= now <= market_end

def should_trigger_mail():
    """Determines whether EOD / EOW / EOM email report should be sent."""
    now = datetime.datetime.now()
    eod_cutoff = now.replace(hour=15, minute=30, second=0, microsecond=0)
    
    if now < eod_cutoff:
        return False

    if MAILER_TIME_MODE == "EOD":
        return True
    elif MAILER_TIME_MODE == "EOW":
        return now.weekday() == 4
    elif MAILER_TIME_MODE == "EOM":
        tomorrow = now + datetime.timedelta(days=1)
        return tomorrow.day == 1

    return False

def load_eod_state():
    today = str(datetime.date.today())
    default_state = {
        "date": today,
        "start_cash": 10000.0,
        "current_cash": 10000.0,
        "transactions": [],
        "mail_sent_for_period": False
    }
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                if state.get("date") == today:
                    return state
        except Exception as e:
            logger.error(f"Error reading state file: {e}")
            
    with open(STATE_FILE, "w") as f:
        json.dump(default_state, f, indent=2)
    return default_state

def save_eod_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def calculate_dynamic_allocation(available_cash):
    calculated = available_cash * ALLOCATION_PCT
    allocation = max(MIN_ALLOCATION, min(calculated, MAX_ALLOCATION))
    return min(allocation, available_cash)

def evaluate_buy_signal(metrics, available_cash):
    """
    Evaluates BUY signals against:
    1. Upper bound (.env)
    2. Lower bound (.env)
    3. Available Demat capital
    """
    price = metrics.get("price", 0.0)
    signals = metrics.get("signals", [])
    rsi = metrics.get("rsi", 50.0)

    # 1 & 2. Upper and Lower Bound Validation
    if price > MAX_ALLOCATION or price < MIN_ALLOCATION:
        return None, 0.0, ""

    suggested_capital = calculate_dynamic_allocation(available_cash)

    # 3. Available Demat Capital Validation
    if suggested_capital < price or available_cash < price:
        return None, 0.0, ""

    if rsi <= RSI_LOWER and "MACD_BULLISH_CROSS" in signals:
        return (
            "BUY (INTRA-DAY / SHORT-TERM)",
            suggested_capital,
            f"Oversold bounce (RSI {rsi:.1f}) + MACD Bullish Crossover. Capital verified."
        )
    elif rsi <= RSI_LOWER:
        return (
            "BUY (LONG-TERM SIP)",
            suggested_capital,
            f"Value entry zone (RSI {rsi:.1f} <= {RSI_LOWER}). Capital verified."
        )
    elif rsi < 45 and "MACD_BULLISH_CROSS" in signals:
        return (
            "BUY (SHORT-TERM)",
            suggested_capital,
            f"Bullish momentum crossover at RSI {rsi:.1f}. Capital verified."
        )

    return None, 0.0, ""

def evaluate_sell_signal(holding, metrics):
    """
    Evaluates SELL signals for active holdings in your Angel One portfolio.
    """
    ticker = holding.get("ticker")
    qty = holding.get("qty", 0)
    avg_price = holding.get("avg_price", 0.0)
    current_price = metrics.get("price", holding.get("current_price", 0.0))
    rsi = metrics.get("rsi", 50.0)
    signals = metrics.get("signals", [])

    pnl_pct = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0.0

    if rsi >= RSI_UPPER or "MACD_BEARISH_CROSS" in signals or "RSI_OVERBOUGHT" in signals:
        return (
            f"SELL (EXIT / TAKE PROFIT)",
            f"RSI overbought ({rsi:.1f}) or bearish crossover detected. P&L: {pnl_pct:+.2f}%"
        )
    elif pnl_pct <= -5.0:  # 5% Stop loss rule
        return (
            f"SELL (STOP LOSS)",
            f"Position hit stop loss threshold of -5.0% (Current P&L: {pnl_pct:.2f}%)."
        )

    return None, ""

def clean_ticker_for_yfinance(symbol: str) -> str:
    """Removes broker suffixes like '-EQ' or '-BE' and formats for Yahoo Finance (.NS)."""
    s = symbol.replace("-EQ", "").replace("-BE", "").strip()
    return s if s.endswith(".NS") else f"{s}.NS"

def run_pipeline():
    state = load_eod_state()
    market_active = is_market_open()
    
    process_telegram_commands()

    # Fetch Angel One Demat account info
    logger.info("Authenticating with Angel One SmartAPI to fetch live portfolio...")
    angel_client = AngelOneClient()
    real_portfolio = angel_client.get_real_portfolio_data()

    available_cash = real_portfolio.get("available_cash", 0.0) if real_portfolio else state["current_cash"]
    holdings = real_portfolio.get("holdings", []) if real_portfolio else []

    logger.info(f"Available Demat Capital: ₹{available_cash:,.2f} | Active Holdings: {len(holdings)}")

    scan_results = {}
    processed_rows = [["Ticker", "Price", "RSI", "MACD", "Signals"]]
    raw_data = []

    if market_active:
        # 1. EVALUATE SELL SIGNALS FOR ACTIVE HOLDINGS FIRST
        if holdings:
            logger.info("Scanning active Angel One holdings for SELL triggers...")
            holding_tickers = [clean_ticker_for_yfinance(h["ticker"]) for h in holdings if h.get("ticker")]
            
            if holding_tickers:
                h_data = yf.download(holding_tickers, period="6mo", group_by="ticker", progress=False)
                for h in holdings:
                    t_raw = h.get("ticker", "")
                    t_yf = clean_ticker_for_yfinance(t_raw)
                    try:
                        df_h = h_data[t_yf].dropna() if len(holding_tickers) > 1 else h_data.dropna()
                        if not df_h.empty:
                            h_metrics = analyze_ticker_data(df_h)
                            if h_metrics:
                                sell_action, sell_reasoning = evaluate_sell_signal(h, h_metrics)
                                if sell_action:
                                    logger.info(f"{CLR_BOLD}{CLR_RED}🔴 SELL Signal [{t_raw}]:{CLR_RESET} {sell_action}")
                                    send_investment_suggestion(
                                        ticker=t_raw,
                                        strategy=sell_action,
                                        amount=h.get("qty", 0) * h_metrics.get("price", 0.0),
                                        current_price=h_metrics.get("price", 0.0),
                                        reasoning=sell_reasoning
                                    )
                    except Exception as e:
                        logger.error(f"Error evaluating holding {t_raw}: {e}")

        # 2. SCAN NSE MARKET FOR CANDIDATE BUY SIGNALS
        tickers = get_dynamic_tickers()
        logger.info(f"{CLR_BOLD}{CLR_GREEN}Market is LIVE.{CLR_RESET} Sweeping {len(tickers)} market tickers...")
        
        if tickers:
            data = yf.download(tickers, period="6mo", group_by="ticker", progress=False)

            for ticker in tickers:
                try:
                    df = data[ticker].dropna() if len(tickers) > 1 else data.dropna()
                    if df.empty:
                        continue
                    
                    metrics = analyze_ticker_data(df)
                    if not metrics:
                        continue
                        
                    scan_results[ticker] = metrics
                    price = metrics["price"]

                    strategy, amount, reasoning = evaluate_buy_signal(metrics, available_cash)
                    if strategy:
                        logger.info(f"{CLR_BOLD}{CLR_GREEN}🟢 BUY Signal [{ticker}]:{CLR_RESET} {strategy} @ ₹{amount:,.2f}")
                        send_investment_suggestion(
                            ticker=ticker,
                            strategy=strategy,
                            amount=amount,
                            current_price=price,
                            reasoning=reasoning
                        )

                except Exception as e:
                    logger.error(f"Error processing {ticker}: {e}")

            for t, m in scan_results.items():
                sig_str = ", ".join(m.get("signals", [])) if m.get("signals") else "NEUTRAL"
                processed_rows.append([t, m.get("price", 0.0), m.get("rsi", 0.0), m.get("macd", 0.0), sig_str])
                raw_data.append({"ticker": t, **m})

    else:
        logger.info(f"{CLR_BOLD}{CLR_YELLOW}Market is CLOSED.{CLR_RESET} Skipping real-time market scan.")

    # 3. Google Sheets Sync
    if CREDENTIALS_FILE.exists() and SPREADSHEET_ID:
        try:
            sync_dashboard_data(
                credentials_path=str(CREDENTIALS_FILE),
                spreadsheet_id=SPREADSHEET_ID,
                processed_data=processed_rows,
                raw_data=raw_data,
                real_portfolio=real_portfolio,
                main_tab_name="Market Scan",
                raw_tab_name="Raw Data",
                wallet_tab_name="Wallet_and_Holdings"
            )
        except Exception as e:
            logger.error(f"Google Sheets sync error: {e}")

    # 4. Scheduled Email Check
    if should_trigger_mail() and not state.get("mail_sent_for_period", False):
        logger.info(f"{CLR_BOLD}{CLR_CYAN}Triggering [{MAILER_TIME_MODE}] Mailer Report...{CLR_RESET}")
        sent = send_eod_email_report(
            start_cash=state["start_cash"],
            end_cash=available_cash,
            transactions=state["transactions"]
        )
        if sent:
            state["mail_sent_for_period"] = True
            save_eod_state(state)

    return market_active

if __name__ == "__main__":
    cycle_min = max(3, env_cycle_buffer)
    logger.info(f"{CLR_BOLD}{CLR_MAGENTA}=== Starting ASTRA Engine ==={CLR_RESET}")
    logger.info(
        f"Mode: {CLR_BOLD}{MAILER_TIME_MODE}{CLR_RESET} | "
        f"Active Cycle: {CLR_BOLD}{cycle_min}m{CLR_RESET} | "
        f"Off-market Cycle: {CLR_BOLD}15m{CLR_RESET} | "
        f"Tickers Count: {CLR_BOLD}{TICKERS_COUNT}{CLR_RESET}"
    )
    
    try:
        while True:
            start_time = time.time()
            market_active = run_pipeline()
            
            elapsed = time.time() - start_time
            target_interval = LIVE_LOOP_INTERVAL if market_active else CLOSED_LOOP_INTERVAL
            sleep_duration = max(0, target_interval - elapsed)
            
            sleep_mins = int(sleep_duration // 60)
            logger.info(f"{CLR_BLUE}Iteration complete. Sleeping for {sleep_mins} minutes...{CLR_RESET}\n")
            time.sleep(sleep_duration)
            
    except KeyboardInterrupt:
        logger.info(f"\n{CLR_RED}{CLR_BOLD}[!] Gracefully shutting down ASTRA Engine. Goodbye!{CLR_RESET}")
        sys.exit(0)

import sys
import logging
import datetime
import os
import time
import argparse
import threading
from pathlib import Path
import yfinance as yf
from dotenv import load_dotenv


# Path Setup
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR / "src"))


# Internal Modules
from decision_engine import analyze_ticker_data
from portfolio_fetcher import sync_dashboard_data
from mailer import send_eod_email_report
from tickers import get_dynamic_tickers
from smartapi import AngelOneClient
from telegram_bot import set_smart_client, send_investment_suggestion, process_telegram_commands, run_telegram_bot_loop, send_critical_failure_alert
from mng_db import DatabaseManager


# Terminal ANSI Color Codes
CLR_RESET = "\033[0m"
CLR_BOLD = "\033[1m"
CLR_RED = "\033[91m"
CLR_GREEN = "\033[92m"
CLR_YELLOW = "\033[93m"
CLR_BLUE = "\033[94m"
CLR_MAGENTA = "\033[95m"
CLR_CYAN = "\033[96m"

# State tracking file for EOD emails
EOD_STATE_FILE = BASE_DIR / "logs" / "last_eod_sent.txt"

# Initialize Database Manager
db = DatabaseManager()


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


def is_market_open():
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_start <= now <= market_end

def mark_eod_mail_sent():
    """Persists today's date to disk to prevent duplicate email dispatches."""
    today_str = datetime.date.today().isoformat()
    try:
        EOD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(EOD_STATE_FILE, "w") as f:
            f.write(today_str)
    except Exception as e:
        logger.error(f"Failed to record EOD email state: {e}")


def should_trigger_mail(mailer_mode):
    """Determines whether EOD / EOW / EOM or Debug (NOW) email report should be sent."""
    now = datetime.datetime.now()
    
    # Debugging trigger: bypasses all time & state checks
    if mailer_mode == "NOW":
        return True

    # Do not trigger before market closing cutoff (15:30 IST)
    eod_cutoff = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now < eod_cutoff:
        return False

    # Check if EOD email has already been sent today
    today_str = datetime.date.today().isoformat()
    if EOD_STATE_FILE.exists():
        try:
            with open(EOD_STATE_FILE, "r") as f:
                last_sent_date = f.read().strip()
                if last_sent_date == today_str:
                    logger.info("EOD email report already dispatched for today. Skipping.")
                    return False
        except Exception as e:
            logger.warning(f"Could not read EOD state file: {e}")

    if mailer_mode == "EOD":
        return True

    elif mailer_mode == "EOW":
        return now.weekday() == 4

    elif mailer_mode == "EOM":
        tomorrow = now + datetime.timedelta(days=1)
        return tomorrow.day == 1

    return False


def calculate_dynamic_allocation(available_cash, alloc_pct, min_alloc, max_alloc):
    calculated = available_cash * alloc_pct
    allocation = max(min_alloc, min(calculated, max_alloc))
    return min(allocation, available_cash)


def evaluate_buy_signal(metrics, available_cash, min_alloc, max_alloc, alloc_pct, rsi_lower):
    """Evaluates BUY signals against dynamically loaded bounds."""
    price = metrics.get("price", 0.0)
    signals = metrics.get("signals", [])
    rsi = metrics.get("rsi", 50.0)

    if price > max_alloc or price < min_alloc:
        return None, 0.0, ""

    suggested_capital = calculate_dynamic_allocation(available_cash, alloc_pct, min_alloc, max_alloc)

    if suggested_capital < price or available_cash < price:
        return None, 0.0, ""

    if rsi <= rsi_lower and "MACD_BULLISH_CROSS" in signals:
        return (
            "BUY (INTRA-DAY / SHORT-TERM)",
            suggested_capital,
            f"Oversold bounce (RSI {rsi:.1f}) + MACD Bullish Crossover. Capital verified."
        )
    elif rsi <= rsi_lower:
        return (
            "BUY (LONG-TERM SIP)",
            suggested_capital,
            f"Value entry zone (RSI {rsi:.1f} <= {rsi_lower}). Capital verified."
        )
    elif rsi < 45 and "MACD_BULLISH_CROSS" in signals:
        return (
            "BUY (SHORT-TERM)",
            suggested_capital,
            f"Bullish momentum crossover at RSI {rsi:.1f}. Capital verified."
        )

    return None, 0.0, ""


def evaluate_sell_signal(holding, metrics, rsi_upper):
    """Evaluates SELL signals for active holdings in your Angel One portfolio."""
    avg_price = holding.get("avg_price", 0.0)
    current_price = metrics.get("price", holding.get("current_price", 0.0))
    rsi = metrics.get("rsi", 50.0)
    signals = metrics.get("signals", [])

    pnl_pct = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0.0

    if rsi >= rsi_upper or "MACD_BEARISH_CROSS" in signals or "RSI_OVERBOUGHT" in signals:
        return (
            "SELL (EXIT / TAKE PROFIT)",
            f"RSI overbought ({rsi:.1f}) or bearish crossover detected. P&L: {pnl_pct:+.2f}%"
        )
    elif pnl_pct <= -5.0:
        return (
            "SELL (STOP LOSS)",
            f"Position hit stop loss threshold of -5.0% (Current P&L: {pnl_pct:.2f}%)."
        )

    return None, ""


def clean_ticker_for_yfinance(symbol: str) -> str:
    s = symbol.replace("-EQ", "").replace("-BE", "").strip()
    return s if s.endswith(".NS") else f"{s}.NS"


def extract_ticker_df(data, ticker, is_multi):
    """Extracts clean single-ticker DataFrame from yfinance batch response."""
    try:
        if is_multi:
            if ticker in data.columns.levels[0]:
                df = data[ticker].dropna()
                # Ensure 'Close' exists in the extracted DataFrame
                if "Close" in df.columns:
                    return df
            return None
        else:
            df = data.dropna()
            return df if "Close" in df.columns else None
    except Exception:
        return None


def run_pipeline(force_email_now=False):
    # Load environment variables dynamically on each run
    load_dotenv(BASE_DIR / ".env", override=True)

    min_alloc = float(os.getenv("MIN_TRADE_ALLOCATION", 100.0))
    max_alloc = float(os.getenv("MAX_TRADE_ALLOCATION", 500.0))
    alloc_pct = float(os.getenv("PORTFOLIO_ALLOCATION_PCT", 0.10))
    rsi_lower = float(os.getenv("RSI_LOWER_THRESHOLD", 35.0))
    rsi_upper = float(os.getenv("RSI_UPPER_THRESHOLD", 65.0))
    tickers_count = int(os.getenv("TICKERS_COUNT", 50))
    
    # Determine mailer mode
    mailer_mode = "NOW" if force_email_now else os.getenv("MAILER_TIME", "EOD").upper()
    
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
    credentials_file = BASE_DIR / os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")

    logger.info(f"{CLR_BOLD}--- Cycle Configuration Loaded ---{CLR_RESET}")
    logger.info(f"Price Window: ₹{min_alloc} - ₹{max_alloc} | Cap Alloc: {alloc_pct*100}% | Mailer Mode: {mailer_mode}")

    market_active = is_market_open()

    if "process_telegram_commands" in globals():
        process_telegram_commands()

    # Dynamic API fetch from Angel One
    logger.info("Authenticating with Angel One SmartAPI to fetch dynamic portfolio...")
    real_portfolio = {}
    try:
        angel_client = AngelOneClient()
        # Register client instance with Telegram bot module for auto-reauth
        set_smart_client(angel_client)
        real_portfolio = angel_client.get_real_portfolio_data() or {}
    except Exception as e:
        err_msg = f"Error fetching portfolio from Angel One: {e}"
        logger.error(err_msg)
        send_critical_failure_alert(err_msg)

    available_cash = real_portfolio.get("available_cash", 0.0)
    holdings = real_portfolio.get("holdings", [])
    
    total_invested = sum(h.get("invested_val", 0.0) for h in holdings)
    total_current = sum(h.get("current_val", 0.0) for h in holdings)
    total_pnl = sum(h.get("pnl", 0.0) for h in holdings)

    # Log Portfolio Snapshot in SQLite
    try:
        db.log_portfolio_snapshot(
            cash=available_cash,
            invested=total_invested,
            current=total_current,
            pnl=total_pnl
        )
    except Exception as e:
        logger.error(f"Failed to record portfolio snapshot: {e}")

    logger.info(f"Available Demat Capital: ₹{available_cash:,.2f} | Active Holdings: {len(holdings)}")

    scan_results = {}
    processed_rows = [["Ticker", "Price", "RSI", "MACD", "Signals"]]
    raw_data = []

    if market_active:
        # 1. EVALUATE SELL SIGNALS FOR ACTIVE HOLDINGS
        if holdings:
            logger.info("Scanning active Angel One holdings for SELL triggers...")
            holding_tickers = list(set([clean_ticker_for_yfinance(h["ticker"]) for h in holdings if h.get("ticker")]))
            
            if holding_tickers:
                try:
                    h_data = yf.download(holding_tickers, period="6mo", group_by="ticker", progress=False)
                    is_multi_h = len(holding_tickers) > 1

                    for h in holdings:
                        t_raw = h.get("ticker", "")
                        t_yf = clean_ticker_for_yfinance(t_raw)
                        df_h = extract_ticker_df(h_data, t_yf, is_multi_h)

                        if df_h is not None and not df_h.empty:
                            h_metrics = analyze_ticker_data(df_h, ticker=t_raw)
                            if h_metrics:
                                sell_action, sell_reasoning = evaluate_sell_signal(h, h_metrics, rsi_upper)
                                if sell_action:
                                    logger.info(f"{CLR_BOLD}{CLR_RED}🔻 SELL Signal [{t_raw}]:{CLR_RESET} {sell_action}")
                                    send_investment_suggestion(
                                        ticker=t_raw,
                                        strategy=sell_action,
                                        amount=h.get("qty", 0) * h_metrics.get("price", 0.0),
                                        current_price=h_metrics.get("price", 0.0),
                                        reasoning=sell_reasoning
                                    )
                except Exception as e:
                    logger.error(f"Error evaluating holdings batch: {e}")

        # 2. SCAN NSE MARKET FOR BUY SIGNALS
        try:
            import inspect
            sig = inspect.signature(get_dynamic_tickers)
            tickers = get_dynamic_tickers(tickers_count) if "count" in sig.parameters or "limit" in sig.parameters else get_dynamic_tickers()
        except Exception:
            tickers = get_dynamic_tickers()

        logger.info(f"{CLR_BOLD}{CLR_GREEN}Market is LIVE.{CLR_RESET} Sweeping {len(tickers)} market tickers...")
        
        if tickers:
            try:
                data = yf.download(tickers, period="6mo", group_by="ticker", progress=False)
                is_multi_m = len(tickers) > 1

                for ticker in tickers:
                    try:
                        df = extract_ticker_df(data, ticker, is_multi_m)
                        if df is None or df.empty:
                            continue
                        
                        metrics = analyze_ticker_data(df, ticker=ticker)
                        if not metrics:
                            continue
                            
                        scan_results[ticker] = metrics
                        price = metrics["price"]

                        strategy, amount, reasoning = evaluate_buy_signal(
                            metrics, available_cash, min_alloc, max_alloc, alloc_pct, rsi_lower
                        )
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

            except Exception as e:
                logger.error(f"Error during market scan download: {e}")

    else:
        logger.info(f"{CLR_BOLD}{CLR_YELLOW}Market is CLOSED.{CLR_RESET} Skipping real-time market scan.")

    # 3. GOOGLE SHEETS SYNCHRONIZATION
    if credentials_file.exists() and spreadsheet_id:
        try:
            sync_dashboard_data(
                credentials_path=str(credentials_file),
                spreadsheet_id=spreadsheet_id,
                processed_data=processed_rows,
                raw_data=raw_data,
                real_portfolio=real_portfolio,
                main_tab_name="Market Scan",
                raw_tab_name="Raw Data",
                wallet_tab_name="Wallet_and_Holdings"
            )
        except Exception as e:
            logger.error(f"Google Sheets sync error: {e}")

    # 4. EMAIL REPORT DISPATCH
    if should_trigger_mail(mailer_mode):
        logger.info(f"{CLR_BOLD}{CLR_CYAN}Sending Email Report (Trigger Mode: {mailer_mode})...{CLR_RESET}")
        try:
            send_eod_email_report(
                start_cash=available_cash,
                end_cash=available_cash,
                transactions=[]
            )
            # Record that the email was successfully sent for today (unless forced using --email-now)
            if mailer_mode != "NOW":
                mark_eod_mail_sent()
        except Exception as e:
            logger.error(f"Error sending email report: {e}")

    return market_active


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ASTRA Algorithmic Trading Engine")
    parser.add_argument("--email-now", action="store_true", help="Force send an immediate debug email report")
    args = parser.parse_args()

    logger.info(f"{CLR_BOLD}{CLR_MAGENTA}=== Starting ASTRA Engine ==={CLR_RESET}")
    
    # Initialize background Telegram bot thread
    if "run_telegram_bot_loop" in globals():
        bot_thread = threading.Thread(target=run_telegram_bot_loop, daemon=True)
        bot_thread.start()
        logger.info("Telegram Interactive Bot Listener initialized in background.")

    try:
        while True:
            load_dotenv(BASE_DIR / ".env", override=True)
            env_cycle_buffer = int(os.getenv("ASTRA_CYCLE_BUFFER", 5))
            live_loop_interval = max(3, env_cycle_buffer) * 60
            closed_loop_interval = 15 * 60

            start_time = time.time()
            market_active = run_pipeline(force_email_now=args.email_now)
            
            # Reset CLI flag after first iteration
            if args.email_now:
                args.email_now = False

            elapsed = time.time() - start_time
            target_interval = live_loop_interval if market_active else closed_loop_interval
            sleep_duration = max(0, target_interval - elapsed)
            
            sleep_mins = int(sleep_duration // 60)
            logger.info(f"{CLR_BLUE}Iteration complete. Sleeping for {sleep_mins} minutes...{CLR_RESET}\n")
            time.sleep(sleep_duration)
            
    except KeyboardInterrupt:
        logger.info(f"\n{CLR_RED}{CLR_BOLD}[!] Gracefully shutting down ASTRA Engine. Goodbye!{CLR_RESET}")
        sys.exit(0)

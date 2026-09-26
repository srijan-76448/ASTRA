import os
import re
import time
import json
import shutil
import requests
import logging
import asyncio
import yfinance as yf
from pathlib import Path
from logging.handlers import RotatingFileHandler
from dotenv import set_key, load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# custom modules for ASTRA
from gold import fetch_gold_data, format_gold_message
from mng_db import DatabaseManager

# Silence verbose HTTP requests
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
ENV_BAK_FILE = BASE_DIR / ".env.bak"
CACHE_FILE = BASE_DIR / "alert_cache.json"
LOG_FILE = BASE_DIR / "astra_bot.log"
ALERT_COOLDOWN_SECONDS = 6 * 3600  # 6 Hours

# Configure Rotating File Logger
logger = logging.getLogger("ASTRA_TELEGRAM")
logger.setLevel(logging.INFO)
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
formatter = logging.Formatter("%(asctime)s - [%(levelname)s] - %(message)s")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Initialize Database Instance
db = DatabaseManager()

# Global states
IS_SUSPENDED = False
ACTIVE_SMART_CLIENT = None


def set_smart_client(client_instance):
    """
    Registers the authenticated AngelOneClient instance initialized in main.py
    so Telegram handlers can reuse the existing SmartAPI session.
    """
    global ACTIVE_SMART_CLIENT
    ACTIVE_SMART_CLIENT = client_instance
    logger.info("Successfully registered active SmartAPI client with Telegram module.")


def get_active_smart_client():
    """
    Auto-reauthentication Middleware: Validates existing session and automatically 
    re-authenticates if session state is expired or invalidated.
    """
    global ACTIVE_SMART_CLIENT
    client = ACTIVE_SMART_CLIENT

    if not client:
        try:
            from smartapi import AngelOneClient
            client = AngelOneClient()
            if hasattr(client, "authenticate") and callable(client.authenticate):
                client.authenticate()
            elif hasattr(client, "login") and callable(client.login):
                client.login()
            ACTIVE_SMART_CLIENT = client
        except Exception as e:
            logger.error(f"Auto-Reauthentication failed: {e}")
            send_critical_failure_alert(f"SmartAPI Auto-Reauthentication Failure: {e}")
            return None

    return client


def send_critical_failure_alert(error_msg: str):
    """Dispatches emergency notification to Telegram in case of system lockouts or uncaught errors."""
    msg = (
        "<b>🚨 ASTRA CRITICAL ENGINE ALERT</b>\n\n"
        f"• <b>Error:</b> <code>{error_msg}</code>\n"
        "• <b>Impact:</b> Automated execution interrupted.\n"
        "• <b>Action Required:</b> Inspect system logs immediately."
    )
    send_telegram_message(msg)


def init_env_backup():
    """Creates a .env.bak file on initial system boot if it does not exist."""
    if ENV_FILE.exists() and not ENV_BAK_FILE.exists():
        try:
            shutil.copyfile(ENV_FILE, ENV_BAK_FILE)
            logger.info(f"Created initial environment backup at: {ENV_BAK_FILE}")
        except Exception as e:
            logger.error(f"Failed to create .env.bak: {e}")


def get_env_val(key: str, default: str = "") -> str:
    """Fetches raw environment variable directly from .env on disk."""
    load_dotenv(ENV_FILE, override=True)
    return os.getenv(key, default)


def load_alert_cache() -> dict:
    """Loads alert history from disk."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read alert cache from disk: {e}")
            return {}
    return {}


def save_alert_cache(cache: dict):
    """Saves alert history to disk."""
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to write alert cache to disk: {e}")


def parse_duration_to_seconds(duration_str: str) -> int | None:
    """Parses time string formats such as '5m' or '2h' into seconds."""
    match = re.match(r"^(\d+)([mh])$", duration_str.lower().strip())
    if not match:
        return None

    val, unit = match.groups()
    val = int(val)
    return val * 60 if unit == "m" else val * 3600


def send_telegram_message(message_html: str):
    """Sends a raw HTML message via Telegram Bot API (Synchronous Dispatch)."""
    if IS_SUSPENDED:
        logger.info("⏸️ Telegram bot is currently suspended. Suppressing outgoing telemetry message.")
        return

    token = get_env_val("TELEGRAM_BOT_TOKEN")
    chat_id = get_env_val("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in .env")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message_html,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Failed to dispatch Telegram message: {e}")


def send_investment_suggestion(ticker: str, strategy: str, amount: float, current_price: float, reasoning: str):
    """Dispatches alerts with persistent 6-hour disk-backed duplicate suppression and SQLite logging."""
    if IS_SUSPENDED:
        return

    current_time = time.time()
    cache_key = f"{ticker}_{strategy}".upper().strip()

    cache = load_alert_cache()

    # Clear entries older than 6 hours
    expired_keys = [
        k for k, ts in cache.items()
        if current_time - ts > ALERT_COOLDOWN_SECONDS
    ]
    for k in expired_keys:
        del cache[k]

    # Check cooldown state
    if cache_key in cache:
        last_sent_time = cache[cache_key]
        elapsed_hours = (current_time - last_sent_time) / 3600.0
        logger.info(
            f"🚫 [DEDUPLICATED] Alert for {ticker} ({strategy}) was sent {elapsed_hours:.2f} hrs ago. "
            f"Suppressing duplicate signal."
        )
        save_alert_cache(cache)
        return

    # Strictly output BUY or SELL signal
    action_clean = "SELL" if "SELL" in strategy.upper() else "BUY"
    emoji = "🔴" if action_clean == "SELL" else "🟢"

    # Calculate Stop Loss and Target Price
    tsl = current_price * 0.98 if action_clean == "BUY" else current_price * 1.02
    tp = current_price * 1.05 if action_clean == "BUY" else current_price * 0.95

    msg = (
        f"<b>{emoji} ASTRA TELEMETRY ALERT: {ticker}</b>\n\n"
        f"• <b>Action:</b> <code>{action_clean}</code>\n"
        f"• <b>Live Price:</b> ₹{current_price:,.2f}\n"
        f"• <b>Stop Loss (TSL -2%):</b> ₹{tsl:,.2f}\n"
        f"• <b>Target Price (TP +5%):</b> ₹{tp:,.2f}\n"
        f"• <b>Allocated Amount:</b> ₹{amount:,.2f}\n"
        f"• <b>Technical Reasoning:</b> {reasoning}\n"
    )

    send_telegram_message(msg)

    # Persist in cache and database
    cache[cache_key] = current_time
    save_alert_cache(cache)
    db.log_signal(ticker, strategy, current_price, 0.0, 0.0, action_clean)


# ==========================================================
# CLI-STYLE TEXT COMMAND INTERFACE
# ==========================================================

async def cmd_resume_callback(context: ContextTypes.DEFAULT_TYPE):
    """Callback execution triggered automatically when suspension timer elapses."""
    global IS_SUSPENDED
    IS_SUSPENDED = False
    chat_id = context.job.chat_id
    logger.info("🟢 [SUSPEND ENDED] Telegram bot timer elapsed. Back online...")
    await context.bot.send_message(
        chat_id=chat_id,
        text="<b>🟢 Back online...</b>",
        parse_mode="HTML"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays available CLI text commands."""
    if IS_SUSPENDED:
        return

    help_text = (
        "<b>💻 ASTRA CLI Terminal Commands</b>\n\n"
        "<code>/status</code> - Output system status and cycle info\n"
        "<code>/sheet</code> or <code>/spreadsheet</code> - Get live Google Sheets telemetry URL\n"
        "<code>/config</code> or <code>/cfg</code> - Read raw .env configuration\n"
        "<code>/set KEY VALUE</code> - Set or overwrite any .env variable\n"
        "<code>/restore_default</code> or <code>/restore</code> - Restore .env from .env.bak\n"
        "<code>/suspend DURATION</code> - Pause system (e.g., <code>/suspend 5m</code> or <code>/suspend 2h</code>)\n"
        "<code>/portfolio</code> - Output portfolio state\n"
        "<code>/analyze SYMBOL</code> - Instant stock technical analysis\n\n"
        "<i>Examples:</i>\n"
        "• <code>/analyze TATAMOTORS</code>\n"
        "• <code>/suspend 30m</code>"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prints system status."""
    if IS_SUSPENDED:
        return

    buf = get_env_val("ASTRA_CYCLE_BUFFER", "5")
    tickers = get_env_val("TICKERS_COUNT", "50")
    min_p = get_env_val("MIN_TRADE_ALLOCATION", "100.0")
    max_p = get_env_val("MAX_TRADE_ALLOCATION", "500.0")

    msg = (
        "<b>🔄 ASTRA SYSTEM TELEMETRY</b>\n"
        "-------------------------------------\n"
        f"• <b>Cycle Buffer:</b> {buf} minutes\n"
        f"• <b>Ticker Sweep Count:</b> {tickers}\n"
        f"• <b>Price Allocation Window:</b> ₹{min_p} - ₹{max_p}\n"
        f"• <b>Backup State:</b> {'Found (.env.bak)' if ENV_BAK_FILE.exists() else 'Missing'}\n"
        f"• <b>Status:</b> Active & Listening"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Returns the direct Google Sheets URL for active telemetry."""
    if IS_SUSPENDED:
        return

    sheet_id = get_env_val("GOOGLE_SHEETS_SPREADSHEET_ID")
    if not sheet_id:
        await update.message.reply_text(
            "❌ <code>GOOGLE_SHEETS_SPREADSHEET_ID</code> is missing in <code>.env</code>.",
            parse_mode="HTML"
        )
        return

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    msg = (
        "<b>📊 ASTRA LIVE TELEMETRY SPREADSHEET</b>\n\n"
        f"🔗 <a href=\"{sheet_url}\">Click here to open Google Sheet</a>\n\n"
        f"<code>{sheet_url}</code>"
    )
    await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=False)


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dumps full .env file contents to chat with token masking."""
    if IS_SUSPENDED:
        return

    if not ENV_FILE.exists():
        await update.message.reply_text("❌ <code>.env</code> file not found on disk.", parse_mode="HTML")
        return

    try:
        with open(ENV_FILE, "r") as f:
            lines = f.readlines()

        masked_lines = []
        for line in lines:
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                masked_lines.append(line_str)
                continue

            if "=" in line_str:
                k, v = line_str.split("=", 1)
                k_upper = k.strip().upper()
                if "TOKEN" in k_upper or "SECRET" in k_upper or "PASSWORD" in k_upper:
                    masked_v = v[:4] + "..." + v[-4:] if len(v) > 8 else "********"
                    masked_lines.append(f"{k.strip()}={masked_v}")
                else:
                    masked_lines.append(line_str)
            else:
                masked_lines.append(line_str)

        env_content = "\n".join(masked_lines)
        msg = (
            "<b>📄 CURRENT .env CONFIGURATION</b>\n"
            f"<pre>{env_content}</pre>\n"
            "Use <code>/set KEY VALUE</code> to update or add any setting."
        )
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to read <code>.env</code>: {str(e)}", parse_mode="HTML")


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Modifies environment variables directly in .env without unnecessary quotes."""
    if IS_SUSPENDED:
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "<b>Usage:</b> <code>/set KEY VALUE</code>\n"
            "<i>Example:</i> <code>/set TICKERS_COUNT 60</code>",
            parse_mode="HTML"
        )
        return

    key = args[0].strip()
    val = " ".join(args[1:]).strip()

    if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
        val = val[1:-1]

    try:
        set_key(ENV_FILE, key, val, quote_mode="never")
        await update.message.reply_text(
            f"✅ <b>Updated .env variable:</b>\n"
            f"<code>{key}</code> = <code>{val}</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ <b>Error modifying .env:</b> {str(e)}",
            parse_mode="HTML"
        )


async def cmd_restore_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restores default .env configuration from .env.bak."""
    if IS_SUSPENDED:
        return

    if not ENV_BAK_FILE.exists():
        await update.message.reply_text(
            "❌ <b>Backup missing:</b> No <code>.env.bak</code> file found to restore from.",
            parse_mode="HTML"
        )
        return

    try:
        shutil.copyfile(ENV_BAK_FILE, ENV_FILE)
        await update.message.reply_text(
            "🔄 <b>Configuration Restored:</b> <code>.env</code> has been successfully restored from <code>.env.bak</code>.",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ <b>Restore failed:</b> {str(e)}",
            parse_mode="HTML"
        )


async def cmd_suspend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Suspends the bot for a designated duration (e.g., 5m or 2h)."""
    global IS_SUSPENDED

    if not context.args:
        await update.message.reply_text(
            "<b>Usage:</b> <code>/suspend DURATION</code>\n"
            "<i>Examples:</i> <code>/suspend 5m</code> or <code>/suspend 2h</code>",
            parse_mode="HTML"
        )
        return

    time_param = context.args[0]
    seconds = parse_duration_to_seconds(time_param)

    if seconds is None:
        await update.message.reply_text(
            "❌ <b>Invalid time format.</b> Use <code>m</code> for minutes or <code>h</code> for hours.\n"
            "<i>Examples:</i> <code>5m</code>, <code>2h</code>",
            parse_mode="HTML"
        )
        return

    if context.job_queue is None:
        await update.message.reply_text(
            "❌ <b>JobQueue extension not installed.</b> Run: <code>pip install \"python-telegram-bot[job-queue]\"</code>",
            parse_mode="HTML"
        )
        return

    IS_SUSPENDED = True
    chat_id = update.effective_chat.id

    context.job_queue.run_once(
        cmd_resume_callback,
        when=seconds,
        chat_id=chat_id,
        name=f"resume_{chat_id}"
    )

    logger.info(f"⏸️ [SUSPENDED] Telegram bot suspended for {time_param} ({seconds}s).")
    await update.message.reply_text(
        f"⏸️ <b>ASTRA engine suspended for {time_param}.</b>\n"
        f"I will send <i>'Back online...'</i> when the duration completes.",
        parse_mode="HTML"
    )


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches and displays live portfolio state directly from SmartAPI client using middleware auto-reauth."""
    if IS_SUSPENDED:
        return

    await update.message.reply_text("🔄 <i>Fetching live portfolio telemetry...</i>", parse_mode="HTML")

    try:
        client = get_active_smart_client()
        if not client:
            await update.message.reply_text("❌ Failed to authenticate with Angel One API.", parse_mode="HTML")
            return

        smart_api_handle = getattr(client, "smart_api", client)
        holdings_res = smart_api_handle.holding()
        rms_res = smart_api_handle.rmsLimit()

        total_invested = 0.0
        total_current = 0.0
        holding_details = []

        if holdings_res and holdings_res.get("status") and holdings_res.get("data"):
            holdings = holdings_res["data"]
            for item in holdings:
                qty = int(item.get("quantity", 0))
                avg_price = float(item.get("averageprice", 0.0))
                ltp = float(item.get("ltp", 0.0))
                symbol = item.get("tradingsymbol", "N/A")

                inv_val = qty * avg_price
                curr_val = qty * ltp
                total_invested += inv_val
                total_current += curr_val

                pnl = curr_val - inv_val
                pnl_pct = (pnl / inv_val * 100) if inv_val > 0 else 0.0
                pnl_icon = "🟢" if pnl >= 0 else "🔴"

                holding_details.append(
                    f"• <b>{symbol}</b> ({qty} Qty)\n"
                    f"  Avg: ₹{avg_price:,.2f} | LTP: ₹{ltp:,.2f}\n"
                    f"  P&amp;L: {pnl_icon} ₹{pnl:,.2f} ({pnl_pct:+.2f}%)"
                )

        total_pnl = total_current - total_invested
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0
        pnl_overall_icon = "🟢" if total_pnl >= 0 else "🔴"

        available_cash = 0.0
        if rms_res and rms_res.get("status") and rms_res.get("data"):
            available_cash = float(rms_res["data"].get("net", 0.0))

        # Log portfolio snapshot to SQLite
        db.log_portfolio_snapshot(available_cash, total_invested, total_current, total_pnl)

        holdings_str = "\n".join(holding_details) if holding_details else "<i>No active holdings.</i>"

        msg = (
            "<b>📊 LIVE PORTFOLIO TELEMETRY</b>\n"
            "-------------------------------------\n"
            f"💰 <b>Available Cash:</b> ₹{available_cash:,.2f}\n"
            f"💼 <b>Total Invested:</b> ₹{total_invested:,.2f}\n"
            f"📈 <b>Current Value:</b> ₹{total_current:,.2f}\n"
            f"{pnl_overall_icon} <b>Total Realized P&amp;L:</b> ₹{total_pnl:,.2f} ({total_pnl_pct:+.2f}%)\n"
            "-------------------------------------\n"
            "<b>Active Positions:</b>\n"
            f"{holdings_str}"
        )

        logger.info("Fetched live portfolio telemetry for /portfolio command.")
        await update.message.reply_text(msg, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Failed to pull live portfolio for Telegram: {e}")
        send_critical_failure_alert(f"Portfolio Fetch Exception: {e}")
        await update.message.reply_text(
            f"❌ <b>Error pulling live portfolio:</b> <code>{str(e)}</code>",
            parse_mode="HTML"
        )


async def cmd_gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executes gold data fetch via gold.py and outputs formatted response to chat."""
    if IS_SUSPENDED:
        return

    await update.message.reply_text("👑 <i>Fetching live Gold & Precious Metals telemetry...</i>", parse_mode="HTML")

    try:
        client = get_active_smart_client()
        gold_data = fetch_gold_data(smart_client=client)
        msg = format_gold_message(gold_data)

        logger.info("Successfully fetched gold telemetry via gold.py module.")
        await update.message.reply_text(msg, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Failed to fetch gold pricing telemetry via gold.py: {e}")
        await update.message.reply_text(
            f"❌ <b>Error fetching gold telemetry:</b> <code>{str(e)}</code>",
            parse_mode="HTML"
        )


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches market data for a given ticker and returns an instant technical evaluation strictly restricted to BUY or SELL with Stop Loss & TP."""
    if IS_SUSPENDED:
        return

    if not context.args:
        await update.message.reply_text(
            "<b>Usage:</b> <code>/analyze TICKER</code> or <code>/predict TICKER</code>\n"
            "<i>Examples:</i> <code>/analyze TATAMOTORS</code> or <code>/predict RELIANCE</code>",
            parse_mode="HTML"
        )
        return

    raw_symbol = context.args[0].upper().strip()
    clean_symbol = raw_symbol if raw_symbol.endswith(".NS") or raw_symbol.endswith(".BO") else f"{raw_symbol}.NS"

    await update.message.reply_text(
        f"🔍 <i>Analyzing market data for <b>{clean_symbol}</b>...</i>",
        parse_mode="HTML"
    )

    try:
        df = yf.download(clean_symbol, period="6mo", progress=False)
        if df is None or df.empty:
            await update.message.reply_text(
                f"❌ Could not retrieve price data for <code>{clean_symbol}</code>. Please check ticker symbol.",
                parse_mode="HTML"
            )
            return

        # Flatten multi-index columns if returned by yfinance
        if hasattr(df.columns, 'levels') and len(df.columns.levels) > 1:
            try:
                df = df.xs(clean_symbol, level=1, axis=1)
            except Exception:
                df.columns = df.columns.get_level_values(0)

        from decision_engine import analyze_ticker_data
        metrics = analyze_ticker_data(df)

        if not metrics:
            await update.message.reply_text(
                f"⚠️ Insufficient technical data to evaluate <code>{clean_symbol}</code>.",
                parse_mode="HTML"
            )
            return

        price = metrics.get("price", 0.0)
        rsi = metrics.get("rsi", 50.0)
        macd = metrics.get("macd", 0.0)
        signals = metrics.get("signals", [])
        sig_str = ", ".join(signals) if signals else "NEUTRAL"

        # Binary Decision Rule: BUY or SELL only
        if rsi < 50.0 or "MACD_BULLISH_CROSS" in signals:
            recommendation = "🟢 BUY"
            reasoning = f"Bullish bias (RSI {rsi:.1f} &lt; 50 or MACD structure)."
            tsl = price * 0.98
            tp = price * 1.05
        else:
            recommendation = "🔴 SELL"
            reasoning = f"Bearish / profit-taking bias (RSI {rsi:.1f} &gt;= 50 or MACD exhaustion)."
            tsl = price * 1.02
            tp = price * 0.95

        # Persist signal in SQLite DB
        db.log_signal(clean_symbol, recommendation, price, rsi, macd, recommendation.split()[-1])

        report = (
            f"<b>📊 TECHNICAL ANALYSIS: {clean_symbol}</b>\n\n"
            f"• <b>Current Price:</b> ₹{price:,.2f}\n"
            f"• <b>Stop Loss (TSL -2%):</b> ₹{tsl:,.2f}\n"
            f"• <b>Target Price (TP +5%):</b> ₹{tp:,.2f}\n"
            f"• <b>RSI (14):</b> {rsi:.1f}\n"
            f"• <b>MACD Value:</b> {macd:.2f}\n"
            f"• <b>Active Signals:</b> <code>{sig_str}</code>\n\n"
            f"💡 <b>RECOMMENDATION:</b> <code>{recommendation}</code>\n"
            f"📝 <b>Reasoning:</b> {reasoning}\n\n"
            f"<i>Note: Analysis calculated independently of wallet balance.</i>"
        )
        await update.message.reply_text(report, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Failed to analyze stock command for {clean_symbol}: {e}")
        send_critical_failure_alert(f"Analysis Error on {clean_symbol}: {e}")
        await update.message.reply_text(
            f"❌ Error analyzing <code>{clean_symbol}</code>: {str(e)}",
            parse_mode="HTML"
        )


async def handle_unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback handler for unrecognized text inputs."""
    if IS_SUSPENDED:
        return

    await update.message.reply_text(
        "Command unrecognized. Type <code>/help</code> for available commands.",
        parse_mode="HTML"
    )


def run_telegram_bot_loop():
    """Starts the asynchronous Telegram bot listener and initializes backups."""
    init_env_backup()

    token = get_env_val("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN missing in .env. Cannot start Telegram listener.")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(token).build()

    # Terminal command routes
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler(["sheet", "spreadsheet"], cmd_sheet))
    app.add_handler(CommandHandler(["config", "cfg"], cmd_config))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler(["restore_default", "restore"], cmd_restore_default))
    app.add_handler(CommandHandler("suspend", cmd_suspend))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("gold", cmd_gold))
    app.add_handler(CommandHandler("analyze", cmd_analyze))

    # Fallback handler for unrecognized commands
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown_text))

    logger.info("🤖 Telegram CLI Command Listener active and polling...")
    app.run_polling(drop_pending_updates=True, stop_signals=None)

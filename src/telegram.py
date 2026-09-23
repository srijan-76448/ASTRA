import os
import time
import json
import requests
import datetime
from pathlib import Path
from dotenv import load_dotenv
from logzero import logger


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
TG_STATE_FILE = BASE_DIR / ".telegram_state.json"


def load_tg_state():
    today = str(datetime.date.today())
    default_state = {
        "date": today,
        "mute_until": None,        # ISO string timestamp if muted
        "notified_tickers": {},    # { ticker: timestamp_epoch }
        "last_update_id": 0
    }
    
    if TG_STATE_FILE.exists():
        try:
            with open(TG_STATE_FILE, "r") as f:
                state = json.load(f)
                if state.get("date") == today:
                    return state
                else:
                    # Preserve last_update_id across days, reset daily mute status
                    default_state["last_update_id"] = state.get("last_update_id", 0)
                    return default_state
        except Exception as e:
            logger.error(f"Error reading TG state file: {e}")

    save_tg_state(default_state)
    return default_state


def save_tg_state(state):
    with open(TG_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def process_telegram_commands():
    """Polls Telegram updates to process commands like 'stop notifying me for today'."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return

    state = load_tg_state()
    last_id = state.get("last_update_id", 0)

    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {"offset": last_id + 1, "timeout": 2}

    try:
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            updates = res.json().get("result", [])
            for up in updates:
                state["last_update_id"] = up["update_id"]
                msg = up.get("message", {}).get("text", "").strip().lower()

                if any(phrase in msg for phrase in ["stop notifying me for today", "/mute", "stop today"]):
                    # Mute until midnight tonight
                    tomorrow = datetime.datetime.now().replace(hour=23, minute=59, second=59)
                    state["mute_until"] = tomorrow.isoformat()
                    send_telegram_alert("🔇 Understood. Notifications muted for the rest of today. Resuming tomorrow.")
                    logger.info("Telegram notifications muted by user request for today.")
                elif any(phrase in msg for phrase in ["resume", "/unmute", "start notifying"]):
                    state["mute_until"] = None
                    send_telegram_alert("🔔 Notifications resumed.")
                    logger.info("Telegram notifications unmuted by user request.")

            save_tg_state(state)
    except Exception as e:
        logger.error(f"Error processing Telegram commands: {e}")

def send_telegram_alert(message: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.error("Telegram credentials missing in .env")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        logger.exception(f"Failed to send Telegram message: {e}")
        return False

def send_investment_suggestion(ticker: str, strategy: str, amount: float, current_price: float, reasoning: str):
    state = load_tg_state()

    # 1. Mute Check
    if state.get("mute_until"):
        mute_until = datetime.datetime.fromisoformat(state["mute_until"])
        if datetime.datetime.now() < mute_until:
            logger.info(f"Skipping alert for {ticker}: Muted until {mute_until}")
            return False

    # 2. Duplicate Suppression Check
    repeat_buffer_hours = max(1.0, float(os.getenv("NOTIFICATION_REPEAT_BUFFER", 8.0)))
    now_epoch = time.time()
    last_sent = state.get("notified_tickers", {}).get(ticker, 0)

    if (now_epoch - last_sent) < (repeat_buffer_hours * 3600):
        logger.info(f"Skipping alert for {ticker}: Already notified within {repeat_buffer_hours} hrs.")
        return False

    msg = (
        f"🚨 *ASTRA Real-Time Trade Signal*\n\n"
        f"• *Ticker*: `{ticker}`\n"
        f"• *Strategy*: *{strategy}*\n"
        f"• *Suggested Capital*: ₹{amount:,.2f}\n"
        f"• *Current Price*: ₹{current_price:,.2f}\n"
        f"• *Analysis*: _{reasoning}_\n"
    )

    sent = send_telegram_alert(msg)
    if sent:
        state["notified_tickers"][ticker] = now_epoch
        save_tg_state(state)
    return sent

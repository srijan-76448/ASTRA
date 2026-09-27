import os
import requests
import logging
from pathlib import Path

logger = logging.getLogger("ASTRA_UTILS")

def send_critical_failure_alert(error_msg: str) -> None:
    """Dispatches emergency notification to Telegram in case of system lockouts or errors."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning("Telegram credentials missing in .env. Alert suppressed.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": (
            "<b>🚨 ASTRA CRITICAL ENGINE ALERT</b>\n\n"
            f"• <b>Error:</b> <code>{error_msg}</code>\n"
            "• <b>Impact:</b> Automated execution interrupted.\n"
            "• <b>Action Required:</b> Inspect system logs immediately."
        ),
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Failed to dispatch emergency alert: {e}")

def clean_ticker_symbol(symbol: str) -> str:
    """Cleans exchange suffix formatting for yfinance lookups."""
    s = symbol.replace("-EQ", "").replace("-BE", "").strip()
    return s if s.endswith(".NS") or s.endswith(".BO") else f"{s}.NS"

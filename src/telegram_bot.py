"""
ASTRA Telegram Runtime.

Runtime responsibilities:
- Telegram command/control plane.
- Smart Intraday activation.
- SIP activation and target management.
- Global/module runtime control.
- Runtime configuration through privileged /set.
- Precious-metals telemetry through hidden /gold.
- Status and wallet telemetry.

Important:
- Runtime commands do not modify .env.
- /gold is intentionally hidden from /help.
- /intraday_off has been removed.
- /kill, /start, /set and /suspend are visible commands.
- /set requires authentication for every invocation.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import types
from html import escape
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from mng_db import DatabaseManager
from utils import get_setting, get_settings, update_setting


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
    .parent
)

load_dotenv(
    BASE_DIR / ".env",
    override=True,
)


def _env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean infrastructure flag from .env."""

    value = os.getenv(key)

    if value is None:
        return default

    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# Always-active flags are infrastructure/runtime-startup controls kept in
# .env. They are intentionally not part of settings.json or /set.
ALWAYS_ACTIVE_INTRADAY = _env_bool(
    "ALWAYS_ACTIVE_INTRADAY",
    False,
)
ALWAYS_ACTIVE_SIP = _env_bool(
    "ALWAYS_ACTIVE_SIP",
    False,
)
ALWAYS_ACTIVE_IPO = _env_bool(
    "ALWAYS_ACTIVE_IPO",
    False,
)

logger = logging.getLogger(
    "ASTRA_TELEGRAM"
)

db = DatabaseManager()

SMART_CLIENT = None
INTRADAY_ENGINE = None
SIP_ENGINE = None

# ----------------------------------------------------------------------
# Runtime control state
# ----------------------------------------------------------------------

ASTRA_KILLED = False

SUSPEND_UNTIL: Optional[dt.datetime] = None

MODULE_KILLS: set[str] = set()

# Runtime-only configuration overrides.
# These never modify .env.
RUNTIME_CONFIG: dict[str, str] = {}

TELEGRAM_ACTIVE = False

CACHE_FILE = (
    BASE_DIR
    / "data"
    / "telegram_cache.json"
)


# ======================================================================
# Shared clients / providers
# ======================================================================

def set_smart_client(
    client_instance,
) -> None:
    """Register the shared Angel One client."""

    global SMART_CLIENT

    SMART_CLIENT = client_instance


def _intraday_expiry_for_session(
    session_date: str,
) -> str:
    """Return the configured intraday expiry as an ISO-8601 IST timestamp."""

    expiry_text = "15:30"

    try:
        from utils import get_setting

        expiry_text = str(
            get_setting(
                "INTRADAY.FORCE_EXIT_TIME",
                "15:30",
            )
        ).strip() or "15:30"
    except Exception:
        logger.debug(
            "Unable to read INTRADAY.FORCE_EXIT_TIME; using 15:30 IST.",
            exc_info=True,
        )

    try:
        hour_text, minute_text = expiry_text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)

        from zoneinfo import ZoneInfo

        expiry = dt.datetime(
            *dt.date.fromisoformat(str(session_date)).timetuple()[:3],
            hour,
            minute,
            tzinfo=ZoneInfo("Asia/Kolkata"),
        )

        return expiry.isoformat()

    except Exception:
        logger.warning(
            "Invalid INTRADAY.FORCE_EXIT_TIME=%r; using 15:30 IST.",
            expiry_text,
        )

        from zoneinfo import ZoneInfo

        expiry = dt.datetime(
            *dt.date.fromisoformat(str(session_date)).timetuple()[:3],
            15,
            30,
            tzinfo=ZoneInfo("Asia/Kolkata"),
        )

        return expiry.isoformat()


def _patch_intraday_session_database(
    database,
) -> None:
    """
    Keep older intraday activation code compatible with the current DB API.

    Some Smart Intraday versions call create_intraday_session() with only
    session_date, mode and started_at. The current DatabaseManager requires
    expires_at as well. Supplying the configured expiry here keeps the
    Telegram control plane compatible without weakening the database schema.
    """

    if database is None:
        return

    if getattr(
        database,
        "_astra_intraday_session_compat",
        False,
    ):
        return

    create_session = getattr(
        database,
        "create_intraday_session",
        None,
    )

    if not callable(create_session):
        return

    def compatible_create_intraday_session(
        session_date,
        mode,
        started_at,
        expires_at=None,
    ):
        if expires_at is None:
            expires_at = _intraday_expiry_for_session(
                str(session_date)
            )

        return create_session(
            session_date,
            mode,
            started_at,
            expires_at,
        )

    database.create_intraday_session = (
        types.MethodType(
            lambda _self, session_date, mode, started_at, expires_at=None:
                compatible_create_intraday_session(
                    session_date,
                    mode,
                    started_at,
                    expires_at,
                ),
            database,
        )
    )

    database._astra_intraday_session_compat = True


def set_intraday_engine_provider(
    client,
    database=None,
) -> None:
    """Register the exact same Smart Intraday singleton used by main.py."""

    global INTRADAY_ENGINE

    from intraday_bot import (
        get_smart_intraday_bot,
    )

    database = database or db

    INTRADAY_ENGINE = get_smart_intraday_bot(
        client,
        database,
    )

    logger.info(
        "Shared Smart Intraday engine registered with Telegram runtime."
    )


def set_sip_engine_provider(
    engine,
) -> None:
    """
    Register the shared SIP engine.

    main.py owns the engine lifecycle; Telegram only controls it.
    """

    global SIP_ENGINE

    SIP_ENGINE = engine


# ======================================================================
# Environment / runtime configuration
# ======================================================================

def get_env(
    key: str,
    default: str = "",
) -> str:
    """Return a runtime setting with legacy .env fallback.

    Runtime application settings live in settings.json. Critical
    infrastructure/secret values continue to come from .env.
    """

    if key in RUNTIME_CONFIG:
        return RUNTIME_CONFIG[key]

    setting_path = _ENV_TO_SETTING_PATH.get(key.upper())
    if setting_path:
        value = get_setting(setting_path, None)
        if value is not None:
            return str(value)

    return os.getenv(
        key,
        default,
    )


def _get_int(
    key: str,
    default: int,
) -> int:

    try:
        return int(
            get_env(
                key,
                str(default),
            )
        )

    except (TypeError, ValueError):
        return default


def _get_float(
    key: str,
    default: float,
) -> float:

    try:
        return float(
            get_env(
                key,
                str(default),
            )
        )

    except (TypeError, ValueError):
        return default


# ======================================================================
# Runtime state helpers
# ======================================================================

def _now_ist() -> dt.datetime:
    """Return current IST time."""

    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(
            ZoneInfo("Asia/Kolkata")
        )

    except Exception:
        return dt.datetime.now(
            dt.timezone.utc
        ).astimezone()


def is_astra_killed() -> bool:
    """Return the global ASTRA kill state."""

    return ASTRA_KILLED


def is_module_killed(
    module: str,
) -> bool:

    return module.lower().strip() in MODULE_KILLS


def is_suspended() -> bool:
    """Return whether a temporary suspension is currently active."""

    global SUSPEND_UNTIL

    if SUSPEND_UNTIL is None:
        return False

    if _now_ist() >= SUSPEND_UNTIL:
        SUSPEND_UNTIL = None
        return False

    return True


def get_suspend_until() -> Optional[dt.datetime]:
    """Return active suspension expiry, if any."""

    if not is_suspended():
        return None

    return SUSPEND_UNTIL


def _runtime_status_text() -> str:
    """Return the current global runtime state."""

    if ASTRA_KILLED:
        return "🔴 KILLED"

    if is_suspended():
        return "🟡 SUSPENDED"

    return "🟢 ACTIVE"


def _operational_blocked(
    module: Optional[str] = None,
) -> bool:
    """
    Return whether operational activity should be blocked.

    Telegram/status/telemetry commands deliberately do not use this helper.
    """

    if ASTRA_KILLED:
        return True

    if is_suspended():
        return True

    if module and is_module_killed(module):
        return True

    return False


async def _reject_operational_command(
    update,
    module: Optional[str] = None,
) -> bool:
    """Reject commands that are blocked by runtime control."""

    if not _operational_blocked(module):
        return False

    if ASTRA_KILLED:

        await update.message.reply_text(
            (
                "🔴 <b>ASTRA is killed.</b>\n\n"
                "Operational activity is currently disabled.\n"
                "Use /start to resume ASTRA."
            ),
            parse_mode="HTML",
        )

        return True

    if is_suspended():

        until = get_suspend_until()

        if until is not None:
            expiry = until.strftime(
                "%H:%M:%S IST"
            )
        else:
            expiry = "automatically"

        await update.message.reply_text(
            (
                "🟡 <b>ASTRA is temporarily suspended.</b>\n\n"
                f"Resume: <b>{expiry}</b>"
            ),
            parse_mode="HTML",
        )

        return True

    if module and is_module_killed(module):

        await update.message.reply_text(
            (
                f"🔴 <b>{escape(module.title())} is killed.</b>\n\n"
                "Use /start for global recovery or "
                f"remove the module kill before activating {escape(module)}."
            ),
            parse_mode="HTML",
        )

        return True

    return False


# ======================================================================
# Cache
# ======================================================================

def load_cache() -> dict:
    try:

        if not CACHE_FILE.exists():
            return {}

        return json.loads(
            CACHE_FILE.read_text(
                encoding="utf-8",
            )
        )

    except Exception:
        return {}


def save_cache(
    cache: dict,
) -> None:

    try:

        CACHE_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        CACHE_FILE.write_text(
            json.dumps(
                cache,
                indent=2,
            ),
            encoding="utf-8",
        )

    except Exception as exc:

        logger.warning(
            "Telegram cache save failed: %s",
            exc,
        )


# ======================================================================
# Telegram send helper
# ======================================================================

def send_telegram_message(
    message_html: str,
) -> None:

    token = get_env(
        "TELEGRAM_BOT_TOKEN"
    )

    chat_id = get_env(
        "TELEGRAM_CHAT_ID"
    )

    if not token or not chat_id:

        logger.warning(
            "Telegram credentials unavailable."
        )

        return

    try:

        import requests

        response = requests.post(
            (
                "https://api.telegram.org/"
                f"bot{token}/sendMessage"
            ),
            json={
                "chat_id": chat_id,
                "text": message_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if not response.ok:

            logger.warning(
                "Telegram send failed: %s",
                response.text,
            )

    except Exception as exc:

        logger.warning(
            "Telegram send exception: %s",
            exc,
        )


# ======================================================================
# Investment suggestion
# ======================================================================

def send_investment_suggestion(
    ticker: str,
    strategy: str,
    amount: float,
    current_price: float,
    reasoning: str,
) -> None:

    message = (
        f"<b>ASTRA SIGNAL: {escape(ticker)}</b>\n\n"
        f"• Strategy: {escape(strategy)}\n"
        f"• Price: ₹{current_price:,.2f}\n"
        f"• Allocation: ₹{amount:,.2f}\n"
        f"• Reason: {escape(reasoning)}\n\n"
        "<i>Advisory only — "
        "no order was submitted.</i>"
    )

    send_telegram_message(
        message
    )


# ======================================================================
# /help
# ======================================================================

async def cmd_help(
    update,
    context,
):

    await update.message.reply_text(
        (
            "<b>ASTRA Commands</b>\n\n"

            "<b>Core</b>\n"
            "/status — show ASTRA status\n"
            "/portfolio — show wallet\n"
            "/analyze — use normal market analysis\n"
            "/config — show configurable settings\n"
            "/cfg — alias for /config\n"

            "\n<b>Runtime Control</b>\n"
            "/kill — stop ASTRA operations\n"
            "/kill &lt;module&gt; — stop a specific module\n"
            "/start — resume ASTRA\n"
            "/suspend &lt;duration&gt; — temporarily suspend operations\n"
            "/set — privileged runtime configuration\n"

            "\n<b>Smart Intraday</b>\n"
            "/intraday — activate intraday for today\n"

            "\n<b>SIP</b>\n"
            "/SIP — activate SIP runtime\n"
            "/SIP --HELP — show SIP commands\n"

            "\n/help — show commands"
        ),
        parse_mode="HTML",
    )


# ======================================================================
# /intraday
# ======================================================================

async def cmd_intraday(
    update,
    context,
):

    if await _reject_operational_command(
        update,
        "intraday",
    ):
        return

    global INTRADAY_ENGINE

    if SMART_CLIENT is None:

        await update.message.reply_text(
            "❌ Shared Angel One client is unavailable."
        )

        return

    if not getattr(
        SMART_CLIENT,
        "is_authenticated",
        False,
    ):

        await update.message.reply_text(
            (
                "❌ Shared Angel One session "
                "is not authenticated.\n\n"
                "/intraday was not activated."
            )
        )

        return

    if INTRADAY_ENGINE is None:

        try:

            set_intraday_engine_provider(
                SMART_CLIENT,
                db,
            )

        except Exception as exc:

            logger.exception(
                "Unable to initialize Smart Intraday engine."
            )

            await update.message.reply_text(
                (
                    "❌ Failed to initialize "
                    f"Smart Intraday: {escape(str(exc))}"
                )
            )

            return

    try:

        activated = INTRADAY_ENGINE.activate()

        if activated:
            first_scan = None
            try:
                first_scan = INTRADAY_ENGINE.run_cycle()
            except Exception:
                logger.exception(
                    "Smart Intraday first scan failed after Telegram activation."
                )

            scan_status = (
                first_scan.get("status")
                if isinstance(first_scan, dict)
                else None
            )

            await update.message.reply_text(
                (
                    "🟢 <b>Smart Intraday activated.</b>\n\n"
                    "Mode: DAILY\n"
                    "Expiry: 15:30 IST\n"
                    "Runtime .env: unchanged\n\n"
                    f"First scan: <b>{escape(str(scan_status or 'TRIGGERED'))}</b>"
                ),
                parse_mode="HTML",
            )

        else:

            await update.message.reply_text(
                (
                    "❌ Smart Intraday activation failed.\n"
                    "Check the ASTRA logs."
                )
            )

    except Exception as exc:

        logger.exception(
            "Telegram /intraday failed."
        )

        await update.message.reply_text(
            (
                "❌ Intraday activation error: "
                f"{escape(str(exc))}"
            )
        )


# ======================================================================
# /kill
# ======================================================================

async def cmd_kill(
    update,
    context,
):

    global ASTRA_KILLED

    args = list(
        getattr(
            context,
            "args",
            [],
        )
        or []
    )

    if len(args) > 1:

        await update.message.reply_text(
            (
                "❌ Invalid syntax.\n\n"
                "/kill\n"
                "/kill &lt;module&gt;"
            ),
            parse_mode="HTML",
        )

        return

    if not args:

        if ASTRA_KILLED:

            await update.message.reply_text(
                "🔴 ASTRA is already killed."
            )

            return

        ASTRA_KILLED = True

        try:

            if INTRADAY_ENGINE is not None:

                stop_method = getattr(
                    INTRADAY_ENGINE,
                    "stop",
                    None,
                )

                if callable(stop_method):
                    stop_method(
                        reason="GLOBAL_KILL"
                    )

        except Exception:

            logger.exception(
                "Failed to stop Smart Intraday during global kill."
            )

        await update.message.reply_text(
            (
                "🛑 <b>ASTRA KILLED</b>\n\n"
                "Operational activity: <b>OFF</b>\n"
                "Telegram: <b>ACTIVE</b>\n"
                "Telemetry: <b>ACTIVE</b>\n"
                "Mailing: <b>ACTIVE</b>\n\n"
                "Use /start to resume ASTRA."
            ),
            parse_mode="HTML",
        )

        return

    module = args[0].strip().lower()

    allowed_modules = {
        "intraday",
        "sip",
    }

    if module not in allowed_modules:

        await update.message.reply_text(
            (
                "❌ Unknown module.\n\n"
                "Available modules:\n"
                "• intraday\n"
                "• sip"
            )
        )

        return

    MODULE_KILLS.add(
        module
    )

    if module == "intraday":

        try:

            if INTRADAY_ENGINE is not None:

                stop_method = getattr(
                    INTRADAY_ENGINE,
                    "stop",
                    None,
                )

                if callable(stop_method):
                    stop_method(
                        reason="MODULE_KILL"
                    )

                else:

                    stop_method = getattr(
                        INTRADAY_ENGINE,
                        "kill",
                        None,
                    )

                    if callable(stop_method):
                        stop_method()

        except Exception:

            logger.exception(
                "Failed to stop Smart Intraday."
            )

    elif module == "sip":

        try:

            if SIP_ENGINE is not None:

                deactivate = getattr(
                    SIP_ENGINE,
                    "deactivate",
                    None,
                )

                if callable(deactivate):
                    deactivate()

        except Exception:

            logger.exception(
                "Failed to deactivate SIP runtime."
            )

    await update.message.reply_text(
        (
            f"🔴 <b>{escape(module.title())} killed.</b>\n\n"
            "Other ASTRA services remain operational."
        ),
        parse_mode="HTML",
    )


# ======================================================================
# /start
# ======================================================================

async def cmd_start(
    update,
    context,
):

    global ASTRA_KILLED

    ASTRA_KILLED = False

    # /start is the global recovery command.
    MODULE_KILLS.clear()

    if (
        ALWAYS_ACTIVE_INTRADAY
        and SMART_CLIENT is not None
        and getattr(SMART_CLIENT, "is_authenticated", False)
        and _market_is_open()
    ):
        try:
            if INTRADAY_ENGINE is None:
                set_intraday_engine_provider(
                    SMART_CLIENT,
                    db,
                )
            INTRADAY_ENGINE.activate()
        except Exception:
            logger.exception(
                "Failed to immediately reactivate Smart Intraday during /start."
            )

    await update.message.reply_text(
        (
            "🟢 <b>ASTRA resumed.</b>\n\n"
            "Global kill state: <b>CLEARED</b>\n"
            "Module kills: <b>CLEARED</b>\n"
            "Telegram: <b>ACTIVE</b>"
        ),
        parse_mode="HTML",
    )


# ======================================================================
# /suspend
# ======================================================================

_DURATION_PATTERN = re.compile(
    r"^(?P<value>[1-9]\d*)(?P<unit>[mMhH])$"
)


def _parse_suspend_duration(
    value: str,
) -> Optional[dt.timedelta]:

    match = _DURATION_PATTERN.fullmatch(
        value.strip()
    )

    if not match:
        return None

    amount = int(
        match.group("value")
    )

    unit = match.group("unit").lower()

    if unit == "m":
        return dt.timedelta(
            minutes=amount
        )

    if unit == "h":
        return dt.timedelta(
            hours=amount
        )

    return None


async def cmd_suspend(
    update,
    context,
):

    global SUSPEND_UNTIL

    args = list(
        getattr(
            context,
            "args",
            [],
        )
        or []
    )

    if len(args) != 1:

        await update.message.reply_text(
            (
                "❌ A suspension duration is required.\n\n"
                "Examples:\n"
                "/suspend 10m\n"
                "/suspend 30m\n"
                "/suspend 1h"
            )
        )

        return

    duration = _parse_suspend_duration(
        args[0]
    )

    if duration is None:

        await update.message.reply_text(
            (
                "❌ Invalid duration.\n\n"
                "Use a positive duration such as "
                "<code>10m</code>, "
                "<code>30m</code> or "
                "<code>1h</code>."
            ),
            parse_mode="HTML",
        )

        return

    SUSPEND_UNTIL = (
        _now_ist()
        + duration
    )

    await update.message.reply_text(
        (
            "🟡 <b>ASTRA suspended.</b>\n\n"
            f"Duration: <b>{escape(args[0])}</b>\n"
            f"Resume: <b>"
            f"{SUSPEND_UNTIL.strftime('%H:%M:%S IST')}"
            f"</b>\n\n"
            "Telegram, telemetry and critical alerts remain available."
        ),
        parse_mode="HTML",
    )


# ======================================================================
# /set
# ======================================================================

def _get_admin_password() -> str:
    """
    Read the privileged /set password from TELEGRAM_PASSWORD.

    The password is read-only runtime configuration. It is never
    modified by Telegram commands.
    """

    return os.getenv(
        "TELEGRAM_PASSWORD",
        "",
    )


# Canonical runtime settings exposed through /config and /set.
# The keys are intentionally mapped to the exact settings.json paths.
_CONFIGURABLE_SETTINGS = (
    "ASTRA_FUNCTIONS.ACTIVE_CYCLE_BUFFER",
    "ASTRA_FUNCTIONS.PASSIVE_CYCLE_BUFFER",
    "ASTRA_FUNCTIONS.NOTIFICATION_REPEAT_BUFFER",
    "ASTRA_FUNCTIONS.PASSIVE_WALLET_REFRESH_BUFFER",
    "ASTRA_FUNCTIONS.KILL",
    "NORMAL_TRADING.TICKERS_COUNT",
    "NORMAL_TRADING.MIN_TRADE_ALLOCATION",
    "NORMAL_TRADING.MAX_TRADE_ALLOCATION",
    "NORMAL_TRADING.PORTFOLIO_ALLOCATION_PCT",
    "NORMAL_TRADING.RSI_LOWER_THRESHOLD",
    "NORMAL_TRADING.RSI_UPPER_THRESHOLD",
    "NORMAL_TRADING.ENABLE_TRAILING_STOP",
    "NORMAL_TRADING.STOP_LOSS_PCT",
    "NORMAL_TRADING.TRAILING_STOP_PCT",
    "INTRADAY.TRADING_ENGINE",
    "INTRADAY.LIVE_TRADING",
    "INTRADAY.TICKERS_COUNT",
    "INTRADAY.SCAN_INTERVAL_SECONDS",
    "INTRADAY.RSI_LOWER_THRESHOLD",
    "INTRADAY.RSI_UPPER_THRESHOLD",
    "INTRADAY.MIN_ALLOCATION",
    "INTRADAY.MAX_ALLOCATION",
    "INTRADAY.MAX_POSITIONS",
    "INTRADAY.MAX_DAILY_LOSS",
    "INTRADAY.STOP_LOSS_PCT",
    "INTRADAY.TRAILING_STOP_PCT",
    "INTRADAY.FORCE_EXIT_TIME",
    "mailer.TIME",
)

# Legacy environment names still used by older runtime code are resolved
# against settings.json first. Secrets and infrastructure variables are not
# included here and therefore remain .env-only.
_ENV_TO_SETTING_PATH = {
    "ASTRA_CYCLE_BUFFER": "ASTRA_FUNCTIONS.ACTIVE_CYCLE_BUFFER",
    "ACTIVE_CYCLE_BUFFER": "ASTRA_FUNCTIONS.ACTIVE_CYCLE_BUFFER",
    "PASSIVE_CYCLE_BUFFER": "ASTRA_FUNCTIONS.PASSIVE_CYCLE_BUFFER",
    "NOTIFICATION_REPEAT_BUFFER": "ASTRA_FUNCTIONS.NOTIFICATION_REPEAT_BUFFER",
    "PASSIVE_WALLET_REFRESH_BUFFER": "ASTRA_FUNCTIONS.PASSIVE_WALLET_REFRESH_BUFFER",
    "TICKERS_COUNT": "NORMAL_TRADING.TICKERS_COUNT",
    "MIN_TRADE_ALLOCATION": "NORMAL_TRADING.MIN_TRADE_ALLOCATION",
    "MAX_TRADE_ALLOCATION": "NORMAL_TRADING.MAX_TRADE_ALLOCATION",
    "PORTFOLIO_ALLOCATION_PCT": "NORMAL_TRADING.PORTFOLIO_ALLOCATION_PCT",
    "RSI_LOWER_THRESHOLD": "NORMAL_TRADING.RSI_LOWER_THRESHOLD",
    "RSI_UPPER_THRESHOLD": "NORMAL_TRADING.RSI_UPPER_THRESHOLD",
    "ENABLE_TRAILING_STOP": "NORMAL_TRADING.ENABLE_TRAILING_STOP",
    "STOP_LOSS_PCT": "NORMAL_TRADING.STOP_LOSS_PCT",
    "TRAILING_STOP_PCT": "NORMAL_TRADING.TRAILING_STOP_PCT",
    "INTRADAY_TRADING_ENGINE": "INTRADAY.TRADING_ENGINE",
    "ASTRA_INTRADAY_LIVE_TRADING": "INTRADAY.LIVE_TRADING",
    "INTRADAY_TICKERS_COUNT": "INTRADAY.TICKERS_COUNT",
    "INTRADAY_SCAN_INTERVAL_SECONDS": "INTRADAY.SCAN_INTERVAL_SECONDS",
    "INTRADAY_RSI_LOWER_THRESHOLD": "INTRADAY.RSI_LOWER_THRESHOLD",
    "INTRADAY_RSI_UPPER_THRESHOLD": "INTRADAY.RSI_UPPER_THRESHOLD",
    "INTRADAY_MIN_ALLOCATION": "INTRADAY.MIN_ALLOCATION",
    "INTRADAY_MAX_ALLOCATION": "INTRADAY.MAX_ALLOCATION",
    "INTRADAY_MAX_POSITIONS": "INTRADAY.MAX_POSITIONS",
    "INTRADAY_MAX_DAILY_LOSS": "INTRADAY.MAX_DAILY_LOSS",
    "INTRADAY_STOP_LOSS_PCT": "INTRADAY.STOP_LOSS_PCT",
    "INTRADAY_TRAILING_STOP_PCT": "INTRADAY.TRAILING_STOP_PCT",
    "INTRADAY_FORCE_EXIT_TIME": "INTRADAY.FORCE_EXIT_TIME",
}

_SETTING_PATH_LOOKUP = {path.lower(): path for path in _CONFIGURABLE_SETTINGS}


def _canonical_setting_path(value: str) -> Optional[str]:
    """Resolve a case-insensitive dotted settings path."""
    return _SETTING_PATH_LOOKUP.get(value.strip().lower())


def _coerce_setting_value(path: str, raw_value: str) -> Any:
    """Convert Telegram text into the type already used by settings.json."""
    raw = raw_value.strip()
    current = get_setting(path, None)

    if isinstance(current, bool):
        lowered = raw.lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        raise ValueError("expected true or false")

    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)

    if isinstance(current, float):
        return float(raw)

    return raw


def _set_value_is_valid(path: str, value: str) -> bool:
    return bool(path and value.strip())


async def cmd_set(
    update,
    context,
):

    args = list(getattr(context, "args", []) or [])

    if len(args) < 3:
        await update.message.reply_text(
            "❌ Invalid syntax.\n\n"
            "/set &lt;password&gt; &lt;path&gt; &lt;value&gt;",
            parse_mode="HTML",
        )
        return

    password = args[0]
    requested_path = args[1].strip()
    raw_value = " ".join(args[2:]).strip()

    configured_password = _get_admin_password()
    if (
        not configured_password
        or not password
        or not __import__("secrets").compare_digest(password, configured_password)
    ):
        logger.warning("Rejected unauthenticated /set request.")
        await update.message.reply_text("❌ Authentication failed.")
        return

    path = _canonical_setting_path(requested_path)
    if not path or not _set_value_is_valid(path, raw_value):
        await update.message.reply_text(
            "❌ Unsupported runtime setting.\n\n"
            "Use a permitted settings.json path such as "
            "<code>mailer.time</code>.",
            parse_mode="HTML",
        )
        return

    try:
        value = _coerce_setting_value(path, raw_value)
        update_setting(path, value, persist=True)
    except (TypeError, ValueError) as exc:
        await update.message.reply_text(
            f"❌ Invalid value for <code>{escape(path)}</code>: "
            f"{escape(str(exc))}",
            parse_mode="HTML",
        )
        return
    except Exception:
        logger.exception("Failed to persist runtime setting: %s", path)
        await update.message.reply_text(
            "❌ Failed to persist the runtime setting.",
        )
        return

    logger.info("Runtime configuration updated: %s", path)
    await update.message.reply_text(
        "🟢 <b>Runtime configuration updated.</b>\n\n"
        f"Setting: <code>{escape(path)}</code>\n"
        f"Value: <code>{escape(str(value))}</code>\n\n"
        "<code>settings.json</code> was updated.",
        parse_mode="HTML",
    )


# ======================================================================
# /status
# ======================================================================

def _market_is_open() -> bool:

    now = _now_ist()

    if now.weekday() >= 5:
        return False

    market_open = dt.time(
        9,
        15,
    )

    market_close = dt.time(
        15,
        30,
    )

    return (
        market_open
        <= now.time()
        < market_close
    )


def _get_intraday_status() -> tuple[str, str]:
    """Return Smart Intraday status from the shared in-memory engine.

    The old implementation queried the database with the wrong signature and
    could therefore report OFF even immediately after /intraday activated the
    engine. The shared singleton is now authoritative for live process state;
    the DB is used only as a recovery/telemetry fallback.
    """

    if is_module_killed("intraday"):
        return (
            "🔴 KILLED",
            "Module killed.",
        )

    if INTRADAY_ENGINE is not None:
        try:
            status_method = getattr(
                INTRADAY_ENGINE,
                "status",
                None,
            )
            if callable(status_method):
                snapshot = status_method()
                if isinstance(snapshot, dict):
                    active = bool(snapshot.get("active", False))
                    session_date = snapshot.get("session_date")
                    execution_mode = snapshot.get(
                        "execution_mode",
                        "ADVISORY",
                    )

                    if active:
                        details = (
                            f"Mode: DAILY\n"
                            f"Date: {escape(str(session_date or 'unknown'))}\n"
                            f"Open positions: {int(snapshot.get('open_positions', 0) or 0)}\n"
                            f"Mode: {escape(str(execution_mode))}"
                        )
                        return ("🟢 ON", details)
        except Exception:
            logger.debug(
                "Unable to read shared Smart Intraday status.",
                exc_info=True,
            )

    # Recovery fallback for a process where the provider has not yet been
    # registered. DatabaseManager requires an explicit session date.
    try:
        session_date = _now_ist().date().isoformat()
        active_session = db.get_active_intraday_session(session_date)
        if active_session:
            mode = getattr(active_session, "mode", "DAILY")
            stored_date = getattr(
                active_session,
                "session_date",
                session_date,
            )
            return (
                "🟢 ON",
                (
                    f"Mode: {escape(str(mode))}\n"
                    f"Date: {escape(str(stored_date))}"
                ),
            )
    except Exception:
        logger.debug(
            "Unable to read persisted Smart Intraday session.",
            exc_info=True,
        )

    return (
        "🔴 OFF",
        "No active Smart Intraday session.",
    )


def _get_sip_status() -> dict[str, Any]:

    result = {
        "active": False,
        "target_count": 0,
        "active_target_count": 0,
        "paused_target_count": 0,
    }

    if SIP_ENGINE is None:
        return result

    try:

        status_method = getattr(
            SIP_ENGINE,
            "status",
            None,
        )

        if callable(status_method):

            snapshot = status_method()

            if isinstance(
                snapshot,
                dict,
            ):

                result.update(
                    snapshot
                )

                return result

    except Exception:

        logger.debug(
            "Unable to read SIP status snapshot.",
            exc_info=True,
        )

    return result


async def cmd_status(
    update,
    context,
):

    runtime_status = _runtime_status_text()

    market_status = (
        "🟢 OPEN"
        if _market_is_open()
        else "🔴 CLOSED"
    )

    intraday_status, intraday_details = (
        _get_intraday_status()
    )

    sip_status = _get_sip_status()

    sip_active = bool(
        sip_status.get(
            "active",
            False,
        )
    )

    sip_status_text = (
        "🟢 ACTIVE"
        if sip_active
        else "🔴 DORMANT"
    )

    target_count = int(
        sip_status.get(
            "target_count",
            0,
        )
        or 0
    )

    active_target_count = int(
        sip_status.get(
            "active_target_count",
            0,
        )
        or 0
    )

    paused_target_count = int(
        sip_status.get(
            "paused_target_count",
            0,
        )
        or 0
    )

    suspend_line = ""

    if is_suspended():

        until = get_suspend_until()

        if until:
            suspend_line = (
                f"\nResume: "
                f"{until.strftime('%H:%M:%S IST')}"
            )

    await update.message.reply_text(
        (
            "<b>ASTRA Status</b>\n"
            "\n"
            f"Status: {runtime_status}\n"
            f"Stock Market: {market_status}\n"
            "Telegram Bot: 🟢 ACTIVE"
            f"{suspend_line}\n"

            "\n<b>Smart Intraday</b>\n"
            f"Status: {intraday_status}\n"
            f"{intraday_details}\n"
            f"Always-active override: {'ON' if ALWAYS_ACTIVE_INTRADAY else 'OFF'}\n"

            "\n<b>SIP</b>\n"
            f"Status: {sip_status_text}\n"
            f"Targets: {target_count} | "
            f"Active: {active_target_count} | "
            f"Paused: {paused_target_count}\n"
            f"Always-active override: {'ON' if ALWAYS_ACTIVE_SIP else 'OFF'}\n"
            f"IPO always-active override: {'ON' if ALWAYS_ACTIVE_IPO else 'OFF'}"
        ),
        parse_mode="HTML",
    )


# ======================================================================
# /portfolio
# ======================================================================

async def cmd_portfolio(
    update,
    context,
):

    if SMART_CLIENT is None:

        await update.message.reply_text(
            "❌ Shared Angel One client unavailable."
        )

        return

    try:

        wallet = (
            SMART_CLIENT
            .get_real_portfolio_data()
        )

    except Exception as exc:

        logger.exception(
            "Telegram portfolio request failed."
        )

        await update.message.reply_text(
            (
                "❌ Wallet request failed: "
                f"{escape(str(exc))}"
            )
        )

        return

    if wallet is None:

        await update.message.reply_text(
            (
                "❌ Wallet unavailable.\n\n"
                "ASTRA did not interpret this as ₹0."
            )
        )

        return

    cash = float(
        wallet.get(
            "available_cash",
            0.0,
        )
        or 0.0
    )

    holdings = wallet.get(
        "holdings",
        [],
    ) or []

    await update.message.reply_text(
        (
            "<b>ASTRA Wallet</b>\n\n"
            f"Available cash: ₹{cash:,.2f}\n"
            f"Holdings: {len(holdings)}"
        ),
        parse_mode="HTML",
    )


# ======================================================================
# /analyze
# ======================================================================

async def cmd_analyze(
    update,
    context,
):

    if await _reject_operational_command(
        update
    ):
        return

    await update.message.reply_text(
        (
            "Use ASTRA's normal market cycle "
            "for technical analysis."
        )
    )


# ======================================================================
# /config
# ======================================================================

async def cmd_config(
    update,
    context,
):
    """Show runtime settings in the same structure as settings.json."""

    settings = get_settings()

    lines = [
        "<b>ASTRA Runtime Configuration</b>",
        "",
    ]

    sections = (
        "ASTRA_FUNCTIONS",
        "NORMAL_TRADING",
        "INTRADAY",
        "mailer",
        "SIP",
        "IPO",
    )

    for section in sections:
        lines.append(f"<b>[{escape(section)}]</b>")
        values = settings.get(section, {})
        if isinstance(values, dict):
            for key, value in values.items():
                if isinstance(value, bool):
                    rendered = "true" if value else "false"
                elif value is None:
                    rendered = "null"
                else:
                    rendered = str(value)
                lines.append(
                    f"{escape(str(key))}: {escape(rendered)}"
                )
        lines.append("")

    # Avoid an unnecessary trailing blank line in Telegram.
    while lines and lines[-1] == "":
        lines.pop()

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


# ======================================================================
# SIP
# ======================================================================

def _sip_help_text() -> str:

    return (
        "<b>SIP Commands</b>\n\n"
        "/SIP — activate SIP runtime\n"
        "/SIP --STATUS — show SIP status\n"
        "/SIP --LIST — list SIP targets\n"
        "/SIP --CANCEL &lt;TARGET_ID&gt; — cancel target\n"
        "/SIP --PAUSE &lt;TARGET_ID&gt; — pause target\n"
        "/SIP --RESUME &lt;TARGET_ID&gt; — resume target"
    )


def _sip_target_to_dict(
    target: Any,
) -> dict[str, Any]:

    if isinstance(
        target,
        dict,
    ):
        return dict(target)

    if hasattr(
        target,
        "to_dict",
    ) and callable(
        target.to_dict
    ):

        try:
            return dict(
                target.to_dict()
            )
        except Exception:
            pass

    result: dict[str, Any] = {}

    for name in (
        "target_id",
        "asset",
        "amount",
        "frequency",
        "start_at",
        "expires_at",
        "status",
        "created_at",
        "next_execution_at",
        "completed_contributions",
        "total_invested",
        "last_contribution_at",
        "last_execution_price",
        "last_execution_quantity",
        "last_order_id",
        "pnl",
        "pnl_pct",
    ):

        if hasattr(
            target,
            name,
        ):

            value = getattr(
                target,
                name,
            )

            if isinstance(
                value,
                (dt.datetime, dt.date, dt.time),
            ):
                value = value.isoformat()

            result[name] = value

    return result


def _get_sip_targets() -> list[dict[str, Any]]:

    if SIP_ENGINE is None:
        return []

    getter = getattr(
        SIP_ENGINE,
        "get_targets",
        None,
    )

    if not callable(getter):
        return []

    try:

        targets = getter(
            include_expired=True
        )

    except TypeError:

        targets = getter()

    except Exception:

        logger.exception(
            "Unable to list SIP targets."
        )

        return []

    return [
        _sip_target_to_dict(target)
        for target in (
            targets or []
        )
    ]


async def _sip_status_command(
    update,
):

    status = _get_sip_status()

    active = bool(
        status.get(
            "active",
            False,
        )
    )

    await update.message.reply_text(
        (
            "<b>SIP Status</b>\n\n"
            f"Runtime: "
            f"{'🟢 ACTIVE' if active else '🔴 DORMANT'}\n"
            f"Targets: "
            f"{status.get('target_count', 0)}\n"
            f"Active: "
            f"{status.get('active_target_count', 0)}\n"
            f"Paused: "
            f"{status.get('paused_target_count', 0)}"
        ),
        parse_mode="HTML",
    )


async def _sip_list_command(
    update,
):

    targets = _get_sip_targets()

    if not targets:

        await update.message.reply_text(
            (
                "<b>SIP Targets</b>\n\n"
                "No SIP targets found."
            ),
            parse_mode="HTML",
        )

        return

    lines = [
        "<b>SIP Targets</b>",
        "",
    ]

    for target in targets:

        target_id = str(
            target.get(
                "target_id",
                "UNKNOWN",
            )
        )

        asset = str(
            target.get(
                "asset",
                "UNKNOWN",
            )
        )

        amount = target.get(
            "amount",
            0,
        )

        frequency = str(
            target.get(
                "frequency",
                "UNKNOWN",
            )
        )

        status = str(
            target.get(
                "status",
                "UNKNOWN",
            )
        )

        lines.append(
            (
                f"• <b>{escape(target_id)}</b>\n"
                f"  Asset: {escape(asset)}\n"
                f"  Amount: ₹{float(amount):,.2f}\n"
                f"  Frequency: {escape(frequency)}\n"
                f"  Status: {escape(status)}"
            )
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


async def _sip_target_action(
    update,
    action: str,
    target_id: str,
):

    if SIP_ENGINE is None:

        await update.message.reply_text(
            "❌ Shared SIP engine unavailable."
        )

        return

    method_name = {
        "cancel": "cancel_target",
        "pause": "pause_target",
        "resume": "resume_target",
    }.get(action)

    if method_name is None:

        await update.message.reply_text(
            "❌ Unsupported SIP action."
        )

        return

    method = getattr(
        SIP_ENGINE,
        method_name,
        None,
    )

    if not callable(method):

        await update.message.reply_text(
            (
                f"❌ SIP engine does not expose "
                f"{method_name}()."
            )
        )

        return

    try:

        result = method(
            target_id
        )

        if result is False:

            await update.message.reply_text(
                (
                    f"❌ Failed to {action} "
                    f"SIP target <code>{escape(target_id)}</code>."
                ),
                parse_mode="HTML",
            )

            return

        await update.message.reply_text(
            (
                f"🟢 SIP target "
                f"<code>{escape(target_id)}</code> "
                f"{action}ed."
            ),
            parse_mode="HTML",
        )

    except Exception as exc:

        logger.exception(
            "SIP %s failed for %s.",
            action,
            target_id,
        )

        await update.message.reply_text(
            (
                f"❌ Failed to {action} SIP target: "
                f"{escape(str(exc))}"
            )
        )


async def cmd_sip(
    update,
    context,
):

    args = [
        str(arg)
        for arg in (
            getattr(
                context,
                "args",
                [],
            )
            or []
        )
    ]

    if not args:

        if await _reject_operational_command(
            update,
            "sip",
        ):
            return

        if SIP_ENGINE is None:

            await update.message.reply_text(
                "❌ Shared SIP engine unavailable."
            )

            return

        try:

            activate = getattr(
                SIP_ENGINE,
                "activate",
                None,
            )

            if not callable(activate):

                await update.message.reply_text(
                    "❌ SIP engine cannot be activated."
                )

                return

            result = activate()

            await update.message.reply_text(
                (
                    "🟢 <b>SIP runtime activated.</b>\n\n"
                    "SIP recommendations are now enabled.\n"
                    "Existing SIP monitoring remains active."
                ),
                parse_mode="HTML",
            )

        except Exception as exc:

            logger.exception(
                "SIP activation failed."
            )

            await update.message.reply_text(
                (
                    "❌ SIP activation failed: "
                    f"{escape(str(exc))}"
                )
            )

        return

    option = args[0].strip().lower()

    if option in {
        "--help",
        "-h",
        "help",
    }:

        await update.message.reply_text(
            _sip_help_text(),
            parse_mode="HTML",
        )

        return

    if option == "--status":

        await _sip_status_command(
            update
        )

        return

    if option == "--list":

        await _sip_list_command(
            update
        )

        return

    if option in {
        "--cancel",
        "--pause",
        "--resume",
    }:

        if await _reject_operational_command(
            update,
            "sip",
        ):
            return

        if len(args) != 2:

            await update.message.reply_text(
                (
                    f"❌ Target ID required.\n\n"
                    f"/SIP {args[0]} &lt;TARGET_ID&gt;"
                ),
                parse_mode="HTML",
            )

            return

        action = option[2:]

        await _sip_target_action(
            update,
            action,
            args[1],
        )

        return

    await update.message.reply_text(
        (
            "❌ Unknown SIP option.\n\n"
            "Use /SIP --HELP."
        )
    )


# ======================================================================
# Hidden /gold
# ======================================================================

async def cmd_gold(
    update,
    context,
):

    try:

        from gold import (
            fetch_gold_data,
            format_gold_message,
        )

        data = fetch_gold_data(
            SMART_CLIENT
        )

        message = format_gold_message(
            data
        )

        await update.message.reply_text(
            message,
            parse_mode="HTML",
        )

    except Exception as exc:

        logger.exception(
            "Telegram /gold failed."
        )

        await update.message.reply_text(
            (
                "❌ Precious metals telemetry failed: "
                f"{escape(str(exc))}"
            )
        )


# ======================================================================
# Unknown messages
# ======================================================================

async def handle_unknown_text(
    update,
    context,
):

    await update.message.reply_text(
        "Unknown command. Use /help."
    )


# ======================================================================
# Compatibility
# ======================================================================

def init_env_backup():
    """
    Kept for compatibility with older ASTRA code.

    Runtime commands never modify .env.
    """

    return None


# ======================================================================
# Telegram runtime
# ======================================================================

def run_telegram_bot_loop():
    """Start the python-telegram-bot polling loop."""

    global TELEGRAM_ACTIVE

    token = get_env(
        "TELEGRAM_BOT_TOKEN"
    )

    if not token:

        logger.warning(
            "TELEGRAM_BOT_TOKEN is not configured. "
            "Telegram bot disabled."
        )

        return

    try:

        from telegram.ext import (
            Application,
            CommandHandler,
            MessageHandler,
            filters,
        )

    except ImportError:

        logger.error(
            "python-telegram-bot is not installed."
        )

        return

    try:

        app = (
            Application
            .builder()
            .token(token)
            .build()
        )

        # --------------------------------------------------------------
        # Core
        # --------------------------------------------------------------

        app.add_handler(
            CommandHandler(
                "help",
                cmd_help,
            )
        )

        app.add_handler(
            CommandHandler(
                "status",
                cmd_status,
            )
        )

        app.add_handler(
            CommandHandler(
                "portfolio",
                cmd_portfolio,
            )
        )

        app.add_handler(
            CommandHandler(
                "analyze",
                cmd_analyze,
            )
        )

        app.add_handler(
            CommandHandler(
                "config",
                cmd_config,
            )
        )

        app.add_handler(
            CommandHandler(
                "cfg",
                cmd_config,
            )
        )

        # --------------------------------------------------------------
        # Smart Intraday
        # --------------------------------------------------------------

        app.add_handler(
            CommandHandler(
                "intraday",
                cmd_intraday,
            )
        )

        # --------------------------------------------------------------
        # Runtime control
        # --------------------------------------------------------------

        app.add_handler(
            CommandHandler(
                "kill",
                cmd_kill,
            )
        )

        app.add_handler(
            CommandHandler(
                "start",
                cmd_start,
            )
        )

        app.add_handler(
            CommandHandler(
                "suspend",
                cmd_suspend,
            )
        )

        app.add_handler(
            CommandHandler(
                "set",
                cmd_set,
            )
        )

        # --------------------------------------------------------------
        # SIP
        # --------------------------------------------------------------

        app.add_handler(
            CommandHandler(
                "sip",
                cmd_sip,
            )
        )

        # --------------------------------------------------------------
        # Hidden command
        # --------------------------------------------------------------

        app.add_handler(
            CommandHandler(
                "gold",
                cmd_gold,
            )
        )

        # --------------------------------------------------------------
        # Plain text
        # --------------------------------------------------------------

        app.add_handler(
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND,
                handle_unknown_text,
            )
        )

        TELEGRAM_ACTIVE = True

        logger.info(
            "Telegram polling started."
        )

        app.run_polling(
            drop_pending_updates=True,
            stop_signals=None,
        )

    except Exception:

        TELEGRAM_ACTIVE = False

        logger.exception(
            "Telegram bot stopped unexpectedly."
        )

"""
ASTRA - 24x7 Persistent Main Orchestrator

Responsibilities:
    - Load runtime configuration.
    - Run the existing ASTRA market-analysis pipeline.
    - Authenticate with Angel One and obtain wallet state.
    - Scan holdings for exit/TSL signals.
    - Scan the market for normal ASTRA recommendations.
    - Synchronize telemetry to Google Sheets.
    - Dispatch scheduled email reports.
    - Run the Telegram listener.
    - Run the autonomous intraday peripheral when activated through Telegram.
    - Keep ASTRA alive 24x7 with separate market-open/off-market workloads.
    - Continue dashboard/database telemetry while NSE is closed.

Intraday modes:
    /intraday
        Daily Telegram activation. The intraday engine expires at 15:30 IST.

The intraday peripheral is intentionally NOT exposed as a CLI mode.
Autonomous intraday trading is activated only through the Telegram
``/intraday`` command and is automatically squared off at the end of the
NSE session.
"""

from __future__ import annotations

import argparse
import logging
import os
import datetime as dt
import signal
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from typing import Any, Optional

import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env", override=True)

SRC_DIR = BASE_DIR / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from decision_engine import analyze_ticker_data, evaluate_exit_signal
from intraday_bot import get_smart_intraday_bot
from sip_engine import get_sip_engine, refresh_sip, deactivate_sip
from mailer import send_eod_email_report
from mng_db import DatabaseManager
from portfolio_fetcher import get_cost_price_from_sheet, sync_dashboard_data
from smartapi import get_smartapi_client, AngelOneClient
from telegram_bot import (
    run_telegram_bot_loop,
    send_investment_suggestion,
    set_smart_client,
    set_intraday_engine_provider,
    is_astra_killed,
    is_suspended,
    is_module_killed,
)
from tickers import get_dynamic_tickers
from utils import (
    get_setting,
    clean_ticker_symbol,
    format_duration,
    send_critical_failure_alert,
    setup_astra_logging,
)


# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

MARKET_OPEN = dt.time(
    hour=9,
    minute=15,
    tzinfo=IST,
)

MARKET_CLOSE = dt.time(
    hour=15,
    minute=30,
    tzinfo=IST,
)

EOD_STATE_FILE = BASE_DIR / "logs" / "last_eod_sent.txt"


def _env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean infrastructure flag from .env."""
    raw = os.getenv(key)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    logging.getLogger("ASTRA_MAIN").warning(
        "Invalid boolean value for %s=%r; using %s.",
        key,
        raw,
        default,
    )
    return default


ALWAYS_ACTIVE_INTRADAY = _env_bool(
    "ALWAYS_ACTIVE_INTRADAY",
    False,
)

# Wallet_and_Holdings is a fixed-width dashboard contract.  The Sheets
# synchronization layer must never receive more than these eight holding
# columns, even if Angel One returns additional fields.
WALLET_AND_HOLDINGS_COLUMNS = (
    "ticker",
    "qty",
    "avg_price",
    "ltp",
    "invested_val",
    "current_val",
    "pnl",
    "pnl_pct",
)


# ---------------------------------------------------------------------------
# MARKET-DATA SYMBOL NORMALIZATION
# ---------------------------------------------------------------------------

# Angel One / NSE instrument symbols may contain broker/security-series
# suffixes such as ``-EQ``. Yahoo Finance does not use those suffixes; for
# NSE equities it expects ``SYMBOL.NS``. Keep this conversion in main.py at
# the market-data boundary so broker symbols can remain untouched internally.
_BROKER_EQUITY_SUFFIXES = (
    "-EQ",
    "-BE",
    "-BL",
    "-BZ",
    "-SM",
    "-ST",
)


def to_yahoo_ticker(symbol: Any) -> str:
    """Convert an ASTRA/broker ticker into a Yahoo Finance ticker.

    Examples:
        WIPRO-EQ       -> WIPRO.NS
        WIPRO-EQ.NS    -> WIPRO.NS
        WIPRO.NS       -> WIPRO.NS
        WIPRO          -> WIPRO.NS

    Existing BSE ``.BO`` symbols are preserved. This function is deliberately
    limited to symbol normalization; it does not claim that a symbol exists
    at the data provider.
    """
    value = str(symbol or "").strip().upper().lstrip("$")

    if not value:
        return ""

    # Normalize accidental repeated whitespace and Yahoo exchange suffixes.
    value = value.replace(" ", "")

    # Preserve explicit Yahoo exchange suffixes. Before preserving, remove
    # broker security-series suffixes from the symbol portion.
    for exchange_suffix in (".NS", ".BO"):
        if value.endswith(exchange_suffix):
            base = value[: -len(exchange_suffix)]
            for broker_suffix in _BROKER_EQUITY_SUFFIXES:
                if base.endswith(broker_suffix):
                    base = base[: -len(broker_suffix)]
                    break
            return f"{base}{exchange_suffix}"

    # Strip a broker suffix before assigning the default NSE market.
    for broker_suffix in _BROKER_EQUITY_SUFFIXES:
        if value.endswith(broker_suffix):
            value = value[: -len(broker_suffix)]
            break

    return f"{value}.NS"


def normalize_yahoo_ticker_list(symbols: list[Any]) -> list[str]:
    """Normalize and de-duplicate a ticker universe for Yahoo Finance."""
    normalized: list[str] = []
    seen: set[str] = set()

    for symbol in symbols:
        ticker = to_yahoo_ticker(symbol)
        if ticker and ticker not in seen:
            normalized.append(ticker)
            seen.add(ticker)

    return normalized




# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logger = setup_astra_logging()


# Keep the interactive CLI output compatible with the original ASTRA console
# format while adding lightweight semantic color coding. File logging remains
# completely unchanged and never receives ANSI escape sequences.
class _ASTRAColorFormatter(logging.Formatter):
    """Format ASTRA console logs using the original layout with ANSI colors."""

    RESET = "\033[0m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    BLUE = "\033[34m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"

    LEVEL_COLORS = {
        "DEBUG": CYAN,
        "INFO": GREEN,
        "WARNING": YELLOW,
        "ERROR": RED,
        "CRITICAL": BRIGHT_RED,
    }

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, self.datefmt)
        level = record.levelname
        logger_name = record.name
        message = record.getMessage()

        level_color = self.LEVEL_COLORS.get(level, self.RESET)

        # Keep the familiar ASTRA logger namespace visually distinct.
        if logger_name == "ASTRA":
            logger_color = self.BLUE
        elif logger_name.startswith("ASTRA."):
            logger_color = self.MAGENTA
        else:
            logger_color = self.RESET

        # A few high-value lifecycle messages get an additional semantic
        # highlight without changing the actual log level.
        if "Successfully authenticated" in message:
            message_color = self.BRIGHT_GREEN
        elif "Market is CLOSED" in message:
            message_color = self.BRIGHT_YELLOW
        elif "CRITICAL" in message or "failed" in message.lower():
            message_color = self.BRIGHT_RED
        else:
            message_color = self.RESET

        return (
            f"{self.DIM}[{timestamp}]{self.RESET} - "
            f"{level_color}[{level}]{self.RESET} - "
            f"{logger_color}[{logger_name}]{self.RESET} - "
            f"{message_color}{message}{self.RESET}"
        )


def _restore_cli_log_format() -> None:
    """Restore ASTRA's compact console format with semantic colors."""
    formatter = _ASTRAColorFormatter(
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            handler.setFormatter(formatter)


_restore_cli_log_format()


# ---------------------------------------------------------------------------
# GLOBAL STATE
# ---------------------------------------------------------------------------

db = DatabaseManager()
angel_client = get_smartapi_client()
sip_engine = get_sip_engine()
last_good_portfolio: dict[str, Any] = {}
last_wallet_refresh_monotonic = 0.0
last_wallet_state = "UNAVAILABLE"

shutdown_event = threading.Event()
telegram_thread: Optional[threading.Thread] = None
sip_thread: Optional[threading.Thread] = None


# ---------------------------------------------------------------------------
# TIME / MARKET STATE
# ---------------------------------------------------------------------------

def now_ist() -> dt.datetime:
    """Return the current timezone-aware IST datetime."""
    return dt.datetime.now(IST)


def today_ist() -> dt.date:
    return now_ist().date()


def is_market_open() -> bool:
    """
    Return whether the NSE regular session is currently open.

    This deliberately uses explicit IST instead of the host machine's local
    timezone.
    """
    now = now_ist()

    if now.weekday() >= 5:
        return False

    return MARKET_OPEN <= now.timetz() <= MARKET_CLOSE


# ---------------------------------------------------------------------------
# EOD MAIL STATE
# ---------------------------------------------------------------------------

def mark_eod_mail_sent() -> None:
    """Persist the date on which the scheduled report was sent."""
    today_str = today_ist().isoformat()

    try:
        EOD_STATE_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        EOD_STATE_FILE.write_text(
            today_str,
            encoding="utf-8",
        )

    except OSError as exc:
        logger.error(
            "Failed to record EOD email state: %s",
            exc,
        )


def _last_eod_mail_date() -> Optional[str]:
    if not EOD_STATE_FILE.exists():
        return None

    try:
        return EOD_STATE_FILE.read_text(
            encoding="utf-8"
        ).strip()

    except OSError as exc:
        logger.warning(
            "Could not read EOD state file: %s",
            exc,
        )
        return None


def should_trigger_mail(mailer_mode: str) -> bool:
    """
    Determine whether the configured email report should run now.

    Supported modes:
        NOW - immediate one-shot report
        EOD - every trading day after market close
        EOW - Friday after market close
        EOM - final trading-day calendar boundary after market close
    """
    mode = str(mailer_mode).upper()
    now = now_ist()

    if mode == "NOW":
        return True

    if now.weekday() >= 5:
        logger.info(
            "Weekend detected. Suppressing scheduled EOD email report."
        )
        return False

    if now.timetz() < MARKET_CLOSE:
        return False

    if _last_eod_mail_date() == now.date().isoformat():
        logger.info(
            "EOD email report already dispatched for today. Skipping."
        )
        return False

    if mode == "EOD":
        return True

    if mode == "EOW":
        return now.weekday() == 4

    if mode == "EOM":
        tomorrow = now.date() + dt.timedelta(days=1)
        return tomorrow.month != now.month

    return False


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

def _setting_float(path: str, default: float) -> float:
    try:
        return float(get_setting(path, default))
    except (TypeError, ValueError):
        return default


def _setting_int(path: str, default: int) -> int:
    try:
        return int(get_setting(path, default))
    except (TypeError, ValueError):
        return default


def _setting_bool(path: str, default: bool) -> bool:
    value = get_setting(path, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_str(path: str, default: str) -> str:
    value = get_setting(path, default)
    return default if value is None else str(value)


def load_runtime_config(force_email_now: bool = False) -> dict[str, Any]:
    """Load and normalize all runtime configuration consumed by main.py."""
    normal_min = _setting_float("NORMAL_TRADING.MIN_TRADE_ALLOCATION", 100.0)
    normal_max = _setting_float("NORMAL_TRADING.MAX_TRADE_ALLOCATION", 500.0)
    normal_pct = _setting_float("NORMAL_TRADING.PORTFOLIO_ALLOCATION_PCT", 0.10)
    normal_rsi_lower = _setting_float("NORMAL_TRADING.RSI_LOWER_THRESHOLD", 35.0)
    normal_rsi_upper = _setting_float("NORMAL_TRADING.RSI_UPPER_THRESHOLD", 65.0)
    normal_stop = _setting_float("NORMAL_TRADING.STOP_LOSS_PCT", 0.05)
    normal_trailing = _setting_float("NORMAL_TRADING.TRAILING_STOP_PCT", 0.04)
    normal_tsl = _setting_bool("NORMAL_TRADING.ENABLE_TRAILING_STOP", True)

    return {
        "min_alloc": normal_min,
        "max_alloc": normal_max,
        "alloc_pct": normal_pct,
        "rsi_lower": normal_rsi_lower,
        "rsi_upper": normal_rsi_upper,
        "stop_loss_pct": normal_stop,
        "trailing_stop_pct": normal_trailing,
        "enable_tsl": normal_tsl,
        "tickers_count": max(1, _setting_int("NORMAL_TRADING.TICKERS_COUNT", 50)),
        "mailer_mode": "NOW" if force_email_now else _setting_str("mailer.TIME", "EOD").upper(),
        # Google credentials are infrastructure/secrets and remain in .env.
        "spreadsheet_id": _setting_str("GOOGLE_SHEETS.SPREADSHEET_ID", ""),
        "credentials_file": BASE_DIR / _setting_str(
            "GOOGLE_SHEETS.SERVICE_ACCOUNT_FILE", "service_account.json"
        ),
        "active_cycle_minutes": max(1, _setting_int("ASTRA_FUNCTIONS.ACTIVE_CYCLE_BUFFER", 5)),
        "passive_cycle_minutes": max(1, _setting_int("ASTRA_FUNCTIONS.PASSIVE_CYCLE_BUFFER", 15)),
        "offmarket_wallet_refresh_minutes": max(1, _setting_int("ASTRA_FUNCTIONS.PASSIVE_WALLET_REFRESH_BUFFER", 30)),
        "intraday_expiry_grace_seconds": max(5, _setting_int("INTRADAY.EXPIRY_GRACE_SECONDS", 30)),
        "sip_runtime_interval_seconds": max(15, _setting_int("SIP.RUNTIME_INTERVAL_SECONDS", 60)),
    }


# ---------------------------------------------------------------------------
# CAPITAL / BUY SIGNAL
# ---------------------------------------------------------------------------

def calculate_dynamic_allocation(
    available_cash: float,
    alloc_pct: float,
    min_alloc: float,
    max_alloc: float,
) -> float:
    """Calculate a bounded capital allocation."""
    if available_cash <= 0:
        return 0.0

    calculated = available_cash * alloc_pct

    allocation = max(
        min_alloc,
        min(calculated, max_alloc),
    )

    return min(
        allocation,
        available_cash,
    )


def evaluate_buy_signal(
    metrics: dict[str, Any],
    available_cash: float,
    min_alloc: float,
    max_alloc: float,
    alloc_pct: float,
    rsi_lower: float,
):
    """
    Evaluate the existing normal BUY logic.

    This remains separate from the new intraday engine. The normal ASTRA
    strategy therefore does not silently inherit intraday-only rules.
    """
    price = float(
        metrics.get("price", 0.0)
    )

    signals = metrics.get(
        "signals",
        [],
    )

    rsi = float(
        metrics.get("rsi", 50.0)
    )

    if price <= 0:
        return None, 0.0, ""

    if price > max_alloc or price < min_alloc:
        return None, 0.0, ""

    suggested_capital = calculate_dynamic_allocation(
        available_cash,
        alloc_pct,
        min_alloc,
        max_alloc,
    )

    if suggested_capital < price:
        return None, 0.0, ""

    if available_cash < price:
        return None, 0.0, ""

    if (
        rsi <= rsi_lower
        and "MACD_BULLISH_CROSS" in signals
    ):
        return (
            "BUY (INTRA-DAY / SHORT-TERM)",
            suggested_capital,
            (
                f"Oversold bounce (RSI {rsi:.1f}) + "
                "MACD Bullish Crossover. "
                "Capital verified."
            ),
        )

    if rsi <= rsi_lower:
        return (
            "BUY (LONG-TERM SIP)",
            suggested_capital,
            (
                f"Value entry zone "
                f"(RSI {rsi:.1f} <= {rsi_lower}). "
                "Capital verified."
            ),
        )

    if (
        rsi < 45
        and "MACD_BULLISH_CROSS" in signals
    ):
        return (
            "BUY (SHORT-TERM)",
            suggested_capital,
            (
                f"Bullish momentum crossover at "
                f"RSI {rsi:.1f}. Capital verified."
            ),
        )

    return None, 0.0, ""


# ---------------------------------------------------------------------------
# SELL / EXIT SIGNAL
# ---------------------------------------------------------------------------

def evaluate_sell_signal(
    holding: dict[str, Any],
    metrics: dict[str, Any],
    rsi_upper: float,
    credentials_file: Path,
    spreadsheet_id: str,
    stop_loss_pct: float = 0.05,
    trailing_stop_pct: float = 0.04,
    enable_tsl: bool = True,
):
    """
    Evaluate an existing holding using the existing exit/TSL engine.

    Highest-price persistence remains optional for backward compatibility
    with databases created by older ASTRA versions.
    """
    ticker = holding.get(
        "ticker",
        "",
    )

    current_price = float(
        metrics.get(
            "price",
            holding.get("current_price", 0.0),
        )
    )

    rsi = float(
        metrics.get("rsi", 50.0)
    )

    signals = metrics.get(
        "signals",
        [],
    )

    cost_price = 0.0

    if (
        credentials_file.exists()
        and spreadsheet_id
        and ticker
    ):
        try:
            cost_price = get_cost_price_from_sheet(
                credentials_path=str(credentials_file),
                spreadsheet_id=spreadsheet_id,
                ticker=ticker,
                tab_name="Wallet_and_Holdings",
            )
        except Exception as exc:
            logger.warning(
                "Failed to retrieve sheet cost price for %s: %s",
                ticker,
                exc,
            )

    if cost_price <= 0:
        cost_price = float(
            holding.get(
                "avg_price",
                current_price,
            )
        )

    prev_peak = cost_price

    # Optional legacy/new peak persistence.
    get_peak = getattr(
        db,
        "get_highest_price",
        None,
    )

    if callable(get_peak):
        try:
            prev_peak = float(
                get_peak(ticker)
                or cost_price
            )
        except Exception as exc:
            logger.warning(
                "Failed to load peak price for %s: %s",
                ticker,
                exc,
            )

    current_peak = max(
        prev_peak,
        current_price,
        cost_price,
    )

    update_peak = getattr(
        db,
        "update_highest_price",
        None,
    )

    if (
        current_peak > prev_peak
        and callable(update_peak)
    ):
        try:
            update_peak(
                ticker,
                current_peak,
            )

            logger.info(
                "📈 Updated highest peak price for %s: ₹%.2f",
                ticker,
                current_peak,
            )

        except Exception as exc:
            logger.error(
                "Failed to update peak price for %s: %s",
                ticker,
                exc,
            )

    macd_bearish = (
        "MACD_BEARISH_CROSS" in signals
        or "RSI_OVERBOUGHT" in signals
        or rsi >= rsi_upper
    )

    should_sell, reason, _stop_floor = evaluate_exit_signal(
        current_price=current_price,
        cost_price=cost_price,
        highest_price=current_peak,
        stop_loss_pct=stop_loss_pct,
        trailing_stop_pct=trailing_stop_pct,
        enable_tsl=enable_tsl,
        rsi_val=rsi,
        macd_bearish=macd_bearish,
    )

    if should_sell:
        pnl_pct = (
            (
                current_price - cost_price
            )
            / cost_price
            * 100
            if cost_price > 0
            else 0.0
        )

        full_reasoning = (
            f"{reason} | "
            f"Cost: ₹{cost_price:.2f}, "
            f"Price: ₹{current_price:.2f}, "
            f"Peak: ₹{current_peak:.2f}, "
            f"P&L: {pnl_pct:+.2f}%"
        )

        return (
            "SELL (EXIT / TAKE PROFIT)",
            full_reasoning,
        )

    return None, ""


# ---------------------------------------------------------------------------
# YFINANCE DATA HELPERS
# ---------------------------------------------------------------------------

def extract_ticker_df(
    data,
    ticker: str,
    is_multi: bool,
):
    """Extract one ticker's OHLCV dataframe from yfinance output."""
    try:
        if data is None or data.empty:
            return None

        if not is_multi:
            df = data.dropna()

            if "Close" in df.columns:
                return df

            return None

        # yfinance may produce MultiIndex columns with either:
        #     ticker -> OHLCV
        # or another level ordering depending on version/configuration.
        if hasattr(data.columns, "levels"):

            level0 = data.columns.get_level_values(0)

            if ticker in level0:
                df = data[ticker].dropna()

                if "Close" in df.columns:
                    return df

            level1 = data.columns.get_level_values(1)

            if ticker in level1:
                df = data.xs(
                    ticker,
                    axis=1,
                    level=1,
                ).dropna()

                if "Close" in df.columns:
                    return df

        return None

    except Exception:
        logger.debug(
            "Unable to extract dataframe for %s",
            ticker,
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# PORTFOLIO
# ---------------------------------------------------------------------------

def _portfolio_snapshot_is_valid(portfolio: Any) -> bool:
    """Return True only when a portfolio contains an explicit wallet value.

    An empty dictionary is *not* a zero-cash wallet.  It means that ASTRA does
    not currently know the broker state and therefore cannot safely make
    capital-dependent decisions.
    """
    if not isinstance(portfolio, dict):
        return False

    if "available_cash" not in portfolio:
        return False

    try:
        float(portfolio.get("available_cash"))
    except (TypeError, ValueError):
        return False

    holdings = portfolio.get("holdings")
    return isinstance(holdings, list)


def fetch_real_portfolio(
    force_refresh: bool = False,
) -> tuple[dict[str, Any], Optional[AngelOneClient], bool, bool]:
    """Retrieve wallet state without ever converting an unknown wallet to ₹0.

    Returns:
        (portfolio, shared_client, wallet_valid, wallet_fresh)

    ``wallet_valid`` means ASTRA has a structurally valid wallet snapshot.
    ``wallet_fresh`` means that snapshot was obtained from the broker during
    this call.  A cached snapshot is deliberately marked stale so callers can
    choose whether capital-dependent strategy decisions are permitted.
    """
    global last_good_portfolio, last_wallet_refresh_monotonic, last_wallet_state

    now_mono = time.monotonic()
    market_active = is_market_open()
    offmarket_refresh_seconds = max(
        1,
        _setting_int(
            "ASTRA_FUNCTIONS.PASSIVE_WALLET_REFRESH_BUFFER",
            30,
        ),
    ) * 60

    if (
        not force_refresh
        and not market_active
        and _portfolio_snapshot_is_valid(last_good_portfolio)
        and now_mono - last_wallet_refresh_monotonic < offmarket_refresh_seconds
    ):
        last_wallet_state = "CACHED"
        logger.info("Using cached wallet snapshot during off-market window.")
        return last_good_portfolio, angel_client, True, False

    try:
        set_smart_client(angel_client)
        portfolio = angel_client.get_real_portfolio_data(
            force_refresh=force_refresh
        )

        if _portfolio_snapshot_is_valid(portfolio):
            last_good_portfolio = portfolio
            last_wallet_refresh_monotonic = now_mono
            last_wallet_state = "FRESH"
            return portfolio, angel_client, True, True

        if _portfolio_snapshot_is_valid(last_good_portfolio):
            last_wallet_state = "STALE"
            logger.warning(
                "Wallet refresh returned no valid snapshot; preserving the "
                "last-known-good wallet state. Capital-dependent strategy "
                "will remain disabled for this cycle."
            )
            return last_good_portfolio, angel_client, True, False

        last_wallet_state = "UNAVAILABLE"
        logger.warning(
            "Angel One wallet snapshot unavailable; no safe cached state "
            "exists. Capital-dependent strategy is disabled for this cycle."
        )
        return {}, angel_client, False, False

    except Exception as exc:
        last_wallet_state = (
            "STALE"
            if _portfolio_snapshot_is_valid(last_good_portfolio)
            else "UNAVAILABLE"
        )

        logger.error(
            "Angel One wallet request failed (%s). Wallet state=%s.",
            type(exc).__name__,
            last_wallet_state,
        )

        if _portfolio_snapshot_is_valid(last_good_portfolio):
            logger.warning(
                "Preserving last-known-good wallet snapshot; "
                "capital-dependent strategy is disabled for this cycle."
            )
            return last_good_portfolio, angel_client, True, False

        try:
            send_critical_failure_alert(
                "Angel One wallet request failed; no valid cached wallet "
                "state is available. ASTRA skipped capital-dependent strategy."
            )
        except Exception:
            logger.exception("Failed to dispatch critical-failure alert.")

        return {}, angel_client, False, False


def portfolio_totals(
    holdings: list[dict[str, Any]],
) -> tuple[float, float, float]:
    """Calculate invested value, current value and P&L."""
    invested = sum(
        float(h.get("invested_val", 0.0) or 0.0)
        for h in holdings
    )

    current = sum(
        float(h.get("current_val", 0.0) or 0.0)
        for h in holdings
    )

    pnl = sum(
        float(h.get("pnl", 0.0) or 0.0)
        for h in holdings
    )

    return invested, current, pnl


# ---------------------------------------------------------------------------
# HOLDING SCAN
# ---------------------------------------------------------------------------

def scan_existing_holdings(
    holdings: list[dict[str, Any]],
    config: dict[str, Any],
    emit_alerts: bool = True,
):
    """Evaluate current holdings for SELL/TSL signals."""
    if not holdings:
        return

    logger.info(
        "Scanning %d active Angel One holdings "
        "for dynamic TSL and SELL triggers...",
        len(holdings),
    )

    holding_tickers = normalize_yahoo_ticker_list(
        [
            h.get("ticker", "")
            for h in holdings
            if h.get("ticker")
        ]
    )

    if not holding_tickers:
        return

    try:
        h_data = yf.download(
            holding_tickers,
            period="6mo",
            group_by="ticker",
            progress=False,
            auto_adjust=False,
            threads=True,
        )

        is_multi = len(holding_tickers) > 1

        for holding in holdings:

            ticker_raw = holding.get(
                "ticker",
                "",
            )

            ticker = to_yahoo_ticker(ticker_raw)

            if not ticker:
                continue

            try:
                df = extract_ticker_df(
                    h_data,
                    ticker,
                    is_multi,
                )

                if df is None or df.empty:
                    continue

                metrics = analyze_ticker_data(
                    df,
                    ticker=ticker_raw,
                )

                if not metrics:
                    continue

                sell_action, sell_reasoning = (
                    evaluate_sell_signal(
                        holding=holding,
                        metrics=metrics,
                        rsi_upper=config["rsi_upper"],
                        credentials_file=config["credentials_file"],
                        spreadsheet_id=config["spreadsheet_id"],
                        stop_loss_pct=config["stop_loss_pct"],
                        trailing_stop_pct=config[
                            "trailing_stop_pct"
                        ],
                        enable_tsl=config["enable_tsl"],
                    )
                )

                if not sell_action:
                    continue

                logger.info("SELL Signal [%s]: %s", ticker_raw, sell_action)

                if emit_alerts:
                    send_investment_suggestion(
                        ticker=ticker_raw,
                        strategy=sell_action,
                        amount=(
                            float(holding.get("qty", 0) or 0)
                            * float(metrics.get("price", 0.0) or 0.0)
                        ),
                        current_price=float(metrics.get("price", 0.0)),
                        reasoning=sell_reasoning,
                    )
                else:
                    logger.info(
                        "Off-market exit condition detected for %s; dashboard/state only, alert suppressed.",
                        ticker_raw,
                    )

            except Exception as exc:
                logger.error(
                    "Error processing holding %s: %s",
                    ticker_raw,
                    exc,
                )

    except Exception as exc:
        logger.error(
            "Error evaluating holdings batch: %s",
            exc,
        )


# ---------------------------------------------------------------------------
# NORMAL MARKET SCAN
# ---------------------------------------------------------------------------

def scan_market(
    available_cash: Optional[float],
    config: dict[str, Any],
    market_active: bool = True,
    emit_signals: bool = True,
    strategy_enabled: bool = True,
) -> tuple[dict[str, dict[str, Any]], list[list[Any]], list[dict[str, Any]]]:
    """
    Run the existing normal ASTRA market scan.

    Returns:
        scan_results, processed_rows, raw_data
    """
    scan_results: dict[str, dict[str, Any]] = {}

    processed_rows = [
        [
            "Ticker",
            "Price",
            "RSI",
            "MACD",
            "Signals",
        ]
    ]

    raw_data: list[dict[str, Any]] = []

    try:
        tickers = get_dynamic_tickers(
            limit=config["tickers_count"]
        )

    except Exception as exc:
        logger.warning(
            "Limited ticker retrieval failed: %s. "
            "Falling back to default universe.",
            exc,
        )

        try:
            tickers = get_dynamic_tickers()
        except Exception:
            logger.exception(
                "Failed to obtain market ticker universe."
            )
            return (
                scan_results,
                processed_rows,
                raw_data,
            )

    if not tickers:
        logger.warning(
            "Ticker universe is empty."
        )

        return (
            scan_results,
            processed_rows,
            raw_data,
        )

    # Convert broker/NSE instrument symbols at the Yahoo boundary. This is
    # what prevents symbols such as WIPRO-EQ.NS from being sent to yfinance.
    tickers = normalize_yahoo_ticker_list(tickers)

    if not tickers:
        logger.warning("Ticker universe became empty after Yahoo symbol normalization.")
        return (scan_results, processed_rows, raw_data)

    logger.info(
        "\033[1;38;2;0;255;255mMarket data phase\033[0m: %s | Sweeping %d tickers...",
        "LIVE" if market_active else "OFF-MARKET / LATEST AVAILABLE",
        len(tickers),
    )

    try:
        data = yf.download(
            tickers,
            period="6mo",
            group_by="ticker",
            progress=False,
            auto_adjust=False,
            threads=True,
        )

        is_multi = len(tickers) > 1

    except Exception as exc:
        logger.error(
            "Error during market scan download: %s",
            exc,
        )

        return (
            scan_results,
            processed_rows,
            raw_data,
        )

    for ticker in tickers:

        try:
            df = extract_ticker_df(
                data,
                ticker,
                is_multi,
            )

            if df is None or df.empty:
                continue

            metrics = analyze_ticker_data(
                df,
                ticker=ticker,
            )

            if not metrics:
                continue

            scan_results[ticker] = metrics

            price = float(
                metrics.get("price", 0.0)
            )

            strategy = None
            amount = 0.0
            reasoning = ""

            if strategy_enabled and available_cash is not None:
                strategy, amount, reasoning = (
                    evaluate_buy_signal(
                        metrics=metrics,
                        available_cash=available_cash,
                        min_alloc=config["min_alloc"],
                        max_alloc=config["max_alloc"],
                        alloc_pct=config["alloc_pct"],
                        rsi_lower=config["rsi_lower"],
                    )
                )

            if strategy:
                logger.info("BUY Signal [%s]: %s @ ₹%s", ticker, strategy, f"{amount:,.2f}")

                if emit_signals:
                    send_investment_suggestion(
                        ticker=ticker,
                        strategy=strategy,
                        amount=amount,
                        current_price=price,
                        reasoning=reasoning,
                    )
                else:
                    logger.info(
                        "Off-market BUY condition detected for %s; dashboard/state only, alert suppressed.",
                        ticker,
                    )

        except Exception as exc:
            logger.error(
                "Error processing %s: %s",
                ticker,
                exc,
            )

    for ticker, metrics in scan_results.items():

        signals = metrics.get(
            "signals",
            [],
        )

        signal_text = (
            ", ".join(signals)
            if signals
            else "NEUTRAL"
        )

        processed_rows.append(
            [
                ticker,
                metrics.get("price", 0.0),
                metrics.get("rsi", 0.0),
                metrics.get("macd", 0.0),
                signal_text,
            ]
        )

        raw_data.append(
            {
                "ticker": ticker,
                **metrics,
            }
        )

    return (
        scan_results,
        processed_rows,
        raw_data,
    )


# ---------------------------------------------------------------------------
# SIP PERIPHERAL
# ---------------------------------------------------------------------------

def _serialize_sip_value(value: Any) -> Any:
    """Convert SIP engine values into Google-Sheets-safe scalar values."""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _sip_target_to_dict(target: Any) -> dict[str, Any]:
    """Convert a SIP target dataclass/object into a serializable mapping."""
    if isinstance(target, dict):
        source = dict(target)
    elif hasattr(target, "to_dict") and callable(target.to_dict):
        source = dict(target.to_dict())
    else:
        source = {}
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
            "metadata",
        ):
            if hasattr(target, name):
                source[name] = getattr(target, name)

    return {
        str(key): _serialize_sip_value(value)
        for key, value in source.items()
    }


def _get_sip_targets() -> list[dict[str, Any]]:
    """Return the current SIP target state for the Sheets dashboard."""
    try:
        getter = getattr(sip_engine, "get_targets", None)
        if not callable(getter):
            return []
        targets = getter(include_expired=True)
        return [
            _sip_target_to_dict(target)
            for target in (targets or [])
        ]
    except Exception as exc:
        logger.exception("Failed to read SIP targets: %s", exc)
        return []


def _get_sip_decisions() -> list[dict[str, Any]]:
    """Read SIP decision history when the engine/DB exposes it."""
    for owner in (sip_engine, db):
        for method_name in (
            "get_decisions",
            "get_sip_decisions",
            "list_decisions",
            "list_sip_decisions",
        ):
            getter = getattr(owner, method_name, None)
            if not callable(getter):
                continue
            try:
                rows = getter()
                if rows is None:
                    continue
                return [
                    dict(row) if isinstance(row, dict) else _sip_target_to_dict(row)
                    for row in rows
                ]
            except TypeError:
                continue
            except Exception as exc:
                logger.warning(
                    "Unable to read SIP decisions from %s.%s: %s",
                    type(owner).__name__,
                    method_name,
                    exc,
                )
    return []


def run_sip_peripheral() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Refresh SIP lifecycle state and return Sheets telemetry.

    SIP activation is intentionally *not* performed here. The Telegram
    ``/SIP`` runtime command controls whether proactive SIP recommendations
    are permitted. This function only reads current SIP state and prepares
    telemetry for the Google Sheets layer.
    """
    # The SIP engine is refreshed by its own background worker so SIP state
    # evolves independently of the main market-analysis cadence. This
    # function is intentionally read-only from the orchestrator's perspective.
    targets = _get_sip_targets()
    decisions = _get_sip_decisions()

    try:
        status = getattr(sip_engine, "status", None)
        if callable(status):
            snapshot = status()
            if isinstance(snapshot, dict):
                logger.info(
                    "\033[1;38;2;0;255;0mSIP state\033[0m: active=%s | targets=%d | active_targets=%d | paused_targets=%d",
                    snapshot.get("active", False),
                    snapshot.get("target_count", len(targets)),
                    snapshot.get("active_target_count", 0),
                    snapshot.get("paused_target_count", 0),
                )
    except Exception:
        logger.debug("Unable to read SIP status snapshot.", exc_info=True)

    return targets, decisions


def register_sip_runtime_provider() -> None:
    """Register the shared SIP engine with Telegram when supported."""
    try:
        import telegram_bot as telegram_runtime

        provider = getattr(telegram_runtime, "set_sip_engine_provider", None)
        if callable(provider):
            # The Telegram runtime provider hook accepts the shared SIP
            # engine as its single runtime dependency. Database access is
            # owned by the Telegram runtime itself, so do not pass ``db``
            # here. Passing both objects causes a TypeError with the current
            # provider contract.
            provider(sip_engine)
            logger.info("Shared SIP engine registered with Telegram runtime.")
        else:
            logger.warning(
                "Telegram runtime has no SIP provider hook; SIP activation "
                "remains unavailable until its /SIP command handler is installed."
            )
    except Exception as exc:
        logger.warning("Failed to register SIP runtime provider: %s", exc)


# ---------------------------------------------------------------------------
# SIP BACKGROUND RUNTIME
# ---------------------------------------------------------------------------

SIP_RUNTIME_INTERVAL_SECONDS = max(
    15,
    _setting_int("SIP.RUNTIME_INTERVAL_SECONDS", 60),
)


def run_sip_background_worker() -> None:
    """Run SIP lifecycle processing independently of the market pipeline.

    The SIP engine is alive for the entire ASTRA process lifetime. It remains
    dormant until the Telegram ``/SIP`` command activates it for proactive
    recommendations, but existing SIP lifecycle/critical-state monitoring can
    continue while dormant.

    The worker never activates SIP on its own and never creates a new SIP
    target. Those actions remain user-controlled.
    """
    logger.info(
        "SIP background runtime initialized in dormant mode."
    )

    while not shutdown_event.is_set():
        try:
            refresh_sip()

        except Exception as exc:
            logger.warning(
                "SIP background cycle failed: %s",
                exc,
            )

        shutdown_event.wait(
            timeout=SIP_RUNTIME_INTERVAL_SECONDS
        )

    logger.info(
        "SIP background runtime stopped."
    )


def start_sip_runtime() -> threading.Thread:
    """Start the SIP engine worker as a daemon thread."""
    thread = threading.Thread(
        target=run_sip_background_worker,
        name="ASTRA-SIP",
        daemon=True,
    )

    thread.start()

    logger.info(
        "SIP engine running in parallel with the main ASTRA runtime."
    )

    return thread


# ---------------------------------------------------------------------------
# TELEMETRY
# ---------------------------------------------------------------------------

def _normalize_wallet_and_holdings_for_sheets(
    real_portfolio: dict[str, Any],
) -> dict[str, Any]:
    """Return a Sheets-safe wallet payload with a strict 8-column holding schema.

    Angel One may return many broker-specific fields. Those fields remain
    available to ASTRA internally, but only the fixed eight dashboard fields
    are exposed to the Google Sheets synchronization layer.
    """
    if not isinstance(real_portfolio, dict):
        return {}

    normalized: dict[str, Any] = {}

    # Keep wallet-level fields required by the existing synchronizer.
    # Holdings are the part whose tabular schema is explicitly fixed.
    for key in ("available_cash", "cash", "wallet", "total_cash"):
        if key in real_portfolio:
            normalized[key] = real_portfolio[key]

    source_holdings = real_portfolio.get("holdings", []) or []
    normalized_holdings: list[dict[str, Any]] = []

    for holding in source_holdings:
        if not isinstance(holding, dict):
            continue

        ticker = holding.get("ticker", holding.get("symbol", ""))
        quantity = holding.get("qty", holding.get("quantity", 0))
        avg_price = holding.get("avg_price", holding.get("average_price", 0))
        ltp = holding.get("ltp", holding.get("last_price", holding.get("current_price", 0)))
        invested = holding.get("invested_val", holding.get("invested_value", 0))
        current = holding.get("current_val", holding.get("current_value", 0))
        pnl = holding.get("pnl", 0)
        pnl_pct = holding.get("pnl_pct", holding.get("pnl_percent", 0))

        # Deliberately construct a new dictionary with exactly eight keys.
        # Keep the internal field names expected by the existing synchronizer;
        # the sheet writer controls their display headers.
        normalized_holdings.append({
            "ticker": ticker,
            "qty": quantity,
            "avg_price": avg_price,
            "ltp": ltp,
            "invested_val": invested,
            "current_val": current,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
        })

    normalized["holdings"] = normalized_holdings

    # Preserve other top-level metadata only if it is scalar and not a nested
    # holding/portfolio structure. This avoids accidentally expanding the
    # Wallet_and_Holdings table through nested broker response fields.
    for key, value in real_portfolio.items():
        if key in {"holdings", "available_cash", "cash", "wallet", "total_cash"}:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            normalized[key] = value

    return normalized


def sync_sheets(
    config: dict[str, Any],
    processed_rows: list[list[Any]],
    raw_data: list[dict[str, Any]],
    real_portfolio: dict[str, Any],
    sip_data: Optional[list[dict[str, Any]]] = None,
    sip_decisions: Optional[list[dict[str, Any]]] = None,
) -> None:
    """Synchronize market and wallet telemetry to Google Sheets."""
    credentials_file = config["credentials_file"]
    spreadsheet_id = config["spreadsheet_id"]

    if not credentials_file.exists():
        logger.debug(
            "Google service-account file not found. "
            "Skipping Sheets synchronization."
        )
        return

    if not spreadsheet_id:
        logger.debug(
            "Google spreadsheet ID not configured. "
            "Skipping Sheets synchronization."
        )
        return

    try:
        sheets_portfolio = _normalize_wallet_and_holdings_for_sheets(
            real_portfolio
        )

        logger.debug(
            "Wallet_and_Holdings schema locked to %d columns: %s",
            len(WALLET_AND_HOLDINGS_COLUMNS),
            ", ".join(WALLET_AND_HOLDINGS_COLUMNS),
        )

        sync_dashboard_data(
            credentials_path=str(
                credentials_file
            ),
            spreadsheet_id=spreadsheet_id,
            processed_data=processed_rows,
            raw_data=raw_data,
            real_portfolio=sheets_portfolio,
            main_tab_name="Market Scan",
            raw_tab_name="Raw Data",
            wallet_tab_name="Wallet_and_Holdings",
            sip_data=sip_data or [],
            sip_decisions=sip_decisions or [],
            sip_tab_name="SIP",
            sip_decisions_tab_name="SIP_Decisions",
        )

    except Exception as exc:
        logger.error(
            "Google Sheets sync error: %s",
            exc,
        )


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------

def maybe_send_email(
    config: dict[str, Any],
    available_cash: Optional[float],
) -> None:
    """Dispatch the configured report only when wallet cash is verified."""
    mode = config["mailer_mode"]

    if not should_trigger_mail(mode):
        return

    if available_cash is None:
        logger.warning(
            "Skipping email report because broker wallet cash is unavailable; "
            "ASTRA will not report unknown cash as ₹0.00."
        )
        return

    logger.info("Sending Email Report (Trigger Mode: %s)...", mode)

    try:
        send_eod_email_report(
            start_cash=available_cash,
            end_cash=available_cash,
            transactions=[],
        )

        if mode != "NOW":
            mark_eod_mail_sent()

    except Exception as exc:
        logger.error(
            "Error sending email report: %s",
            exc,
        )


# ---------------------------------------------------------------------------
# NORMAL ASTRA PIPELINE
# ---------------------------------------------------------------------------

def run_pipeline(
    force_email_now: bool = False,
) -> bool:
    """Run one 24x7 ASTRA cycle. Market-open and off-market work are separate."""
    config = load_runtime_config(force_email_now=force_email_now)
    now = now_ist()
    market_active = is_market_open()
    phase = "MARKET-OPEN" if market_active else "OFF-MARKET"

    sip_data, sip_decisions = run_sip_peripheral()

    logger.info("\033[1;38;2;255;200;0m=== ASTRA %s CYCLE | %s IST ===\033[0m", phase, now.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info(
        "Cycle configuration: allocation ₹%s-₹%s | TSL=%s | trail=%.2f%% | mailer=%s",
        f'{config["min_alloc"]:,.2f}',
        f'{config["max_alloc"]:,.2f}',
        config["enable_tsl"],
        config["trailing_stop_pct"] * 100,
        config["mailer_mode"],
    )

    db.log_system(
        "INFO",
        f"24x7 cycle started | phase={phase} | market_status={'OPEN' if market_active else 'CLOSED'}",
    )

    # Wallet: use the shared client. During off-market cycles, the SmartAPI
    # client can serve its last-known-good snapshot instead of hammering auth.
    logger.info("Refreshing wallet/portfolio state...")
    (
        real_portfolio,
        _,
        wallet_valid,
        wallet_fresh,
    ) = fetch_real_portfolio(force_refresh=market_active)

    available_cash: Optional[float] = None
    holdings: list[dict[str, Any]] = []

    if wallet_valid:
        try:
            available_cash = float(
                real_portfolio.get("available_cash")
            )
        except (TypeError, ValueError):
            available_cash = None

        candidate_holdings = real_portfolio.get("holdings", [])
        if isinstance(candidate_holdings, list):
            holdings = candidate_holdings

    invested, current_value, total_pnl = portfolio_totals(holdings)

    if wallet_valid and available_cash is not None:
        if not db.log_portfolio_snapshot(
            cash=available_cash,
            invested=invested,
            current=current_value,
            pnl=total_pnl,
        ):
            logger.warning("Portfolio snapshot could not be persisted.")

        logger.info(
            "\033[1;38;2;255;0;255mWallet telemetry\033[0m: %s wallet | cash ₹%s | holdings %d | "
            "invested ₹%s | value ₹%s | P&L ₹%s",
            "fresh" if wallet_fresh else "cached/stale",
            f"{available_cash:,.2f}",
            len(holdings),
            f"{invested:,.2f}",
            f"{current_value:,.2f}",
            f"{total_pnl:,.2f}",
        )
    else:
        logger.warning(
            "Wallet telemetry: UNAVAILABLE | cash unknown | holdings unknown. "
            "ASTRA will not treat wallet failure as ₹0.00."
        )

    # A broker wallet failure must never be interpreted as an empty portfolio.
    # Therefore holdings are only scanned when ASTRA has a valid broker
    # snapshot. This prevents false SELL/TSL decisions against unknown
    # holdings.
    if wallet_valid and wallet_fresh:
        scan_existing_holdings(
            holdings=holdings,
            config=config,
            emit_alerts=market_active,
        )
    else:
        logger.warning(
            "Skipping holdings/SELL/TSL evaluation because a fresh broker "
            "wallet snapshot is unavailable."
        )

    # Market-data collection is safe to continue without a wallet. Strategy
    # decisions are not: BUY recommendations require a fresh broker wallet
    # snapshot so the triple-bound capital check cannot operate on stale or
    # fabricated cash.
    strategy_enabled = wallet_valid and wallet_fresh and available_cash is not None

    if not strategy_enabled:
        logger.warning(
            "Capital-dependent BUY strategy disabled this cycle: "
            "wallet must be freshly verified by Angel One."
        )

    _scan_results, processed_rows, raw_data = scan_market(
        available_cash=available_cash if strategy_enabled else None,
        config=config,
        market_active=market_active,
        emit_signals=market_active and strategy_enabled,
        strategy_enabled=strategy_enabled,
    )

    # Persist system phase information for the long-running timeline.
    db.log_system(
        "INFO",
        f"Market phase complete | phase={phase} | tickers_processed={len(raw_data)}",
    )

    # Dashboard synchronization is 24x7, but the wallet tab must never be
    # overwritten with an empty/unknown broker state. When the broker is
    # unavailable, preserve the existing Sheets wallet snapshot and continue
    # the safe DB/market-data telemetry path.
    if wallet_valid:
        sync_sheets(
            config=config,
            processed_rows=processed_rows,
            raw_data=raw_data,
            real_portfolio=real_portfolio,
            sip_data=sip_data,
            sip_decisions=sip_decisions,
        )
    else:
        logger.warning(
            "Skipping Google Sheets synchronization for this cycle because "
            "wallet state is unavailable; existing wallet history is preserved."
        )

    maybe_send_email(config=config, available_cash=available_cash)

    db.log_system(
        "INFO",
        f"24x7 cycle completed | phase={phase} | market_status={'OPEN' if market_active else 'CLOSED'}",
    )

    if not market_active:
        logger.info(
            "Off-market processing complete: portfolio, latest available prices, indicators, DB and dashboard updated."
        )

    return market_active


# ---------------------------------------------------------------------------
# PROCESS CONTROL
# ---------------------------------------------------------------------------

def _request_shutdown(
    signum: int,
    _frame: Any,
) -> None:
    """Request shutdown without performing logging or cleanup in the signal handler."""
    del signum
    print()
    sys.stdout.flush()
    shutdown_event.set()


def install_signal_handlers() -> None:
    """Install SIGINT/SIGTERM handlers where supported."""
    signal.signal(
        signal.SIGINT,
        _request_shutdown,
    )

    if hasattr(signal, "SIGTERM"):
        signal.signal(
            signal.SIGTERM,
            _request_shutdown,
        )


def start_telegram_listener() -> threading.Thread:
    """Start Telegram polling in a daemon thread."""
    thread = threading.Thread(
        target=run_telegram_bot_loop,
        name="ASTRA-Telegram",
        daemon=True,
    )

    thread.start()

    logger.info(
        "Telegram Interactive Bot Listener initialized in background."
    )

    return thread



# ---------------------------------------------------------------------------
# AUTONOMOUS INTRADAY PERIPHERAL
# ---------------------------------------------------------------------------

# The intraday bot may be activated by Telegram or explicitly from the
# terminal with ``--intraday``. Main.py provides the long-running scheduler
# that gives the bot execution time and performs the end-of-day square-off.
INTRADAY_EXPIRY_GRACE_SECONDS = max(
    5,
    _setting_int("INTRADAY.EXPIRY_GRACE_SECONDS", 30),
)


def run_intraday_peripheral() -> None:
    """Run one Smart Intraday step and enforce the daily expiry boundary.

    Telegram and main.py intentionally use the same process-wide
    SmartIntradayBot instance. When ALWAYS_ACTIVE_INTRADAY=true, this
    function activates that shared bot automatically during the NSE session.
    """
    try:
        bot = get_smart_intraday_bot(
            angel_client,
            db,
        )
        now = now_ist()

        if now.weekday() >= 5:
            return

        # Respect global/module kill and temporary suspension controls.
        blocked = (
            is_astra_killed()
            or is_suspended()
            or is_module_killed("intraday")
        )

        # ---------------------------------------------------------------
        # ALWAYS_ACTIVE_INTRADAY
        # ---------------------------------------------------------------
        if (
            ALWAYS_ACTIVE_INTRADAY
            and not blocked
            and MARKET_OPEN <= now.timetz() < MARKET_CLOSE
            and not bot.gate.allowed()
        ):
            activated = bot.activate(
                now.date().isoformat()
            )
            if activated:
                logger.info(
                    "ALWAYS_ACTIVE_INTRADAY override activated Smart Intraday | session=%s | expiry=15:30 IST",
                    now.date().isoformat(),
                )

        state = bot.status()
        logger.info(
            "\033[1;38;2;0;255;0mIntraday state\033[0m: active=%s | killed=%s | session=%s | open_positions=%s | daily_pnl=%.2f | mode=%s",
            state.get("active", False),
            state.get("killed", False),
            state.get("session_date"),
            state.get("open_positions", 0),
            float(state.get("daily_pnl", 0.0) or 0.0),
            state.get("execution_mode", "ADVISORY"),
        )

        # Nothing further should run while globally/module-blocked.
        if blocked:
            return

        expiry_start = (
            dt.datetime.combine(
                now.date(),
                MARKET_CLOSE,
                tzinfo=IST,
            )
            - dt.timedelta(seconds=INTRADAY_EXPIRY_GRACE_SECONDS)
        )

        # EOD square-off takes precedence over another strategy cycle.
        if now >= expiry_start:
            gate = getattr(bot, "gate", None)
            if gate is not None and getattr(gate, "expiry_allowed", lambda: False)():
                logger.warning(
                    "Intraday EOD window reached. Generating expiry SELL recommendations for all ASTRA-owned positions..."
                )
                results = bot.expire()
                logger.info(
                    "Intraday EOD square-off completed: %d result(s).",
                    len(results),
                )
            return

        result = bot.run_cycle()
        if result.get("status") not in {"BLOCKED", "OK"}:
            logger.warning(
                "Intraday peripheral returned: %s",
                result,
            )

    except Exception as exc:
        logger.exception(
            "Intraday peripheral failed: %s",
            exc,
        )
        try:
            send_critical_failure_alert(
                f"ASTRA intraday peripheral failure: {exc}"
            )
        except Exception:
            logger.exception(
                "Failed to send intraday peripheral failure alert."
            )


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

def run(
    *,
    force_email_now: bool = False,
    cli_intraday: bool = False,
    cli_sip: bool = False,
) -> int:
    """Run the ASTRA service until shutdown."""
    install_signal_handlers()

    logger.info("\033[1;38;2;255;200;0m=== Starting ASTRA Engine (24x7) ===\033[0m")

    global telegram_thread, sip_thread

    # Register the shared SmartAPI client and the exact same intraday singleton
    # with Telegram before polling starts. This removes the old split between
    # ``intraday_trading`` and ``intraday_bot`` state machines.
    try:
        set_smart_client(angel_client)
        set_intraday_engine_provider(
            angel_client,
            db,
        )
        logger.info(
            "Shared Smart Intraday engine registered with main + Telegram."
        )
    except Exception:
        logger.exception(
            "Failed to register shared Smart Intraday engine."
        )

    register_sip_runtime_provider()

    # ------------------------------------------------------------------
    # Console-only subsystem activation
    # ------------------------------------------------------------------
    # ``--intraday`` and ``--sip`` are explicit terminal overrides. They
    # activate only the requested subsystem; normal ASTRA processing remains
    # unchanged. Both flags may be supplied together.
    if cli_intraday:
        try:
            bot = get_smart_intraday_bot(
                angel_client,
                db,
            )
            now = now_ist()

            if now.weekday() >= 5:
                logger.warning(
                    "--intraday requested, but today is outside the NSE weekday session."
                )
            else:
                expiry = dt.datetime.combine(
                    now.date(),
                    MARKET_CLOSE,
                    tzinfo=IST,
                )
                bot.activate(
                    now.date().isoformat(),
                    expiry,
                )
                logger.info(
                    "Console --intraday activation: ACTIVE | session=%s | expiry=%s",
                    now.date().isoformat(),
                    expiry.strftime("%Y-%m-%d %H:%M:%S %Z"),
                )
        except Exception as exc:
            logger.exception(
                "Console --intraday activation failed: %s",
                exc,
            )

    if cli_sip:
        try:
            activated = sip_engine.activate()
            logger.info(
                "Console --sip activation: %s",
                "ACTIVE" if activated else "FAILED",
            )
        except Exception as exc:
            logger.exception(
                "Console --sip activation failed: %s",
                exc,
            )

    sip_thread = start_sip_runtime()

    telegram_thread = start_telegram_listener()

    email_now_pending = force_email_now

    while not shutdown_event.is_set():

        cycle_started = time.monotonic()

        try:
            market_active = run_pipeline(
                force_email_now=email_now_pending
            )

        except Exception as exc:
            logger.exception(
                "Unhandled exception in ASTRA pipeline: %s",
                exc,
            )

            try:
                send_critical_failure_alert(
                    f"ASTRA pipeline failure: {exc}"
                )
            except Exception:
                logger.exception(
                    "Failed to send critical failure alert."
                )

            # Keep the service alive after a cycle failure.
            market_active = False

        # The autonomous intraday subsystem is independently activated by
        # Telegram. It runs in parallel with normal ASTRA advisory processing.
        if market_active or (now_ist().weekday() < 5):
            run_intraday_peripheral()

        # --email-now is a one-shot command-line trigger.
        email_now_pending = False

        # Align the next cycle to a wall-clock minute boundary rather than
        # sleeping for N minutes after the previous cycle completed. This
        # means a cycle started at 13:05:40 with a 5-minute cadence schedules
        # the next cycle for exactly 13:10:00.
        runtime_config = load_runtime_config()
        cadence_minutes = (
            runtime_config["active_cycle_minutes"]
            if market_active
            else runtime_config["passive_cycle_minutes"]
        )

        now = now_ist()
        cadence_seconds = cadence_minutes * 60
        elapsed_since_boundary = (
            now.minute * 60
            + now.second
            + now.microsecond / 1_000_000
        ) % cadence_seconds
        sleep_duration = cadence_seconds - elapsed_since_boundary
        if sleep_duration <= 0.001:
            sleep_duration = cadence_seconds

        logger.info(
            "Iteration complete. Next %s cycle in \033[1m%s\033[0m...\n",
            "market-open" if market_active else "off-market",
            format_duration(sleep_duration),
        )

        # Event.wait() is interruptible, unlike time.sleep().
        shutdown_event.wait(
            timeout=sleep_duration
        )

    # ---------------------------------------------------------------
    # Graceful shutdown
    # ---------------------------------------------------------------

    logger.info("Received shutdown request. Starting graceful shutdown...")
    logger.info("[!] Gracefully shutting down ASTRA Engine...")

    try:
        deactivate_sip(reason="PROCESS_SHUTDOWN")
        logger.info("SIP runtime deactivated for process shutdown.")
    except Exception:
        logger.exception("Failed to deactivate SIP engine cleanly.")

    if sip_thread and sip_thread.is_alive():
        logger.info("Waiting for SIP background runtime to stop...")
        sip_thread.join(timeout=5.0)

    # The intraday peripheral is driven synchronously by this main loop, so
    # there is no second worker thread to join. Stop its session gate cleanly.
    try:
        bot = get_smart_intraday_bot(angel_client, db)
        bot.stop("PROCESS_SHUTDOWN")
    except Exception:
        logger.exception("Failed to stop intraday peripheral cleanly.")

    # DatabaseManager normally uses short-lived SQLite connections, but some
    # deployed versions expose a close() compatibility method. Call it when
    # present and never turn shutdown into an error merely because the DB
    # implementation has no persistent connection to close.
    try:
        close_method = getattr(db, "close", None)
        if callable(close_method):
            close_method()
        else:
            logger.debug(
                "DatabaseManager has no persistent close() method; no DB cleanup required."
            )
    except Exception:
        logger.exception("Database shutdown cleanup failed.")

    logger.info("ASTRA Engine shutdown complete.")

    if telegram_thread and telegram_thread.is_alive():
        logger.info(
            "Telegram listener is a daemon thread and "
            "will terminate with the process."
        )

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ASTRA Algorithmic Market Analysis Engine"
    )

    parser.add_argument(
        "--email-now",
        action="store_true",
        help=(
            "Force one immediate email report "
            "during the next ASTRA cycle."
        ),
    )

    parser.add_argument(
        "--intraday",
        action="store_true",
        help=(
            "Activate the Smart Intraday subsystem from the terminal "
            "for the current NSE session."
        ),
    )

    parser.add_argument(
        "--sip",
        action="store_true",
        help=(
            "Activate the SIP subsystem from the terminal."
        ),
    )

    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    return run(
        force_email_now=args.email_now,
        cli_intraday=args.intraday,
        cli_sip=args.sip,
    )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )

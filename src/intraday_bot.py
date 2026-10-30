"""
ASTRA - Intraday Market Analysis Engine
=======================================

Purpose
-------
Dedicated intraday analysis and position-management subsystem for ASTRA.

This module supports two explicitly configured modes:

    advisory: generate recommendations only; never invoke an executor.
    trader: automatically process signals through the configured executor.

The built-in executor is PaperExecutor. Real broker execution is NOT
implemented by this module and must not be inferred from ``mode="trader"``.

It can:
    - scan the intraday market universe
    - calculate RSI/MACD-based entry signals
    - calculate stop-loss and trailing-stop levels
    - maintain ASTRA's intraday ownership ledger
    - monitor ASTRA-owned positions
    - generate BUY/SELL recommendations
    - enforce capital/risk limits
    - prepare execution intents for a human/operator

The built-in implementation does NOT autonomously submit live broker orders.
Trader mode currently performs paper execution only unless a separately
reviewed executor is explicitly injected by a future integration.

Ownership model
---------------
Wallet_and_Holdings
    -> actual Angel One account truth.

Intraday Holdings
    -> positions explicitly opened/owned by ASTRA's intraday workflow.

Manual broker holdings
    -> never treated as ASTRA-owned positions.

Important
---------
A broker wallet failure means UNKNOWN.

It must never become:

    available_cash = 0

because that can incorrectly trigger capital/risk decisions.

Market session
--------------
NSE-style session:

    09:15 IST -> 15:30 IST

Normal intraday management is blocked outside the session.

Daily expiry handling is deliberately separate from the normal session gate
so that ASTRA can still produce the required square-off recommendation at
15:30 without accidentally treating the expired session as an active entry
window.

Live execution
--------------
This module deliberately does not call Angel One placeOrder().

The execution boundary belongs outside this advisory subsystem and requires
explicit human confirmation.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import threading
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import yfinance as yf

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:  # pragma: no cover - optional runtime dependency
    gspread = None
    Credentials = None

from decision_engine import (
    analyze_ticker_data,
    evaluate_exit_signal,
)
from mng_db import DatabaseManager
from paper_trading import PaperTradingSimulator
from tickers import get_dynamic_tickers
from utils import get_setting, clean_ticker_symbol, to_yahoo_ticker


# ============================================================================
# LOGGER
# ============================================================================

logger = logging.getLogger(
    "ASTRA_INTRADAY_BOT"
)


# ============================================================================
# MARKET CONSTANTS
# ============================================================================

IST = dt.timezone(
    dt.timedelta(
        hours=5,
        minutes=30,
    )
)

MARKET_OPEN = dt.time(
    9,
    15,
)

MARKET_CLOSE = dt.time(
    15,
    30,
)

OWNER = "ASTRA_INTRADAY"

# Google Sheets dashboard contract for the intraday subsystem. SQLite remains
# authoritative; this worksheet is a human-readable mirror of ASTRA-owned
# intraday holdings only.
INTRADAY_HOLDINGS_TAB = "Intraday Bot Holdings"
INTRADAY_HOLDINGS_COLUMNS = (
    "ticker",
    "quantity",
    "entry_price",
    "current_price",
    "invested_value",
    "current_value",
    "pnl",
    "pnl_pct",
    "stop_loss",
    "trailing_stop",
    "peak_price",
    "session_date",
    "status",
    "owner",
    "entry_order_id",
    "exit_order_id",
)

BASE_DIR = Path(__file__).resolve().parent.parent




# ============================================================================
# GOOGLE SHEETS HELPERS
# ============================================================================

def _sheet_setting(name: str, default: str = "") -> str:
    """Read a non-secret Google Sheets runtime setting."""

    try:
        value = get_setting(
            name,
            None,
        )
    except Exception:
        value = None

    if value is None or str(value).strip() == "":
        leaf = name.split(".")[-1]
        aliases = {
            "SPREADSHEET_ID": (
                "GOOGLE_SHEETS_SPREADSHEET_ID",
                "SPREADSHEET_ID",
                "GOOGLE_SHEET_ID",
            ),
            "SERVICE_ACCOUNT_FILE": (
                "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE",
                "SERVICE_ACCOUNT_FILE",
                "GOOGLE_APPLICATION_CREDENTIALS",
            ),
        }.get(leaf, (leaf,))
        value = next(
            (os.getenv(alias) for alias in aliases if os.getenv(alias)),
            default,
        )

    return str(value or default).strip()


def _sheet_credentials_path() -> Path:
    """Resolve the configured Google service-account file."""

    configured = _sheet_setting(
        "GOOGLE_SHEETS.SERVICE_ACCOUNT_FILE",
        "service_account.json",
    )

    path = Path(configured).expanduser()

    if not path.is_absolute():
        path = BASE_DIR / path

    return path


def _serialize_sheet_value(value: Any) -> Any:
    """Convert an internal value into a Google-Sheets-safe scalar."""

    if value is None:
        return ""

    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()

    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"

    if isinstance(value, (str, int, float)):
        return value

    return str(value)


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass(frozen=True)
class TradeIntent:
    """
    Proposed intraday action.

    This is an analysis/execution-request object only.

    Creating a TradeIntent does NOT place an order.
    """

    action: str
    ticker: str
    quantity: int
    price: float
    reason: str
    stop_loss: float
    trailing_stop: float
    session_date: str
    intent_id: str = field(
        default_factory=lambda: uuid.uuid4().hex
    )


@dataclass(frozen=True)
class RiskDecision:
    """Result of the deterministic risk gateway."""

    approved: bool
    reason: str
    allocation: float = 0.0


@dataclass
class IntradayPosition:
    """
    ASTRA-owned intraday position.

    This is the internal ownership representation.

    It must never be inferred from aggregate broker holdings.
    """

    ticker: str
    quantity: int

    entry_price: float
    current_price: float

    stop_loss: float
    trailing_stop: float

    peak_price: float

    session_date: str

    status: str = "OPEN"
    owner: str = OWNER

    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None

    @property
    def pnl(self) -> float:
        return (
            self.current_price
            - self.entry_price
        ) * self.quantity

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0

        return (
            (
                self.current_price
                - self.entry_price
            )
            / self.entry_price
        ) * 100.0


# ============================================================================
# EXECUTION PROTOCOL
# ============================================================================

class OrderExecutor(Protocol):
    """
    Execution abstraction.

    Implementations may be used for:
        - paper trading
        - simulation
        - explicitly confirmed manual execution

    The default ASTRA implementation below never sends broker orders.
    """

    def buy(
        self,
        intent: TradeIntent,
    ) -> Optional[str]:
        ...

    def sell(
        self,
        intent: TradeIntent,
    ) -> Optional[str]:
        ...


def _setting_number(path: str, default: float) -> float:
    """Read a numeric setting and reject non-finite or negative values."""
    try:
        value = float(get_setting(path, default))
        return value if math.isfinite(value) and value >= 0 else float(default)
    except (TypeError, ValueError, OverflowError):
        return float(default)


class PaperExecutor:
    """SQLite-backed virtual executor. It never submits broker orders."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        simulator: Optional[PaperTradingSimulator] = None,
    ) -> None:
        if db_path is None:
            project_root = Path(__file__).resolve().parents[1]
            db_path = project_root / "data" / "astra_paper_trading.db"

        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.simulator = simulator or PaperTradingSimulator(
            db_path=db_path,
            initial_balance=_setting_number("INTRADAY.PAPER.INITIAL_BALANCE", 100000.0),
            slippage_bps=_setting_number("INTRADAY.PAPER.SLIPPAGE_BPS", 5.0),
            brokerage_rate=_setting_number("INTRADAY.PAPER.BROKERAGE_RATE", 0.0003),
            brokerage_cap_per_order=_setting_number("INTRADAY.PAPER.BROKERAGE_CAP_PER_ORDER", 20.0),
            transaction_charge_bps=_setting_number("INTRADAY.PAPER.TRANSACTION_CHARGE_BPS", 3.5),
            stt_sell_bps=_setting_number("INTRADAY.PAPER.STT_SELL_BPS", 2.5),
            stamp_buy_bps=_setting_number("INTRADAY.PAPER.STAMP_BUY_BPS", 0.3),
            gst_rate=_setting_number("INTRADAY.PAPER.GST_RATE", 0.18),
        )

    def buy(self, intent: TradeIntent) -> Optional[str]:
        return self.simulator.buy(intent)

    def sell(self, intent: TradeIntent) -> Optional[str]:
        return self.simulator.sell(intent)

    def get_available_cash(self) -> float:
        return self.simulator.get_available_cash()

    def get_fill_price(self, order_id: str) -> Optional[float]:
        return self.simulator.get_fill_price(order_id)

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        return self.simulator.get_order(order_id)

    def mark_price(self, ticker: str, price: float) -> bool:
        return self.simulator.mark_price(ticker, price)

    def snapshot(self) -> dict[str, Any]:
        return self.simulator.snapshot().__dict__

    def session_pnl(self, session_date: str) -> float:
        return self.simulator.session_pnl(session_date)

    def daily_loss_breached(self, max_daily_loss_fraction: float, session_date: str) -> bool:
        return self.simulator.daily_loss_breached(max_daily_loss_fraction, session_date)

    def rollback_order(self, order_id: str, reason: str = "LEDGER_SYNC_FAILED") -> bool:
        return self.simulator.rollback_order(order_id, reason)


# ============================================================================
# CONFIGURATION
# ============================================================================

def _env(
    name: str,
    default: str,
) -> str:

    value = os.getenv(
        name,
        default,
    )

    if value is None:
        return default

    return str(value).strip()


def _env_int(
    name: str,
    default: int,
    minimum: int = 0,
) -> int:

    try:
        value = int(
            _env(
                name,
                str(default),
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        value = default

    return max(
        minimum,
        value,
    )


def _env_float(
    name: str,
    default: float,
    minimum: Optional[float] = None,
) -> float:

    try:
        value = float(
            _env(
                name,
                str(default),
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        value = default

    if minimum is not None:
        value = max(
            minimum,
            value,
        )

    return value


def _env_bool(
    name: str,
    default: bool,
) -> bool:

    return (
        _env(
            name,
            str(default),
        )
        .lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        }
    )


class BotConfig:
    """
    Runtime configuration for the intraday analysis engine.
    """

    def __init__(self) -> None:

        # Mode is a runtime application setting, not an infrastructure secret.
        # Fail closed to advisory if missing or invalid. The setting is read
        # when the shared bot is constructed; restart ASTRA after changing it.
        try:
            # JSON keys are case-sensitive. Accept the documented MODE spelling
            # and the older lowercase mode spelling for backwards compatibility.
            configured_mode = get_setting("INTRADAY.MODE", None)
            if configured_mode is None:
                configured_mode = get_setting("INTRADAY.mode", "advisory")
        except Exception:
            configured_mode = "advisory"

        configured_mode = str(configured_mode or "advisory").strip().lower()
        if configured_mode not in {"advisory", "trader"}:
            logger.warning(
                "Invalid INTRADAY.mode=%r; falling back to advisory mode.",
                configured_mode,
            )
            configured_mode = "advisory"

        self.mode = configured_mode

        self.scan_interval = max(15, int(_setting_number(
            "INTRADAY.SCAN_INTERVAL_SECONDS",
            _env_int("INTRADAY_SCAN_INTERVAL_SECONDS", 180, minimum=15),
        )))

        self.tickers_count = max(1, int(_setting_number(
            "INTRADAY.TICKERS_COUNT",
            _env_int("INTRADAY_TICKERS_COUNT", _env_int("TICKERS_COUNT", 50, minimum=1), minimum=1),
        )))

        self.rsi_lower = _setting_number(
            "INTRADAY.RSI_LOWER_THRESHOLD",
            _env_float("INTRADAY_RSI_LOWER_THRESHOLD", _env_float("RSI_LOWER_THRESHOLD", 35.0)),
        )

        self.rsi_upper = _setting_number(
            "INTRADAY.RSI_UPPER_THRESHOLD",
            _env_float("INTRADAY_RSI_UPPER_THRESHOLD", _env_float("RSI_UPPER_THRESHOLD", 65.0)),
        )

        self.stop_loss_pct = abs(_setting_number(
            "INTRADAY.STOP_LOSS_PCT",
            _env_float("INTRADAY_STOP_LOSS_PCT", 0.03),
        ))

        self.trailing_stop_pct = abs(_setting_number(
            "INTRADAY.TRAILING_STOP_PCT",
            _env_float("INTRADAY_TRAILING_STOP_PCT", 0.02),
        ))

        self.min_alloc = max(0.0, _setting_number(
            "INTRADAY.MIN_ALLOCATION",
            _env_float("INTRADAY_MIN_ALLOCATION", _env_float("MIN_TRADE_ALLOCATION", 100.0)),
        ))

        self.max_alloc = max(self.min_alloc, _setting_number(
            "INTRADAY.MAX_ALLOCATION",
            _env_float("INTRADAY_MAX_ALLOCATION", _env_float("MAX_TRADE_ALLOCATION", 500.0)),
        ))

        self.alloc_pct = max(0.0, _setting_number(
            "INTRADAY.PORTFOLIO_ALLOCATION_PCT",
            _env_float("INTRADAY_PORTFOLIO_ALLOCATION_PCT", _env_float("PORTFOLIO_ALLOCATION_PCT", 0.10)),
        ))

        self.max_positions = max(1, int(_setting_number(
            "INTRADAY.MAX_POSITIONS",
            _env_int("INTRADAY_MAX_POSITIONS", 5, minimum=1),
        )))

        self.max_daily_loss = abs(_setting_number(
            "INTRADAY.MAX_DAILY_LOSS",
            _env_float("INTRADAY_MAX_DAILY_LOSS", 0.02),
        ))

        self.live_trading_requested = _env_bool(
            "ASTRA_INTRADAY_LIVE_TRADING",
            False,
        )

        configured_trailing = get_setting("INTRADAY.ENABLE_TRAILING_STOP", None)
        self.enable_trailing_stop = (
            bool(configured_trailing) if configured_trailing is not None
            else _env_bool("ENABLE_TRAILING_STOP", True)
        )

        self.force_exit_enabled = True

        # Compatibility field consumed by existing status/Telegram callers.
        # Trader mode is paper-only in this implementation: no live Angel One
        # order-placement executor is supplied by this module.
        self.execution_mode = self.mode.upper()
        self.execution_backend = "PAPER" if self.mode == "trader" else "NONE"

    def summary(self) -> dict[str, Any]:

        return {
            "scan_interval": self.scan_interval,
            "tickers_count": self.tickers_count,
            "rsi_lower": self.rsi_lower,
            "rsi_upper": self.rsi_upper,
            "stop_loss_pct": self.stop_loss_pct,
            "trailing_stop_pct": self.trailing_stop_pct,
            "min_alloc": self.min_alloc,
            "max_alloc": self.max_alloc,
            "alloc_pct": self.alloc_pct,
            "max_positions": self.max_positions,
            "max_daily_loss": self.max_daily_loss,
            "mode": self.mode,
            "live_trading_requested": (
                self.live_trading_requested
            ),
            "execution_mode": self.execution_mode,
            "execution_backend": self.execution_backend,
        }


# ============================================================================
# SESSION GATE
# ============================================================================

class SessionGate:
    """
    Runtime permission boundary.

    This gate controls whether intraday analysis is allowed.

    Expiry handling intentionally has a separate permission path.
    """

    def __init__(self) -> None:

        self._lock = threading.RLock()

        self._active = False
        self._killed = False

        self._session_date: Optional[
            str
        ] = None

        self._expires_at: Optional[
            dt.datetime
        ] = None

    def activate(
        self,
        session_date: str,
        expires_at: dt.datetime,
    ) -> None:

        with self._lock:

            self._active = True
            self._killed = False

            self._session_date = (
                session_date
            )

            self._expires_at = (
                expires_at
            )

    def stop(self) -> None:

        with self._lock:
            self._active = False

    def kill(self) -> None:

        with self._lock:

            self._active = False
            self._killed = True

    def allowed(self) -> bool:

        with self._lock:

            if self._killed:
                return False

            if not self._active:
                return False

            now = dt.datetime.now(
                IST
            )

            if (
                self._session_date
                != now.date().isoformat()
            ):
                return False

            if (
                self._expires_at
                and now >= self._expires_at
            ):
                return False

            return (
                MARKET_OPEN
                <= now.time()
                < MARKET_CLOSE
            )

    def expiry_allowed(self) -> bool:
        """
        Permission specifically for producing the 15:30 expiry action.

        This does NOT reopen the trading session.

        It only permits management of already-owned ASTRA positions.
        """

        with self._lock:

            if self._killed:
                return False

            if not self._active:
                return False

            now = dt.datetime.now(
                IST
            )

            if (
                self._session_date
                != now.date().isoformat()
            ):
                return False

            return (
                now.time()
                >= MARKET_CLOSE
            )

    @property
    def killed(self) -> bool:

        with self._lock:
            return self._killed

    @property
    def active(self) -> bool:

        with self._lock:
            return self._active

    @property
    def session_date(self) -> Optional[str]:

        with self._lock:
            return self._session_date


# ============================================================================
# INTRADAY LEDGER
# ============================================================================

class IntradayLedger:
    """
    Adapter around DatabaseManager's ASTRA intraday tables.

    This is the ownership boundary.

    Never use broker aggregate holdings to decide whether ASTRA owns a
    position.
    """

    def __init__(
        self,
        db: DatabaseManager,
    ) -> None:

        self.db = db

    def positions(
        self,
    ) -> list[IntradayPosition]:

        fn = getattr(
            self.db,
            "get_open_intraday_holdings",
            None,
        )

        if not callable(fn):
            logger.error(
                "DatabaseManager does not expose "
                "get_open_intraday_holdings()."
            )
            return []

        try:

            rows = (
                fn(
                    owner=OWNER
                )
                or []
            )

        except Exception:

            logger.exception(
                "Unable to read ASTRA intraday holdings."
            )

            return []

        result: list[
            IntradayPosition
        ] = []

        for row in rows:

            try:
                position = self._row(
                    row
                )
            except Exception:

                logger.exception(
                    "Malformed intraday ledger row."
                )

                continue

            if (
                position.quantity > 0
                and position.ticker
            ):

                result.append(
                    position
                )

        return result

    def get(
        self,
        ticker: str,
    ) -> Optional[
        IntradayPosition
    ]:

        fn = getattr(
            self.db,
            "get_open_intraday_holding",
            None,
        )

        if not callable(fn):
            return None

        normalized = clean_ticker_symbol(
            ticker
        )

        try:

            row = fn(
                ticker=normalized,
                owner=OWNER,
            )

        except Exception:

            logger.exception(
                "Unable to read intraday holding for %s.",
                normalized,
            )

            return None

        if not row:
            return None

        try:

            return self._row(
                row
            )

        except Exception:

            logger.exception(
                "Malformed intraday holding for %s.",
                normalized,
            )

            return None

    def record_entry(
        self,
        position: IntradayPosition,
    ) -> bool:

        fn = getattr(
            self.db,
            "open_intraday_holding",
            None,
        )

        if not callable(fn):

            logger.error(
                "Intraday Holdings DB API is unavailable."
            )

            return False

        try:

            result = fn(
                ticker=position.ticker,
                quantity=position.quantity,
                entry_price=position.entry_price,
                current_price=position.current_price,
                stop_loss=position.stop_loss,
                trailing_stop=position.trailing_stop,
                peak_price=position.peak_price,
                session_date=position.session_date,
                owner=OWNER,
                entry_order_id=position.entry_order_id,
                status="OPEN",
            )

            return bool(
                result
            )

        except Exception:

            logger.exception(
                "Failed to persist intraday entry for %s.",
                position.ticker,
            )

            return False

    def update(
        self,
        position: IntradayPosition,
    ) -> bool:

        fn = getattr(
            self.db,
            "update_intraday_holding",
            None,
        )

        if not callable(fn):
            return False

        try:

            result = fn(
                ticker=position.ticker,
                owner=OWNER,
                quantity=position.quantity,
                current_price=position.current_price,
                stop_loss=position.stop_loss,
                trailing_stop=position.trailing_stop,
                peak_price=position.peak_price,
            )

            return bool(
                result
            )

        except Exception:

            logger.exception(
                "Failed to update intraday holding for %s.",
                position.ticker,
            )

            return False

    def close(
        self,
        position: IntradayPosition,
        exit_price: float,
        order_id: Optional[str],
        reason: str,
    ) -> bool:

        fn = getattr(
            self.db,
            "close_intraday_holding",
            None,
        )

        if not callable(fn):
            return False

        try:

            result = fn(
                ticker=position.ticker,
                owner=OWNER,
                exit_price=exit_price,
                exit_order_id=order_id,
                exit_reason=reason,
            )

            return bool(
                result
            )

        except Exception:

            logger.exception(
                "Failed to close intraday holding for %s.",
                position.ticker,
            )

            return False

    def record_trade(
        self,
        *,
        ticker: str,
        action: str,
        quantity: int,
        price: float,
        order_id: Optional[str],
        reason: str,
        status: str,
        session_date: str,
    ) -> bool:
        """
        Persist an analysis/paper-trade event when the DB supports it.
        """

        fn = getattr(
            self.db,
            "record_intraday_trade",
            None,
        )

        if not callable(fn):

            logger.debug(
                "record_intraday_trade() unavailable."
            )

            return False

        try:

            result = fn(
                ticker=ticker,
                action=action,
                quantity=quantity,
                price=price,
                order_id=order_id,
                reason=reason,
                status=status,
                session_date=session_date,
                owner=OWNER,
            )

            return bool(
                result
            )

        except TypeError:

            # Preserve compatibility with a DB implementation whose
            # signature differs slightly.
            try:

                result = fn(
                    ticker=ticker,
                    action=action,
                    quantity=quantity,
                    price=price,
                    order_id=order_id,
                    reason=reason,
                    status=status,
                )

                return bool(
                    result
                )

            except Exception:

                logger.exception(
                    "Unable to record intraday trade."
                )

                return False

        except Exception:

            logger.exception(
                "Unable to record intraday trade."
            )

            return False

    @staticmethod
    def _row(
        row: Any,
    ) -> IntradayPosition:

        def get(
            key: str,
            default: Any = None,
        ) -> Any:

            if isinstance(
                row,
                dict,
            ):

                return row.get(
                    key,
                    default,
                )

            try:

                return row[key]

            except (
                KeyError,
                IndexError,
                TypeError,
            ):

                return default

        ticker = clean_ticker_symbol(
            str(
                get(
                    "ticker",
                    "",
                )
                or ""
            )
        )

        return IntradayPosition(
            ticker=ticker,

            quantity=int(
                get(
                    "quantity",
                    0,
                )
                or 0
            ),

            entry_price=float(
                get(
                    "entry_price",
                    0,
                )
                or 0
            ),

            current_price=float(
                get(
                    "current_price",
                    0,
                )
                or 0
            ),

            stop_loss=float(
                get(
                    "stop_loss",
                    0,
                )
                or 0
            ),

            trailing_stop=float(
                get(
                    "trailing_stop",
                    0,
                )
                or 0
            ),

            peak_price=float(
                get(
                    "peak_price",
                    get(
                        "current_price",
                        0,
                    ),
                )
                or 0
            ),

            session_date=str(
                get(
                    "session_date",
                    "",
                )
                or ""
            ),

            status=str(
                get(
                    "status",
                    "OPEN",
                )
                or "OPEN"
            ),

            owner=str(
                get(
                    "owner",
                    OWNER,
                )
                or OWNER
            ),

            entry_order_id=get(
                "entry_order_id"
            ),

            exit_order_id=get(
                "exit_order_id"
            ),
        )


# ============================================================================
# RISK GATEWAY
# ============================================================================

class RiskGateway:
    """
    Deterministic pre-action safety gate.

    Unknown state always fails closed.
    """

    def __init__(
        self,
        config: BotConfig,
        gate: SessionGate,
        ledger: IntradayLedger,
    ) -> None:

        self.config = config
        self.gate = gate
        self.ledger = ledger

    def approve(
        self,
        intent: TradeIntent,
        available_cash: Optional[float],
        *,
        expiry: bool = False,
    ) -> RiskDecision:
        """
        Validate a proposed action.

        expiry=True allows a SELL recommendation after 15:30 for an
        already-owned ASTRA position.

        BUY is never permitted through the expiry path.
        """

        action = (
            str(
                intent.action
            )
            .strip()
            .upper()
        )

        if action not in {
            "BUY",
            "SELL",
        }:

            return RiskDecision(
                False,
                "UNKNOWN_ACTION",
            )

        # ---------------------------------------------------------------
        # SESSION
        # ---------------------------------------------------------------

        if expiry:

            if action != "SELL":

                return RiskDecision(
                    False,
                    "EXPIRY_ONLY_ALLOWS_SELL",
                )

            if not self.gate.expiry_allowed():

                return RiskDecision(
                    False,
                    "EXPIRY_SESSION_NOT_AVAILABLE",
                )

        else:

            if not self.gate.allowed():

                return RiskDecision(
                    False,
                    "INTRADAY_SESSION_NOT_ACTIVE",
                )

        # ---------------------------------------------------------------
        # ORDER SIZE
        # ---------------------------------------------------------------

        if intent.quantity <= 0:

            return RiskDecision(
                False,
                "INVALID_ORDER_QUANTITY",
            )

        if intent.price <= 0:

            return RiskDecision(
                False,
                "INVALID_ORDER_PRICE",
            )

        # ---------------------------------------------------------------
        # PROTECTION
        # ---------------------------------------------------------------

        if (
            intent.stop_loss <= 0
            or intent.trailing_stop <= 0
        ):

            return RiskDecision(
                False,
                "INVALID_PROTECTION",
            )

        # ---------------------------------------------------------------
        # SELL
        # ---------------------------------------------------------------

        if action == "SELL":

            position = self.ledger.get(
                intent.ticker
            )

            if position is None:

                return RiskDecision(
                    False,
                    "NO_ASTRA_OWNED_POSITION",
                )

            if (
                position.owner
                != OWNER
            ):

                return RiskDecision(
                    False,
                    "POSITION_OWNER_MISMATCH",
                )

            if (
                intent.quantity
                > position.quantity
            ):

                return RiskDecision(
                    False,
                    "SELL_EXCEEDS_ASTRA_POSITION",
                )

            allocation = (
                intent.quantity
                * intent.price
            )

            return RiskDecision(
                True,
                "APPROVED",
                allocation,
            )

        # ---------------------------------------------------------------
        # BUY
        # ---------------------------------------------------------------

        if available_cash is None:

            return RiskDecision(
                False,
                "WALLET_UNAVAILABLE",
            )

        try:

            cash = float(
                available_cash
            )

        except (
            TypeError,
            ValueError,
        ):

            return RiskDecision(
                False,
                "WALLET_VALUE_INVALID",
            )

        if (
            not math.isfinite(
                cash
            )
            or cash < 0
        ):

            return RiskDecision(
                False,
                "WALLET_VALUE_INVALID",
            )

        if expiry:

            return RiskDecision(
                False,
                "BUY_BLOCKED_DURING_EXPIRY",
            )

        if self.ledger.get(
            intent.ticker
        ) is not None:

            return RiskDecision(
                False,
                "ASTRA_POSITION_ALREADY_OPEN",
            )

        positions = (
            self.ledger.positions()
        )

        if (
            len(positions)
            >= self.config.max_positions
        ):

            return RiskDecision(
                False,
                "MAX_POSITIONS_REACHED",
            )

        allocation = (
            intent.quantity
            * intent.price
        )

        # ---------------------------------------------------------------
        # MINIMUM ALLOCATION
        # ---------------------------------------------------------------

        if (
            allocation
            < self.config.min_alloc
        ):

            return RiskDecision(
                False,
                "BELOW_MIN_ALLOCATION",
            )

        # ---------------------------------------------------------------
        # MAXIMUM ALLOCATION
        # ---------------------------------------------------------------

        if (
            allocation
            > self.config.max_alloc
        ):

            return RiskDecision(
                False,
                "ABOVE_MAX_ALLOCATION",
            )

        # ---------------------------------------------------------------
        # PORTFOLIO ALLOCATION
        # ---------------------------------------------------------------

        if (
            self.config.alloc_pct
            > 0
            and allocation
            > cash
            * self.config.alloc_pct
        ):

            return RiskDecision(
                False,
                "PORTFOLIO_ALLOCATION_LIMIT",
            )

        # ---------------------------------------------------------------
        # ACTUAL CASH
        # ---------------------------------------------------------------

        if allocation > cash:

            return RiskDecision(
                False,
                "INSUFFICIENT_CASH",
            )

        return RiskDecision(
            True,
            "APPROVED",
            allocation,
        )


# ============================================================================
# SMART INTRADAY BOT
# ============================================================================

class SmartIntradayBot:
    """
    RSI + MACD intraday analysis controller.

    New entries:
        discovered from the market universe.

    Existing positions:
        managed ONLY from ASTRA Intraday Holdings.

    Broker holdings:
        never directly sold by this subsystem.

    Execution:
        advisory mode: recommendations only, no executor calls.
        trader mode: automatic paper execution through PaperExecutor.
        live broker execution is not implemented in this module.
    """

    def __init__(
        self,
        client: Any,
        db: Optional[
            DatabaseManager
        ] = None,
        config: Optional[
            BotConfig
        ] = None,
        executor: Optional[
            OrderExecutor
        ] = None,
    ) -> None:

        self.client = client

        self.db = (
            db
            or DatabaseManager()
        )

        self.config = (
            config
            or BotConfig()
        )

        self.gate = (
            SessionGate()
        )

        self.ledger = (
            IntradayLedger(
                self.db
            )
        )

        self.risk = (
            RiskGateway(
                self.config,
                self.gate,
                self.ledger,
            )
        )

        self.executor = (
            executor
            or PaperExecutor(db_path=self.db.db_path)
        )

        self._lock = (
            threading.RLock()
        )

        self.daily_pnl = 0.0

        self._last_cycle_at: Optional[
            dt.datetime
        ] = None

    # ========================================================================
    # GOOGLE SHEETS
    # ========================================================================

    def _sync_google_sheet_holdings(self) -> None:
        """Mirror ASTRA intraday holdings into the dedicated Sheets tab.

        SQLite remains the authoritative state store. This method only
        publishes the current ASTRA-owned intraday holdings to the
        ``Intraday Bot Holdings`` worksheet and never writes broker holdings.
        """

        spreadsheet_id = _sheet_setting(
            "GOOGLE_SHEETS.SPREADSHEET_ID",
            "",
        )

        if not spreadsheet_id:
            logger.debug(
                "Intraday Google Sheets sync skipped: spreadsheet ID not configured."
            )
            return

        if gspread is None or Credentials is None:
            logger.warning(
                "Intraday Google Sheets sync skipped: gspread/google-auth unavailable."
            )
            return

        credentials_path = _sheet_credentials_path()

        if not credentials_path.exists():
            logger.debug(
                "Intraday Google Sheets sync skipped: credentials file not found: %s",
                credentials_path,
            )
            return

        try:
            credentials = Credentials.from_service_account_file(
                str(credentials_path),
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets",
                ],
            )

            client = gspread.authorize(credentials)
            spreadsheet = client.open_by_key(spreadsheet_id)

            try:
                worksheet = spreadsheet.worksheet(
                    INTRADAY_HOLDINGS_TAB
                )
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(
                    title=INTRADAY_HOLDINGS_TAB,
                    rows=max(100, len(INTRADAY_HOLDINGS_COLUMNS) + 10),
                    cols=len(INTRADAY_HOLDINGS_COLUMNS),
                )

            positions = self.ledger.positions()

            rows: list[list[Any]] = [
                list(INTRADAY_HOLDINGS_COLUMNS)
            ]

            for position in positions:
                invested_value = (
                    position.entry_price
                    * position.quantity
                )
                current_value = (
                    position.current_price
                    * position.quantity
                )

                rows.append([
                    _serialize_sheet_value(position.ticker),
                    _serialize_sheet_value(position.quantity),
                    _serialize_sheet_value(position.entry_price),
                    _serialize_sheet_value(position.current_price),
                    _serialize_sheet_value(invested_value),
                    _serialize_sheet_value(current_value),
                    _serialize_sheet_value(position.pnl),
                    _serialize_sheet_value(position.pnl_pct),
                    _serialize_sheet_value(position.stop_loss),
                    _serialize_sheet_value(position.trailing_stop),
                    _serialize_sheet_value(position.peak_price),
                    _serialize_sheet_value(position.session_date),
                    _serialize_sheet_value(position.status),
                    _serialize_sheet_value(position.owner),
                    _serialize_sheet_value(position.entry_order_id),
                    _serialize_sheet_value(position.exit_order_id),
                ])

            required_rows = max(
                len(rows),
                2,
            )
            required_cols = len(
                INTRADAY_HOLDINGS_COLUMNS
            )

            if worksheet.row_count < required_rows:
                worksheet.resize(
                    rows=required_rows,
                    cols=max(worksheet.col_count, required_cols),
                )

            if worksheet.col_count < required_cols:
                worksheet.resize(
                    rows=max(worksheet.row_count, required_rows),
                    cols=required_cols,
                )

            worksheet.clear()
            worksheet.update(
                range_name="A1",
                values=rows,
                raw=True,
            )

            try:
                worksheet.freeze(rows=1)
            except Exception:
                logger.debug(
                    "Unable to freeze Intraday Bot Holdings header.",
                    exc_info=True,
                )

            logger.debug(
                "Intraday holdings synced to Google Sheets | tab=%s | open_positions=%d",
                INTRADAY_HOLDINGS_TAB,
                len(positions),
            )

        except Exception:
            logger.error(
                "Failed to sync intraday holdings to Google Sheets.",
                exc_info=True,
            )


    # ========================================================================
    # SESSION CONTROL
    # ========================================================================

    def activate(
        self,
        session_date: Optional[str] = None,
    ) -> bool:

        now = dt.datetime.now(IST)

        if now.weekday() >= 5:
            logger.warning(
                "SMART INTRADAY ACTIVATION BLOCKED | market closed for weekend"
            )
            return False

        if not (
            MARKET_OPEN <= now.time() < MARKET_CLOSE
        ):
            logger.warning(
                "SMART INTRADAY ACTIVATION BLOCKED | "
                "market session closed | time=%s",
                now.strftime("%H:%M:%S"),
            )
            return False

        date_text = (
            session_date
            or now.date().isoformat()
        )

        expiry = dt.datetime.combine(
            now.date(),
            MARKET_CLOSE,
            tzinfo=IST,
        )

        self.gate.activate(
            date_text,
            expiry,
        )

        self._sync_google_sheet_holdings()

        logger.warning(
            "SMART INTRADAY ACTIVATED | mode=%s | backend=%s | date=%s",
            self.config.mode.upper(),
            self.config.execution_backend,
            date_text,
        )
        if self.config.mode == "trader":
            logger.warning(
                "TRADER MODE IS PAPER-ONLY: no live Angel One order executor "
                "is configured by intraday_bot.py."
            )

        return True

    def stop(
        self,
        reason: str = "MANUAL_STOP",
    ) -> None:

        self.gate.stop()
        self._sync_google_sheet_holdings()

        logger.warning(
            "SMART INTRADAY STOPPED | %s",
            reason,
        )

    def kill(self) -> None:

        self.gate.kill()

        logger.critical(
            "SMART INTRADAY KILLED"
        )

    # ========================================================================
    # MAIN CYCLE
    # ========================================================================

    def run_cycle(
        self,
    ) -> dict[str, Any]:

        with self._lock:

            now = dt.datetime.now(
                IST
            )

            self._last_cycle_at = now

            if not self.gate.allowed():

                return {
                    "status": "BLOCKED",
                    "reason": (
                        "SESSION_NOT_ACTIVE"
                    ),
                }

            if self.config.mode == "trader":
                # Paper mode uses virtual funds; never require or read the real wallet.
                try:
                    cash = float(self.executor.get_available_cash())
                    if not math.isfinite(cash) or cash < 0:
                        raise ValueError("invalid virtual cash")
                except Exception:
                    logger.exception("Paper cycle blocked: virtual account unavailable.")
                    return {"status": "BLOCKED", "reason": "PAPER_ACCOUNT_UNAVAILABLE"}
            else:
                wallet = self._wallet()
                if wallet is None:
                    logger.warning("Intraday advisory cycle blocked: Angel One wallet unavailable.")
                    return {"status": "BLOCKED", "reason": "WALLET_UNAVAILABLE"}
                try:
                    cash = float(wallet["available_cash"])
                    if not math.isfinite(cash) or cash < 0:
                        raise ValueError("invalid wallet cash")
                except (TypeError, ValueError, KeyError):
                    return {"status": "BLOCKED", "reason": "WALLET_INVALID"}

            exits = (
                self.manage_owned_positions()
            )

            entries = (
                self.scan_entries(
                    cash
                )
            )

            self._sync_google_sheet_holdings()

            return {
                "status": "OK",
                "entries": entries,
                "exits": exits,
                "open_positions": len(
                    self.ledger.positions()
                ),
                "daily_pnl": (
                    self.executor.session_pnl(now.date().isoformat())
                    if self.config.mode == "trader" and callable(getattr(self.executor, "session_pnl", None))
                    else self.daily_pnl
                ),
                "mode": self.config.mode,
                "execution_mode": self.config.execution_mode,
                "execution_backend": self.config.execution_backend,
                "paper_account": self.executor.snapshot() if self.config.mode == "trader" and callable(getattr(self.executor, "snapshot", None)) else None,
            }

    # ========================================================================
    # WALLET
    # ========================================================================

    def _wallet(
        self,
    ) -> Optional[
        dict[str, Any]
    ]:

        getter = getattr(
            self.client,
            "get_real_portfolio_data",
            None,
        )

        if not callable(
            getter
        ):

            logger.error(
                "Shared SmartAPI client does not expose "
                "get_real_portfolio_data()."
            )

            return None

        try:

            try:

                wallet = getter(
                    force_refresh=True
                )

            except TypeError:

                wallet = getter()

        except Exception:

            logger.error(
                "Angel One wallet refresh failed."
            )

            logger.debug(
                "Wallet refresh exception.",
                exc_info=True,
            )

            return None

        if not isinstance(
            wallet,
            dict,
        ):

            return None

        if (
            "available_cash"
            not in wallet
        ):

            return None

        if not isinstance(
            wallet.get(
                "holdings"
            ),
            list,
        ):

            return None

        try:

            cash = float(
                wallet[
                    "available_cash"
                ]
            )

        except (
            TypeError,
            ValueError,
        ):

            return None

        if not math.isfinite(
            cash
        ):

            return None

        if cash < 0:

            return None

        return wallet

    def _cash(
        self,
    ) -> Optional[float]:

        if self.config.mode == "trader":
            try:
                value = float(self.executor.get_available_cash())
                return value if math.isfinite(value) and value >= 0 else None
            except Exception:
                logger.exception("Unable to read ASTRA virtual paper balance.")
                return None

        wallet = self._wallet()

        if wallet is None:
            return None

        try:

            value = float(
                wallet[
                    "available_cash"
                ]
            )

        except (
            TypeError,
            ValueError,
            KeyError,
        ):

            return None

        if not math.isfinite(
            value
        ):

            return None

        return value

    def _execution_price(self, order_id: str, fallback: float) -> float:
        getter = getattr(self.executor, "get_fill_price", None)
        if callable(getter):
            try:
                fill = getter(order_id)
                if fill is not None and math.isfinite(float(fill)) and float(fill) > 0:
                    return float(fill)
            except Exception:
                logger.exception("Unable to read execution fill price for %s", order_id)
        return float(fallback)

    def _execution_order(self, order_id: str) -> dict[str, Any]:
        getter = getattr(self.executor, "get_order", None)
        if callable(getter):
            try:
                result = getter(order_id)
                if isinstance(result, dict):
                    return result
            except Exception:
                logger.exception("Unable to read execution order %s", order_id)
        return {}

    # ========================================================================
    # ENTRY SCAN
    # ========================================================================

    def scan_entries(
        self,
        cash: float,
    ) -> list[
        dict[str, Any]
    ]:

        if not self.gate.allowed():

            return []

        if self.config.mode == "trader":
            session_date = dt.datetime.now(IST).date().isoformat()
            loss_check = getattr(self.executor, "daily_loss_breached", None)
            if callable(loss_check):
                try:
                    if loss_check(self.config.max_daily_loss, session_date):
                        session_pnl = getattr(self.executor, "session_pnl", lambda _day: 0.0)(session_date)
                        logger.critical(
                            "PAPER ENTRIES HALTED | daily loss limit reached | session_pnl=%.2f | limit_pct=%.4f",
                            session_pnl,
                            self.config.max_daily_loss,
                        )
                        return []
                except Exception:
                    logger.exception("Unable to verify paper daily-loss limit; blocking new entries.")
                    return []

        if cash < 0:

            return []

        try:

            tickers = (
                get_dynamic_tickers(
                    limit=self.config.tickers_count
                )
            )

        except Exception:

            logger.error(
                "Unable to obtain intraday ticker universe."
            )

            logger.debug(
                "Ticker universe exception.",
                exc_info=True,
            )

            return []

        if not tickers:

            return []

        owned = {
            p.ticker
            for p in self.ledger.positions()
        }

        # Normalize to Yahoo symbols BEFORE constructing the lookup list.
        # Broker symbols such as ``MARUTI-EQ`` must never reach yfinance.
        yahoo_symbols: list[str] = []
        broker_symbols: list[str] = []
        seen_yahoo: set[str] = set()

        for ticker in tickers:

            try:

                broker_symbol = clean_ticker_symbol(
                    ticker
                )
                yahoo_symbol = self._yahoo(
                    broker_symbol
                )

            except Exception:

                continue

            if not broker_symbol or not yahoo_symbol:
                continue

            if broker_symbol in owned:
                continue

            if yahoo_symbol in seen_yahoo:
                continue

            seen_yahoo.add(yahoo_symbol)
            broker_symbols.append(broker_symbol)
            yahoo_symbols.append(yahoo_symbol)

        symbols = broker_symbols

        if not yahoo_symbols:

            return []

        data = (
            self._download_intraday_data(
                yahoo_symbols
            )
        )

        if data is None:

            return []

        results: list[
            dict[str, Any]
        ] = []

        multi = (
            len(yahoo_symbols)
            > 1
        )

        for broker_symbol, yahoo_symbol in zip(
            symbols,
            yahoo_symbols,
        ):

            try:

                df = self._extract(
                    data,
                    yahoo_symbol,
                    multi,
                )

                if (
                    df is None
                    or df.empty
                ):

                    logger.debug(
                        "No intraday data for %s.",
                        broker_symbol,
                    )

                    continue

                metrics = (
                    analyze_ticker_data(
                        df,
                        ticker=broker_symbol,
                    )
                )

                if not metrics:
                    continue

                price = float(
                    metrics.get(
                        "price",
                        0,
                    )
                )

                rsi = float(
                    metrics.get(
                        "rsi",
                        50,
                    )
                )

                signals = metrics.get(
                    "signals",
                    [],
                )

                if price <= 0:
                    continue

                # ---------------------------------------------------------
                # RSI
                # ---------------------------------------------------------

                if (
                    rsi
                    > self.config.rsi_lower
                ):

                    continue

                # ---------------------------------------------------------
                # MACD
                # ---------------------------------------------------------

                if (
                    "MACD_BULLISH_CROSS"
                    not in signals
                ):

                    continue

                quantity = self._size(
                    price,
                    cash,
                )

                if quantity <= 0:

                    continue

                session_date = (
                    dt.datetime.now(
                        IST
                    )
                    .date()
                    .isoformat()
                )

                stop_loss = (
                    price
                    * (
                        1
                        - self.config.stop_loss_pct
                    )
                )

                trailing_stop = (
                    price
                    * (
                        1
                        - self.config.trailing_stop_pct
                    )
                )

                reason = (
                    f"RSI={rsi:.2f}; "
                    "MACD_BULLISH_CROSS"
                )

                intent = TradeIntent(
                    action="BUY",
                    ticker=broker_symbol,
                    quantity=quantity,
                    price=price,
                    reason=reason,
                    stop_loss=stop_loss,
                    trailing_stop=trailing_stop,
                    session_date=session_date,
                )

                decision = (
                    self.risk.approve(
                        intent,
                        cash,
                    )
                )

                if not decision.approved:

                    logger.info(
                        "ENTRY BLOCKED | %s | %s",
                        broker_symbol,
                        decision.reason,
                    )

                    continue

                # ---------------------------------------------------------
                # ADVISORY MODE: RECOMMEND ONLY
                # ---------------------------------------------------------

                if self.config.mode == "advisory":
                    recommendation = {
                        "ticker": broker_symbol,
                        "action": "BUY_RECOMMENDATION",
                        "quantity": quantity,
                        "price": price,
                        "stop_loss": stop_loss,
                        "trailing_stop": trailing_stop,
                        "reason": reason,
                        "risk": decision.reason,
                        "mode": "advisory",
                    }
                    logger.info(
                        "ADVISORY BUY | %s | qty=%s | price=%.2f | SL=%.2f | TSL=%.2f | %s",
                        broker_symbol,
                        quantity,
                        price,
                        stop_loss,
                        trailing_stop,
                        reason,
                    )
                    results.append(recommendation)
                    continue

                # ---------------------------------------------------------
                # TRADER MODE: BUILT-IN EXECUTOR IS PAPER-ONLY
                # ---------------------------------------------------------

                order_id = self.executor.buy(intent)

                if not order_id:

                    results.append(
                        {
                            "ticker": broker_symbol,
                            "action": "BUY_SIGNAL",
                            "quantity": quantity,
                            "price": price,
                            "reason": reason,
                            "risk": decision.reason,
                        }
                    )

                    continue

                fill_price = self._execution_price(order_id, price)
                fill_stop_loss = fill_price * (1 - self.config.stop_loss_pct)
                fill_trailing_stop = fill_price * (1 - self.config.trailing_stop_pct)
                position = IntradayPosition(
                    ticker=broker_symbol,
                    quantity=quantity,
                    entry_price=fill_price,
                    current_price=price,
                    stop_loss=fill_stop_loss,
                    trailing_stop=fill_trailing_stop,
                    peak_price=price,
                    session_date=session_date,
                    entry_order_id=order_id,
                )

                if not self.ledger.record_entry(position):
                    rollback = getattr(self.executor, "rollback_order", None)
                    rolled_back = bool(rollback(order_id, "INTRADAY_LEDGER_ENTRY_FAILED")) if callable(rollback) else False
                    logger.error("Paper BUY ledger persistence failed for %s; simulator rollback=%s", broker_symbol, rolled_back)
                    results.append({
                        "ticker": broker_symbol, "action": "BUY_REJECTED", "quantity": quantity,
                        "reference_price": price, "fill_price": fill_price,
                        "reason": "LEDGER_PERSISTENCE_FAILED", "simulator_rollback": rolled_back,
                    })
                    continue

                self.ledger.record_trade(
                    ticker=broker_symbol,
                    action="BUY",
                    quantity=quantity,
                    price=fill_price,
                    order_id=order_id,
                    reason=reason,
                    status="PAPER",
                    session_date=session_date,
                )
                results.append({
                    "ticker": broker_symbol,
                    "action": "BUY_SIGNAL",
                    "execution_status": "PAPER_FILLED",
                    "quantity": quantity,
                    "reference_price": price,
                    "fill_price": fill_price,
                    "order_id": order_id,
                    "fees": self._execution_order(order_id).get("fees", 0.0),
                    "reason": reason,
                })

            except Exception:
                logger.error(
                    "Entry analysis failed for %s.",
                    broker_symbol,
                )

                logger.debug(
                    "Entry exception.",
                    exc_info=True,
                )

        return results

    # ========================================================================
    # POSITION MANAGEMENT
    # ========================================================================

    def manage_owned_positions(
        self,
    ) -> list[
        dict[str, Any]
    ]:

        positions = (
            self.ledger.positions()
        )

        if not positions:

            return []

        symbols = [
            self._yahoo(
                position.ticker
            )
            for position in positions
        ]

        data = (
            self._download_intraday_data(
                symbols
            )
        )

        if data is None:

            return []

        results: list[
            dict[str, Any]
        ] = []

        multi = (
            len(symbols)
            > 1
        )

        for position in positions:

            try:

                yahoo_symbol = (
                    self._yahoo(
                        position.ticker
                    )
                )

                df = self._extract(
                    data,
                    yahoo_symbol,
                    multi,
                )

                if (
                    df is None
                    or df.empty
                ):

                    continue

                metrics = (
                    analyze_ticker_data(
                        df,
                        ticker=position.ticker,
                    )
                )

                if not metrics:

                    continue

                current = float(
                    metrics.get(
                        "price",
                        position.current_price,
                    )
                )

                if current <= 0:

                    continue

                # ---------------------------------------------------------
                # PRICE STATE
                # ---------------------------------------------------------

                position.current_price = current
                mark_price = getattr(self.executor, "mark_price", None)
                if callable(mark_price) and self.config.mode == "trader":
                    mark_price(position.ticker, current)

                position.peak_price = max(
                    position.peak_price,
                    current,
                )

                # ---------------------------------------------------------
                # TRAILING STOP
                # ---------------------------------------------------------

                if self.config.enable_trailing_stop:

                    calculated_trailing = (
                        position.peak_price
                        * (
                            1
                            - self.config.trailing_stop_pct
                        )
                    )

                    position.trailing_stop = max(
                        position.trailing_stop,
                        calculated_trailing,
                    )

                # ---------------------------------------------------------
                # PERSIST PRICE STATE
                # ---------------------------------------------------------

                self.ledger.update(
                    position
                )

                rsi = float(
                    metrics.get(
                        "rsi",
                        50,
                    )
                )

                signals = metrics.get(
                    "signals",
                    [],
                )

                macd_bearish = (
                    "MACD_BEARISH_CROSS"
                    in signals
                )

                # ---------------------------------------------------------
                # DECISION ENGINE
                # ---------------------------------------------------------

                should_exit, reason, _ = (
                    evaluate_exit_signal(
                        current_price=current,
                        cost_price=position.entry_price,
                        highest_price=position.peak_price,
                        stop_loss_pct=self.config.stop_loss_pct,
                        trailing_stop_pct=self.config.trailing_stop_pct,
                        enable_tsl=self.config.enable_trailing_stop,
                        rsi_val=rsi,
                        macd_bearish=macd_bearish,
                    )
                )

                # ---------------------------------------------------------
                # HARD PRICE PROTECTION
                # ---------------------------------------------------------

                protection_floor = max(
                    position.stop_loss,
                    position.trailing_stop,
                )

                if (
                    protection_floor > 0
                    and current
                    <= protection_floor
                ):

                    should_exit = True

                    reason = (
                        "INTRADAY_STOP_OR_TRAILING_STOP"
                    )

                if not should_exit:

                    continue

                # ---------------------------------------------------------
                # SELL INTENT
                # ---------------------------------------------------------

                intent = TradeIntent(
                    action="SELL",
                    ticker=position.ticker,
                    quantity=position.quantity,
                    price=current,
                    reason=str(
                        reason
                    ),
                    stop_loss=position.stop_loss,
                    trailing_stop=position.trailing_stop,
                    session_date=position.session_date,
                )

                cash = self._cash()

                decision = (
                    self.risk.approve(
                        intent,
                        cash,
                    )
                )

                if not decision.approved:

                    logger.warning(
                        "EXIT BLOCKED | %s | %s",
                        position.ticker,
                        decision.reason,
                    )

                    continue

                if self.config.mode == "advisory":
                    recommendation = {
                        "ticker": position.ticker,
                        "action": "SELL_RECOMMENDATION",
                        "quantity": position.quantity,
                        "price": current,
                        "reason": str(reason),
                        "mode": "advisory",
                    }
                    logger.info(
                        "ADVISORY SELL | %s | qty=%s | price=%.2f | reason=%s",
                        position.ticker,
                        position.quantity,
                        current,
                        reason,
                    )
                    results.append(recommendation)
                    continue

                order_id = self.executor.sell(intent)

                if not order_id:

                    results.append(
                        {
                            "ticker": position.ticker,
                            "action": "SELL_SIGNAL",
                            "quantity": position.quantity,
                            "price": current,
                            "reason": str(
                                reason
                            ),
                        }
                    )

                    continue

                fill_price = self._execution_price(order_id, current)
                closed = self.ledger.close(position, fill_price, order_id, str(reason))
                if not closed:
                    rollback = getattr(self.executor, "rollback_order", None)
                    rolled_back = bool(rollback(order_id, "INTRADAY_LEDGER_EXIT_FAILED")) if callable(rollback) else False
                    logger.error("Paper SELL ledger close failed for %s; simulator rollback=%s", position.ticker, rolled_back)
                    results.append({
                        "ticker": position.ticker, "action": "SELL_RECONCILIATION_ERROR",
                        "quantity": position.quantity, "reference_price": current, "fill_price": fill_price,
                        "order_id": order_id, "ledger_closed": False, "simulator_rollback": rolled_back,
                    })
                    continue

                order_data = self._execution_order(order_id)
                gross_pnl = (fill_price - position.entry_price) * position.quantity
                pnl = float(order_data.get("realized_pnl_delta", gross_pnl))
                self.daily_pnl += pnl
                self.ledger.record_trade(
                    ticker=position.ticker, action="SELL", quantity=position.quantity, price=fill_price,
                    order_id=order_id, reason=str(reason), status="PAPER_FILLED", session_date=position.session_date,
                )
                results.append({
                    "ticker": position.ticker, "action": "PAPER_SELL_FILLED", "quantity": position.quantity,
                    "reference_price": current, "fill_price": fill_price, "order_id": order_id,
                    "fees": order_data.get("fees", 0.0), "reason": str(reason),
                    "net_realized_pnl": pnl, "ledger_closed": True,
                })

            except Exception:

                logger.error(
                    "Owned-position management failed for %s.",
                    position.ticker,
                )

                logger.debug(
                    "Position-management exception.",
                    exc_info=True,
                )

        return results

    # ========================================================================
    # DAILY EXPIRY
    # ========================================================================

    def expire(
        self,
    ) -> list[
        dict[str, Any]
    ]:
        """
        Generate expiry SELL actions for all ASTRA-owned positions.

        This is deliberately separate from run_cycle() because the regular
        session gate rejects times >= 15:30.

        No BUY can be generated through this path.
        """

        if not self.config.force_exit_enabled:

            self.stop(
                "DAILY_EXPIRY_DISABLED"
            )

            return []

        if not self.gate.expiry_allowed():

            logger.warning(
                "Intraday expiry requested outside the "
                "allowed expiry window."
            )

            return []

        results: list[
            dict[str, Any]
        ] = []

        positions = (
            self.ledger.positions()
        )

        if not positions:

            self._sync_google_sheet_holdings()
            self.stop(
                "DAILY_EXPIRY"
            )

            return results

        for position in positions:

            try:

                price = (
                    self._latest_price(
                        position.ticker
                    )
                )

                if price <= 0:

                    logger.critical(
                        "No valid expiry price for %s; "
                        "position remains open in ledger.",
                        position.ticker,
                    )

                    results.append(
                        {
                            "ticker": position.ticker,
                            "action": "EXPIRY_BLOCKED",
                            "reason": "NO_VALID_PRICE",
                        }
                    )

                    continue

                intent = TradeIntent(
                    action="SELL",
                    ticker=position.ticker,
                    quantity=position.quantity,
                    price=price,
                    reason="DAILY_EXPIRY",
                    stop_loss=position.stop_loss,
                    trailing_stop=position.trailing_stop,
                    session_date=position.session_date,
                )

                decision = (
                    self.risk.approve(
                        intent,
                        None,
                        expiry=True,
                    )
                )

                if not decision.approved:

                    logger.critical(
                        "Expiry SELL blocked | %s | %s",
                        position.ticker,
                        decision.reason,
                    )

                    results.append(
                        {
                            "ticker": position.ticker,
                            "action": "EXPIRY_BLOCKED",
                            "reason": decision.reason,
                        }
                    )

                    continue

                if self.config.mode == "advisory":
                    results.append(
                        {
                            "ticker": position.ticker,
                            "action": "EXPIRY_SELL_RECOMMENDATION",
                            "quantity": position.quantity,
                            "price": price,
                            "reason": "DAILY_EXPIRY",
                            "mode": "advisory",
                        }
                    )
                    logger.warning(
                        "ADVISORY EXPIRY SELL | %s | qty=%s | price=%.2f",
                        position.ticker,
                        position.quantity,
                        price,
                    )
                    continue

                order_id = self.executor.sell(intent)

                if not order_id:

                    results.append(
                        {
                            "ticker": position.ticker,
                            "action": "EXPIRY_SELL_SIGNAL",
                            "quantity": position.quantity,
                            "price": price,
                            "reason": "DAILY_EXPIRY",
                        }
                    )

                    continue

                fill_price = self._execution_price(order_id, price)
                closed = self.ledger.close(position, fill_price, order_id, "DAILY_EXPIRY")
                if not closed:
                    rollback = getattr(self.executor, "rollback_order", None)
                    rolled_back = bool(rollback(order_id, "INTRADAY_EXPIRY_LEDGER_FAILED")) if callable(rollback) else False
                    logger.critical("Expiry paper SELL ledger close failed for %s; simulator rollback=%s", position.ticker, rolled_back)
                    results.append({"ticker": position.ticker, "action": "EXPIRY_RECONCILIATION_ERROR",
                                    "order_id": order_id, "ledger_closed": False, "simulator_rollback": rolled_back})
                    continue

                order_data = self._execution_order(order_id)
                gross_pnl = (fill_price - position.entry_price) * position.quantity
                pnl = float(order_data.get("realized_pnl_delta", gross_pnl))
                self.daily_pnl += pnl
                self.ledger.record_trade(
                    ticker=position.ticker, action="SELL", quantity=position.quantity, price=fill_price,
                    order_id=order_id, reason="DAILY_EXPIRY", status="PAPER_FILLED", session_date=position.session_date,
                )
                results.append({"ticker": position.ticker, "action": "PAPER_EXPIRY_SELL_FILLED",
                                "quantity": position.quantity, "reference_price": price, "fill_price": fill_price,
                                "order_id": order_id, "reason": "DAILY_EXPIRY", "net_realized_pnl": pnl,
                                "ledger_closed": True})

            except Exception:

                logger.error(
                    "Expiry processing failed for %s.",
                    position.ticker,
                )

                logger.debug(
                    "Expiry exception.",
                    exc_info=True,
                )

        self._sync_google_sheet_holdings()

        self.stop(
            "DAILY_EXPIRY"
        )

        return results

    # ========================================================================
    # POSITION SIZING
    # ========================================================================

    def _size(
        self,
        price: float,
        cash: float,
    ) -> int:

        if price <= 0:
            return 0

        if cash <= 0:
            return 0

        if not math.isfinite(
            price
        ):

            return 0

        if not math.isfinite(
            cash
        ):

            return 0

        target = min(
            self.config.max_alloc,
            cash,
        )

        if (
            self.config.alloc_pct
            > 0
        ):

            target = min(
                target,
                cash
                * self.config.alloc_pct,
            )

        if (
            target
            < self.config.min_alloc
        ):

            return 0

        quantity = math.floor(
            target / price
        )

        if quantity <= 0:
            return 0

        allocation = (
            quantity
            * price
        )

        if (
            allocation
            < self.config.min_alloc
        ):

            return 0

        if (
            allocation
            > self.config.max_alloc
        ):

            return 0

        if allocation > cash:

            return 0

        if (
            self.config.alloc_pct
            > 0
            and allocation
            > cash
            * self.config.alloc_pct
        ):

            return 0

        return int(
            quantity
        )

    # ========================================================================
    # YAHOO SYMBOL CONVERSION
    # ========================================================================

    @staticmethod
    def _yahoo(
        ticker: str,
    ) -> str:
        """Return the cleaned Yahoo Finance symbol used for lookup."""

        return to_yahoo_ticker(
            clean_ticker_symbol(ticker)
        )

    # ========================================================================
    # MARKET DATA
    # ========================================================================

    def _download_intraday_data(
        self,
        symbols: list[str],
    ) -> Any:

        if not symbols:

            return None

        try:

            data = yf.download(
                symbols,
                period="5d",
                interval="5m",
                group_by="ticker",
                progress=False,
                auto_adjust=False,
                threads=True,
            )

        except Exception:

            logger.warning(
                "Intraday market-data download failed."
            )

            logger.debug(
                "yfinance exception.",
                exc_info=True,
            )

            return None

        if data is None:

            return None

        try:

            if data.empty:
                return None

        except Exception:

            return None

        return data

    @staticmethod
    def _extract(
        data: Any,
        ticker: str,
        multi: bool,
    ) -> Any:

        if data is None:

            return None

        try:

            if data.empty:

                return None

        except Exception:

            return None

        # ---------------------------------------------------------------
        # Single ticker
        # ---------------------------------------------------------------

        if not multi:

            try:

                if (
                    "Close"
                    not in data.columns
                ):

                    return None

                return data.dropna(
                    how="all"
                )

            except Exception:

                return None

        # ---------------------------------------------------------------
        # Multi ticker
        # ---------------------------------------------------------------

        try:

            columns = data.columns

            if hasattr(
                columns,
                "levels",
            ):

                level0 = (
                    columns
                    .get_level_values(
                        0
                    )
                )

                if ticker in level0:

                    df = (
                        data[ticker]
                        .dropna(
                            how="all"
                        )
                    )

                    if (
                        "Close"
                        in df.columns
                    ):

                        return df

                level1 = (
                    columns
                    .get_level_values(
                        1
                    )
                )

                if ticker in level1:

                    df = (
                        data
                        .xs(
                            ticker,
                            axis=1,
                            level=1,
                        )
                        .dropna(
                            how="all"
                        )
                    )

                    if (
                        "Close"
                        in df.columns
                    ):

                        return df

        except Exception:

            logger.debug(
                "Unable to extract %s from yfinance response.",
                ticker,
                exc_info=True,
            )

        return None

    # ========================================================================
    # LATEST PRICE
    # ========================================================================

    def _latest_price(
        self,
        ticker: str,
    ) -> float:

        symbol = self._yahoo(
            ticker
        )

        try:

            data = yf.download(
                symbol,
                period="1d",
                interval="5m",
                progress=False,
                auto_adjust=False,
                threads=False,
            )

            if (
                data is None
                or data.empty
            ):

                return 0.0

            if (
                "Close"
                not in data.columns
            ):

                return 0.0

            close = (
                data[
                    "Close"
                ]
                .dropna()
            )

            if close.empty:

                return 0.0

            value = float(
                close.iloc[-1]
            )

            if not math.isfinite(
                value
            ):

                return 0.0

            return value

        except Exception:

            logger.warning(
                "Latest price lookup failed for %s.",
                ticker,
            )

            logger.debug(
                "Latest-price exception.",
                exc_info=True,
            )

            return 0.0

    # ========================================================================
    # STATUS
    # ========================================================================

    def status(
        self,
    ) -> dict[str, Any]:

        positions = (
            self.ledger.positions()
        )

        return {
            "active": self.gate.active,
            "killed": self.gate.killed,
            "session_date": (
                self.gate.session_date
            ),
            "mode": self.config.mode,
            "execution_mode": (
                self.config.execution_mode
            ),
            "execution_backend": self.config.execution_backend,
            "live_requested": (
                self.config.live_trading_requested
            ),
            "open_positions": len(
                positions
            ),
            "daily_pnl": (
                self.executor.session_pnl(dt.datetime.now(IST).date().isoformat())
                if self.config.mode == "trader" and callable(getattr(self.executor, "session_pnl", None))
                else self.daily_pnl
            ),
            "positions": [
                {
                    "ticker": p.ticker,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "current_price": p.current_price,
                    "stop_loss": p.stop_loss,
                    "trailing_stop": p.trailing_stop,
                    "peak_price": p.peak_price,
                    "pnl": p.pnl,
                    "pnl_pct": p.pnl_pct,
                    "session_date": p.session_date,
                }
                for p in positions
            ],
            "config": self.config.summary(),
            "paper_account": self.executor.snapshot() if self.config.mode == "trader" and callable(getattr(self.executor, "snapshot", None)) else None,
        }


# ============================================================================
# SHARED BOT INSTANCE
# ============================================================================

_BOT: Optional[
    SmartIntradayBot
] = None

_BOT_LOCK = (
    threading.RLock()
)


def get_smart_intraday_bot(
    client: Any,
    db: Optional[
        DatabaseManager
    ] = None,
) -> SmartIntradayBot:
    """
    Return the process-wide intraday bot.

    This prevents Telegram and main.py from accidentally creating separate
    intraday state machines.
    """

    global _BOT

    with _BOT_LOCK:

        if (
            _BOT is None
            or _BOT.client is not client
        ):

            _BOT = SmartIntradayBot(
                client=client,
                db=db,
            )

        return _BOT


def activate_smart_intraday(
    client: Any,
    db: Optional[DatabaseManager] = None,
    session_date: Optional[str] = None,
) -> Optional[SmartIntradayBot]:

    bot = get_smart_intraday_bot(
        client,
        db,
    )

    if not bot.activate(session_date):
        return None

    return bot


def stop_smart_intraday(
    reason: str = "MANUAL_STOP",
) -> None:

    with _BOT_LOCK:

        if _BOT is not None:

            _BOT.stop(
                reason
            )


def kill_smart_intraday() -> None:

    with _BOT_LOCK:

        if _BOT is not None:

            _BOT.kill()


def get_intraday_status(
) -> dict[str, Any]:

    with _BOT_LOCK:

        if _BOT is None:

            return {
                "active": False,
                "killed": False,
                "mode": "advisory",
                "execution_mode": "ADVISORY",
                "execution_backend": "NONE",
                "open_positions": 0,
                "daily_pnl": 0.0,
                "positions": [],
            }

        return _BOT.status()

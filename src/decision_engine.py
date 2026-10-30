"""
ASTRA Decision Engine
=====================

Pure market-analysis and trading-decision layer.

Responsibilities
----------------
- RSI calculation
- MACD calculation
- Technical signal detection
- Buy/sell decision generation
- Stop-loss / trailing-stop evaluation
- DataFrame normalization for yfinance
- Signal persistence to SQLite

IMPORTANT
---------
This module MUST NOT place broker orders.

Normal ASTRA:
    Decision Engine -> Advisory signal

Intraday ASTRA:
    Decision Engine -> Risk Gateway -> Intraday Bot -> Broker

The decision engine is intentionally unaware of Angel One order
execution so that advisory signals can never accidentally submit
broker orders.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from mng_db import DatabaseManager


logger = logging.getLogger("ASTRA_DECISION_ENGINE")

# Shared database manager for signal telemetry.
db = DatabaseManager()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_RSI_PERIOD = 14

DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9

DEFAULT_RSI_OVERSOLD = 30.0
DEFAULT_RSI_OVERBOUGHT = 70.0

DEFAULT_EXIT_RSI_THRESHOLD = 65.0

MIN_ANALYSIS_CANDLES = 35


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to float."""

    try:
        result = float(value)

        if not np.isfinite(result):
            return default

        return result

    except (TypeError, ValueError):
        return default


def _clean_price_array(prices: Any) -> np.ndarray:
    """
    Convert arbitrary price input into a clean 1-D float array.

    NaN and infinite values are removed.
    """

    try:
        array = np.asarray(prices, dtype=float).flatten()
    except (TypeError, ValueError):
        return np.asarray([], dtype=float)

    if array.size == 0:
        return array

    return array[np.isfinite(array)]


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

def calculate_rsi(
    prices: np.ndarray,
    period: int = DEFAULT_RSI_PERIOD,
) -> float:
    """
    Calculate RSI using Wilder's smoothing method.

    Returns
    -------
    float
        RSI value in the range 0-100.

    Notes
    -----
    If insufficient data is available, 50.0 is returned instead of
    generating a false oversold/overbought signal.
    """

    prices = _clean_price_array(prices)

    if period <= 0:
        raise ValueError("RSI period must be greater than zero")

    if len(prices) <= period:
        return 50.0

    deltas = np.diff(prices)

    if len(deltas) < period:
        return 50.0

    seed = deltas[:period]

    gains = np.where(seed > 0, seed, 0.0)
    losses = np.where(seed < 0, -seed, 0.0)

    average_gain = float(gains.sum() / period)
    average_loss = float(losses.sum() / period)

    # No losses means maximum RSI.
    # Seed the Wilder-smoothed RSI. Do not return early when the seed has
    # zero losses: later candles may introduce losses and change the RSI.
    if average_loss == 0.0:
        if average_gain == 0.0:
            rsi_seed = 50.0
        else:
            rsi_seed = 100.0
    else:
        rs = average_gain / average_loss
        rsi_seed = 100.0 - (100.0 / (1.0 + rs))

    rsi = np.full(len(deltas) + 1, 50.0, dtype=float)
    rsi[period] = rsi_seed

    for index in range(period, len(deltas)):
        delta = deltas[index]

        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0

        average_gain = (
            (average_gain * (period - 1)) + gain
        ) / period

        average_loss = (
            (average_loss * (period - 1)) + loss
        ) / period

        if average_loss == 0.0:
            current_rsi = 100.0 if average_gain > 0 else 50.0
        else:
            rs = average_gain / average_loss
            current_rsi = 100.0 - (100.0 / (1.0 + rs))

        rsi[index + 1] = current_rsi

    return float(np.clip(rsi[-1], 0.0, 100.0))


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def calculate_macd(
    prices: np.ndarray,
    fast: int = DEFAULT_MACD_FAST,
    slow: int = DEFAULT_MACD_SLOW,
    signal: int = DEFAULT_MACD_SIGNAL,
) -> Tuple[float, float, float, float]:
    """
    Calculate MACD and the previous MACD values.

    Returns
    -------
    tuple
        (
            current_macd,
            current_signal,
            previous_macd,
            previous_signal
        )

    Previous/current values are used for crossover detection.
    """

    prices = _clean_price_array(prices)

    if fast <= 0 or slow <= 0 or signal <= 0:
        raise ValueError("MACD periods must be greater than zero")

    if fast >= slow:
        raise ValueError("MACD fast period must be smaller than slow period")

    minimum_required = slow + signal

    if len(prices) < minimum_required:
        return 0.0, 0.0, 0.0, 0.0

    series = pd.Series(prices, dtype=float)

    ema_fast = series.ewm(
        span=fast,
        adjust=False,
        min_periods=0,
    ).mean()

    ema_slow = series.ewm(
        span=slow,
        adjust=False,
        min_periods=0,
    ).mean()

    macd_line = ema_fast - ema_slow

    signal_line = macd_line.ewm(
        span=signal,
        adjust=False,
        min_periods=0,
    ).mean()

    return (
        float(macd_line.iloc[-1]),
        float(signal_line.iloc[-1]),
        float(macd_line.iloc[-2]),
        float(signal_line.iloc[-2]),
    )


# ---------------------------------------------------------------------------
# DataFrame extraction
# ---------------------------------------------------------------------------

def extract_ticker_df(
    data: pd.DataFrame,
    ticker: str,
    is_multi: bool = False,
) -> Optional[pd.DataFrame]:
    """
    Extract one ticker's OHLCV DataFrame from yfinance output.

    Handles:

    1. Normal single-ticker columns:
        Open, High, Low, Close, Volume

    2. MultiIndex columns produced when downloading multiple tickers.

    Parameters
    ----------
    data:
        yfinance DataFrame.

    ticker:
        Requested ticker symbol.

    is_multi:
        Whether the source DataFrame is expected to contain MultiIndex
        columns.
    """

    if data is None or data.empty:
        return None

    try:
        result = data

        if isinstance(data.columns, pd.MultiIndex):
            levels = data.columns

            # Try ticker-first layout:
            # ('WIPRO.NS', 'Close')
            try:
                if ticker in levels.get_level_values(0):
                    result = data[ticker]
                elif ticker in levels.get_level_values(1):
                    result = data.xs(ticker, axis=1, level=1)
                else:
                    # If only one ticker exists, use it.
                    unique_first = list(
                        dict.fromkeys(levels.get_level_values(0))
                    )

                    unique_second = list(
                        dict.fromkeys(levels.get_level_values(1))
                    )

                    if len(unique_first) == 1:
                        result = data[unique_first[0]]
                    elif len(unique_second) == 1:
                        result = data.xs(
                            unique_second[0],
                            axis=1,
                            level=1,
                        )
                    else:
                        return None

            except (KeyError, IndexError):
                return None

        elif is_multi:
            # is_multi was explicitly requested but the DataFrame is not
            # MultiIndex. Continue using the supplied DataFrame rather
            # than failing unnecessarily.
            result = data

        # Flatten single-column MultiIndex remnants if present.
        if isinstance(result.columns, pd.MultiIndex):
            try:
                result.columns = [
                    col[-1] if isinstance(col, tuple) else col
                    for col in result.columns
                ]
            except Exception:
                pass

        # Normalize column names.
        rename_map = {}

        for column in result.columns:
            normalized = str(column).strip().lower()

            if normalized == "close":
                rename_map[column] = "Close"
            elif normalized == "open":
                rename_map[column] = "Open"
            elif normalized == "high":
                rename_map[column] = "High"
            elif normalized == "low":
                rename_map[column] = "Low"
            elif normalized == "volume":
                rename_map[column] = "Volume"

        result = result.rename(columns=rename_map)

        if "Close" not in result.columns:
            return None

        return result

    except Exception as exc:
        logger.error(
            "Failed to extract ticker DataFrame for %s: %s",
            ticker,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

def calculate_dynamic_allocation(
    available_cash: float,
    alloc_pct: float,
    min_alloc: float,
    max_alloc: float,
) -> float:
    """
    Calculate a trade allocation using three bounds.

    Bound 1:
        Portfolio allocation percentage.

    Bound 2:
        Configured minimum/maximum allocation.

    Bound 3:
        Actual available cash.

    The result is never greater than available cash.
    """

    available_cash = max(_safe_float(available_cash), 0.0)
    alloc_pct = max(_safe_float(alloc_pct), 0.0)
    min_alloc = max(_safe_float(min_alloc), 0.0)
    max_alloc = max(_safe_float(max_alloc), 0.0)

    if available_cash <= 0:
        return 0.0

    if max_alloc > 0 and min_alloc > max_alloc:
        min_alloc, max_alloc = max_alloc, min_alloc

    percentage_allocation = available_cash * alloc_pct

    allocation = percentage_allocation

    if min_alloc > 0:
        allocation = max(allocation, min_alloc)

    if max_alloc > 0:
        allocation = min(allocation, max_alloc)

    allocation = min(allocation, available_cash)

    return round(max(allocation, 0.0), 2)


# ---------------------------------------------------------------------------
# Buy decision
# ---------------------------------------------------------------------------

def evaluate_buy_signal(
    metrics: Dict[str, Any],
    available_cash: float,
    min_alloc: float,
    max_alloc: float,
    alloc_pct: float,
    rsi_lower: float = 35.0,
) -> Optional[Tuple[str, float, str]]:
    """
    Evaluate whether the supplied metrics justify a BUY recommendation.

    This function ONLY produces a decision.

    It never:
        - authenticates with Angel One
        - submits an order
        - modifies holdings
        - executes a trade

    Returns
    -------
    Optional[Tuple[str, float, str]]
        (
            action,
            allocation,
            reasoning
        )

        None means no buy signal.
    """

    if not metrics:
        return None

    price = _safe_float(metrics.get("price"))
    rsi = _safe_float(metrics.get("rsi"), 50.0)

    signals = set(metrics.get("signals") or [])

    if price <= 0:
        return None

    available_cash = max(
        _safe_float(available_cash),
        0.0,
    )

    if available_cash < price:
        return None

    # Keep compatibility with ASTRA's existing capital-safety rule:
    # a single share must itself be affordable within the configured
    # allocation range.
    if min_alloc > 0 and price < min_alloc:
        return None

    if max_alloc > 0 and price > max_alloc:
        return None

    allocation = calculate_dynamic_allocation(
        available_cash=available_cash,
        alloc_pct=alloc_pct,
        min_alloc=min_alloc,
        max_alloc=max_alloc,
    )

    if allocation < price:
        return None

    macd_bullish = "MACD_BULLISH_CROSS" in signals

    # ---------------------------------------------------------------
    # Intraday / short-term setup
    # ---------------------------------------------------------------

    if rsi <= rsi_lower and macd_bullish:
        reasoning = (
            f"RSI oversold ({rsi:.2f}) and bullish MACD crossover "
            f"detected. Allocation ₹{allocation:.2f}."
        )

        return (
            "BUY (INTRA-DAY / SHORT-TERM)",
            allocation,
            reasoning,
        )

    # ---------------------------------------------------------------
    # Long-term oversold setup
    # ---------------------------------------------------------------

    if rsi <= rsi_lower:
        reasoning = (
            f"RSI is below the configured lower threshold "
            f"({rsi:.2f} <= {rsi_lower:.2f}). "
            f"Allocation ₹{allocation:.2f}."
        )

        return (
            "BUY (LONG-TERM SIP)",
            allocation,
            reasoning,
        )

    # ---------------------------------------------------------------
    # Moderate short-term setup
    # ---------------------------------------------------------------

    if rsi < 45.0 and macd_bullish:
        reasoning = (
            f"RSI indicates improving momentum ({rsi:.2f}) and "
            f"a bullish MACD crossover was detected. "
            f"Allocation ₹{allocation:.2f}."
        )

        return (
            "BUY (SHORT-TERM)",
            allocation,
            reasoning,
        )

    return None


# ---------------------------------------------------------------------------
# Exit / stop-loss engine
# ---------------------------------------------------------------------------

def evaluate_exit_signal(
    current_price: float,
    cost_price: float,
    highest_price: Optional[float] = None,
    stop_loss_pct: float = 0.05,
    trailing_stop_pct: float = 0.04,
    enable_tsl: bool = True,
    rsi_val: Optional[float] = None,
    macd_bearish: bool = False,
) -> Tuple[bool, str, float]:
    """
    Evaluate an existing position for exit.

    Exit hierarchy:

    1. Hard stop-loss
    2. Dynamic trailing stop-loss
    3. RSI + bearish MACD profit-taking

    The trailing stop is calculated from the highest observed price.

    Returns
    -------
    Tuple[bool, str, float]
        (
            should_sell,
            reason,
            effective_stop_price
        )
    """

    current_price = _safe_float(current_price)
    cost_price = _safe_float(cost_price)

    if current_price <= 0 or cost_price <= 0:
        return False, "INVALID_PRICING", 0.0

    stop_loss_pct = max(
        min(_safe_float(stop_loss_pct), 1.0),
        0.0,
    )

    trailing_stop_pct = max(
        min(_safe_float(trailing_stop_pct), 1.0),
        0.0,
    )

    supplied_peak = (
        _safe_float(highest_price)
        if highest_price is not None
        else cost_price
    )

    # The current price can establish a new peak.
    peak_price = max(
        cost_price,
        supplied_peak,
        current_price,
    )

    hard_stop_price = cost_price * (
        1.0 - stop_loss_pct
    )

    if enable_tsl:
        tsl_floor_price = peak_price * (
            1.0 - trailing_stop_pct
        )

        # Never allow the trailing stop to weaken the hard stop.
        effective_stop_price = max(
            hard_stop_price,
            tsl_floor_price,
        )
    else:
        effective_stop_price = hard_stop_price

    # ---------------------------------------------------------------
    # 1. Hard stop / trailing stop
    # ---------------------------------------------------------------

    if current_price <= effective_stop_price:

        if (
            enable_tsl
            and effective_stop_price > hard_stop_price
        ):
            reason = (
                "TSL_TRIGGERED "
                f"(Peak: ₹{peak_price:.2f} -> "
                f"Floor: ₹{effective_stop_price:.2f})"
            )
        else:
            reason = (
                "HARD_STOP_LOSS_HIT "
                f"(Cost: ₹{cost_price:.2f} -> "
                f"Floor: ₹{effective_stop_price:.2f})"
            )

        return (
            True,
            reason,
            round(effective_stop_price, 2),
        )

    # ---------------------------------------------------------------
    # 2. Technical profit-taking
    # ---------------------------------------------------------------

    if (
        rsi_val is not None
        and _safe_float(rsi_val) >= DEFAULT_EXIT_RSI_THRESHOLD
        and macd_bearish
    ):
        reason = (
            "PROFIT_TAKING_TECHNICAL "
            f"(RSI: {_safe_float(rsi_val):.1f}, "
            "Bearish MACD Cross)"
        )

        return (
            True,
            reason,
            round(effective_stop_price, 2),
        )

    return (
        False,
        "HOLD",
        round(effective_stop_price, 2),
    )


# ---------------------------------------------------------------------------
# Sell decision
# ---------------------------------------------------------------------------

def evaluate_sell_signal(
    holding: Dict[str, Any],
    metrics: Dict[str, Any],
    rsi_upper: float = 65.0,
    credentials_file: Optional[str] = None,
    spreadsheet_id: Optional[str] = None,
    stop_loss_pct: float = 0.05,
    trailing_stop_pct: float = 0.04,
    enable_tsl: bool = True,
) -> Optional[Tuple[str, float, str]]:
    """
    Evaluate an existing holding for a SELL recommendation.

    The Google Sheets parameters are retained for backward compatibility.
    The decision engine itself does not require Google Sheets access.

    Parameters
    ----------
    holding:
        Existing holding dictionary.

    metrics:
        Current technical metrics.

    Returns
    -------
    Optional[Tuple[str, float, str]]
        (
            action,
            pnl_pct,
            reasoning
        )

        None means HOLD.
    """

    if not holding or not metrics:
        return None

    ticker = str(
        holding.get("ticker")
        or holding.get("symbol")
        or ""
    ).strip()

    current_price = _safe_float(
        metrics.get("price")
    )

    rsi_val = _safe_float(
        metrics.get("rsi"),
        50.0,
    )

    signals = set(
        metrics.get("signals") or []
    )

    if current_price <= 0:
        return None

    # ---------------------------------------------------------------
    # Determine cost basis
    # ---------------------------------------------------------------

    cost_price = _safe_float(
        holding.get("avg_price")
        or holding.get("average_price")
        or holding.get("cost_price")
        or holding.get("entry_price")
    )

    if cost_price <= 0 and ticker:
        # Compatibility with older ASTRA DB implementations.
        try:
            if hasattr(db, "get_highest_price"):
                # This is intentionally NOT used as cost basis.
                # It only exists as a compatibility fallback path.
                pass
        except Exception:
            pass

    if cost_price <= 0:
        logger.warning(
            "Cannot evaluate SELL for %s: invalid cost price",
            ticker or "<unknown>",
        )
        return None

    # ---------------------------------------------------------------
    # Determine highest observed price
    # ---------------------------------------------------------------

    stored_peak = _safe_float(
        holding.get("peak_price"),
        cost_price,
    )

    # Compatibility with older DB schema where peak price was stored
    # separately.
    if ticker and hasattr(db, "get_highest_price"):
        try:
            database_peak = db.get_highest_price(ticker)

            if database_peak is not None:
                stored_peak = max(
                    stored_peak,
                    _safe_float(
                        database_peak,
                        cost_price,
                    ),
                )

        except Exception as exc:
            logger.debug(
                "Could not retrieve stored peak for %s: %s",
                ticker,
                exc,
            )

    current_peak = max(
        cost_price,
        stored_peak,
        current_price,
    )

    # Persist the new peak when supported.
    if ticker and hasattr(db, "update_highest_price"):
        try:
            db.update_highest_price(
                ticker,
                current_peak,
            )
        except Exception as exc:
            logger.debug(
                "Could not persist peak for %s: %s",
                ticker,
                exc,
            )

    # ---------------------------------------------------------------
    # Technical exit
    # ---------------------------------------------------------------

    macd_bearish = (
        "MACD_BEARISH_CROSS" in signals
        or "RSI_OVERBOUGHT" in signals
        or rsi_val >= rsi_upper
    )

    should_sell, reason, stop_price = evaluate_exit_signal(
        current_price=current_price,
        cost_price=cost_price,
        highest_price=current_peak,
        stop_loss_pct=stop_loss_pct,
        trailing_stop_pct=trailing_stop_pct,
        enable_tsl=enable_tsl,
        rsi_val=rsi_val,
        macd_bearish=macd_bearish,
    )

    if not should_sell:
        return None

    pnl_pct = (
        (current_price - cost_price)
        / cost_price
    )

    reasoning = (
        f"{reason}. "
        f"Entry: ₹{cost_price:.2f}, "
        f"Current: ₹{current_price:.2f}, "
        f"Peak: ₹{current_peak:.2f}, "
        f"Stop: ₹{stop_price:.2f}, "
        f"P&L: {pnl_pct * 100:.2f}%."
    )

    return (
        "SELL (EXIT / TAKE PROFIT)",
        pnl_pct,
        reasoning,
    )


# ---------------------------------------------------------------------------
# Technical analysis
# ---------------------------------------------------------------------------

def analyze_ticker_data(
    df: pd.DataFrame,
    ticker: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Analyze a ticker's historical/intraday price data.

    Indicators:
        - RSI
        - MACD
        - MACD bullish crossover
        - MACD bearish crossover

    The function may persist detected signals to SQLite for telemetry.

    It NEVER places a trade.
    """

    if df is None or df.empty:
        return None

    if "Close" not in df.columns:
        return None

    try:
        closes = pd.to_numeric(
            df["Close"],
            errors="coerce",
        ).dropna().to_numpy(
            dtype=float
        )
    except Exception as exc:
        logger.error(
            "Unable to extract close prices for %s: %s",
            ticker or "<unknown>",
            exc,
        )
        return None

    if len(closes) < MIN_ANALYSIS_CANDLES:
        return None

    if not np.all(np.isfinite(closes)):
        closes = closes[np.isfinite(closes)]

    if len(closes) < MIN_ANALYSIS_CANDLES:
        return None

    current_price = float(closes[-1])

    if current_price <= 0:
        return None

    rsi_val = calculate_rsi(closes)

    (
        macd_current,
        signal_current,
        macd_previous,
        signal_previous,
    ) = calculate_macd(closes)

    signals = []

    # ---------------------------------------------------------------
    # RSI
    # ---------------------------------------------------------------

    if rsi_val <= DEFAULT_RSI_OVERSOLD:
        signals.append("RSI_OVERSOLD")

    elif rsi_val >= DEFAULT_RSI_OVERBOUGHT:
        signals.append("RSI_OVERBOUGHT")

    # ---------------------------------------------------------------
    # MACD crossover
    # ---------------------------------------------------------------

    bullish_cross = (
        macd_previous < signal_previous
        and macd_current >= signal_current
    )

    bearish_cross = (
        macd_previous > signal_previous
        and macd_current <= signal_current
    )

    if bullish_cross:
        signals.append("MACD_BULLISH_CROSS")

    elif bearish_cross:
        signals.append("MACD_BEARISH_CROSS")

    # ---------------------------------------------------------------
    # Signal telemetry
    # ---------------------------------------------------------------

    if ticker and signals:
        for signal in signals:
            try:
                if (
                    "BULLISH" in signal
                    or "OVERSOLD" in signal
                ):
                    action = "BUY"
                else:
                    action = "SELL"

                db.log_signal(
                    ticker=ticker,
                    strategy=signal,
                    price=round(current_price, 2),
                    rsi=round(rsi_val, 2),
                    macd=round(macd_current, 2),
                    action=action,
                )

            except Exception as exc:
                # Signal logging must never prevent analysis.
                logger.error(
                    "Failed to log signal for %s: %s",
                    ticker,
                    exc,
                )

    return {
        "price": round(current_price, 2),
        "rsi": round(rsi_val, 2),
        "macd": round(macd_current, 2),
        "macd_signal": round(signal_current, 2),
        "macd_previous": round(macd_previous, 2),
        "macd_signal_previous": round(
            signal_previous,
            2,
        ),
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# Signal classification helpers
# ---------------------------------------------------------------------------

def has_bullish_signal(
    metrics: Optional[Dict[str, Any]],
) -> bool:
    """Return True if the metrics contain a bullish MACD crossover."""

    if not metrics:
        return False

    return (
        "MACD_BULLISH_CROSS"
        in set(metrics.get("signals") or [])
    )


def has_bearish_signal(
    metrics: Optional[Dict[str, Any]],
) -> bool:
    """Return True if the metrics contain a bearish MACD crossover."""

    if not metrics:
        return False

    signals = set(
        metrics.get("signals") or []
    )

    return (
        "MACD_BEARISH_CROSS" in signals
        or "RSI_OVERBOUGHT" in signals
    )


def is_oversold(
    metrics: Optional[Dict[str, Any]],
    threshold: float = DEFAULT_RSI_OVERSOLD,
) -> bool:
    """Return True when RSI is at/below the supplied threshold."""

    if not metrics:
        return False

    return (
        _safe_float(metrics.get("rsi"), 50.0)
        <= threshold
    )


def is_overbought(
    metrics: Optional[Dict[str, Any]],
    threshold: float = DEFAULT_RSI_OVERBOUGHT,
) -> bool:
    """Return True when RSI is at/above the supplied threshold."""

    if not metrics:
        return False

    return (
        _safe_float(metrics.get("rsi"), 50.0)
        >= threshold
    )


# ---------------------------------------------------------------------------
# Trailing-stop helper
# ---------------------------------------------------------------------------

def calculate_trailing_stop(
    cost_price: float,
    current_price: float,
    previous_peak: Optional[float],
    trailing_stop_pct: float,
    hard_stop_pct: float,
) -> Tuple[float, float]:
    """
    Calculate the current peak and effective stop.

    Returns
    -------
    Tuple[float, float]
        (
            peak_price,
            effective_stop_price
        )

    The peak is monotonic:

        new_peak = max(old_peak, cost, current)

    Therefore the trailing stop can never move downward because
    the market temporarily falls.
    """

    cost_price = _safe_float(cost_price)
    current_price = _safe_float(current_price)

    if cost_price <= 0 or current_price <= 0:
        return 0.0, 0.0

    previous_peak = _safe_float(
        previous_peak,
        cost_price,
    )

    trailing_stop_pct = max(
        min(_safe_float(trailing_stop_pct), 1.0),
        0.0,
    )

    hard_stop_pct = max(
        min(_safe_float(hard_stop_pct), 1.0),
        0.0,
    )

    peak_price = max(
        cost_price,
        previous_peak,
        current_price,
    )

    hard_stop = cost_price * (
        1.0 - hard_stop_pct
    )

    trailing_stop = peak_price * (
        1.0 - trailing_stop_pct
    )

    effective_stop = max(
        hard_stop,
        trailing_stop,
    )

    return (
        round(peak_price, 2),
        round(effective_stop, 2),
    )


# ---------------------------------------------------------------------------
# Module diagnostics
# ---------------------------------------------------------------------------

def decision_engine_health() -> Dict[str, Any]:
    """
    Return lightweight diagnostic information.

    No broker/network operations are performed.
    """

    return {
        "module": "decision_engine",
        "status": "READY",
        "execution_boundary": "ANALYSIS_ONLY",
        "places_broker_orders": False,
        "supports_live_execution": False,
        "rsi_period": DEFAULT_RSI_PERIOD,
        "macd": {
            "fast": DEFAULT_MACD_FAST,
            "slow": DEFAULT_MACD_SLOW,
            "signal": DEFAULT_MACD_SIGNAL,
        },
        "rsi_oversold": DEFAULT_RSI_OVERSOLD,
        "rsi_overbought": DEFAULT_RSI_OVERBOUGHT,
        "min_analysis_candles": MIN_ANALYSIS_CANDLES,
        "broker_execution": False,
    }

import logging
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, Tuple
from mng_db import DatabaseManager

logger = logging.getLogger("ASTRA_DECISION_ENGINE")
db = DatabaseManager()


def calculate_rsi(prices: np.ndarray, period: int = 14) -> float:
    """Calculates the Relative Strength Index (RSI) using Wilder's Smoothing Method."""
    if len(prices) <= period:
        return 50.0

    deltas = np.diff(prices)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period

    if down == 0:
        return 100.0

    rs = up / down
    rsi = np.zeros(len(prices))
    rsi[:period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period, len(deltas)):
        delta = deltas[i]
        upval = delta if delta > 0 else 0.0
        downval = -delta if delta < 0 else 0.0

        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period

        rs = up / (down if down != 0 else 1e-10)
        rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return float(rsi[-1])


def calculate_macd(prices: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[float, float, float, float]:
    """Calculates MACD, Signal line, and previous step values for crossover detection."""
    if len(prices) < slow + signal:
        return 0.0, 0.0, 0.0, 0.0

    df_series = pd.Series(prices)
    ema_fast = df_series.ewm(span=fast, adjust=False).mean().to_numpy()
    ema_slow = df_series.ewm(span=slow, adjust=False).mean().to_numpy()
    
    macd_line = ema_fast - ema_slow
    signal_line = pd.Series(macd_line).ewm(span=signal, adjust=False).mean().to_numpy()

    return float(macd_line[-1]), float(signal_line[-1]), float(macd_line[-2]), float(signal_line[-2])


def evaluate_exit_signal(
    current_price: float,
    cost_price: float,
    highest_price: Optional[float] = None,
    stop_loss_pct: float = 0.05,
    trailing_stop_pct: float = 0.04,
    enable_tsl: bool = True,
    rsi_val: Optional[float] = None,
    macd_bearish: bool = False
) -> Tuple[bool, str, float]:
    """
    Evaluates whether an active stock holding triggers an exit signal based on
    hard stop-loss, dynamic trailing stop-loss (TSL), or technical indicators.
    
    Returns:
        (should_sell: bool, reason: str, dynamic_stop_price: float)
    """
    if cost_price <= 0 or current_price <= 0:
        return False, "INVALID_PRICING", 0.0

    peak_price = max(cost_price, highest_price or cost_price, current_price)
    hard_stop_price = cost_price * (1.0 - stop_loss_pct)

    if enable_tsl:
        tsl_floor_price = peak_price * (1.0 - trailing_stop_pct)
        effective_stop_price = max(hard_stop_price, tsl_floor_price)
    else:
        effective_stop_price = hard_stop_price

    # 1. Stop Loss / TSL Trigger
    if current_price <= effective_stop_price:
        if enable_tsl and effective_stop_price > hard_stop_price:
            reason = f"TSL_TRIGGERED (Peak: ₹{peak_price:.2f} -> Floor: ₹{effective_stop_price:.2f})"
        else:
            reason = f"HARD_STOP_LOSS_HIT (Cost: ₹{cost_price:.2f} -> Floor: ₹{effective_stop_price:.2f})"
        return True, reason, effective_stop_price

    # 2. Technical Profit-Taking Signal Trigger
    if rsi_val is not None and rsi_val >= 65.0 and macd_bearish:
        reason = f"PROFIT_TAKING_TECHNICAL (RSI: {rsi_val:.1f}, Bearish MACD Cross)"
        return True, reason, effective_stop_price

    return False, "HOLD", effective_stop_price


def analyze_ticker_data(df: pd.DataFrame, ticker: str = "") -> Optional[Dict[str, Any]]:
    """Analyzes price action DataFrames and logs identified signals to SQLite."""
    if df is None or df.empty or "Close" not in df.columns:
        return None

    closes = df["Close"].to_numpy(dtype=float).flatten()
    if len(closes) < 35:
        return None

    current_price = float(closes[-1])
    rsi_val = calculate_rsi(closes)
    m_curr, s_curr, m_prev, s_prev = calculate_macd(closes)

    signals = []
    if rsi_val <= 30.0:
        signals.append("RSI_OVERSOLD")
    elif rsi_val >= 70.0:
        signals.append("RSI_OVERBOUGHT")

    if m_prev < s_prev and m_curr >= s_curr:
        signals.append("MACD_BULLISH_CROSS")
    elif m_prev > s_prev and m_curr <= s_curr:
        signals.append("MACD_BEARISH_CROSS")

    if ticker and signals:
        try:
            for sig in signals:
                action = "BUY" if "BULLISH" in sig or "OVERSOLD" in sig else "SELL"
                db.log_signal(
                    ticker=ticker,
                    strategy=sig,
                    price=round(current_price, 2),
                    rsi=round(rsi_val, 2),
                    macd=round(m_curr, 2),
                    action=action
                )
        except Exception as e:
            logger.error(f"Failed to log signal for {ticker}: {e}")

    return {
        "price": round(current_price, 2),
        "rsi": round(rsi_val, 2),
        "macd": round(m_curr, 2),
        "signals": signals
    }

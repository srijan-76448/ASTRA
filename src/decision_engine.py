import numpy as np
import logging
import pandas as pd
from typing import Dict, Any, Optional
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

def calculate_macd(prices: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    """Calculates MACD, Signal line, and previous step values for crossover detection."""
    if len(prices) < slow + signal:
        return 0.0, 0.0, 0.0, 0.0

    df_series = pd.Series(prices)
    ema_fast = df_series.ewm(span=fast, adjust=False).mean().to_numpy()
    ema_slow = df_series.ewm(span=slow, adjust=False).mean().to_numpy()
    
    macd_line = ema_fast - ema_slow
    signal_line = pd.Series(macd_line).ewm(span=signal, adjust=False).mean().to_numpy()

    return float(macd_line[-1]), float(signal_line[-1]), float(macd_line[-2]), float(signal_line[-2])

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

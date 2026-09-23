import numpy as np

def calculate_rsi(prices, period=14):
    deltas = np.diff(prices)
    seed = deltas[:period+1]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    
    if down == 0:
        return 100.0
        
    rs = up / down
    rsi = np.zeros_like(prices)
    rsi[:period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period, len(prices)):
        delta = deltas[i - 1]
        if delta > 0:
            upval = delta
            downval = 0.0
        else:
            upval = 0.0
            downval = -delta

        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period

        rs = up / (down if down != 0 else 1e-10)
        rsi[i] = 100.0 - (100.0 / (1.0 + rs))

    return float(rsi[-1])

def calculate_macd(prices, fast=12, slow=26, signal=9):
    def ema(data, window):
        weights = np.exp(np.linspace(-1., 0., window))
        weights /= weights.sum()
        a = np.convolve(data, weights, mode='full')[:len(data)]
        a[:window] = a[window]
        return a

    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    
    return float(macd_line[-1]), float(signal_line[-1]), float(macd_line[-2]), float(signal_line[-2])

def analyze_ticker_data(df):
    closes = df['Close'].to_numpy(dtype=float).flatten()
    if len(closes) < 35:
        return None

    current_price = float(closes[-1])
    rsi_val = calculate_rsi(closes)
    m_curr, s_curr, m_prev, s_prev = calculate_macd(closes)

    signals = []
    if rsi_val <= 30:
        signals.append("RSI_OVERSOLD")
    elif rsi_val >= 70:
        signals.append("RSI_OVERBOUGHT")

    if m_prev < s_prev and m_curr >= s_curr:
        signals.append("MACD_BULLISH_CROSS")
    elif m_prev > s_prev and m_curr <= s_curr:
        signals.append("MACD_BEARISH_CROSS")

    return {
        "price": round(current_price, 2),
        "rsi": round(rsi_val, 2),
        "macd": round(m_curr, 2),
        "signals": signals
    }

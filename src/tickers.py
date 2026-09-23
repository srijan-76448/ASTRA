import os
import logging
import io
import requests
import pandas as pd
import yfinance as yf
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)

def fetch_nse_equity_list() -> list:
    """
    Downloads the live, official list of all active NSE equity symbols 
    directly from NSE's public CSV repository.
    """
    url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            df = pd.read_csv(io.StringIO(response.text))
            if "SYMBOL" in df.columns:
                symbols = [f"{sym.strip()}.NS" for sym in df["SYMBOL"].dropna().unique()]
                logger.info(f"Fetched {len(symbols)} official equities from NSE.")
                return symbols
    except Exception as e:
        logger.error(f"Failed to fetch official NSE equity list: {e}")

    return []

def get_dynamic_tickers() -> list:
    """
    Fetches the live NSE symbol list, evaluates prices via yfinance batch download, 
    and returns stocks falling strictly within your specified budget bounds.
    """
    min_price = float(os.getenv("MIN_TRADE_ALLOCATION", 100.0))
    max_price = float(os.getenv("MAX_TRADE_ALLOCATION", 2000.0))
    limit = int(os.getenv("TICKERS_COUNT", 50))

    # 1. Fetch live official NSE symbols
    all_symbols = fetch_nse_equity_list()
    if not all_symbols:
        logger.warning("Could not retrieve NSE stock universe.")
        return []

    # Process in a manageable chunk to find active candidates
    sample_symbols = all_symbols[:300]
    logger.info(f"Filtering symbols for prices between ₹{min_price:.2f} and ₹{max_price:.2f}...")

    # 2. Batch download price snapshot (works even off-market using last close)
    try:
        data = yf.download(sample_symbols, period="1d", progress=False)
        valid_tickers = []

        for ticker in sample_symbols:
            try:
                if len(sample_symbols) > 1:
                    price = float(data["Close"][ticker].dropna().iloc[-1])
                else:
                    price = float(data["Close"].dropna().iloc[-1])

                # Range check: between MIN_TRADE_ALLOCATION and MAX_TRADE_ALLOCATION
                if min_price <= price <= max_price:
                    valid_tickers.append((ticker, price))
            except Exception:
                continue

        # Sort by price ascending to prioritize budget-friendly setups
        valid_tickers.sort(key=lambda x: x[1])
        selected = [t[0] for t in valid_tickers[:limit]]

        logger.info(f"Dynamic Ticker Engine: Selected top {len(selected)} stocks in ₹{min_price:.2f}-₹{max_price:.2f} range.")
        return selected

    except Exception as e:
        logger.error(f"Error filtering stock prices: {e}")
        return []

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tickers = get_dynamic_tickers()
    print("\nDynamically Swept Tickers:", tickers)

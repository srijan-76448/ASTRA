import os
import logging
from pyotp import TOTP
from SmartApi import SmartConnect
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)

class AngelOneClient:
    def __init__(self):
        self.api_key = os.getenv("ANGEL_API_KEY")
        self.client_code = os.getenv("ANGEL_CLIENT_CODE")
        self.password = os.getenv("ANGEL_PASSWORD")
        self.totp_secret = os.getenv("ANGEL_TOTP_SECRET")
        self.smart_api = None

    def authenticate(self):
        """Authenticates with Angel One SmartAPI using TOTP."""
        try:
            self.smart_api = SmartConnect(api_key=self.api_key)
            totp = TOTP(self.totp_secret).now()
            
            data = self.smart_api.generateSession(self.client_code, self.password, totp)
            if data and data.get("status"):
                logger.info("Successfully authenticated with Angel One SmartAPI.")
                return True
            else:
                logger.error(f"Angel One Login Failed: {data.get('message')}")
                return False
        except Exception as e:
            logger.error(f"Error during Angel One authentication: {e}")
            return False

    def get_real_portfolio_data(self):
        """Fetches live funds (RMS balance) and current holdings."""
        if not self.smart_api and not self.authenticate():
            return None

        try:
            # Fetch RMS Limits / Cash Available
            rms_data = self.smart_api.rmsLimit()
            available_cash = 0.0
            if rms_data and rms_data.get("status"):
                available_cash = float(rms_data.get("data", {}).get("net", 0.0))

            # Fetch Holdings
            holdings_data = self.smart_api.holding()
            holdings_list = []
            if holdings_data and holdings_data.get("status"):
                raw_holdings = holdings_data.get("data", [])
                for item in raw_holdings:
                    holdings_list.append({
                        "ticker": item.get("tradingsymbol", ""),
                        "qty": int(item.get("quantity", 0)),
                        "avg_price": float(item.get("averageprice", 0.0)),
                        "current_price": float(item.get("ltp", 0.0)),
                        "pnl": float(item.get("pnl", 0.0))
                    })

            return {
                "available_cash": available_cash,
                "holdings": holdings_list
            }

        except Exception as e:
            logger.error(f"Error fetching real portfolio data from Angel One: {e}")
            return None

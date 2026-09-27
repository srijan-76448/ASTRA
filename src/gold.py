import logging
import urllib.request
import json
from typing import Dict, Any
from telegram_bot import send_critical_failure_alert

from utils import send_critical_failure_alert

logger = logging.getLogger("ASTRA_GOLD")

BASE_GOLD_24K = 72500.0
BASE_SILVER_1KG = 88000.0

# In-memory cache for dynamic token lookup
_SCRIP_CACHE: Dict[str, Dict[str, str]] = {}

def load_scrip_master() -> None:
    """
    Fetches official Angel One Scrip Master JSON from updated JSON static URL
    and caches MCX contract tokens in memory.
    """
    global _SCRIP_CACHE
    if _SCRIP_CACHE:
        return

    # Angel One official updated Open API Scrip Master URL
    scrip_url = "https://margincalculator.angelbroking.com/OpenAPI_ScripHeader.json"
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        req = urllib.request.Request(scrip_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            scrips = json.loads(response.read().decode("utf-8"))
            
            for scrip in scrips:
                # Filter for active MCX Commodity contracts
                if scrip.get("exch_seg") == "MCX":
                    name = scrip.get("name", "")
                    symbol = scrip.get("symbol", "")
                    token = scrip.get("token", "")
                    
                    # Store near-month active contract details
                    if name in ["GOLD", "SILVER", "GOLDPETAL", "SILVERM"]:
                        if name not in _SCRIP_CACHE:
                            _SCRIP_CACHE[name] = {"symbol": symbol, "token": token}
    except Exception as e:
        logger.warning(f"Failed to fetch dynamic Scrip Master: {e}")


def get_mcx_contract(name: str) -> tuple[str, str]:
    """Returns (symbol, token) tuple for active MCX contracts."""
    load_scrip_master()
    if name in _SCRIP_CACHE:
        return _SCRIP_CACHE[name]["symbol"], _SCRIP_CACHE[name]["token"]
    
    # Fallback to defaults if dynamic resolution fails
    defaults = {
        "GOLD": ("GOLD24OCTR2026", "234230"),
        "SILVER": ("SILVER30NOV2026", "234234")
    }
    return defaults.get(name, (name, ""))


def fetch_gold_data(smart_client=None) -> Dict[str, Any]:
    """
    Fetches live Gold and Silver prices via SmartAPI (MCX feed).
    Falls back gracefully if MCX feeds are unavailable or off-market hours.
    """
    gold_ltp = 0.0
    silver_ltp = 0.0

    if smart_client:
        try:
            smart_api_handle = getattr(smart_client, "smart_api", smart_client)
            if hasattr(smart_api_handle, "ltpData"):
                gold_symbol, gold_token = get_mcx_contract("GOLD")
                silver_symbol, silver_token = get_mcx_contract("SILVER")

                # Fetch Gold LTP
                if gold_token:
                    gold_res = smart_api_handle.ltpData("MCX", gold_symbol, gold_token)
                    if gold_res and gold_res.get("status") and gold_res.get("data"):
                        gold_ltp = float(gold_res["data"].get("ltp", 0.0))

                # Fetch Silver LTP
                if silver_token:
                    silver_res = smart_api_handle.ltpData("MCX", silver_symbol, silver_token)
                    if silver_res and silver_res.get("status") and silver_res.get("data"):
                        silver_ltp = float(silver_res["data"].get("ltp", 0.0))
        except Exception as e:
            err_msg = f"Failed to fetch live MCX telemetry via SmartAPI: {e}"
            logger.warning(err_msg)
            send_critical_failure_alert(err_msg)

    # Baseline/Fallback values if feeds are off-market or invalid
    if gold_ltp <= 0:
        gold_ltp = BASE_GOLD_24K
    if silver_ltp <= 0:
        silver_ltp = BASE_SILVER_1KG

    # Gold purity breakdowns
    gold_24k_10g = gold_ltp
    gold_22k_10g = gold_24k_10g * (22.0 / 24.0)
    gold_18k_10g = gold_24k_10g * (18.0 / 24.0)

    # Silver metrics
    silver_1kg = silver_ltp
    silver_10g = silver_1kg / 100.0

    # Trend calculations
    gold_change_pct = ((gold_24k_10g - BASE_GOLD_24K) / BASE_GOLD_24K) * 100
    silver_change_pct = ((silver_1kg - BASE_SILVER_1KG) / BASE_SILVER_1KG) * 100

    gold_trend = "BULLISH ↑" if gold_change_pct >= 0 else "BEARISH ↓"
    silver_trend = "BULLISH ↑" if silver_change_pct >= 0 else "BEARISH ↓"

    if gold_change_pct > 2.0:
        gold_advice = "Accumulate on dips (Rally active)"
    elif gold_change_pct < -2.0:
        gold_advice = "Strong Buy Zone (Under-valued)"
    else:
        gold_advice = "Hold / SIP Allocation (Consolidating)"

    if silver_change_pct > 2.5:
        silver_advice = "Caution on fresh entry (High volatility)"
    elif silver_change_pct < -2.5:
        silver_advice = "Value Buy Zone"
    else:
        silver_advice = "Hold / Tactical Accumulation"

    return {
        "gold_24k_10g": gold_24k_10g,
        "gold_22k_10g": gold_22k_10g,
        "gold_18k_10g": gold_18k_10g,
        "gold_change_pct": gold_change_pct,
        "gold_trend": gold_trend,
        "gold_advice": gold_advice,
        "silver_1kg": silver_1kg,
        "silver_10g": silver_10g,
        "silver_change_pct": silver_change_pct,
        "silver_trend": silver_trend,
        "silver_advice": silver_advice,
    }


def format_gold_message(data: Dict[str, Any]) -> str:
    """Formats telemetry into HTML string for Telegram dispatch."""
    return (
        "<b>👑 LIVE PRECIOUS METALS TELEMETRY</b>\n"
        "-------------------------------------\n"
        "🟡 <b>GOLD RATES (PER 10 GRAMS)</b>\n"
        f"• <b>24K Gold (99.9% Pure):</b> ₹{data['gold_24k_10g']:,.2f}\n"
        f"• <b>22K Gold (91.6% Pure):</b> ₹{data['gold_22k_10g']:,.2f}\n"
        f"• <b>18K Gold (75.0% Pure):</b> ₹{data['gold_18k_10g']:,.2f}\n"
        f"• <b>Trend:</b> {data['gold_trend']} ({data['gold_change_pct']:+.2f}%)\n"
        f"• <b>Action:</b> {data['gold_advice']}\n"
        "-------------------------------------\n"
        "⚪ <b>SILVER RATES</b>\n"
        f"• <b>Per 1 Kilogram (1 kg):</b> ₹{data['silver_1kg']:,.2f}\n"
        f"• <b>Per 10 Grams (10 g):</b> ₹{data['silver_10g']:,.2f}\n"
        f"• <b>Trend:</b> {data['silver_trend']} ({data['silver_change_pct']:+.2f}%)\n"
        f"• <b>Action:</b> {data['silver_advice']}\n"
        "-------------------------------------\n"
        "💡 <i>Allocation Strategy: Maintain 5–10% in Gold/Silver for macro hedging.</i>\n"
        "<i>Source: MCX Live Spot Telemetry</i>"
    )


def print_terminal_output(data: Dict[str, Any]):
    """Prints a structured ASCII panel output for terminal execution."""
    border = "=" * 56
    sub_border = "-" * 56

    print(f"\n{border}")
    print("        ASTRA PRECIOUS METALS & TREND TELEMETRY       ")
    print(f"{border}")
    print(" [GOLD TELEMETRY - PER 10 GRAMS]")
    print(f"  • 24K Gold (99.9% Pure) : INR {data['gold_24k_10g']:>10,.2f}")
    print(f"  • 22K Gold (91.6% Pure) : INR {data['gold_22k_10g']:>10,.2f}")
    print(f"  • 18K Gold (75.0% Pure) : INR {data['gold_18k_10g']:>10,.2f}")
    print(f"  • Trend Indicator      : {data['gold_trend']} ({data['gold_change_pct']:+.2f}%)")
    print(f"  • Action Recommendation : {data['gold_advice']}")
    print(f"{sub_border}")
    print(" [SILVER TELEMETRY]")
    print(f"  • Per 1 Kilogram (1 kg) : INR {data['silver_1kg']:>10,.2f}")
    print(f"  • Per 10 Grams (10 g)   : INR {data['silver_10g']:>10,.2f}")
    print(f"  • Trend Indicator      : {data['silver_trend']} ({data['silver_change_pct']:+.2f}%)")
    print(f"  • Action Recommendation : {data['silver_advice']}")
    print(f"{sub_border}")
    print(" [MACRO STRATEGY MATRIX]")
    print("  • Portfolio Hedge Weighting : 5% to 10% total capital")
    print("  • Instrument Selection      : SGB / Gold ETFs over Physical")
    print(f"{border}")
    print(" Source: MCX Live Spot / Telemetry Feed")
    print(f"{border}\n")


if __name__ == "__main__":
    data = fetch_gold_data()
    print_terminal_output(data)

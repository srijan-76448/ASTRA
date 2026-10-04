"""
ASTRA V3 - Precious Metals Telemetry
====================================

Responsibilities
----------------
- Fetch live MCX Gold and Silver telemetry through SmartAPI.
- Dynamically resolve MCX contracts from the official Angel One Scrip Master.
- Provide safe fallback contract identifiers when the Scrip Master is
  unavailable.
- Calculate Gold/Silver derived rates and trend telemetry.
- Produce advisory allocation guidance.
- Provide Telegram HTML output.
- Provide terminal output.

Configuration
-------------
Non-secret runtime configuration is read from settings.json through
utils.get_setting().

Secrets and broker credentials are NOT stored or read here.

Important
---------
This module is telemetry/advisory only.

It does NOT:
    - place orders
    - modify broker positions
    - execute trades
    - perform autonomous portfolio changes

The Gold/Silver recommendations are informational signals for the
human-in-the-loop ASTRA workflow.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Dict, Tuple

from utils import (
    get_setting,
    send_critical_failure_alert,
)


# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger(
    "ASTRA_GOLD"
)


# ============================================================================
# DEFAULT RUNTIME SETTINGS
# ============================================================================

DEFAULT_BASE_GOLD_24K = 72500.0

DEFAULT_BASE_SILVER_1KG = 88000.0

DEFAULT_SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_ScripHeader.json"
)

DEFAULT_SCRIP_MASTER_TIMEOUT = 10

DEFAULT_GOLD_BULLISH_THRESHOLD = 2.0

DEFAULT_GOLD_VALUE_THRESHOLD = -2.0

DEFAULT_SILVER_CAUTION_THRESHOLD = 2.5

DEFAULT_SILVER_VALUE_THRESHOLD = -2.5

DEFAULT_HEDGE_MIN_PCT = 5.0

DEFAULT_HEDGE_MAX_PCT = 10.0


# ============================================================================
# SETTINGS HELPERS
# ============================================================================

def _setting_float(
    path: str,
    default: float,
) -> float:
    """
    Read a floating-point runtime setting from settings.json.

    Invalid values fall back to the supplied default.
    """

    try:

        return float(
            get_setting(
                path,
                default,
            )
        )

    except (
        TypeError,
        ValueError,
    ):

        return default


def _setting_int(
    path: str,
    default: int,
) -> int:
    """
    Read an integer runtime setting from settings.json.

    Invalid values fall back to the supplied default.
    """

    try:

        return int(
            get_setting(
                path,
                default,
            )
        )

    except (
        TypeError,
        ValueError,
    ):

        return default


def _setting_str(
    path: str,
    default: str,
) -> str:
    """
    Read a string runtime setting from settings.json.

    Empty or invalid values fall back to the supplied default.
    """

    try:

        value = get_setting(
            path,
            default,
        )

        if value is None:
            return default

        value = str(
            value
        ).strip()

        return value or default

    except Exception:

        return default


def _base_gold_24k() -> float:
    """
    Return the configured Gold baseline.

    This baseline is used only for telemetry/change calculations and
    fallback pricing. It is not an execution price.
    """

    return max(
        0.0,
        _setting_float(
            "GOLD.BASE_GOLD_24K",
            DEFAULT_BASE_GOLD_24K,
        ),
    )


def _base_silver_1kg() -> float:
    """
    Return the configured Silver baseline.

    This baseline is used only for telemetry/change calculations and
    fallback pricing.
    """

    return max(
        0.0,
        _setting_float(
            "GOLD.BASE_SILVER_1KG",
            DEFAULT_BASE_SILVER_1KG,
        ),
    )


def _scrip_master_url() -> str:
    """
    Return the configured Angel One Scrip Master URL.
    """

    return _setting_str(
        "GOLD.SCRIP_MASTER_URL",
        DEFAULT_SCRIP_MASTER_URL,
    )


def _scrip_master_timeout() -> int:
    """
    Return the configured Scrip Master network timeout.
    """

    return max(
        1,
        _setting_int(
            "GOLD.SCRIP_MASTER_TIMEOUT",
            DEFAULT_SCRIP_MASTER_TIMEOUT,
        ),
    )


def _gold_bullish_threshold() -> float:
    """
    Return the Gold bullish/rally threshold in percent.
    """

    return abs(
        _setting_float(
            "GOLD.GOLD_BULLISH_THRESHOLD",
            DEFAULT_GOLD_BULLISH_THRESHOLD,
        )
    )


def _gold_value_threshold() -> float:
    """
    Return the Gold value-buy threshold in percent.

    The returned value is negative so it can be compared directly
    against the percentage change.
    """

    value = _setting_float(
        "GOLD.GOLD_VALUE_THRESHOLD",
        DEFAULT_GOLD_VALUE_THRESHOLD,
    )

    return -abs(
        value
    )


def _silver_caution_threshold() -> float:
    """
    Return the Silver caution threshold in percent.
    """

    return abs(
        _setting_float(
            "GOLD.SILVER_CAUTION_THRESHOLD",
            DEFAULT_SILVER_CAUTION_THRESHOLD,
        )
    )


def _silver_value_threshold() -> float:
    """
    Return the Silver value-buy threshold in percent.

    The returned value is negative so it can be compared directly
    against the percentage change.
    """

    value = _setting_float(
        "GOLD.SILVER_VALUE_THRESHOLD",
        DEFAULT_SILVER_VALUE_THRESHOLD,
    )

    return -abs(
        value
    )


def _hedge_min_pct() -> float:
    """
    Return minimum recommended precious-metals hedge allocation.
    """

    return max(
        0.0,
        _setting_float(
            "GOLD.HEDGE_MIN_PCT",
            DEFAULT_HEDGE_MIN_PCT,
        ),
    )


def _hedge_max_pct() -> float:
    """
    Return maximum recommended precious-metals hedge allocation.
    """

    return max(
        _hedge_min_pct(),
        _setting_float(
            "GOLD.HEDGE_MAX_PCT",
            DEFAULT_HEDGE_MAX_PCT,
        ),
    )


# ============================================================================
# MCX SCRIPT MASTER CACHE
# ============================================================================

_SCRIP_CACHE: Dict[
    str,
    Dict[str, str],
] = {}


# ============================================================================
# SCRIPT MASTER
# ============================================================================

def load_scrip_master() -> None:
    """
    Fetch the official Angel One Scrip Master JSON and cache relevant MCX
    contracts in memory.

    The cache is intentionally process-local.

    A network failure does not make the entire Gold module unusable because
    get_mcx_contract() retains fallback contract identifiers.
    """

    global _SCRIP_CACHE

    if _SCRIP_CACHE:
        return

    scrip_url = _scrip_master_url()

    timeout = _scrip_master_timeout()

    try:

        headers = {
            "User-Agent": "Mozilla/5.0",
        }

        request = urllib.request.Request(
            scrip_url,
            headers=headers,
        )

        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:

            raw_data = response.read()

            scrips = json.loads(
                raw_data.decode(
                    "utf-8"
                )
            )

            if not isinstance(
                scrips,
                list,
            ):

                logger.warning(
                    "Angel One Scrip Master returned an "
                    "unexpected data structure."
                )

                return

            for scrip in scrips:

                if not isinstance(
                    scrip,
                    dict,
                ):
                    continue

                if (
                    scrip.get(
                        "exch_seg",
                        "",
                    )
                    != "MCX"
                ):
                    continue

                name = str(
                    scrip.get(
                        "name",
                        "",
                    )
                ).strip().upper()

                symbol = str(
                    scrip.get(
                        "symbol",
                        "",
                    )
                ).strip()

                token = str(
                    scrip.get(
                        "token",
                        "",
                    )
                ).strip()

                if name in {
                    "GOLD",
                    "SILVER",
                    "GOLDPETAL",
                    "SILVERM",
                }:

                    if name not in _SCRIP_CACHE:

                        _SCRIP_CACHE[name] = {
                            "symbol": symbol,
                            "token": token,
                        }

            logger.info(
                "MCX Scrip Master loaded. "
                "Cached contracts: %s",
                sorted(
                    _SCRIP_CACHE.keys()
                ),
            )

    except Exception as exc:

        logger.warning(
            "Failed to fetch dynamic Scrip Master: %s",
            exc,
        )


# ============================================================================
# MCX CONTRACT RESOLUTION
# ============================================================================

def get_mcx_contract(
    name: str,
) -> Tuple[str, str]:
    """
    Return:

        (symbol, token)

    for the requested MCX contract.

    Dynamic Scrip Master data is preferred.

    Fallback identifiers are retained for compatibility if the Scrip Master
    cannot be reached or does not contain the requested contract.
    """

    normalized_name = (
        str(
            name
        )
        .strip()
        .upper()
    )

    load_scrip_master()

    if normalized_name in _SCRIP_CACHE:

        cached = _SCRIP_CACHE[
            normalized_name
        ]

        return (
            cached.get(
                "symbol",
                "",
            ),
            cached.get(
                "token",
                "",
            ),
        )

    defaults = {
        "GOLD": (
            "GOLD24OCTR2026",
            "234230",
        ),
        "SILVER": (
            "SILVER30NOV2026",
            "234234",
        ),
    }

    return defaults.get(
        normalized_name,
        (
            normalized_name,
            "",
        ),
    )


# ============================================================================
# GOLD / SILVER TELEMETRY
# ============================================================================

def fetch_gold_data(
    smart_client=None,
) -> Dict[str, Any]:
    """
    Fetch live Gold and Silver prices through SmartAPI MCX telemetry.

    Parameters
    ----------
    smart_client:
        Existing ASTRA SmartAPI wrapper/client.

    Returns
    -------
    dict
        Derived Gold/Silver telemetry and advisory information.

    Notes
    -----
    No broker order operations are performed.
    """

    gold_ltp = 0.0

    silver_ltp = 0.0

    if smart_client:

        try:

            smart_api_handle = getattr(
                smart_client,
                "smart_api",
                smart_client,
            )

            if hasattr(
                smart_api_handle,
                "ltpData",
            ):

                gold_symbol, gold_token = (
                    get_mcx_contract(
                        "GOLD"
                    )
                )

                silver_symbol, silver_token = (
                    get_mcx_contract(
                        "SILVER"
                    )
                )

                if gold_token:

                    gold_response = (
                        smart_api_handle.ltpData(
                            "MCX",
                            gold_symbol,
                            gold_token,
                        )
                    )

                    if (
                        gold_response
                        and gold_response.get(
                            "status"
                        )
                        and gold_response.get(
                            "data"
                        )
                    ):

                        gold_ltp = float(
                            gold_response[
                                "data"
                            ].get(
                                "ltp",
                                0.0,
                            )
                        )

                if silver_token:

                    silver_response = (
                        smart_api_handle.ltpData(
                            "MCX",
                            silver_symbol,
                            silver_token,
                        )
                    )

                    if (
                        silver_response
                        and silver_response.get(
                            "status"
                        )
                        and silver_response.get(
                            "data"
                        )
                    ):

                        silver_ltp = float(
                            silver_response[
                                "data"
                            ].get(
                                "ltp",
                                0.0,
                            )
                        )

        except Exception as exc:

            error_message = (
                "Failed to fetch live MCX telemetry "
                f"via SmartAPI: {exc}"
            )

            logger.warning(
                error_message
            )

            try:

                send_critical_failure_alert(
                    error_message
                )

            except Exception as alert_exc:

                logger.warning(
                    "Failed to send critical failure "
                    "alert for Gold telemetry: %s",
                    alert_exc,
                )

    # ------------------------------------------------------------------
    # Safe fallback values
    # ------------------------------------------------------------------

    base_gold = _base_gold_24k()

    base_silver = _base_silver_1kg()

    if gold_ltp <= 0:

        gold_ltp = base_gold

    if silver_ltp <= 0:

        silver_ltp = base_silver

    # ------------------------------------------------------------------
    # Derived Gold prices
    # ------------------------------------------------------------------

    gold_24k_10g = gold_ltp

    gold_22k_10g = (
        gold_24k_10g
        * (
            22.0
            / 24.0
        )
    )

    gold_18k_10g = (
        gold_24k_10g
        * (
            18.0
            / 24.0
        )
    )

    # ------------------------------------------------------------------
    # Derived Silver prices
    # ------------------------------------------------------------------

    silver_1kg = silver_ltp

    silver_10g = (
        silver_1kg
        / 100.0
    )

    # ------------------------------------------------------------------
    # Baseline changes
    # ------------------------------------------------------------------

    if base_gold > 0:

        gold_change_pct = (
            (
                gold_24k_10g
                - base_gold
            )
            / base_gold
        ) * 100.0

    else:

        gold_change_pct = 0.0

    if base_silver > 0:

        silver_change_pct = (
            (
                silver_1kg
                - base_silver
            )
            / base_silver
        ) * 100.0

    else:

        silver_change_pct = 0.0

    # ------------------------------------------------------------------
    # Trend
    # ------------------------------------------------------------------

    gold_trend = (
        "BULLISH ↑"
        if gold_change_pct >= 0
        else "BEARISH ↓"
    )

    silver_trend = (
        "BULLISH ↑"
        if silver_change_pct >= 0
        else "BEARISH ↓"
    )

    # ------------------------------------------------------------------
    # Advisory thresholds
    # ------------------------------------------------------------------

    gold_bullish_threshold = (
        _gold_bullish_threshold()
    )

    gold_value_threshold = (
        _gold_value_threshold()
    )

    silver_caution_threshold = (
        _silver_caution_threshold()
    )

    silver_value_threshold = (
        _silver_value_threshold()
    )

    # ------------------------------------------------------------------
    # Gold advisory
    # ------------------------------------------------------------------

    if (
        gold_change_pct
        > gold_bullish_threshold
    ):

        gold_advice = (
            "Accumulate on dips "
            "(Rally active)"
        )

    elif (
        gold_change_pct
        < gold_value_threshold
    ):

        gold_advice = (
            "Strong Buy Zone "
            "(Under-valued)"
        )

    else:

        gold_advice = (
            "Hold / SIP Allocation "
            "(Consolidating)"
        )

    # ------------------------------------------------------------------
    # Silver advisory
    # ------------------------------------------------------------------

    if (
        silver_change_pct
        > silver_caution_threshold
    ):

        silver_advice = (
            "Caution on fresh entry "
            "(High volatility)"
        )

    elif (
        silver_change_pct
        < silver_value_threshold
    ):

        silver_advice = (
            "Value Buy Zone"
        )

    else:

        silver_advice = (
            "Hold / Tactical Accumulation"
        )

    # ------------------------------------------------------------------
    # Return telemetry
    # ------------------------------------------------------------------

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
        "gold_base_price": base_gold,
        "silver_base_price": base_silver,
        "hedge_min_pct": _hedge_min_pct(),
        "hedge_max_pct": _hedge_max_pct(),
    }


# ============================================================================
# TELEGRAM FORMATTER
# ============================================================================

def format_gold_message(
    data: Dict[str, Any],
) -> str:
    """
    Format precious-metals telemetry into an HTML Telegram message.
    """

    hedge_min = data.get(
        "hedge_min_pct",
        _hedge_min_pct(),
    )

    hedge_max = data.get(
        "hedge_max_pct",
        _hedge_max_pct(),
    )

    return (
        "<b>👑 LIVE PRECIOUS METALS TELEMETRY</b>\n"
        "-------------------------------------\n"
        "🟡 <b>GOLD RATES (PER 10 GRAMS)</b>\n"
        f"• <b>24K Gold (99.9% Pure):</b> "
        f"₹{data['gold_24k_10g']:,.2f}\n"
        f"• <b>22K Gold (91.6% Pure):</b> "
        f"₹{data['gold_22k_10g']:,.2f}\n"
        f"• <b>18K Gold (75.0% Pure):</b> "
        f"₹{data['gold_18k_10g']:,.2f}\n"
        f"• <b>Trend:</b> "
        f"{data['gold_trend']} "
        f"({data['gold_change_pct']:+.2f}%)\n"
        f"• <b>Action:</b> "
        f"{data['gold_advice']}\n"
        "-------------------------------------\n"
        "⚪ <b>SILVER RATES</b>\n"
        f"• <b>Per 1 Kilogram (1 kg):</b> "
        f"₹{data['silver_1kg']:,.2f}\n"
        f"• <b>Per 10 Grams (10 g):</b> "
        f"₹{data['silver_10g']:,.2f}\n"
        f"• <b>Trend:</b> "
        f"{data['silver_trend']} "
        f"({data['silver_change_pct']:+.2f}%)\n"
        f"• <b>Action:</b> "
        f"{data['silver_advice']}\n"
        "-------------------------------------\n"
        f"💡 <i>Allocation Strategy: Maintain "
        f"{hedge_min:g}–{hedge_max:g}% in Gold/Silver "
        "for macro hedging.</i>\n"
        "<i>Source: MCX Live Spot Telemetry</i>"
    )


# ============================================================================
# TERMINAL OUTPUT
# ============================================================================

def print_terminal_output(
    data: Dict[str, Any],
) -> None:
    """
    Print structured precious-metals telemetry to the terminal.
    """

    border = "=" * 56

    sub_border = "-" * 56

    hedge_min = data.get(
        "hedge_min_pct",
        _hedge_min_pct(),
    )

    hedge_max = data.get(
        "hedge_max_pct",
        _hedge_max_pct(),
    )

    print(
        f"\n{border}"
    )

    print(
        "        ASTRA PRECIOUS METALS & TREND TELEMETRY       "
    )

    print(
        f"{border}"
    )

    print(
        " [GOLD TELEMETRY - PER 10 GRAMS]"
    )

    print(
        "  • 24K Gold (99.9% Pure) : "
        f"INR {data['gold_24k_10g']:>10,.2f}"
    )

    print(
        "  • 22K Gold (91.6% Pure) : "
        f"INR {data['gold_22k_10g']:>10,.2f}"
    )

    print(
        "  • 18K Gold (75.0% Pure) : "
        f"INR {data['gold_18k_10g']:>10,.2f}"
    )

    print(
        "  • Trend Indicator        : "
        f"{data['gold_trend']} "
        f"({data['gold_change_pct']:+.2f}%)"
    )

    print(
        "  • Action Recommendation  : "
        f"{data['gold_advice']}"
    )

    print(
        f"{sub_border}"
    )

    print(
        " [SILVER TELEMETRY]"
    )

    print(
        "  • Per 1 Kilogram (1 kg) : "
        f"INR {data['silver_1kg']:>10,.2f}"
    )

    print(
        "  • Per 10 Grams (10 g)   : "
        f"INR {data['silver_10g']:>10,.2f}"
    )

    print(
        "  • Trend Indicator        : "
        f"{data['silver_trend']} "
        f"({data['silver_change_pct']:+.2f}%)"
    )

    print(
        "  • Action Recommendation  : "
        f"{data['silver_advice']}"
    )

    print(
        f"{sub_border}"
    )

    print(
        " [MACRO STRATEGY MATRIX]"
    )

    print(
        "  • Portfolio Hedge Weighting : "
        f"{hedge_min:g}% to {hedge_max:g}% total capital"
    )

    print(
        "  • Instrument Selection      : "
        "SGB / Gold ETFs over Physical"
    )

    print(
        f"{border}\n"
    )


# ============================================================================
# HEALTH / DIAGNOSTICS
# ============================================================================

def gold_engine_health() -> Dict[str, Any]:
    """
    Return current Gold telemetry configuration.

    No network request is performed.
    """

    return {
        "module": "gold",
        "status": "READY",
        "base_gold_24k": _base_gold_24k(),
        "base_silver_1kg": _base_silver_1kg(),
        "scrip_master_url": _scrip_master_url(),
        "scrip_master_timeout": _scrip_master_timeout(),
        "gold_bullish_threshold": (
            _gold_bullish_threshold()
        ),
        "gold_value_threshold": (
            _gold_value_threshold()
        ),
        "silver_caution_threshold": (
            _silver_caution_threshold()
        ),
        "silver_value_threshold": (
            _silver_value_threshold()
        ),
        "hedge_min_pct": _hedge_min_pct(),
        "hedge_max_pct": _hedge_max_pct(),
        "cached_contracts": sorted(
            _SCRIP_CACHE.keys()
        ),
        "broker_execution": False,
    }


# ============================================================================
# CLI ENTRY POINT
# ============================================================================

if __name__ == "__main__":

    data = fetch_gold_data()

    print_terminal_output(
        data
    )

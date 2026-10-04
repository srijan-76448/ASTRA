"""
ASTRA - Dynamic NSE Ticker Universe
===================================

Responsibilities
----------------
    - Maintain ASTRA's NSE equity universe.
    - Provide a deterministic fallback universe.
    - Support configurable ticker limits.
    - Keep broker/NSE symbols separate from Yahoo Finance symbols.
    - Remove duplicates and malformed symbols.
    - Provide separate universes for normal and intraday scans.
    - Avoid network dependency for basic ticker-universe operation.

Important
---------
This module returns broker/NSE-style symbols such as:

    WIPRO-EQ
    RELIANCE-EQ
    TCS-EQ

It does NOT convert them to Yahoo Finance symbols.

Yahoo conversion belongs at the market-data boundary, where:

    WIPRO-EQ -> WIPRO.NS

That separation is important because Angel One and Yahoo Finance use
different symbol conventions.
"""

from __future__ import annotations

import logging
import random
import threading
from typing import Iterable, Optional, Sequence

from utils import clean_ticker_symbol, get_setting


logger = logging.getLogger("ASTRA_TICKERS")


# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_TICKERS_COUNT = 50

MIN_TICKERS = 1

# Intraday should normally use a reasonably sized universe, but never allow
# an accidental environment value to request an absurd amount of work.
MAX_TICKERS = 500

# Runtime settings are stored in settings.json and accessed through utils.
# The ticker subsystem intentionally does not read runtime configuration
# directly from .env.
NORMAL_TICKERS_SETTING = "NORMAL_TRADING.TICKERS_COUNT"
INTRADAY_TICKERS_SETTING = "INTRADAY.TICKERS_COUNT"
CUSTOM_TICKERS_SETTING = "TICKERS.CUSTOM_UNIVERSE"
RANDOMIZE_SETTING = "TICKERS.RANDOMIZE"
RANDOM_SEED_SETTING = "TICKERS.RANDOM_SEED"


# ============================================================================
# DEFAULT NSE EQUITY UNIVERSE
# ============================================================================
#
# These are broker-style NSE equity symbols.
#
# The "-EQ" suffix is intentional. It is useful when the same universe is
# later passed to Angel One / SmartAPI.
#
# DO NOT append ".NS" here.
#
# The Yahoo conversion is performed elsewhere.
# ============================================================================

DEFAULT_NSE_TICKERS: tuple[str, ...] = (
    # ------------------------------------------------------------------------
    # Large-cap / NIFTY 50 style universe
    # ------------------------------------------------------------------------

    "ADANIENT-EQ",
    "ADANIPORTS-EQ",
    "APOLLOHOSP-EQ",
    "ASIANPAINT-EQ",
    "AXISBANK-EQ",
    "BAJAJ-AUTO-EQ",
    "BAJFINANCE-EQ",
    "BAJAJFINSV-EQ",
    "BEL-EQ",
    "BHARTIARTL-EQ",
    "CIPLA-EQ",
    "COALINDIA-EQ",
    "DRREDDY-EQ",
    "EICHERMOT-EQ",
    "ETERNAL-EQ",
    "GRASIM-EQ",
    "HCLTECH-EQ",
    "HDFCBANK-EQ",
    "HDFCLIFE-EQ",
    "HEROMOTOCO-EQ",
    "HINDALCO-EQ",
    "HINDUNILVR-EQ",
    "ICICIBANK-EQ",
    "INDUSINDBK-EQ",
    "INFY-EQ",
    "ITC-EQ",
    "JIOFIN-EQ",
    "JSWSTEEL-EQ",
    "KOTAKBANK-EQ",
    "LT-EQ",
    "M&M-EQ",
    "MARUTI-EQ",
    "MAXHEALTH-EQ",
    "NESTLEIND-EQ",
    "NTPC-EQ",
    "ONGC-EQ",
    "POWERGRID-EQ",
    "RELIANCE-EQ",
    "SBILIFE-EQ",
    "SBIN-EQ",
    "SHRIRAMFIN-EQ",
    "SUNPHARMA-EQ",
    "TATACONSUM-EQ",
    "TATAMOTORS-EQ",
    "TATASTEEL-EQ",
    "TCS-EQ",
    "TECHM-EQ",
    "TITAN-EQ",
    "TRENT-EQ",
    "ULTRACEMCO-EQ",
    "WIPRO-EQ",

    # ------------------------------------------------------------------------
    # Additional liquid large/mid-cap equities
    # ------------------------------------------------------------------------

    "ABB-EQ",
    "ABCAPITAL-EQ",
    "ABFRL-EQ",
    "ACC-EQ",
    "ADANIGREEN-EQ",
    "ADANIPOWER-EQ",
    "ALKEM-EQ",
    "AMBUJACEM-EQ",
    "ASHOKLEY-EQ",
    "ASTRAL-EQ",
    "AUROPHARMA-EQ",
    "BANDHANBNK-EQ",
    "BANKBARODA-EQ",
    "BANKINDIA-EQ",
    "BATAINDIA-EQ",
    "BDL-EQ",
    "BERGEPAINT-EQ",
    "BHARATFORG-EQ",
    "BIOCON-EQ",
    "BOSCHLTD-EQ",
    "BPCL-EQ",
    "BRITANNIA-EQ",
    "CANBK-EQ",
    "CASTROLIND-EQ",
    "CDSL-EQ",
    "CHOLAFIN-EQ",
    "COLPAL-EQ",
    "CONCOR-EQ",
    "COROMANDEL-EQ",
    "CROMPTON-EQ",
    "CUMMINSIND-EQ",
    "DABUR-EQ",
    "DALBHARAT-EQ",
    "DEEPAKNTR-EQ",
    "DELHIVERY-EQ",
    "DIVISLAB-EQ",
    "DIXON-EQ",
    "DLF-EQ",
    "DMART-EQ",
    "DREDGECORP-EQ",
    "EXIDEIND-EQ",
    "FEDERALBNK-EQ",
    "GAIL-EQ",
    "GLENMARK-EQ",
    "GMRINFRA-EQ",
    "GODREJCP-EQ",
    "GODREJPROP-EQ",
    "HAL-EQ",
    "HAVELLS-EQ",
    "HINDPETRO-EQ",
    "HINDZINC-EQ",
    "HUDCO-EQ",
    "ICICIGI-EQ",
    "IDFCFIRSTB-EQ",
    "IEX-EQ",
    "IGL-EQ",
    "INDHOTEL-EQ",
    "INDIANB-EQ",
    "INDIGO-EQ",
    "INDUSTOWER-EQ",
    "IOC-EQ",
    "IRCTC-EQ",
    "IREDA-EQ",
    "IRFC-EQ",
    "JINDALSTEL-EQ",
    "JUBLFOOD-EQ",
    "JSWENERGY-EQ",
    "KEI-EQ",
    "KPITTECH-EQ",
    "LAURUSLABS-EQ",
    "LICHSGFIN-EQ",
    "LICI-EQ",
    "LODHA-EQ",
    "LUPIN-EQ",
    "MANAPPURAM-EQ",
    "MARICO-EQ",
    "MCX-EQ",
    "MOTHERSON-EQ",
    "MUTHOOTFIN-EQ",
    "NATIONALUM-EQ",
    "NBCC-EQ",
    "NHPC-EQ",
    "NMDC-EQ",
    "OFSS-EQ",
    "OIL-EQ",
    "PAGEIND-EQ",
    "PERSISTENT-EQ",
    "PETRONET-EQ",
    "PFC-EQ",
    "PIDILITIND-EQ",
    "PIIND-EQ",
    "PNB-EQ",
    "POLYCAB-EQ",
    "POONAWALLA-EQ",
    "PRESTIGE-EQ",
    "PVRINOX-EQ",
    "RAMCOCEM-EQ",
    "RBLBANK-EQ",
    "RECLTD-EQ",
    "SAIL-EQ",
    "SBICARD-EQ",
    "SHREECEM-EQ",
    "SIEMENS-EQ",
    "SOLARINDS-EQ",
    "SONACOMS-EQ",
    "SRF-EQ",
    "STARHEALTH-EQ",
    "SUMICHEM-EQ",
    "SUPREMEIND-EQ",
    "SYNGENE-EQ",
    "TATACHEM-EQ",
    "TATACOMM-EQ",
    "TATAPOWER-EQ",
    "TATATECH-EQ",
    "TIINDIA-EQ",
    "TORNTPHARM-EQ",
    "TORNTPOWER-EQ",
    "TVSMOTOR-EQ",
    "UNIONBANK-EQ",
    "UNITEDSPIRITS-EQ",
    "UPL-EQ",
    "VEDL-EQ",
    "VOLTAS-EQ",
    "YESBANK-EQ",
    "ZEEL-EQ",
)


# ============================================================================
# SYMBOL NORMALIZATION
# ============================================================================

# Security-series suffixes commonly encountered in broker/security-master
# symbols.
_EQUITY_SUFFIXES: tuple[str, ...] = (
    "-EQ",
    "-BE",
    "-BL",
    "-BZ",
    "-SM",
    "-ST",
)


def normalize_broker_ticker(symbol: object) -> str:
    """
    Normalize a ticker into ASTRA's broker/NSE representation.

    Examples
    --------
    WIPRO        -> WIPRO-EQ
    WIPRO-EQ    -> WIPRO-EQ
    WIPRO.NS    -> WIPRO-EQ
    WIPRO-EQ.NS -> WIPRO-EQ

    The function intentionally does not validate whether the symbol
    currently exists on NSE or in Angel One's security master.
    """

    value = str(symbol or "").strip().upper()

    if not value:
        return ""

    value = value.replace(" ", "")

    # Remove Yahoo exchange suffix.
    if value.endswith(".NS"):
        value = value[:-3]

    # Remove BSE suffix when a symbol has accidentally entered the NSE
    # universe. We normalize it to the ASTRA NSE representation.
    if value.endswith(".BO"):
        value = value[:-3]

    # Already has a known broker/security suffix.
    for suffix in _EQUITY_SUFFIXES:
        if value.endswith(suffix):
            base = value[: -len(suffix)]
            if base:
                return f"{base}-EQ"

    # Remove a duplicated -EQ if necessary.
    while value.endswith("-EQ-EQ"):
        value = value[:-3]

    if not value:
        return ""

    return f"{value}-EQ"


def clean_ticker_list(
    symbols: Iterable[object],
) -> list[str]:
    """
    Normalize, validate superficially, and de-duplicate symbols.

    Ordering is preserved.
    """

    result: list[str] = []
    seen: set[str] = set()

    for symbol in symbols:
        normalized = normalize_broker_ticker(symbol)

        if not normalized:
            continue

        # Use the existing ASTRA cleaner as an additional sanity check.
        try:
            cleaned = clean_ticker_symbol(normalized)
        except Exception:
            cleaned = normalized

        if not cleaned:
            continue

        # clean_ticker_symbol() may remove broker suffixes. Restore the
        # canonical broker representation afterward.
        canonical = normalize_broker_ticker(cleaned)

        if not canonical:
            continue

        if canonical in seen:
            continue

        seen.add(canonical)
        result.append(canonical)

    return result


# ============================================================================
# UNIVERSE MANAGEMENT
# ============================================================================

_LOCK = threading.RLock()

_UNIVERSE_CACHE: Optional[tuple[str, ...]] = None


def _build_default_universe() -> tuple[str, ...]:
    """
    Build and cache the default ASTRA universe.
    """

    global _UNIVERSE_CACHE

    with _LOCK:
        if _UNIVERSE_CACHE is not None:
            return _UNIVERSE_CACHE

        cleaned = clean_ticker_list(DEFAULT_NSE_TICKERS)

        if not cleaned:
            raise RuntimeError(
                "ASTRA ticker universe is empty after normalization."
            )

        _UNIVERSE_CACHE = tuple(cleaned)

        logger.info(
            "Ticker universe initialized: %d symbols.",
            len(_UNIVERSE_CACHE),
        )

        return _UNIVERSE_CACHE


def get_default_tickers() -> list[str]:
    """
    Return the complete built-in ASTRA NSE universe.

    A new list is returned so callers cannot mutate the internal cache.
    """

    return list(_build_default_universe())


# ============================================================================
# OPTIONAL EXTERNAL UNIVERSE
# ============================================================================

def _parse_custom_tickers() -> list[str]:
    """Read the optional custom ticker universe from settings.json."""

    raw = get_setting(
        CUSTOM_TICKERS_SETTING,
        None,
    )

    if raw is None:
        return []

    if isinstance(raw, str):
        values = [
            item.strip()
            for item in raw.split(",")
            if item.strip()
        ]
    elif isinstance(raw, (list, tuple, set)):
        values = [
            str(item).strip()
            for item in raw
            if str(item).strip()
        ]
    else:
        logger.warning(
            "Invalid %s value; ignoring custom ticker universe.",
            CUSTOM_TICKERS_SETTING,
        )
        return []

    return clean_ticker_list(values)


def _get_base_universe() -> list[str]:
    """
    Select the configured ticker universe.

    ASTRA_TICKERS is intentionally an explicit override rather than an
    automatic replacement with an external web/API source. This keeps
    ticker selection deterministic and prevents a network failure from
    silently changing the trading universe.
    """

    custom = _parse_custom_tickers()

    if custom:
        logger.info(
            "Using custom ASTRA ticker universe: %d symbols.",
            len(custom),
        )
        return custom

    return get_default_tickers()


# ============================================================================
# LIMIT / RANDOMIZATION
# ============================================================================

def _safe_limit(
    limit: Optional[int],
    *,
    intraday: bool = False,
) -> int:
    """Resolve and clamp the requested ticker count from settings.json."""

    if limit is None:
        setting_name = (
            INTRADAY_TICKERS_SETTING
            if intraday
            else NORMAL_TICKERS_SETTING
        )

        fallback = (
            DEFAULT_TICKERS_COUNT
        )

        raw = get_setting(
            setting_name,
            fallback,
        )

        try:
            limit = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid %s=%r; using %d.",
                setting_name,
                raw,
                fallback,
            )
            limit = fallback

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_TICKERS_COUNT

    limit = max(MIN_TICKERS, limit)
    limit = min(MAX_TICKERS, limit)

    return limit


def _setting_bool(
    path: str,
    default: bool,
) -> bool:
    value = get_setting(path, default)

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    return bool(value)


def _randomization_enabled() -> bool:
    return _setting_bool(
        RANDOMIZE_SETTING,
        False,
    )


def _make_rng() -> random.Random:
    """Build a local RNG without modifying Python's global random state."""

    seed = get_setting(
        RANDOM_SEED_SETTING,
        None,
    )

    if seed is None or str(seed).strip() == "":
        return random.Random()

    try:
        return random.Random(int(seed))
    except (TypeError, ValueError):
        return random.Random(str(seed))


def _select_tickers(
    universe: Sequence[str],
    limit: int,
) -> list[str]:
    """
    Select up to ``limit`` symbols.

    Default behaviour is deterministic.

    When ASTRA_RANDOMIZE_TICKERS is enabled, the selection is shuffled
    using a local RNG.
    """

    if not universe:
        return []

    if limit >= len(universe):
        selected = list(universe)

        if _randomization_enabled():
            rng = _make_rng()
            rng.shuffle(selected)

        return selected

    if _randomization_enabled():
        rng = _make_rng()

        # sample() avoids modifying the cached/default universe.
        return rng.sample(
            list(universe),
            limit,
        )

    return list(universe[:limit])


# ============================================================================
# PUBLIC API
# ============================================================================

def get_dynamic_tickers(
    limit: Optional[int] = None,
) -> list[str]:
    """
    Return the ticker universe used by ASTRA.

    Parameters
    ----------
    limit:
        Maximum number of tickers to return.

        If omitted, TICKERS_COUNT is used.

    Returns
    -------
    list[str]
        Broker/NSE symbols such as ``RELIANCE-EQ``.

    Notes
    -----
    This function deliberately does not perform Yahoo conversion.

    Example:

        get_dynamic_tickers(50)

    may return:

        [
            "ADANIENT-EQ",
            "ADANIPORTS-EQ",
            ...
        ]

    The caller responsible for Yahoo Finance data should convert them to:

        ADANIENT.NS
        ADANIPORTS.NS

    etc.
    """

    universe = _get_base_universe()

    requested_limit = _safe_limit(
        limit,
        intraday=False,
    )

    selected = _select_tickers(
        universe,
        requested_limit,
    )

    logger.debug(
        "get_dynamic_tickers(limit=%s) -> %d symbols.",
        limit,
        len(selected),
    )

    return selected


def get_intraday_tickers(
    limit: Optional[int] = None,
) -> list[str]:
    """
    Return the universe specifically intended for the autonomous intraday
    subsystem.

    INTRADAY_TICKERS_COUNT takes precedence when ``limit`` is omitted.

    This is intentionally separate from get_dynamic_tickers() so the
    intraday scanner can evolve independently without changing normal
    advisory scanning behaviour.
    """

    universe = _get_base_universe()

    requested_limit = _safe_limit(
        limit,
        intraday=True,
    )

    selected = _select_tickers(
        universe,
        requested_limit,
    )

    logger.debug(
        "get_intraday_tickers(limit=%s) -> %d symbols.",
        limit,
        len(selected),
    )

    return selected


def ticker_count() -> int:
    """
    Return the number of symbols in the active base universe.
    """

    return len(_get_base_universe())


def contains_ticker(
    ticker: object,
) -> bool:
    """
    Return True if a normalized ticker exists in the active universe.
    """

    normalized = normalize_broker_ticker(ticker)

    if not normalized:
        return False

    return normalized in set(_get_base_universe())


def reset_ticker_cache() -> None:
    """
    Clear the in-memory ticker cache.

    Useful for tests or when ASTRA intentionally reloads its universe.
    """

    global _UNIVERSE_CACHE

    with _LOCK:
        _UNIVERSE_CACHE = None

    logger.debug("Ticker universe cache cleared.")


# ============================================================================
# DIAGNOSTIC HELPERS
# ============================================================================

def ticker_universe_summary() -> dict[str, object]:
    """
    Return diagnostic information about the active ticker universe.
    """

    universe = _get_base_universe()

    return {
        "count": len(universe),
        "custom_override": bool(
            _parse_custom_tickers()
        ),
        "randomized": _randomization_enabled(),
        "default_limit": _safe_limit(),
        "intraday_limit": _safe_limit(
            None,
            intraday=True,
        ),
        "symbols": list(universe),
    }


# ============================================================================
# MODULE INITIALIZATION
# ============================================================================

# Validate the built-in universe during import.

try:
    _build_default_universe()
except Exception:
    logger.exception(
        "Failed to initialize ASTRA ticker universe."
    )


__all__ = [
    "DEFAULT_NSE_TICKERS",
    "DEFAULT_TICKERS_COUNT",
    "MAX_TICKERS",
    "MIN_TICKERS",
    "clean_ticker_list",
    "contains_ticker",
    "get_default_tickers",
    "get_dynamic_tickers",
    "get_intraday_tickers",
    "normalize_broker_ticker",
    "reset_ticker_cache",
    "ticker_count",
    "ticker_universe_summary",
]

"""
ASTRA Utility Module
====================

Shared utility functions used across ASTRA.

Responsibilities
----------------
- Environment helpers
- Ticker/symbol normalization
- Duration formatting
- Logging setup
- Semantic CLI color formatting
- Telegram critical-failure alerts
- Generic safe conversion helpers
- Runtime filesystem helpers
- JSON-safe value conversion
- Percentage helpers
- Runtime information

This module intentionally contains NO:
    - trading strategy
    - broker order execution
    - portfolio decision logic
    - autonomous trading permissions

Those responsibilities belong to their respective modules.
"""

from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import os
import re
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any, Optional


# ============================================================================
# PATHS
# ============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent

SRC_DIR = BASE_DIR / "src"

LOG_DIR = BASE_DIR / "logs"

LOG_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

ASTRA_LOG_FILE = LOG_DIR / "astra.log"

ASTRA_ERROR_LOG_FILE = LOG_DIR / "astra_error.log"


# ============================================================================
# CONSTANTS
# ============================================================================

ASTRA_LOGGER_NAME = "ASTRA"

# Compatibility markers used by the ASTRA runtime.
_ASTERA_CONSOLE_HANDLER_MARKER = "_astra_console_handler"
_ASTRA_HANDLER_MARKER = "_astra_console_handler"

BROKER_EQUITY_SUFFIXES = (
    "-EQ",
    "-BE",
    "-BL",
    "-BZ",
    "-SM",
    "-ST",
)

YAHOO_EXCHANGE_SUFFIXES = (
    ".NS",
    ".BO",
)

DEFAULT_LOG_LEVEL = "INFO"

DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5


# ============================================================================
# ANSI / CLI COLORS
# ============================================================================

ANSI_RESET = "\033[0m"

ANSI_BLACK = "\033[30m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"
ANSI_WHITE = "\033[37m"

ANSI_BRIGHT_BLACK = "\033[90m"
ANSI_BRIGHT_RED = "\033[91m"
ANSI_BRIGHT_GREEN = "\033[92m"
ANSI_BRIGHT_YELLOW = "\033[93m"
ANSI_BRIGHT_BLUE = "\033[94m"
ANSI_BRIGHT_MAGENTA = "\033[95m"
ANSI_BRIGHT_CYAN = "\033[96m"
ANSI_BRIGHT_WHITE = "\033[97m"

ANSI_BOLD = "\033[1m"


# ============================================================================
# SEMANTIC ASTRA COLOR MAP
# ============================================================================

"""
Semantic colors are deliberately independent from logging levels.

For example:

    logger.info(
        "BUY signal generated",
        extra={"astra_color": "buy"},
    )

The message is still INFO in the log files, but appears bright green
in the interactive terminal.

Supported semantic colors:

    startup
    shutdown
    auth
    wallet
    market
    buy
    sell
    risk
    tsl
    market_closed
    sheets
    intraday
    telegram
    debug
    info
    success
    warning
    error
    critical
    neutral
"""

ASTRA_COLORS: dict[str, str] = {
    # Lifecycle
    "startup": ANSI_BRIGHT_MAGENTA,
    "shutdown": ANSI_MAGENTA,

    # Authentication / broker
    "auth": ANSI_BRIGHT_BLUE,
    "broker": ANSI_BRIGHT_BLUE,

    # Wallet / capital
    "wallet": ANSI_BRIGHT_GREEN,
    "capital": ANSI_BRIGHT_GREEN,
    "success": ANSI_BRIGHT_GREEN,

    # Market data
    "market": ANSI_CYAN,
    "market_data": ANSI_CYAN,

    # Trading signals
    "buy": ANSI_BRIGHT_GREEN,
    "sell": ANSI_BRIGHT_RED,

    # Risk / protection
    "risk": ANSI_BRIGHT_YELLOW,
    "tsl": ANSI_YELLOW,
    "stop_loss": ANSI_YELLOW,

    # Market state
    "market_closed": ANSI_YELLOW,

    # External services
    "sheets": ANSI_BRIGHT_BLUE,
    "telegram": ANSI_CYAN,

    # Intraday
    "intraday": ANSI_BRIGHT_MAGENTA,

    # Standard levels
    "debug": ANSI_CYAN,
    "info": ANSI_GREEN,
    "warning": ANSI_YELLOW,
    "error": ANSI_RED,
    "critical": f"{ANSI_BOLD}{ANSI_BRIGHT_RED}",

    # Generic
    "neutral": ANSI_WHITE,
}


# ============================================================================
# ENVIRONMENT HELPERS
# ============================================================================

def get_env(
    key: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """
    Return an environment variable safely.

    Empty environment values are treated as missing.
    """

    value = os.getenv(key)

    if value is None:
        return default

    value = value.strip()

    if not value:
        return default

    return value


def get_env_bool(
    key: str,
    default: bool = False,
) -> bool:
    """
    Read a boolean environment variable.

    Accepted true values:
        1, true, yes, on, enabled

    Accepted false values:
        0, false, no, off, disabled
    """

    value = get_env(key)

    if value is None:
        return default

    normalized = value.strip().lower()

    if normalized in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }:
        return True

    if normalized in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }:
        return False

    return default


def get_env_int(
    key: str,
    default: int = 0,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Read and validate an integer environment variable."""

    value = get_env(key)

    if value is None:
        result = default
    else:
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default

    if minimum is not None:
        result = max(result, minimum)

    if maximum is not None:
        result = min(result, maximum)

    return result


def get_env_float(
    key: str,
    default: float = 0.0,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    """Read and validate a floating-point environment variable."""

    value = get_env(key)

    if value is None:
        result = default
    else:
        try:
            result = float(value)
        except (TypeError, ValueError):
            result = default

    if minimum is not None:
        result = max(result, minimum)

    if maximum is not None:
        result = min(result, maximum)

    return result


# ============================================================================
# ASTRA RUNTIME SETTINGS
# ============================================================================

# settings.json intentionally contains non-secret runtime configuration.
# Secrets, credentials, tokens, passwords, and infrastructure credentials
# remain in .env and must never be copied into this file.
SETTINGS_FILE = BASE_DIR / "settings.json"

_SETTINGS_LOCK = threading.RLock()
_SETTINGS_CACHE: Optional[dict[str, Any]] = None


class SettingsError(Exception):
    """Base exception for ASTRA settings operations."""


class SettingsFileError(SettingsError):
    """Raised when settings.json cannot be read or written safely."""


class SettingsValidationError(SettingsError):
    """Raised when settings data has an invalid top-level structure."""


def _ensure_settings_dict(settings: Any) -> dict[str, Any]:
    """Validate and normalize the top-level settings object."""

    if not isinstance(settings, dict):
        raise SettingsValidationError(
            "ASTRA settings must be a JSON object."
        )

    return settings


def _deep_merge_dicts(
    target: dict[str, Any],
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Recursively merge dictionaries without mutating the source updates."""

    for key, value in updates.items():
        if (
            key in target
            and isinstance(target[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge_dicts(target[key], value)
        else:
            target[key] = copy.deepcopy(value)

    return target


def _split_setting_path(path: str) -> list[str]:
    """Split a dotted setting path such as NORMAL_TRADING.TICKERS_COUNT."""

    normalized = safe_str(path)

    if not normalized:
        raise SettingsValidationError("Setting path cannot be empty.")

    parts = [part.strip() for part in normalized.split(".")]

    if any(not part for part in parts):
        raise SettingsValidationError(
            f"Invalid setting path: {path!r}"
        )

    return parts


def _get_nested_value(
    settings: dict[str, Any],
    path: str,
    *,
    default: Any = None,
) -> Any:
    """Read a setting using a dotted path."""

    current: Any = settings

    for part in _split_setting_path(path):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]

    return current


def _set_nested_value(
    settings: dict[str, Any],
    path: str,
    value: Any,
) -> None:
    """Set a setting using a dotted path, creating missing dictionaries."""

    parts = _split_setting_path(path)
    current = settings

    for part in parts[:-1]:
        existing = current.get(part)

        if existing is None:
            current[part] = {}
        elif not isinstance(existing, dict):
            raise SettingsValidationError(
                f"Cannot descend through non-object setting: {part!r}"
            )

        current = current[part]

    current[parts[-1]] = copy.deepcopy(value)


def _delete_nested_value(
    settings: dict[str, Any],
    path: str,
) -> bool:
    """Delete a dotted setting path. Returns True when a value was removed."""

    parts = _split_setting_path(path)
    current: Any = settings

    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]

    if not isinstance(current, dict) or parts[-1] not in current:
        return False

    del current[parts[-1]]
    return True


def settings_file_exists() -> bool:
    """Return True when the ASTRA settings.json file exists."""

    return SETTINGS_FILE.is_file()


def load_settings(
    *,
    force_reload: bool = False,
) -> dict[str, Any]:
    """
    Load ASTRA runtime settings from settings.json.

    The loaded object is cached in memory. Call ``force_reload=True`` when the
    file may have been changed outside the current ASTRA process.

    A deep copy is returned so callers cannot accidentally mutate the cache
    without going through the settings controller functions.
    """

    global _SETTINGS_CACHE

    with _SETTINGS_LOCK:
        if _SETTINGS_CACHE is not None and not force_reload:
            return copy.deepcopy(_SETTINGS_CACHE)

        if not SETTINGS_FILE.is_file():
            raise SettingsFileError(
                f"ASTRA settings file not found: {SETTINGS_FILE}"
            )

        try:
            with SETTINGS_FILE.open(
                "r",
                encoding="utf-8",
            ) as handle:
                loaded = json.load(handle)
        except json.JSONDecodeError as exc:
            raise SettingsFileError(
                f"Invalid JSON in {SETTINGS_FILE}: {exc}"
            ) from exc
        except OSError as exc:
            raise SettingsFileError(
                f"Unable to read {SETTINGS_FILE}: {exc}"
            ) from exc

        _SETTINGS_CACHE = _ensure_settings_dict(loaded)

        return copy.deepcopy(_SETTINGS_CACHE)


def reload_settings() -> dict[str, Any]:
    """Force a fresh read of settings.json."""

    return load_settings(force_reload=True)


def get_settings() -> dict[str, Any]:
    """Return the complete current runtime settings object."""

    return load_settings()


def get_setting(
    path: str,
    default: Any = None,
) -> Any:
    """
    Return one runtime setting using a dotted path.

    Examples
    --------
    get_setting("NORMAL_TRADING.TICKERS_COUNT")
    get_setting("INTRADAY.SCAN_INTERVAL_SECONDS")
    get_setting("mailer.TIME", "EOD")
    """

    settings = load_settings()
    value = _get_nested_value(
        settings,
        path,
        default=default,
    )

    return copy.deepcopy(value)


def has_setting(path: str) -> bool:
    """Return True when a setting path exists."""

    sentinel = object()
    return _get_nested_value(
        load_settings(),
        path,
        default=sentinel,
    ) is not sentinel


def save_settings(
    settings: dict[str, Any],
) -> dict[str, Any]:
    """
    Persist a complete settings object atomically to settings.json.

    The existing file is replaced only after the new JSON has been fully
    written and flushed. The in-memory cache is updated after the replacement
    succeeds.
    """

    global _SETTINGS_CACHE

    _ensure_settings_dict(settings)
    snapshot = copy.deepcopy(settings)

    try:
        SETTINGS_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with _SETTINGS_LOCK:
            fd, temporary_name = tempfile.mkstemp(
                prefix=".settings.",
                suffix=".tmp",
                dir=str(SETTINGS_FILE.parent),
                text=True,
            )

            try:
                with os.fdopen(
                    fd,
                    "w",
                    encoding="utf-8",
                ) as handle:
                    json.dump(
                        snapshot,
                        handle,
                        indent=4,
                        ensure_ascii=False,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())

                os.replace(
                    temporary_name,
                    SETTINGS_FILE,
                )

            finally:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

            _SETTINGS_CACHE = copy.deepcopy(snapshot)

            return copy.deepcopy(_SETTINGS_CACHE)

    except OSError as exc:
        raise SettingsFileError(
            f"Unable to write {SETTINGS_FILE}: {exc}"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise SettingsValidationError(
            f"Settings contain a value that cannot be serialized as JSON: {exc}"
        ) from exc


def update_settings(
    updates: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """
    Update multiple runtime settings and optionally persist them.

    Nested dictionaries are merged recursively. Existing settings not present
    in ``updates`` are preserved.

    Example
    -------
    update_settings({
        "NORMAL_TRADING": {
            "TICKERS_COUNT": 25,
        },
        "mailer": {
            "TIME": "15:30",
        },
    })
    """

    _ensure_settings_dict(updates)

    with _SETTINGS_LOCK:
        settings = load_settings()
        _deep_merge_dicts(settings, updates)

        if persist:
            return save_settings(settings)

        global _SETTINGS_CACHE
        _SETTINGS_CACHE = copy.deepcopy(settings)
        return copy.deepcopy(_SETTINGS_CACHE)


def update_setting(
    path: str,
    value: Any,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """
    Update one runtime setting using a dotted path.

    Example
    -------
    update_setting("INTRADAY.SCAN_INTERVAL_SECONDS", 180)
    """

    with _SETTINGS_LOCK:
        settings = load_settings()
        _set_nested_value(
            settings,
            path,
            value,
        )

        if persist:
            return save_settings(settings)

        global _SETTINGS_CACHE
        _SETTINGS_CACHE = copy.deepcopy(settings)
        return copy.deepcopy(_SETTINGS_CACHE)


def set_setting(
    path: str,
    value: Any,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Alias for ``update_setting()`` for command/control code."""

    return update_setting(
        path,
        value,
        persist=persist,
    )


def delete_setting(
    path: str,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """
    Remove a runtime setting using a dotted path.

    This is provided for configuration-management code and should normally be
    protected by the Telegram command layer's explicit allow-list/validation.
    """

    with _SETTINGS_LOCK:
        settings = load_settings()

        removed = _delete_nested_value(
            settings,
            path,
        )

        if not removed:
            raise SettingsValidationError(
                f"Setting does not exist: {path}"
            )

        if persist:
            return save_settings(settings)

        global _SETTINGS_CACHE
        _SETTINGS_CACHE = copy.deepcopy(settings)
        return copy.deepcopy(_SETTINGS_CACHE)


def reset_settings_cache() -> None:
    """
    Clear the in-process settings cache.

    The settings.json file itself is not modified.
    """

    global _SETTINGS_CACHE

    with _SETTINGS_LOCK:
        _SETTINGS_CACHE = None


# ============================================================================
# GENERIC CONVERSION HELPERS
# ============================================================================

def safe_int(
    value: Any,
    default: int = 0,
) -> int:
    """Safely convert a value to int."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(
    value: Any,
    default: float = 0.0,
) -> float:
    """Safely convert a value to float."""

    try:
        result = float(value)

        # NaN check without importing math.
        if result != result:
            return default

        return result

    except (TypeError, ValueError):
        return default


def safe_str(
    value: Any,
    default: str = "",
) -> str:
    """Safely convert a value to a stripped string."""

    if value is None:
        return default

    try:
        result = str(value).strip()
    except Exception:
        return default

    return result if result else default


# ============================================================================
# TICKER NORMALIZATION
# ============================================================================

def clean_ticker_symbol(
    ticker: Any,
) -> str:
    """
    Normalize an ASTRA/broker ticker symbol.

    Examples
    --------
    WIPRO-EQ       -> WIPRO-EQ
    wipro-eq       -> WIPRO-EQ
    WIPRO.NS       -> WIPRO.NS
    WIPRO-EQ.NS    -> WIPRO-EQ.NS
    $WIPRO         -> WIPRO
    " WIPRO-EQ "   -> WIPRO-EQ

    This function does NOT convert broker symbols to Yahoo symbols.

    Use:
        to_yahoo_ticker()

    when a Yahoo-specific ticker is required.
    """

    if ticker is None:
        return ""

    try:
        value = str(ticker).strip().upper()
    except Exception:
        return ""

    if not value:
        return ""

    # Remove accidental leading '$'.
    value = value.lstrip("$")

    # Remove whitespace anywhere in the symbol.
    value = re.sub(r"\s+", "", value)

    # Remove common accidental surrounding punctuation.
    value = value.strip("\"'`")

    return value


def broker_symbol_to_base(
    ticker: Any,
) -> str:
    """
    Remove broker security-series suffixes.

    Examples
    --------
    WIPRO-EQ -> WIPRO
    WIPRO-BE -> WIPRO
    WIPRO     -> WIPRO
    """

    value = clean_ticker_symbol(ticker)

    if not value:
        return ""

    # First remove Yahoo exchange suffix if present.
    for exchange_suffix in YAHOO_EXCHANGE_SUFFIXES:

        if value.endswith(exchange_suffix):
            value = value[:-len(exchange_suffix)]
            break

    for broker_suffix in BROKER_EQUITY_SUFFIXES:

        if value.endswith(broker_suffix):
            value = value[:-len(broker_suffix)]
            break

    return value


def to_yahoo_ticker(
    ticker: Any,
    default_exchange: str = ".NS",
) -> str:
    """
    Convert an ASTRA/broker ticker into a Yahoo Finance ticker.

    Examples
    --------
    WIPRO-EQ      -> WIPRO.NS
    WIPRO         -> WIPRO.NS
    WIPRO.NS      -> WIPRO.NS
    WIPRO-EQ.NS   -> WIPRO.NS
    WIPRO.BO      -> WIPRO.BO
    """

    value = clean_ticker_symbol(ticker)

    if not value:
        return ""

    # Explicit Yahoo exchange symbols.
    for exchange_suffix in YAHOO_EXCHANGE_SUFFIXES:

        if value.endswith(exchange_suffix):

            base = value[:-len(exchange_suffix)]

            for broker_suffix in BROKER_EQUITY_SUFFIXES:

                if base.endswith(broker_suffix):
                    base = base[:-len(broker_suffix)]
                    break

            if not base:
                return ""

            return f"{base}{exchange_suffix}"

    # Remove broker/security-series suffix.
    for broker_suffix in BROKER_EQUITY_SUFFIXES:

        if value.endswith(broker_suffix):
            value = value[:-len(broker_suffix)]
            break

    if not value:
        return ""

    exchange = str(
        default_exchange or ".NS"
    ).strip().upper()

    if not exchange.startswith("."):
        exchange = f".{exchange}"

    return f"{value}{exchange}"


def normalize_ticker_list(
    tickers: Any,
) -> list[str]:
    """
    Clean, normalize and de-duplicate a ticker list.

    This returns broker/base symbols rather than Yahoo symbols.
    """

    if tickers is None:
        return []

    if isinstance(tickers, str):

        values = re.split(
            r"[,;\n]+",
            tickers,
        )

    else:

        try:
            values = list(tickers)
        except TypeError:
            return []

    result: list[str] = []
    seen: set[str] = set()

    for ticker in values:

        symbol = clean_ticker_symbol(ticker)

        if not symbol:
            continue

        if symbol in seen:
            continue

        seen.add(symbol)
        result.append(symbol)

    return result


def normalize_yahoo_ticker_list(
    tickers: Any,
) -> list[str]:
    """
    Convert a ticker list into unique Yahoo Finance symbols.

    Examples
    --------
    [
        "WIPRO-EQ",
        "INFY-EQ",
        "WIPRO.NS"
    ]

    becomes:

    [
        "WIPRO.NS",
        "INFY.NS"
    ]
    """

    if tickers is None:
        return []

    if isinstance(tickers, str):

        values = re.split(
            r"[,;\n]+",
            tickers,
        )

    else:

        try:
            values = list(tickers)
        except TypeError:
            return []

    result: list[str] = []
    seen: set[str] = set()

    for ticker in values:

        normalized = to_yahoo_ticker(ticker)

        if not normalized:
            continue

        if normalized in seen:
            continue

        seen.add(normalized)
        result.append(normalized)

    return result


# ============================================================================
# TICKER VALIDATION
# ============================================================================

def is_valid_ticker_symbol(
    ticker: Any,
) -> bool:
    """
    Perform basic ticker validation.

    This does NOT verify that the ticker actually exists at NSE,
    BSE, Angel One, or Yahoo Finance.
    """

    value = clean_ticker_symbol(ticker)

    if not value:
        return False

    if len(value) > 50:
        return False

    # Permit normal NSE/BSE symbols plus broker/Yahoo suffixes.
    if not re.fullmatch(
        r"[A-Z0-9._&@-]+",
        value,
    ):
        return False

    return True


# ============================================================================
# DURATION FORMATTING
# ============================================================================

def format_duration(
    seconds: Any,
) -> str:
    """
    Convert seconds into a human-readable duration.

    Examples
    --------
    0       -> "0s"
    45      -> "45s"
    90      -> "1m 30s"
    3600    -> "1h"
    3661    -> "1h 1m 1s"
    """

    try:
        total_seconds = max(
            0,
            int(float(seconds)),
        )
    except (TypeError, ValueError):
        total_seconds = 0

    days, remainder = divmod(
        total_seconds,
        86400,
    )

    hours, remainder = divmod(
        remainder,
        3600,
    )

    minutes, seconds_remaining = divmod(
        remainder,
        60,
    )

    parts: list[str] = []

    if days:
        parts.append(f"{days}d")

    if hours:
        parts.append(f"{hours}h")

    if minutes:
        parts.append(f"{minutes}m")

    if seconds_remaining or not parts:
        parts.append(f"{seconds_remaining}s")

    return " ".join(parts)


# ============================================================================
# LOGGING
# ============================================================================

def _resolve_log_level(
    value: Optional[str],
) -> int:
    """Convert a logging-level string into a logging constant."""

    if not value:
        return logging.INFO

    normalized = value.strip().upper()

    return getattr(
        logging,
        normalized,
        logging.INFO,
    )


def _handler_already_installed(
    logger: logging.Logger,
    marker: str,
) -> bool:
    """Check whether an ASTRA handler is already attached."""

    for handler in logger.handlers:

        if getattr(
            handler,
            marker,
            False,
        ):
            return True

    return False


def _mark_handler(
    handler: logging.Handler,
    marker: str,
) -> None:
    """Mark a logging handler so repeated setup does not duplicate it."""

    try:
        setattr(
            handler,
            marker,
            True,
        )
    except Exception:
        pass


def _ansi_enabled(stream: Any = None) -> bool:
    """
    Determine whether ANSI terminal colors should be emitted.

    Colors are disabled when:
        - NO_COLOR is set
        - output is not a TTY
        - TERM explicitly indicates a dumb terminal
    """

    if get_env("NO_COLOR") is not None:
        return False

    target = stream or sys.stdout

    try:
        if not target.isatty():
            return False
    except Exception:
        return False

    term = get_env("TERM", "")

    if term and term.lower() == "dumb":
        return False

    return True


class AstraConsoleFormatter(logging.Formatter):
    """
    Colored formatter used only by the interactive ASTRA console.

    Semantic colors can be selected with:

        logger.info(
            "BUY signal generated",
            extra={"astra_color": "buy"},
        )

    If no semantic color is supplied, the logging level determines
    the fallback color.
    """

    LEVEL_COLORS = {
        logging.DEBUG: ANSI_CYAN,
        logging.INFO: ANSI_GREEN,
        logging.WARNING: ANSI_YELLOW,
        logging.ERROR: ANSI_RED,
        logging.CRITICAL: f"{ANSI_BOLD}{ANSI_BRIGHT_RED}",
    }

    def __init__(
        self,
        *args: Any,
        use_color: Optional[bool] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.use_color = (
            _ansi_enabled()
            if use_color is None
            else bool(use_color)
        )

    def format(
        self,
        record: logging.LogRecord,
    ) -> str:

        message = super().format(record)

        if not self.use_color:
            return message

        semantic_color = getattr(
            record,
            "astra_color",
            None,
        )

        if semantic_color:
            color = ASTRA_COLORS.get(
                str(semantic_color).lower(),
            )
        else:
            color = self.LEVEL_COLORS.get(
                record.levelno,
                ANSI_WHITE,
            )

        if not color:
            return message

        return (
            f"{color}"
            f"{message}"
            f"{ANSI_RESET}"
        )


class AstraFileFormatter(logging.Formatter):
    """
    Plain formatter for ASTRA log files.

    ANSI escape sequences are never written to disk.
    """

    pass


def _configure_third_party_logging() -> None:
    """
    Prevent noisy third-party libraries from polluting ASTRA's CLI.

    ASTRA owns its application logging presentation. Libraries such as
    Angel One's smartConnect may emit their own INFO messages directly.
    """

    noisy_loggers = (
        "smartConnect",
        "smartapi",
        "SmartApi",
    )

    for logger_name in noisy_loggers:

        third_party_logger = logging.getLogger(
            logger_name
        )

        third_party_logger.setLevel(
            logging.WARNING
        )

        third_party_logger.propagate = False


def setup_astra_logging(
    level: Optional[str] = None,
) -> logging.Logger:
    """
    Configure and return ASTRA's root application logger.

    The function is idempotent.

    Calling it multiple times does not create duplicate handlers.

    Console output uses semantic ANSI colors.

    Log files remain completely plain-text.
    """

    logger = logging.getLogger(
        ASTRA_LOGGER_NAME
    )

    log_level = _resolve_log_level(
        level
        or get_env(
            "ASTRA_LOG_LEVEL",
            DEFAULT_LOG_LEVEL,
        )
    )

    logger.setLevel(log_level)

    logger.propagate = False

    formatter_kwargs = {
        "fmt": (
            "%(asctime)s | "
            "%(levelname)-8s | "
            "%(name)s | "
            "%(message)s"
        ),
        "datefmt": "%Y-%m-%d %H:%M:%S",
    }

    console_formatter = AstraConsoleFormatter(
        **formatter_kwargs,
        use_color=_ansi_enabled(sys.stdout),
    )

    file_formatter = AstraFileFormatter(
        **formatter_kwargs,
    )

    # ------------------------------------------------------------------------
    # Suppress noisy third-party loggers.
    # ------------------------------------------------------------------------

    _configure_third_party_logging()

    # ------------------------------------------------------------------------
    # Console
    # ------------------------------------------------------------------------

    if not _handler_already_installed(
        logger,
        _ASTRA_HANDLER_MARKER,
    ):

        console_handler = logging.StreamHandler(
            sys.stdout
        )

        console_handler.setLevel(
            log_level
        )

        console_handler.setFormatter(
            console_formatter
        )

        _mark_handler(
            console_handler,
            _ASTRA_HANDLER_MARKER,
        )

        logger.addHandler(
            console_handler
        )

    else:

        # Refresh the formatter in case stdout/TTY state changed.
        for handler in logger.handlers:

            if getattr(
                handler,
                _ASTRA_HANDLER_MARKER,
                False,
            ):
                handler.setLevel(
                    log_level
                )
                handler.setFormatter(
                    AstraConsoleFormatter(
                        **formatter_kwargs,
                        use_color=_ansi_enabled(
                            getattr(
                                handler,
                                "stream",
                                sys.stdout,
                            )
                        ),
                    )
                )

    # ------------------------------------------------------------------------
    # Main rotating log
    # ------------------------------------------------------------------------

    if not any(
        isinstance(
            handler,
            logging.handlers.RotatingFileHandler,
        )
        and getattr(
            handler,
            "_astra_main_file_handler",
            False,
        )
        for handler in logger.handlers
    ):

        try:

            file_handler = (
                logging.handlers.RotatingFileHandler(
                    ASTRA_LOG_FILE,
                    maxBytes=get_env_int(
                        "ASTRA_LOG_MAX_BYTES",
                        DEFAULT_LOG_MAX_BYTES,
                        minimum=1024,
                    ),
                    backupCount=get_env_int(
                        "ASTRA_LOG_BACKUP_COUNT",
                        DEFAULT_LOG_BACKUP_COUNT,
                        minimum=1,
                    ),
                    encoding="utf-8",
                )
            )

            file_handler.setLevel(
                log_level
            )

            file_handler.setFormatter(
                file_formatter
            )

            setattr(
                file_handler,
                "_astra_main_file_handler",
                True,
            )

            logger.addHandler(
                file_handler
            )

        except OSError:

            # Logging to stdout must continue working even if the log
            # directory/file cannot be opened.
            logger.warning(
                "Unable to initialize ASTRA rotating log file.",
                exc_info=True,
            )

    # ------------------------------------------------------------------------
    # Error log
    # ------------------------------------------------------------------------

    if not any(
        isinstance(
            handler,
            logging.handlers.RotatingFileHandler,
        )
        and getattr(
            handler,
            "_astra_error_file_handler",
            False,
        )
        for handler in logger.handlers
    ):

        try:

            error_handler = (
                logging.handlers.RotatingFileHandler(
                    ASTRA_ERROR_LOG_FILE,
                    maxBytes=get_env_int(
                        "ASTRA_ERROR_LOG_MAX_BYTES",
                        DEFAULT_LOG_MAX_BYTES,
                        minimum=1024,
                    ),
                    backupCount=get_env_int(
                        "ASTRA_ERROR_LOG_BACKUP_COUNT",
                        DEFAULT_LOG_BACKUP_COUNT,
                        minimum=1,
                    ),
                    encoding="utf-8",
                )
            )

            error_handler.setLevel(
                logging.ERROR
            )

            error_handler.setFormatter(
                file_formatter
            )

            setattr(
                error_handler,
                "_astra_error_file_handler",
                True,
            )

            logger.addHandler(
                error_handler
            )

        except OSError:

            logger.warning(
                "Unable to initialize ASTRA error log file.",
                exc_info=True,
            )

    return logger


# ============================================================================
# LOGGER HELPERS
# ============================================================================

def get_logger(
    name: str,
) -> logging.Logger:
    """
    Return an ASTRA child logger.

    Example:

        logger = get_logger("TELEGRAM")

    Produces:

        ASTRA.TELEGRAM
    """

    # Ensure the parent logger exists.
    setup_astra_logging()

    return logging.getLogger(
        f"{ASTRA_LOGGER_NAME}.{name}"
    )


def log_startup(
    logger: logging.Logger,
    message: str,
) -> None:
    """Log a startup event using the startup semantic color."""

    logger.info(
        message,
        extra={
            "astra_color": "startup",
        },
    )


def log_shutdown(
    logger: logging.Logger,
    message: str,
) -> None:
    """Log a shutdown event using the shutdown semantic color."""

    logger.info(
        message,
        extra={
            "astra_color": "shutdown",
        },
    )


def log_auth(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
) -> None:
    """Log an authentication/broker event."""

    logger.log(
        level,
        message,
        extra={
            "astra_color": "auth",
        },
    )


def log_wallet(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
) -> None:
    """Log wallet/capital information."""

    logger.log(
        level,
        message,
        extra={
            "astra_color": "wallet",
        },
    )


def log_market(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
) -> None:
    """Log market-data information."""

    logger.log(
        level,
        message,
        extra={
            "astra_color": "market",
        },
    )


def log_buy(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
) -> None:
    """Log a BUY-related event."""

    logger.log(
        level,
        message,
        extra={
            "astra_color": "buy",
        },
    )


def log_sell(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
) -> None:
    """Log a SELL-related event."""

    logger.log(
        level,
        message,
        extra={
            "astra_color": "sell",
        },
    )


def log_risk(
    logger: logging.Logger,
    message: str,
    level: int = logging.WARNING,
) -> None:
    """Log a risk-control event."""

    logger.log(
        level,
        message,
        extra={
            "astra_color": "risk",
        },
    )


def log_tsl(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
) -> None:
    """Log a trailing-stop-loss event."""

    logger.log(
        level,
        message,
        extra={
            "astra_color": "tsl",
        },
    )


def log_intraday(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
) -> None:
    """Log an intraday subsystem event."""

    logger.log(
        level,
        message,
        extra={
            "astra_color": "intraday",
        },
    )


def log_telegram(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
) -> None:
    """Log a Telegram subsystem event."""

    logger.log(
        level,
        message,
        extra={
            "astra_color": "telegram",
        },
    )


def log_sheets(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
) -> None:
    """Log a Google Sheets subsystem event."""

    logger.log(
        level,
        message,
        extra={
            "astra_color": "sheets",
        },
    )


# ============================================================================
# TELEGRAM ALERTS
# ============================================================================

def is_telegram_configured() -> bool:
    """
    Check whether Telegram credentials appear to be configured.

    This is intentionally a local configuration check.

    It does NOT contact Telegram.
    """

    token = get_env(
        "TELEGRAM_BOT_TOKEN"
    )

    chat_id = get_env(
        "TELEGRAM_CHAT_ID"
    )

    if not token or not chat_id:
        return False

    # Catch obvious placeholder configuration without ever printing
    # the credential itself.
    placeholder_markers = (
        "YOUR_",
        "CHANGE_ME",
        "REPLACE_ME",
        "PLACEHOLDER",
    )

    upper_token = token.upper()

    if any(
        marker in upper_token
        for marker in placeholder_markers
    ):
        return False

    return True


def send_telegram_message(
    message: str,
    *,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True,
) -> bool:
    """
    Send a Telegram message using ASTRA's configured bot.

    Returns True on successful HTTP response and False otherwise.

    This function is intentionally best-effort. A Telegram failure must
    never crash the trading/analysis process.
    """

    token = get_env(
        "TELEGRAM_BOT_TOKEN"
    )

    chat_id = get_env(
        "TELEGRAM_CHAT_ID"
    )

    if not token or not chat_id:
        return False

    if not is_telegram_configured():
        return False

    try:
        import requests

    except ImportError:

        logging.getLogger(
            ASTRA_LOGGER_NAME
        ).error(
            "requests is unavailable; Telegram alert cannot be sent.",
            extra={
                "astra_color": "telegram",
            },
        )

        return False

    url = (
        "https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": str(message),
        "disable_web_page_preview": (
            disable_web_page_preview
        ),
    }

    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=get_env_int(
                "TELEGRAM_TIMEOUT",
                15,
                minimum=3,
                maximum=60,
            ),
        )

        if response.ok:
            return True

        logging.getLogger(
            ASTRA_LOGGER_NAME
        ).warning(
            "Telegram request failed: HTTP %s",
            response.status_code,
            extra={
                "astra_color": "telegram",
            },
        )

        return False

    except Exception:

        logging.getLogger(
            ASTRA_LOGGER_NAME
        ).warning(
            "Telegram request raised an exception.",
            exc_info=True,
            extra={
                "astra_color": "telegram",
            },
        )

        return False


def send_critical_failure_alert(
    message: str,
) -> bool:
    """
    Send a high-priority ASTRA failure alert.

    This is used by main.py and the intraday peripheral when a critical
    subsystem failure occurs.

    It deliberately does not attempt any recovery or broker action.
    """

    formatted = (
        "🚨 <b>ASTRA CRITICAL ALERT</b>\n\n"
        f"{message}"
    )

    return send_telegram_message(
        formatted,
        parse_mode="HTML",
    )


# ============================================================================
# EXCEPTION FORMATTING
# ============================================================================

def format_exception(
    exc: BaseException,
    include_traceback: bool = False,
) -> str:
    """
    Convert an exception into a concise log/alert message.
    """

    if include_traceback:

        trace = "".join(
            traceback.format_exception(
                type(exc),
                exc,
                exc.__traceback__,
            )
        )

        return trace.strip()

    return (
        f"{type(exc).__name__}: "
        f"{exc}"
    )


# ============================================================================
# FILESYSTEM HELPERS
# ============================================================================

def ensure_directory(
    path: str | Path,
) -> Path:
    """Create a directory if necessary and return it as Path."""

    directory = Path(path)

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    return directory


def ensure_parent_directory(
    path: str | Path,
) -> Path:
    """Ensure the parent directory of a file exists."""

    file_path = Path(path)

    file_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    return file_path


# ============================================================================
# JSON-SAFE VALUE HELPERS
# ============================================================================

def json_safe(
    value: Any,
) -> Any:
    """
    Convert common Python/numpy values into JSON-compatible values.

    This function intentionally avoids importing numpy/pandas so utils.py
    remains lightweight.
    """

    if value is None:
        return None

    if isinstance(
        value,
        (str, int, float, bool),
    ):
        return value

    if isinstance(
        value,
        Path,
    ):
        return str(value)

    if isinstance(
        value,
        dict,
    ):
        return {
            str(key): json_safe(item)
            for key, item in value.items()
        }

    if isinstance(
        value,
        (list, tuple, set),
    ):
        return [
            json_safe(item)
            for item in value
        ]

    # Handle numpy scalar-like values without importing numpy.
    item_method = getattr(
        value,
        "item",
        None,
    )

    if callable(item_method):

        try:
            return item_method()
        except Exception:
            pass

    # Datetime-like objects.
    isoformat = getattr(
        value,
        "isoformat",
        None,
    )

    if callable(isoformat):

        try:
            return isoformat()
        except Exception:
            pass

    return str(value)


# ============================================================================
# PERCENTAGE HELPERS
# ============================================================================

def normalize_percentage(
    value: Any,
) -> float:
    """
    Normalize percentage-like values into decimal form.

    Examples
    --------
    12.5   -> 0.125
    5      -> 0.05
    -7.5   -> -0.075
    0.125  -> 0.125
    """

    number = safe_float(
        value,
        0.0,
    )

    if -1.0 <= number <= 1.0:
        return number

    return number / 100.0


def percentage_text(
    value: Any,
    decimals: int = 2,
) -> str:
    """
    Format a decimal percentage value.

    Example:
        0.125 -> "12.50%"
    """

    normalized = normalize_percentage(
        value
    )

    return (
        f"{normalized * 100:.{decimals}f}%"
    )


# ============================================================================
# RUNTIME INFORMATION
# ============================================================================

def runtime_info() -> dict[str, Any]:
    """Return basic ASTRA runtime information."""

    return {
        "base_dir": str(BASE_DIR),
        "src_dir": str(SRC_DIR),
        "log_dir": str(LOG_DIR),
        "python_version": (
            f"{sys.version_info.major}."
            f"{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        ),
        "platform": sys.platform,
    }

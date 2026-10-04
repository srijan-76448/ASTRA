"""
ASTRA - Angel One SmartAPI Integration Layer

Responsibilities
----------------
- Maintain one shared Angel One SmartAPI session.
- Authenticate with Angel One using TOTP.
- Prevent authentication retry storms.
- Fetch wallet and holdings truth.
- Cache wallet data for a short period.
- Resolve NSE instrument tokens.
- Expose controlled order-placement access to the autonomous
  intraday subsystem.
- Keep broker/API failures fail-closed.

Important
---------
This module does NOT decide whether ASTRA should buy or sell.

Decision making belongs to:
    decision_engine.py
    intraday_bot.py

This module is only the broker/API boundary.

Wallet semantics
----------------
None / unavailable wallet data means:

    UNKNOWN

It must NEVER be converted to:

    ₹0 cash

because doing so can cause incorrect capital decisions.

Authentication
--------------
Angel One authentication is rate-limited through a configurable cooldown.

A malformed TOTP secret, invalid credentials, network failure, or rejected
session will therefore produce one controlled error followed by cooldown
rather than a rapid authentication loop.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from pyotp import TOTP
from SmartApi import SmartConnect

from utils import get_setting


# ============================================================================
# LOGGER
# ============================================================================

logger = logging.getLogger(
    "ASTRA.SMARTAPI"
)


# ============================================================================
# THIRD-PARTY LOGGER CONTROL
# ============================================================================

def _quiet_smartapi_dependency() -> None:
    """
    Prevent SmartApi's internal logger from flooding ASTRA's console.

    ASTRA owns the user-facing logging format.
    """

    for name in (
        "smartConnect",
        "SmartConnect",
        "smartapi",
        "SmartApi",
    ):

        dependency_logger = logging.getLogger(
            name
        )

        dependency_logger.setLevel(
            logging.WARNING
        )

        dependency_logger.propagate = False


_quiet_smartapi_dependency()


# ============================================================================
# RUNTIME CONFIGURATION
# ============================================================================

# Critical broker credentials remain in .env.
# Runtime-tunable SmartAPI behaviour lives in settings.json and is accessed
# through utils.get_setting().

def _setting_float(
    path: str,
    default: float,
    minimum: Optional[float] = None,
) -> float:

    try:
        value = float(
            get_setting(
                path,
                default,
            )
        )
    except (TypeError, ValueError):
        value = default

    if minimum is not None:
        value = max(
            minimum,
            value,
        )

    return value


def _setting_int(
    path: str,
    default: int,
    minimum: Optional[int] = None,
) -> int:

    try:
        value = int(
            get_setting(
                path,
                default,
            )
        )
    except (TypeError, ValueError):
        value = default

    if minimum is not None:
        value = max(
            minimum,
            value,
        )

    return value


def _setting_bool(
    path: str,
    default: bool,
) -> bool:

    value = get_setting(
        path,
        default,
    )

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    return str(value).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# Defaults preserve the previous SmartAPI behaviour. These values are not
# secrets and may be overridden at runtime through settings.json /set.
DEFAULT_AUTH_RETRY_COOLDOWN = 60.0
DEFAULT_WALLET_CACHE_TTL = 30.0
DEFAULT_WALLET_RETRY_COOLDOWN = 15.0
DEFAULT_WALLET_REFRESH_SPACING = 1.10
DEFAULT_INSTRUMENT_CACHE_TTL = 3600.0


def _load_runtime_config() -> dict[str, float]:
    return {
        "auth_retry_cooldown": _setting_float(
            "SMARTAPI.AUTH_RETRY_COOLDOWN",
            DEFAULT_AUTH_RETRY_COOLDOWN,
            minimum=5.0,
        ),
        "wallet_cache_ttl": _setting_float(
            "SMARTAPI.WALLET_CACHE_TTL",
            DEFAULT_WALLET_CACHE_TTL,
            minimum=0.0,
        ),
        "wallet_retry_cooldown": _setting_float(
            "SMARTAPI.WALLET_RETRY_COOLDOWN",
            DEFAULT_WALLET_RETRY_COOLDOWN,
            minimum=1.0,
        ),
        "wallet_refresh_spacing": _setting_float(
            "SMARTAPI.WALLET_REFRESH_SPACING",
            DEFAULT_WALLET_REFRESH_SPACING,
            minimum=0.0,
        ),
        "instrument_cache_ttl": _setting_float(
            "SMARTAPI.INSTRUMENT_CACHE_TTL",
            DEFAULT_INSTRUMENT_CACHE_TTL,
            minimum=30.0,
        ),
    }


# ============================================================================
# DATA TYPES
# ============================================================================

@dataclass(frozen=True)
class WalletSnapshot:
    """Immutable wallet snapshot."""

    available_cash: float
    holdings: list[dict[str, Any]]
    fetched_at: float


@dataclass(frozen=True)
class Instrument:
    """
    Verified Angel One instrument.

    exchange:
        NSE / BSE etc.

    tradingsymbol:
        Angel One trading symbol, e.g. SYMBOL-EQ.

    symboltoken:
        Angel One instrument token.
    """

    exchange: str
    tradingsymbol: str
    symboltoken: str


class SmartAPIError(RuntimeError):
    """Base ASTRA SmartAPI exception."""


class SmartAPIAuthenticationError(
    SmartAPIError
):
    """Authentication/session error."""


class SmartAPIWalletError(
    SmartAPIError
):
    """Wallet/holdings retrieval error."""


class SmartAPIInstrumentError(
    SmartAPIError
):
    """Instrument-resolution error."""


class SmartAPIOrderError(
    SmartAPIError
):
    """Order-placement error."""


# ============================================================================
# SECRET / PLACEHOLDER VALIDATION
# ============================================================================

_PLACEHOLDER_VALUES = {
    "",
    "YOUR_API_KEY",
    "YOUR_NEW_ANGEL_API_KEY",
    "YOUR_ANGEL_API_KEY",
    "YOUR_CLIENT_CODE",
    "YOUR_NEW_ANGEL_CLIENT_CODE",
    "YOUR_ANGEL_CLIENT_CODE",
    "YOUR_PASSWORD",
    "YOUR_NEW_ANGEL_PASSWORD",
    "YOUR_NEW_ANGEL_PASSWORD_OR_PIN",
    "YOUR_ANGEL_PASSWORD",
    "YOUR_TOTP_SECRET",
    "YOUR_NEW_ANGEL_TOTP_SECRET",
    "YOUR_NEW_ANGEL_TOTP_SECRET",
}


def _is_placeholder(
    value: Optional[str],
) -> bool:

    if value is None:
        return True

    normalized = (
        str(value)
        .strip()
        .upper()
    )

    return (
        normalized in _PLACEHOLDER_VALUES
        or normalized.startswith(
            "YOUR_"
        )
    )


def _validate_base32_secret(
    secret: str,
) -> tuple[bool, str]:
    """
    Validate the TOTP secret before pyotp is invoked.

    We intentionally do not log the actual secret.
    """

    if _is_placeholder(secret):

        return (
            False,
            "ANGEL_TOTP_SECRET is missing or still contains a placeholder.",
        )

    normalized = (
        secret
        .strip()
        .replace(
            " ",
            "",
        )
        .upper()
    )

    # TOTP secrets normally use Base32 alphabet.
    # Padding is optional for pyotp, so we do not require '='.
    if not re.fullmatch(
        r"[A-Z2-7]+=*",
        normalized,
    ):

        return (
            False,
            "ANGEL_TOTP_SECRET contains invalid Base32 characters.",
        )

    try:

        # Constructing TOTP performs pyotp's own validation.
        TOTP(normalized)

    except Exception:

        return (
            False,
            "ANGEL_TOTP_SECRET is not a valid TOTP secret.",
        )

    return (
        True,
        "",
    )


def _mask_identifier(
    value: Optional[str],
) -> str:
    """
    Safe representation for non-secret account identifiers.

    This function is intentionally conservative.
    """

    if not value:
        return "<missing>"

    text = str(value)

    if len(text) <= 4:
        return "****"

    return (
        text[:2]
        + "****"
        + text[-2:]
    )


# ============================================================================
# ANGEL ONE CLIENT
# ============================================================================

class AngelOneClient:
    """
    Shared Angel One SmartAPI client.

    One instance is intended to serve:
        - main.py
        - telegram_bot.py
        - intraday_bot.py

    Authentication is lazy but the factory can perform the initial
    authentication attempt so the application knows immediately whether
    broker access is available.
    """

    def __init__(self) -> None:

        self.api_key = os.getenv(
            "ANGEL_API_KEY",
            "",
        ).strip()

        self.client_code = os.getenv(
            "ANGEL_CLIENT_CODE",
            "",
        ).strip()

        self.password = os.getenv(
            "ANGEL_PASSWORD",
            "",
        ).strip()

        self.totp_secret = os.getenv(
            "ANGEL_TOTP_SECRET",
            "",
        ).strip()

        self.smart_api: Optional[
            SmartConnect
        ] = None

        self._auth_lock = (
            threading.RLock()
        )

        self._data_lock = (
            threading.RLock()
        )

        self._order_lock = (
            threading.RLock()
        )

        self._instrument_lock = (
            threading.RLock()
        )

        self._authenticated = False

        self._last_auth_attempt = 0.0

        self._last_auth_success = 0.0

        self._last_auth_error = ""

        self._last_wallet_error = ""

        self._last_wallet_attempt = 0.0

        self._wallet_cache: Optional[
            WalletSnapshot
        ] = None

        self._instrument_cache: dict[
            str,
            tuple[
                Instrument,
                float,
            ],
        ] = {}

        self._auth_retry_cooldown = 0.0
        self._wallet_cache_ttl = 0.0
        self._wallet_retry_cooldown = 0.0
        self._wallet_refresh_spacing = 0.0
        self._instrument_cache_ttl = 0.0

        self._refresh_runtime_config()

    def _refresh_runtime_config(self) -> None:
        """Refresh non-secret SmartAPI settings from settings.json."""

        config = _load_runtime_config()

        self._auth_retry_cooldown = config[
            "auth_retry_cooldown"
        ]
        self._wallet_cache_ttl = config[
            "wallet_cache_ttl"
        ]
        self._wallet_retry_cooldown = config[
            "wallet_retry_cooldown"
        ]
        self._wallet_refresh_spacing = config[
            "wallet_refresh_spacing"
        ]
        self._instrument_cache_ttl = config[
            "instrument_cache_ttl"
        ]


    # =========================================================================
    # PROPERTIES
    # =========================================================================

    @property
    def is_authenticated(
        self,
    ) -> bool:

        return (
            self._authenticated
            and self.smart_api is not None
        )

    @property
    def last_auth_error(
        self,
    ) -> str:

        return self._last_auth_error

    @property
    def last_wallet_error(
        self,
    ) -> str:

        return self._last_wallet_error

    @property
    def wallet_cache(
        self,
    ) -> Optional[WalletSnapshot]:

        return self._wallet_cache

    # =========================================================================
    # CONFIGURATION VALIDATION
    # =========================================================================

    def _validate_credentials(
        self,
    ) -> tuple[
        bool,
        str,
    ]:

        missing: list[str] = []

        values = (
            (
                "ANGEL_API_KEY",
                self.api_key,
            ),
            (
                "ANGEL_CLIENT_CODE",
                self.client_code,
            ),
            (
                "ANGEL_PASSWORD",
                self.password,
            ),
            (
                "ANGEL_TOTP_SECRET",
                self.totp_secret,
            ),
        )

        for name, value in values:

            if _is_placeholder(value):
                missing.append(name)

        if missing:

            return (
                False,
                (
                    "Missing or placeholder "
                    "Angel One configuration: "
                    + ", ".join(missing)
                ),
            )

        valid_totp, totp_error = (
            _validate_base32_secret(
                self.totp_secret
            )
        )

        if not valid_totp:

            return (
                False,
                totp_error,
            )

        return (
            True,
            "",
        )

    # =========================================================================
    # AUTHENTICATION
    # =========================================================================

    def authenticate(
        self,
        force: bool = False,
    ) -> bool:
        """
        Authenticate with Angel One.

        Authentication is deliberately fail-closed and rate-limited.
        """

        with self._auth_lock:

            self._refresh_runtime_config()

            if (
                self.is_authenticated
                and not force
            ):
                return True

            now = time.monotonic()

            if (
                not force
                and (
                    now
                    - self._last_auth_attempt
                    < self._auth_retry_cooldown
                )
            ):

                remaining = max(
                    0,
                    int(
                        self._auth_retry_cooldown
                        - (
                            now
                            - self._last_auth_attempt
                        )
                    ),
                )

                logger.warning(
                    "Angel One authentication "
                    "cooldown active (%ss remaining).",
                    remaining,
                )

                return False

            valid, error = (
                self._validate_credentials()
            )

            if not valid:

                self._last_auth_attempt = now
                self._last_auth_error = error
                self._authenticated = False
                self.smart_api = None

                logger.error(
                    "Angel One authentication "
                    "configuration invalid: %s",
                    error,
                )

                return False

            self._last_auth_attempt = now
            self._last_auth_error = ""

            logger.info(
                "Authenticating with Angel One "
                "SmartAPI for client %s...",
                _mask_identifier(
                    self.client_code
                ),
            )

            try:

                api = SmartConnect(
                    api_key=self.api_key
                )

                # ---------------------------------------------------------
                # TOTP
                # ---------------------------------------------------------

                try:

                    totp = TOTP(
                        self.totp_secret
                    ).now()

                except Exception:

                    self._authenticated = False
                    self.smart_api = None
                    self._last_auth_error = (
                        "Unable to generate Angel One TOTP."
                    )

                    logger.error(
                        "Angel One authentication failed: "
                        "TOTP generation error."
                    )

                    return False

                # ---------------------------------------------------------
                # SESSION
                # ---------------------------------------------------------

                response = (
                    api.generateSession(
                        self.client_code,
                        self.password,
                        totp,
                    )
                )

                if not isinstance(
                    response,
                    dict,
                ):

                    response = {}

                if not response.get(
                    "status"
                ):

                    message = str(
                        response.get(
                            "message",
                            "Angel One rejected the session.",
                        )
                    )

                    self._authenticated = False
                    self.smart_api = None
                    self._last_auth_error = message

                    logger.error(
                        "Angel One authentication failed: %s",
                        message,
                    )

                    return False

                # ---------------------------------------------------------
                # SUCCESS
                # ---------------------------------------------------------

                self.smart_api = api
                self._authenticated = True
                self._last_auth_success = (
                    time.monotonic()
                )
                self._last_auth_error = ""

                # New authentication invalidates cached account data.
                self._wallet_cache = None
                self._last_wallet_error = ""

                logger.info(
                    "Successfully authenticated with "
                    "Angel One SmartAPI."
                )

                return True

            except Exception as exc:

                self._authenticated = False
                self.smart_api = None
                self._last_auth_error = (
                    str(exc)
                )

                # Never print credentials or TOTP values.
                logger.error(
                    "Angel One authentication failed: %s",
                    type(exc).__name__,
                )

                logger.debug(
                    "Angel One authentication "
                    "exception detail: %s",
                    exc,
                )

                return False

    # =========================================================================
    # SESSION MANAGEMENT
    # =========================================================================

    def logout(
        self,
    ) -> None:
        """Invalidate the local session."""

        with self._auth_lock:

            api = self.smart_api

            try:

                logout = getattr(
                    api,
                    "terminateSession",
                    None,
                )

                if callable(logout):
                    logout(
                        self.client_code
                    )

            except Exception:

                logger.debug(
                    "Angel One logout request failed.",
                    exc_info=True,
                )

            finally:

                self.smart_api = None
                self._authenticated = False
                self._wallet_cache = None

                logger.info(
                    "Angel One SmartAPI session "
                    "cleared."
                )

    def _ensure_session(
        self,
    ) -> None:

        if self.is_authenticated:
            return

        if not self.authenticate():

            raise SmartAPIAuthenticationError(
                self._last_auth_error
                or "Angel One authentication failed."
            )

    def _invalidate_session(
        self,
        reason: str = "",
    ) -> None:

        with self._auth_lock:

            self._authenticated = False
            self.smart_api = None

            if reason:
                self._last_auth_error = reason

    # =========================================================================
    # API ACCESS
    # =========================================================================

    def _api(
        self,
    ) -> SmartConnect:

        self._ensure_session()

        if self.smart_api is None:

            raise SmartAPIAuthenticationError(
                "Angel One SmartAPI session is unavailable."
            )

        return self.smart_api

    # =========================================================================
    # HOLDINGS PARSER
    # =========================================================================

    @staticmethod
    def _parse_holdings(
        raw: Any,
    ) -> list[
        dict[str, Any]
    ]:
        """
        Convert Angel One holdings into ASTRA's normalized structure.
        """

        result: list[
            dict[str, Any]
        ] = []

        if not isinstance(
            raw,
            list,
        ):
            return result

        for item in raw:

            if not isinstance(
                item,
                dict,
            ):
                logger.warning(
                    "Skipping malformed "
                    "Angel One holding entry."
                )

                continue

            try:

                qty = int(
                    float(
                        item.get(
                            "quantity",
                            0,
                        )
                        or 0
                    )
                )

                avg_price = float(
                    item.get(
                        "averageprice",
                        item.get(
                            "averagePrice",
                            0,
                        ),
                    )
                    or 0
                )

                ltp = float(
                    item.get(
                        "ltp",
                        0,
                    )
                    or 0
                )

                pnl = float(
                    item.get(
                        "pnl",
                        0,
                    )
                    or 0
                )

                ticker = str(
                    item.get(
                        "tradingsymbol",
                        item.get(
                            "symbol",
                            "",
                        ),
                    )
                    or ""
                ).strip()

                if not ticker:

                    logger.warning(
                        "Skipping Angel One "
                        "holding without trading symbol."
                    )

                    continue

                invested_value = (
                    qty
                    * avg_price
                )

                current_value = (
                    qty
                    * ltp
                )

                pnl_pct = (
                    (
                        pnl
                        / invested_value
                    )
                    * 100.0
                    if invested_value > 0
                    else 0.0
                )

                result.append(
                    {
                        "ticker": ticker,
                        "qty": qty,
                        "avg_price": avg_price,
                        "current_price": ltp,
                        "ltp": ltp,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "invested_val": invested_value,
                        "current_val": current_value,
                    }
                )

            except (
                TypeError,
                ValueError,
            ):

                logger.warning(
                    "Skipping malformed Angel One holding."
                )

        return result

    # =========================================================================
    # WALLET VALIDATION
    # =========================================================================

    @staticmethod
    def _validate_wallet_response(
        data: Any,
    ) -> tuple[
        bool,
        float,
        list[dict[str, Any]],
        str,
    ]:
        """
        Validate an already-normalized wallet response.
        """

        if not isinstance(
            data,
            dict,
        ):

            return (
                False,
                0.0,
                [],
                "Wallet response is not a dictionary.",
            )

        if (
            "available_cash"
            not in data
        ):

            return (
                False,
                0.0,
                [],
                "Wallet response has no available_cash field.",
            )

        try:

            available_cash = float(
                data[
                    "available_cash"
                ]
            )

        except (
            TypeError,
            ValueError,
        ):

            return (
                False,
                0.0,
                [],
                "available_cash is not numeric.",
            )

        holdings = data.get(
            "holdings"
        )

        if not isinstance(
            holdings,
            list,
        ):

            return (
                False,
                0.0,
                [],
                "Wallet response has no valid holdings list.",
            )

        if available_cash != available_cash:
            return (
                False,
                0.0,
                [],
                "available_cash is NaN.",
            )

        if available_cash == float(
            "inf"
        ) or available_cash == -float(
            "inf"
        ):

            return (
                False,
                0.0,
                [],
                "available_cash is infinite.",
            )

        return (
            True,
            available_cash,
            holdings,
            "",
        )

    # =========================================================================
    # WALLET DATA
    # =========================================================================

    def _cached_wallet_if_valid(
        self,
        now: float,
    ) -> Optional[
        dict[str, Any]
    ]:

        snapshot = (
            self._wallet_cache
        )

        if snapshot is None:
            return None

        age = (
            now
            - snapshot.fetched_at
        )

        if (
            self._wallet_cache_ttl <= 0
            or age > self._wallet_cache_ttl
        ):
            return None

        return {
            "available_cash": (
                snapshot.available_cash
            ),
            "holdings": list(
                snapshot.holdings
            ),
            "fetched_at": (
                snapshot.fetched_at
            ),
            "cached": True,
        }

    def get_real_portfolio_data(
        self,
        force_refresh: bool = False,
    ) -> Optional[
        dict[str, Any]
    ]:
        """
        Return complete broker wallet state.

        Returns None when the broker state is unavailable.

        IMPORTANT:
            None != zero cash.
        """

        with self._data_lock:

            self._refresh_runtime_config()

            now = time.monotonic()

            # -------------------------------------------------------------
            # CACHE
            # -------------------------------------------------------------

            if not force_refresh:

                cached = (
                    self._cached_wallet_if_valid(
                        now
                    )
                )

                if cached is not None:

                    logger.debug(
                        "Using cached Angel One "
                        "wallet snapshot."
                    )

                    return cached

            # -------------------------------------------------------------
            # RETRY COOLDOWN AFTER FAILURE
            # -------------------------------------------------------------

            if (
                not force_refresh
                and (
                    now
                    - self._last_wallet_attempt
                    < self._wallet_retry_cooldown
                )
                and self._last_wallet_error
            ):

                logger.warning(
                    "Skipping Angel One wallet "
                    "refresh: retry cooldown active."
                )

                return None

            # -------------------------------------------------------------
            # API
            # -------------------------------------------------------------

            self._last_wallet_attempt = now

            try:

                api = self._api()

                # Avoid unnecessary back-to-back broker calls.
                if (
                    self._wallet_refresh_spacing
                    > 0
                ):

                    time.sleep(
                        self._wallet_refresh_spacing
                    )

                # ---------------------------------------------------------
                # RMS
                # ---------------------------------------------------------

                rms_data = (
                    api.rmsLimit()
                )

                if not isinstance(
                    rms_data,
                    dict,
                ):

                    raise SmartAPIWalletError(
                        "RMS response is invalid."
                    )

                if not rms_data.get(
                    "status"
                ):

                    raise SmartAPIWalletError(
                        "RMS balance unavailable: "
                        + str(
                            rms_data.get(
                                "message",
                                "no response",
                            )
                        )
                    )

                rms_payload = (
                    rms_data.get(
                        "data"
                    )
                    or {}
                )

                if "net" not in rms_payload:

                    raise SmartAPIWalletError(
                        "RMS response did not contain "
                        "'net' available cash."
                    )

                available_cash = float(
                    rms_payload[
                        "net"
                    ]
                )

                # ---------------------------------------------------------
                # HOLDINGS
                # ---------------------------------------------------------

                holdings_data = (
                    api.holding()
                )

                if not isinstance(
                    holdings_data,
                    dict,
                ):

                    raise SmartAPIWalletError(
                        "Holdings response is invalid."
                    )

                if not holdings_data.get(
                    "status"
                ):

                    raise SmartAPIWalletError(
                        "Holdings unavailable: "
                        + str(
                            holdings_data.get(
                                "message",
                                "no response",
                            )
                        )
                    )

                holdings = (
                    self._parse_holdings(
                        holdings_data.get(
                            "data"
                        )
                        or []
                    )
                )

                # ---------------------------------------------------------
                # VALIDATE
                # ---------------------------------------------------------

                snapshot = (
                    WalletSnapshot(
                        available_cash=available_cash,
                        holdings=holdings,
                        fetched_at=time.monotonic(),
                    )
                )

                self._wallet_cache = (
                    snapshot
                )

                self._last_wallet_error = ""

                return {
                    "available_cash": (
                        snapshot.available_cash
                    ),
                    "holdings": list(
                        snapshot.holdings
                    ),
                    "fetched_at": time.time(),
                    "cached": False,
                }

            except SmartAPIAuthenticationError:

                self._last_wallet_error = (
                    "Authentication unavailable."
                )

                logger.error(
                    "Angel One wallet unavailable: "
                    "authentication failed."
                )

                return None

            except Exception as exc:

                self._last_wallet_error = (
                    str(exc)
                )

                logger.error(
                    "Angel One wallet request failed: %s",
                    type(exc).__name__,
                )

                logger.debug(
                    "Angel One wallet error detail: %s",
                    exc,
                )

                return None

    # =========================================================================
    # WALLET SNAPSHOT
    # =========================================================================

    def get_wallet_snapshot(
        self,
        force_refresh: bool = False,
    ) -> Optional[
        WalletSnapshot
    ]:

        data = (
            self.get_real_portfolio_data(
                force_refresh=force_refresh
            )
        )

        if data is None:
            return None

        try:

            return WalletSnapshot(
                available_cash=float(
                    data[
                        "available_cash"
                    ]
                ),
                holdings=list(
                    data[
                        "holdings"
                    ]
                ),
                fetched_at=float(
                    data.get(
                        "fetched_at",
                        time.time(),
                    )
                ),
            )

        except (
            TypeError,
            ValueError,
            KeyError,
        ):

            logger.error(
                "Failed to convert Angel One "
                "wallet response into WalletSnapshot."
            )

            return None

    # =========================================================================
    # INSTRUMENT NORMALIZATION
    # =========================================================================

    @staticmethod
    def _normalize_symbol(
        symbol: str,
    ) -> str:

        value = (
            str(
                symbol
                or ""
            )
            .strip()
            .upper()
        )

        value = value.replace(
            " ",
            "",
        )

        if value.endswith(
            ".NS"
        ):
            value = value[:-3]

        if value.endswith(
            ".BO"
        ):
            value = value[:-3]

        for suffix in (
            "-EQ",
            "-BE",
            "-BL",
            "-BZ",
            "-SM",
            "-ST",
        ):

            if value.endswith(
                suffix
            ):

                value = value[
                    : -len(suffix)
                ]

                break

        return value

    @classmethod
    def _candidate_trading_symbols(
        cls,
        symbol: str,
    ) -> list[str]:

        base = cls._normalize_symbol(
            symbol
        )

        if not base:
            return []

        return [
            f"{base}-EQ",
            base,
        ]

    # =========================================================================
    # INSTRUMENT RESOLUTION
    # =========================================================================

    def resolve_instrument(
        self,
        symbol: str,
        exchange: str = "NSE",
    ) -> dict[str, str]:
        """
        Resolve a broker instrument.

        Returns:
            {
                "exchange": "NSE",
                "tradingsymbol": "XYZ-EQ",
                "symboltoken": "12345",
            }
        """

        normalized = (
            self._normalize_symbol(
                symbol
            )
        )

        if not normalized:

            raise SmartAPIInstrumentError(
                "Cannot resolve an empty trading symbol."
            )

        cache_key = (
            f"{exchange.upper()}:{normalized}"
        )

        now = time.monotonic()

        with self._instrument_lock:

            self._refresh_runtime_config()

            cached = (
                self._instrument_cache.get(
                    cache_key
                )
            )

            if cached is not None:

                instrument, cached_at = (
                    cached
                )

                if (
                    now - cached_at
                    <= self._instrument_cache_ttl
                ):

                    return {
                        "exchange": (
                            instrument.exchange
                        ),
                        "tradingsymbol": (
                            instrument.tradingsymbol
                        ),
                        "symboltoken": (
                            instrument.symboltoken
                        ),
                    }

                del self._instrument_cache[
                    cache_key
                ]

            api = self._api()

            search = getattr(
                api,
                "searchScrip",
                None,
            )

            if not callable(search):

                raise SmartAPIInstrumentError(
                    "Angel One SDK does not expose searchScrip()."
                )

            last_error = ""

            for candidate in (
                self._candidate_trading_symbols(
                    normalized
                )
            ):

                try:

                    response = (
                        search(
                            exchange=exchange.upper(),
                            searchscrip=candidate,
                        )
                    )

                except Exception as exc:

                    last_error = str(
                        exc
                    )

                    continue

                if not isinstance(
                    response,
                    dict,
                ):
                    continue

                rows = (
                    response.get(
                        "data"
                    )
                    or []
                )

                if not isinstance(
                    rows,
                    list,
                ):
                    continue

                # Prefer an exact trading-symbol match.
                for row in rows:

                    if not isinstance(
                        row,
                        dict,
                    ):
                        continue

                    trading_symbol = str(
                        row.get(
                            "tradingsymbol",
                            "",
                        )
                        or ""
                    ).upper()

                    token = str(
                        row.get(
                            "symboltoken",
                            row.get(
                                "token",
                                "",
                            ),
                        )
                        or ""
                    )

                    if (
                        trading_symbol
                        == candidate.upper()
                        and token
                    ):

                        instrument = (
                            Instrument(
                                exchange=exchange.upper(),
                                tradingsymbol=trading_symbol,
                                symboltoken=token,
                            )
                        )

                        self._instrument_cache[
                            cache_key
                        ] = (
                            instrument,
                            now,
                        )

                        logger.debug(
                            "Resolved Angel One instrument "
                            "%s -> token %s.",
                            trading_symbol,
                            token,
                        )

                        return {
                            "exchange": (
                                instrument.exchange
                            ),
                            "tradingsymbol": (
                                instrument.tradingsymbol
                            ),
                            "symboltoken": (
                                instrument.symboltoken
                            ),
                        }

                # Fall back to the first valid NSE result.
                for row in rows:

                    if not isinstance(
                        row,
                        dict,
                    ):
                        continue

                    trading_symbol = str(
                        row.get(
                            "tradingsymbol",
                            "",
                        )
                        or ""
                    )

                    token = str(
                        row.get(
                            "symboltoken",
                            row.get(
                                "token",
                                "",
                            ),
                        )
                        or ""
                    )

                    if (
                        trading_symbol
                        and token
                    ):

                        instrument = (
                            Instrument(
                                exchange=exchange.upper(),
                                tradingsymbol=trading_symbol,
                                symboltoken=token,
                            )
                        )

                        self._instrument_cache[
                            cache_key
                        ] = (
                            instrument,
                            now,
                        )

                        return {
                            "exchange": (
                                instrument.exchange
                            ),
                            "tradingsymbol": (
                                instrument.tradingsymbol
                            ),
                            "symboltoken": (
                                instrument.symboltoken
                            ),
                        }

            if last_error:

                raise SmartAPIInstrumentError(
                    f"Unable to resolve {normalized}: "
                    f"{last_error}"
                )

            raise SmartAPIInstrumentError(
                f"No verified {exchange.upper()} "
                f"instrument found for {normalized}."
            )

    # =========================================================================
    # SEARCH COMPATIBILITY
    # =========================================================================

    def searchScrip(
        self,
        exchange: str = "NSE",
        searchscrip: str = "",
    ) -> dict[str, Any]:
        """
        Compatibility wrapper used by intraday_bot.py.

        This delegates to Angel One's native searchScrip().
        """

        api = self._api()

        search = getattr(
            api,
            "searchScrip",
            None,
        )

        if not callable(search):

            raise SmartAPIInstrumentError(
                "Angel One SDK does not expose searchScrip()."
            )

        try:

            response = (
                search(
                    exchange=exchange,
                    searchscrip=searchscrip,
                )
            )

        except Exception as exc:

            logger.error(
                "Angel One instrument search failed: %s",
                type(exc).__name__,
            )

            raise SmartAPIInstrumentError(
                str(exc)
            ) from exc

        if not isinstance(
            response,
            dict,
        ):

            raise SmartAPIInstrumentError(
                "Angel One instrument search returned invalid data."
            )

        return response

    # =========================================================================
    # ORDER VALIDATION
    # =========================================================================

    @staticmethod
    def _validate_order_params(
        order_params: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Validate the minimum order fields before the broker call.

        This does not make a trading decision. It only prevents malformed
        API requests.
        """

        if not isinstance(
            order_params,
            dict,
        ):

            raise SmartAPIOrderError(
                "Order parameters must be a dictionary."
            )

        required = (
            "variety",
            "tradingsymbol",
            "symboltoken",
            "transactiontype",
            "exchange",
            "ordertype",
            "producttype",
            "duration",
            "quantity",
        )

        missing = [
            key
            for key in required
            if not order_params.get(
                key
            )
        ]

        if missing:

            raise SmartAPIOrderError(
                "Order missing required fields: "
                + ", ".join(missing)
            )

        transaction = str(
            order_params[
                "transactiontype"
            ]
        ).upper()

        if transaction not in {
            "BUY",
            "SELL",
        }:

            raise SmartAPIOrderError(
                "Invalid transaction type."
            )

        exchange = str(
            order_params[
                "exchange"
            ]
        ).upper()

        if exchange not in {
            "NSE",
            "BSE",
        }:

            raise SmartAPIOrderError(
                "Unsupported exchange."
            )

        try:

            quantity = int(
                float(
                    order_params[
                        "quantity"
                    ]
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            raise SmartAPIOrderError(
                "Order quantity is invalid."
            )

        if quantity <= 0:

            raise SmartAPIOrderError(
                "Order quantity must be greater than zero."
            )

        sanitized = dict(
            order_params
        )

        sanitized[
            "quantity"
        ] = str(
            quantity
        )

        return sanitized

    # =========================================================================
    # ORDER PLACEMENT
    # =========================================================================

    def placeOrder(
        self,
        order_params: dict[str, Any],
    ) -> Optional[str]:
        """
        Controlled broker order-placement boundary.

        The intraday risk gateway must make the decision before reaching here.

        This method does NOT autonomously create a strategy decision.
        """

        sanitized = (
            self._validate_order_params(
                order_params
            )
        )

        with self._order_lock:

            api = self._api()

            place_order = getattr(
                api,
                "placeOrder",
                None,
            )

            if not callable(
                place_order
            ):

                raise SmartAPIOrderError(
                    "Angel One SDK does not expose placeOrder()."
                )

            side = str(
                sanitized.get(
                    "transactiontype",
                    "",
                )
            ).upper()

            ticker = str(
                sanitized.get(
                    "tradingsymbol",
                    "",
                )
            )

            logger.warning(
                "Submitting Angel One %s order for %s.",
                side,
                ticker,
            )

            try:

                response = (
                    place_order(
                        sanitized
                    )
                )

            except Exception as exc:

                logger.error(
                    "Angel One %s order failed for %s: %s",
                    side,
                    ticker,
                    type(exc).__name__,
                )

                logger.debug(
                    "Angel One order error detail: %s",
                    exc,
                )

                raise SmartAPIOrderError(
                    str(exc)
                ) from exc

            # SmartAPI commonly returns the order ID directly.
            if isinstance(
                response,
                str,
            ):

                order_id = response.strip()

                if order_id:

                    logger.info(
                        "Angel One %s order accepted | "
                        "symbol=%s | order_id=%s",
                        side,
                        ticker,
                        order_id,
                    )

                    return order_id

            # Some wrappers may return a dict.
            if isinstance(
                response,
                dict,
            ):

                if not response.get(
                    "status",
                    True,
                ):

                    message = str(
                        response.get(
                            "message",
                            "Order rejected.",
                        )
                    )

                    raise SmartAPIOrderError(
                        message
                    )

                data = response.get(
                    "data"
                )

                order_id = None

                if isinstance(
                    data,
                    dict,
                ):

                    order_id = (
                        data.get(
                            "orderid"
                        )
                        or data.get(
                            "orderId"
                        )
                        or data.get(
                            "order_id"
                        )
                    )

                if not order_id:

                    order_id = (
                        response.get(
                            "orderid"
                        )
                        or response.get(
                            "orderId"
                        )
                        or response.get(
                            "order_id"
                        )
                    )

                if order_id:

                    logger.info(
                        "Angel One %s order accepted | "
                        "symbol=%s | order_id=%s",
                        side,
                        ticker,
                        order_id,
                    )

                    return str(
                        order_id
                    )

            logger.error(
                "Angel One returned an unrecognized "
                "order response for %s.",
                ticker,
            )

            raise SmartAPIOrderError(
                "Angel One returned no usable order ID."
            )

    # =========================================================================
    # ORDER STATUS
    # =========================================================================

    def orderBook(
        self,
    ) -> dict[str, Any]:
        """Return Angel One's order book."""

        api = self._api()

        method = getattr(
            api,
            "orderBook",
            None,
        )

        if not callable(
            method
        ):

            raise SmartAPIOrderError(
                "Angel One SDK does not expose orderBook()."
            )

        try:

            response = method()

        except Exception as exc:

            logger.error(
                "Angel One orderBook request failed: %s",
                type(exc).__name__,
            )

            raise SmartAPIOrderError(
                str(exc)
            ) from exc

        if not isinstance(
            response,
            dict,
        ):

            raise SmartAPIOrderError(
                "Angel One orderBook returned invalid data."
            )

        return response

    def get_order_status(
        self,
        order_id: str,
    ) -> Optional[dict[str, Any]]:
        """
        Search the broker order book for a specific order ID.

        This is intentionally available for future fill verification.
        """

        if not order_id:
            return None

        try:

            response = (
                self.orderBook()
            )

        except Exception:

            return None

        rows = (
            response.get(
                "data"
            )
            if isinstance(
                response,
                dict,
            )
            else None
        )

        if not isinstance(
            rows,
            list,
        ):
            return None

        target = str(
            order_id
        )

        for row in rows:

            if not isinstance(
                row,
                dict,
            ):
                continue

            candidate = str(
                row.get(
                    "orderid",
                    row.get(
                        "orderId",
                        "",
                    ),
                )
                or ""
            )

            if candidate == target:
                return row

        return None

    # =========================================================================
    # POSITIONS
    # =========================================================================

    def position(
        self,
    ) -> dict[str, Any]:
        """Return broker positions."""

        api = self._api()

        method = getattr(
            api,
            "position",
            None,
        )

        if not callable(
            method
        ):

            raise SmartAPIError(
                "Angel One SDK does not expose position()."
            )

        try:

            response = method()

        except Exception as exc:

            logger.error(
                "Angel One position request failed: %s",
                type(exc).__name__,
            )

            raise SmartAPIError(
                str(exc)
            ) from exc

        if not isinstance(
            response,
            dict,
        ):

            raise SmartAPIError(
                "Angel One position response is invalid."
            )

        return response

    # =========================================================================
    # RAW API HELPERS
    # =========================================================================

    def rmsLimit(
        self,
    ) -> dict[str, Any]:
        """Expose RMS balance API for controlled internal use."""

        api = self._api()

        method = getattr(
            api,
            "rmsLimit",
            None,
        )

        if not callable(
            method
        ):

            raise SmartAPIWalletError(
                "Angel One SDK does not expose rmsLimit()."
            )

        return method()

    def holding(
        self,
    ) -> dict[str, Any]:
        """Expose holdings API for controlled internal use."""

        api = self._api()

        method = getattr(
            api,
            "holding",
            None,
        )

        if not callable(
            method
        ):

            raise SmartAPIWalletError(
                "Angel One SDK does not expose holding()."
            )

        return method()


# ============================================================================
# SHARED CLIENT
# ============================================================================

_CLIENT: Optional[
    AngelOneClient
] = None

_CLIENT_LOCK = (
    threading.RLock()
)


def get_smartapi_client(
    authenticate: bool = True,
) -> AngelOneClient:
    """
    Return the process-wide Angel One client.

    A single shared object prevents multiple modules from creating independent
    SmartConnect sessions.

    If initial authentication fails, the client is still returned. This allows
    ASTRA to continue running in fail-closed / data-independent mode and retry
    later according to the authentication cooldown.
    """

    global _CLIENT

    with _CLIENT_LOCK:

        if _CLIENT is None:

            _CLIENT = AngelOneClient()

        client = _CLIENT

    if authenticate:

        client.authenticate()

    return client


def reset_smartapi_client() -> None:
    """
    Explicitly destroy the shared broker session.

    Primarily useful during graceful shutdown or controlled testing.
    """

    global _CLIENT

    with _CLIENT_LOCK:

        if _CLIENT is not None:

            try:
                _CLIENT.logout()

            except Exception:

                logger.debug(
                    "SmartAPI client logout failed.",
                    exc_info=True,
                )

        _CLIENT = None


# ============================================================================
# COMPATIBILITY HELPERS
# ============================================================================

def get_wallet_snapshot(
    force_refresh: bool = False,
) -> Optional[
    WalletSnapshot
]:
    """
    Convenience wrapper around the shared client.
    """

    return (
        get_smartapi_client(
            authenticate=False
        )
        .get_wallet_snapshot(
            force_refresh=force_refresh
        )
    )


def get_real_portfolio_data(
    force_refresh: bool = False,
) -> Optional[
    dict[str, Any]
]:
    """
    Convenience wrapper around the shared client.
    """

    return (
        get_smartapi_client(
            authenticate=False
        )
        .get_real_portfolio_data(
            force_refresh=force_refresh
        )
    )

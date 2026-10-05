"""
ASTRA V3 - SIP Engine
======================

Responsibilities
----------------
- Maintain the runtime state of ASTRA's SIP subsystem.
- Activate/deactivate the SIP subsystem independently from V1 and V2.
- Create and manage SIP targets.
- Maintain SIP target lifecycle state.
- Track scheduled SIP execution metadata.
- Record SIP decision telemetry.
- Preserve historical decision records for later persistence.
- Provide a clean interface for SQLite and Google Sheets integrations.

Architecture
------------
V1:
    Normal ASTRA market analysis.

V2:
    Smart Intraday subsystem.

V3:
    SIP subsystem.

The SIP engine is intentionally independent from:
    - intraday_bot.py
    - broker order execution
    - Google Sheets
    - SQLite persistence
    - Telegram transport

Those integrations belong to their respective layers.

Important
---------
Activating ASTRA itself does NOT activate SIP.

The SIP engine becomes active only when the runtime control layer
explicitly calls:

    activate_sip()

Creating a SIP target also requires the SIP engine to be active.

Decision telemetry
------------------
The engine can record why a SIP decision was made.

A decision may contain:
    - price
    - RSI
    - MACD
    - trend
    - market condition
    - allocation
    - risk checks
    - technical signals
    - decision
    - reason

The decision structure is deliberately extensible so future versions
can add additional indicators and market-analysis fields without
changing the core SIP target model.

Persistence
-----------
This module currently keeps runtime state in memory.

SQLite persistence will be added through mng_db.py.

Google Sheets synchronization will be added through portfolio_fetcher.py.

The intended architecture is:

    SIP Engine
        |
        +---- SQLite authoritative state
        |
        +---- Google Sheets human-readable telemetry
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger("ASTRA_SIP")


# ============================================================================
# TIMEZONE
# ============================================================================

IST = dt.timezone(
    dt.timedelta(
        hours=5,
        minutes=30,
    )
)


# ============================================================================
# CONSTANTS
# ============================================================================

DEFAULT_FREQUENCY = "MONTHLY"

VALID_FREQUENCIES = {
    "WEEKLY",
    "BIWEEKLY",
    "MONTHLY",
    "QUARTERLY",
    "YEARLY",
}

DEFAULT_DURATION_DAYS = 365

MIN_SIP_AMOUNT = 1.0

DECISION_HISTORY_LIMIT = 5000


# ============================================================================
# ENUMS
# ============================================================================

class SIPTargetStatus(str, Enum):
    """
    Lifecycle state of a SIP target.
    """

    ACTIVE = "ACTIVE"

    PAUSED = "PAUSED"

    CANCELLED = "CANCELLED"

    EXPIRED = "EXPIRED"

    COMPLETED = "COMPLETED"


# ============================================================================
# EXCEPTIONS
# ============================================================================

class SIPError(Exception):
    """
    Base exception for SIP subsystem errors.
    """


class SIPNotActiveError(SIPError):
    """
    Raised when an operation requires an active SIP engine.
    """


class SIPTargetNotFoundError(SIPError):
    """
    Raised when a requested SIP target does not exist.
    """


class SIPTargetStateError(SIPError):
    """
    Raised when an operation is invalid for the target's current state.
    """


class SIPValidationError(SIPError):
    """
    Raised when SIP configuration or target data is invalid.
    """


# ============================================================================
# SIP TARGET
# ============================================================================

@dataclass
class SIPTarget:
    """
    Represents one SIP target/plan.

    target_id
        Unique ASTRA-generated identifier.

    asset
        Asset symbol associated with the SIP.

    amount
        Intended contribution amount per execution.

    frequency
        Contribution frequency.

    start_at
        Start time of the SIP target.

    expires_at
        End time of the SIP target.

    status
        Current lifecycle state.

    next_execution_at
        Next scheduled contribution/evaluation time.

    completed_contributions
        Number of completed contributions.

    total_invested
        Total amount invested through this target.

    metadata
        Extensible target-specific information.
    """

    target_id: str

    asset: str

    amount: float

    frequency: str

    start_at: dt.datetime

    expires_at: dt.datetime

    status: SIPTargetStatus = SIPTargetStatus.ACTIVE

    created_at: dt.datetime = field(
        default_factory=lambda: dt.datetime.now(IST)
    )

    next_execution_at: Optional[dt.datetime] = None

    completed_contributions: int = 0

    total_invested: float = 0.0

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )

    def is_expired(
        self,
        now: Optional[dt.datetime] = None,
    ) -> bool:
        """
        Return True when the target has reached its expiry time.
        """

        current_time = now or dt.datetime.now(IST)

        return current_time >= self.expires_at

    def refresh_status(
        self,
        now: Optional[dt.datetime] = None,
    ) -> SIPTargetStatus:
        """
        Refresh automatic expiry state.

        Only ACTIVE targets are automatically transitioned to EXPIRED.

        PAUSED targets remain PAUSED so that a future resume operation can
        explicitly decide whether the target is still eligible.
        """

        if (
            self.status == SIPTargetStatus.ACTIVE
            and self.is_expired(now)
        ):
            self.status = SIPTargetStatus.EXPIRED

        return self.status

    def is_operational(self) -> bool:
        """
        Return whether the target is currently operational.
        """

        return self.status == SIPTargetStatus.ACTIVE

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize the SIP target into a dictionary.
        """

        return {
            "target_id": self.target_id,
            "asset": self.asset,
            "amount": self.amount,
            "frequency": self.frequency,
            "start_at": self.start_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "next_execution_at": (
                self.next_execution_at.isoformat()
                if self.next_execution_at is not None
                else None
            ),
            "completed_contributions": self.completed_contributions,
            "total_invested": self.total_invested,
            "metadata": copy.deepcopy(self.metadata),
        }


# ============================================================================
# SIP DECISION
# ============================================================================

@dataclass
class SIPDecision:
    """
    Historical SIP decision telemetry.

    This object represents the reasoning snapshot taken when ASTRA
    evaluates a SIP target.

    The structure is intentionally extensible.

    Core fields:
        timestamp
        target_id
        asset
        decision
        price
        allocation
        reason

    Technical-analysis fields:
        rsi
        macd
        trend
        technical_signals

    Market-analysis fields:
        market_condition
        market_context

    Risk fields:
        risk_checks
        risk_passed

    Additional fields:
        metadata
    """

    timestamp: dt.datetime

    target_id: str

    asset: str

    decision: str

    price: Optional[float] = None

    rsi: Optional[float] = None

    macd: Optional[float] = None

    trend: Optional[str] = None

    market_condition: Optional[str] = None

    allocation: Optional[float] = None

    reason: str = ""

    technical_signals: Dict[str, Any] = field(
        default_factory=dict
    )

    market_context: Dict[str, Any] = field(
        default_factory=dict
    )

    risk_checks: Dict[str, Any] = field(
        default_factory=dict
    )

    risk_passed: Optional[bool] = None

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize the decision into a dictionary suitable for:

            - SQLite persistence
            - Google Sheets
            - Telegram status output
            - logging
        """

        return {
            "timestamp": self.timestamp.isoformat(),
            "target_id": self.target_id,
            "asset": self.asset,
            "decision": self.decision,
            "price": self.price,
            "rsi": self.rsi,
            "macd": self.macd,
            "trend": self.trend,
            "market_condition": self.market_condition,
            "allocation": self.allocation,
            "reason": self.reason,
            "technical_signals": copy.deepcopy(
                self.technical_signals
            ),
            "market_context": copy.deepcopy(
                self.market_context
            ),
            "risk_checks": copy.deepcopy(
                self.risk_checks
            ),
            "risk_passed": self.risk_passed,
            "metadata": copy.deepcopy(
                self.metadata
            ),
        }


def _env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean environment override safely."""
    raw = os.getenv(key)
    if raw is None:
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


ALWAYS_ACTIVE_SIP = _env_bool(
    "ALWAYS_ACTIVE_SIP",
    False,
)


# ============================================================================
# SIP ENGINE
# ============================================================================

class SIPEngine:
    """
    Runtime controller and state manager for ASTRA V3 SIP.

    The engine is deliberately independent from the broker and from
    persistence.

    Thread safety
    -------------
    A re-entrant lock protects runtime state because SIP can later be
    accessed simultaneously by:

        - Telegram
        - main ASTRA loop
        - decision worker
        - persistence worker
        - dashboard synchronization
    """

    def __init__(
        self,
        decision_history_limit: int = DECISION_HISTORY_LIMIT,
    ) -> None:

        self._lock = threading.RLock()

        self._active = ALWAYS_ACTIVE_SIP

        self._activated_at: Optional[dt.datetime] = (
            dt.datetime.now(IST)
            if ALWAYS_ACTIVE_SIP
            else None
        )

        if ALWAYS_ACTIVE_SIP:
            logger.info(
                "ALWAYS_ACTIVE_SIP override enabled; SIP engine initialized active."
            )

        self._targets: Dict[str, SIPTarget] = {}

        self._decisions: List[SIPDecision] = []

        self._decision_history_limit = max(
            1,
            int(decision_history_limit),
        )

    # ------------------------------------------------------------------
    # PROPERTIES
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """
        Return whether the SIP engine is currently active.
        """

        with self._lock:
            return self._active

    @property
    def activated_at(self) -> Optional[dt.datetime]:
        """
        Return the time at which the SIP engine was activated.
        """

        with self._lock:
            return self._activated_at

    # ------------------------------------------------------------------
    # ENGINE LIFECYCLE
    # ------------------------------------------------------------------

    def activate(self) -> bool:
        """
        Activate the SIP subsystem.

        Repeated activation is harmless and idempotent.
        """

        with self._lock:

            if self._active:
                logger.info(
                    "SIP engine is already active."
                )

                return True

            self._active = True

            self._activated_at = dt.datetime.now(
                IST
            )

            logger.info(
                "ASTRA V3 SIP engine activated at %s",
                self._activated_at.isoformat(),
            )

            return True

    def deactivate(
        self,
        reason: str = "MANUAL",
    ) -> bool:
        """
        Deactivate the SIP runtime.

        Existing targets are preserved.

        Deactivation does NOT cancel SIP targets.

        This distinction is intentional:

            engine inactive
                !=
            target cancelled
        """

        with self._lock:

            if not self._active:
                logger.info(
                    "SIP engine is already inactive. reason=%s",
                    reason,
                )

                return False

            self._active = False

            logger.info(
                "ASTRA V3 SIP engine deactivated. reason=%s",
                reason,
            )

            return True

    # ------------------------------------------------------------------
    # TARGET ID
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_target_id() -> str:
        """
        Generate a unique ASTRA SIP target identifier.

        Example:

            SIP-20261004-A1B2C3
        """

        date_part = dt.datetime.now(
            IST
        ).strftime("%Y%m%d")

        random_part = uuid.uuid4().hex[:6].upper()

        return (
            f"SIP-{date_part}-{random_part}"
        )

    # ------------------------------------------------------------------
    # VALIDATION
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_amount(
        amount: float,
    ) -> float:
        """
        Validate and normalize SIP amount.
        """

        try:
            normalized = float(amount)
        except (
            TypeError,
            ValueError,
        ) as exc:

            raise SIPValidationError(
                "SIP amount must be numeric."
            ) from exc

        if normalized < MIN_SIP_AMOUNT:
            raise SIPValidationError(
                f"SIP amount must be at least "
                f"{MIN_SIP_AMOUNT:.2f}."
            )

        return normalized

    @staticmethod
    def _validate_frequency(
        frequency: str,
    ) -> str:
        """
        Validate and normalize SIP frequency.
        """

        if not isinstance(
            frequency,
            str,
        ):
            raise SIPValidationError(
                "SIP frequency must be a string."
            )

        normalized = frequency.strip().upper()

        if normalized not in VALID_FREQUENCIES:
            valid = ", ".join(
                sorted(VALID_FREQUENCIES)
            )

            raise SIPValidationError(
                f"Invalid SIP frequency "
                f"'{frequency}'. Valid values: {valid}."
            )

        return normalized

    @staticmethod
    def _validate_dates(
        start_at: dt.datetime,
        expires_at: dt.datetime,
    ) -> None:
        """
        Validate SIP target dates.
        """

        if not isinstance(
            start_at,
            dt.datetime,
        ):
            raise SIPValidationError(
                "start_at must be a datetime."
            )

        if not isinstance(
            expires_at,
            dt.datetime,
        ):
            raise SIPValidationError(
                "expires_at must be a datetime."
            )

        if start_at.tzinfo is None:
            raise SIPValidationError(
                "start_at must be timezone-aware."
            )

        if expires_at.tzinfo is None:
            raise SIPValidationError(
                "expires_at must be timezone-aware."
            )

        if expires_at <= start_at:
            raise SIPValidationError(
                "SIP expiry must be after the start time."
            )

    @staticmethod
    def _normalize_asset(
        asset: str,
    ) -> str:
        """
        Normalize the SIP asset identifier.
        """

        if not isinstance(
            asset,
            str,
        ):
            raise SIPValidationError(
                "SIP asset must be a string."
            )

        normalized = asset.strip().upper()

        if not normalized:
            raise SIPValidationError(
                "SIP asset cannot be empty."
            )

        return normalized

    # ------------------------------------------------------------------
    # TARGET CREATION
    # ------------------------------------------------------------------

    def create_target(
        self,
        asset: str,
        amount: float,
        frequency: str = DEFAULT_FREQUENCY,
        duration_days: int = DEFAULT_DURATION_DAYS,
        start_at: Optional[dt.datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SIPTarget:
        """
        Create a new SIP target.

        The SIP engine must already be active.

        Parameters
        ----------
        asset:
            Asset/security symbol.

        amount:
            Intended contribution amount per execution.

        frequency:
            WEEKLY, BIWEEKLY, MONTHLY, QUARTERLY or YEARLY.

        duration_days:
            Number of days until target expiry.

        start_at:
            Optional timezone-aware start timestamp.

        metadata:
            Optional extensible metadata dictionary.
        """

        with self._lock:

            if not self._active:
                raise SIPNotActiveError(
                    "SIP engine is not active. "
                    "Activate SIP using /SIP before "
                    "creating a target."
                )

            normalized_asset = self._normalize_asset(
                asset
            )

            normalized_amount = self._validate_amount(
                amount
            )

            normalized_frequency = (
                self._validate_frequency(
                    frequency
                )
            )

            try:
                normalized_duration = int(
                    duration_days
                )
            except (
                TypeError,
                ValueError,
            ) as exc:

                raise SIPValidationError(
                    "duration_days must be an integer."
                ) from exc

            if normalized_duration <= 0:
                raise SIPValidationError(
                    "duration_days must be greater than zero."
                )

            if start_at is None:
                normalized_start = dt.datetime.now(
                    IST
                )
            else:
                normalized_start = start_at

            if normalized_start.tzinfo is None:
                raise SIPValidationError(
                    "start_at must be timezone-aware."
                )

            expires_at = (
                normalized_start
                + dt.timedelta(
                    days=normalized_duration
                )
            )

            self._validate_dates(
                normalized_start,
                expires_at,
            )

            target_id = self._generate_target_id()

            target = SIPTarget(
                target_id=target_id,
                asset=normalized_asset,
                amount=normalized_amount,
                frequency=normalized_frequency,
                start_at=normalized_start,
                expires_at=expires_at,
                status=SIPTargetStatus.ACTIVE,
                next_execution_at=normalized_start,
                metadata=copy.deepcopy(
                    metadata or {}
                ),
            )

            self._targets[target_id] = target

            logger.info(
                "SIP target created | id=%s | asset=%s | "
                "amount=%.2f | frequency=%s | start=%s | "
                "expires=%s",
                target.target_id,
                target.asset,
                target.amount,
                target.frequency,
                target.start_at.isoformat(),
                target.expires_at.isoformat(),
            )

            return copy.deepcopy(target)

    # ------------------------------------------------------------------
    # TARGET LOOKUP
    # ------------------------------------------------------------------

    def get_target(
        self,
        target_id: str,
    ) -> SIPTarget:
        """
        Return one SIP target.

        Automatic expiry is refreshed before returning it.
        """

        with self._lock:

            target = self._targets.get(
                target_id
            )

            if target is None:
                raise SIPTargetNotFoundError(
                    f"SIP target '{target_id}' was not found."
                )

            target.refresh_status()

            return copy.deepcopy(target)

    def get_targets(
        self,
        include_expired: bool = True,
    ) -> List[SIPTarget]:
        """
        Return all SIP targets.

        Parameters
        ----------
        include_expired:
            When False, terminal/expired targets are omitted.
        """

        with self._lock:

            for target in self._targets.values():
                target.refresh_status()

            targets = list(
                self._targets.values()
            )

            if not include_expired:
                targets = [
                    target
                    for target in targets
                    if target.status
                    not in {
                        SIPTargetStatus.EXPIRED,
                        SIPTargetStatus.CANCELLED,
                        SIPTargetStatus.COMPLETED,
                    }
                ]

            targets.sort(
                key=lambda item: item.created_at
            )

            return [
                copy.deepcopy(target)
                for target in targets
            ]

    # ------------------------------------------------------------------
    # TARGET STATE CONTROL
    # ------------------------------------------------------------------

    def pause_target(
        self,
        target_id: str,
    ) -> SIPTarget:
        """
        Pause an ACTIVE SIP target.
        """

        with self._lock:

            target = self._get_mutable_target(
                target_id
            )

            target.refresh_status()

            if target.status != SIPTargetStatus.ACTIVE:
                raise SIPTargetStateError(
                    f"Cannot pause SIP target "
                    f"'{target_id}' from state "
                    f"'{target.status.value}'."
                )

            target.status = SIPTargetStatus.PAUSED

            logger.info(
                "SIP target paused | id=%s",
                target_id,
            )

            return copy.deepcopy(target)

    def resume_target(
        self,
        target_id: str,
    ) -> SIPTarget:
        """
        Resume a PAUSED SIP target.

        Expired targets cannot be resumed.
        """

        with self._lock:

            target = self._get_mutable_target(
                target_id
            )

            target.refresh_status()

            if target.status == SIPTargetStatus.EXPIRED:
                raise SIPTargetStateError(
                    f"SIP target '{target_id}' has expired "
                    "and cannot be resumed."
                )

            if target.status != SIPTargetStatus.PAUSED:
                raise SIPTargetStateError(
                    f"Cannot resume SIP target "
                    f"'{target_id}' from state "
                    f"'{target.status.value}'."
                )

            target.status = SIPTargetStatus.ACTIVE

            logger.info(
                "SIP target resumed | id=%s",
                target_id,
            )

            return copy.deepcopy(target)

    def cancel_target(
        self,
        target_id: str,
    ) -> SIPTarget:
        """
        Cancel a SIP target.

        Cancellation is terminal.
        """

        with self._lock:

            target = self._get_mutable_target(
                target_id
            )

            target.refresh_status()

            if target.status in {
                SIPTargetStatus.CANCELLED,
                SIPTargetStatus.COMPLETED,
                SIPTargetStatus.EXPIRED,
            }:
                raise SIPTargetStateError(
                    f"Cannot cancel SIP target "
                    f"'{target_id}' from terminal state "
                    f"'{target.status.value}'."
                )

            target.status = SIPTargetStatus.CANCELLED

            logger.info(
                "SIP target cancelled | id=%s",
                target_id,
            )

            return copy.deepcopy(target)

    def complete_target(
        self,
        target_id: str,
    ) -> SIPTarget:
        """
        Mark an ACTIVE or PAUSED target as completed.

        Completion is intended for a target whose planned SIP lifecycle
        has been fulfilled.
        """

        with self._lock:

            target = self._get_mutable_target(
                target_id
            )

            target.refresh_status()

            if target.status not in {
                SIPTargetStatus.ACTIVE,
                SIPTargetStatus.PAUSED,
            }:
                raise SIPTargetStateError(
                    f"Cannot complete SIP target "
                    f"'{target_id}' from state "
                    f"'{target.status.value}'."
                )

            target.status = SIPTargetStatus.COMPLETED

            logger.info(
                "SIP target completed | id=%s",
                target_id,
            )

            return copy.deepcopy(target)

    # ------------------------------------------------------------------
    # TARGET INTERNAL LOOKUP
    # ------------------------------------------------------------------

    def _get_mutable_target(
        self,
        target_id: str,
    ) -> SIPTarget:
        """
        Return the internal mutable target object.

        Caller must hold _lock.
        """

        target = self._targets.get(
            target_id
        )

        if target is None:
            raise SIPTargetNotFoundError(
                f"SIP target '{target_id}' was not found."
            )

        return target

    # ------------------------------------------------------------------
    # CONTRIBUTION ACCOUNTING
    # ------------------------------------------------------------------

    def register_contribution(
        self,
        target_id: str,
        amount: float,
        execution_time: Optional[dt.datetime] = None,
        execution_price: Optional[float] = None,
        quantity: Optional[float] = None,
        broker_order_id: Optional[str] = None,
    ) -> SIPTarget:
        """
        Register a completed SIP contribution.

        This does NOT place an order.

        It only records the fact that an external execution layer
        confirmed a contribution.

        Broker execution will be implemented separately.
        """

        with self._lock:

            target = self._get_mutable_target(
                target_id
            )

            target.refresh_status()

            if target.status != SIPTargetStatus.ACTIVE:
                raise SIPTargetStateError(
                    f"Cannot register a contribution for "
                    f"SIP target '{target_id}' because its "
                    f"state is '{target.status.value}'."
                )

            normalized_amount = self._validate_amount(
                amount
            )

            if execution_time is None:
                execution_time = dt.datetime.now(
                    IST
                )

            if execution_time.tzinfo is None:
                raise SIPValidationError(
                    "execution_time must be timezone-aware."
                )

            target.completed_contributions += 1

            target.total_invested += (
                normalized_amount
            )

            target.metadata[
                "last_contribution"
            ] = {
                "timestamp": execution_time.isoformat(),
                "amount": normalized_amount,
                "execution_price": execution_price,
                "quantity": quantity,
                "broker_order_id": broker_order_id,
            }

            logger.info(
                "SIP contribution registered | id=%s | "
                "amount=%.2f | contribution_count=%d | "
                "total_invested=%.2f",
                target.target_id,
                normalized_amount,
                target.completed_contributions,
                target.total_invested,
            )

            return copy.deepcopy(target)

    # ------------------------------------------------------------------
    # DECISION TELEMETRY
    # ------------------------------------------------------------------

    def record_decision(
        self,
        target_id: str,
        decision: str,
        reason: str = "",
        *,
        asset: Optional[str] = None,
        price: Optional[float] = None,
        rsi: Optional[float] = None,
        macd: Optional[float] = None,
        trend: Optional[str] = None,
        market_condition: Optional[str] = None,
        allocation: Optional[float] = None,
        technical_signals: Optional[Dict[str, Any]] = None,
        market_context: Optional[Dict[str, Any]] = None,
        risk_checks: Optional[Dict[str, Any]] = None,
        risk_passed: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[dt.datetime] = None,
    ) -> SIPDecision:
        """
        Record a SIP decision.

        This method does not execute any trade.

        Example decisions may include:

            CONTRIBUTE
            HOLD
            SKIP
            PAUSE
            REJECT
            WAIT

        The decision string is intentionally not restricted to a fixed
        enum because future SIP strategies may introduce additional
        decision states.

        The target must exist.

        Parameters such as RSI, MACD, trend and risk checks are optional
        because not every future SIP strategy will necessarily use the
        same analytical inputs.
        """

        with self._lock:

            target = self._get_mutable_target(
                target_id
            )

            if not isinstance(
                decision,
                str,
            ):
                raise SIPValidationError(
                    "decision must be a string."
                )

            normalized_decision = (
                decision.strip().upper()
            )

            if not normalized_decision:
                raise SIPValidationError(
                    "decision cannot be empty."
                )

            if timestamp is None:
                timestamp = dt.datetime.now(
                    IST
                )

            if timestamp.tzinfo is None:
                raise SIPValidationError(
                    "timestamp must be timezone-aware."
                )

            resolved_asset = (
                asset
                if asset is not None
                else target.asset
            )

            resolved_asset = self._normalize_asset(
                resolved_asset
            )

            if price is not None:
                try:
                    price = float(price)
                except (
                    TypeError,
                    ValueError,
                ) as exc:
                    raise SIPValidationError(
                        "price must be numeric."
                    ) from exc

            if rsi is not None:
                try:
                    rsi = float(rsi)
                except (
                    TypeError,
                    ValueError,
                ) as exc:
                    raise SIPValidationError(
                        "rsi must be numeric."
                    ) from exc

            if macd is not None:
                try:
                    macd = float(macd)
                except (
                    TypeError,
                    ValueError,
                ) as exc:
                    raise SIPValidationError(
                        "macd must be numeric."
                    ) from exc

            if allocation is not None:
                try:
                    allocation = float(
                        allocation
                    )
                except (
                    TypeError,
                    ValueError,
                ) as exc:
                    raise SIPValidationError(
                        "allocation must be numeric."
                    ) from exc

            if not isinstance(
                reason,
                str,
            ):
                reason = str(reason)

            decision_record = SIPDecision(
                timestamp=timestamp,
                target_id=target.target_id,
                asset=resolved_asset,
                decision=normalized_decision,
                price=price,
                rsi=rsi,
                macd=macd,
                trend=trend,
                market_condition=market_condition,
                allocation=allocation,
                reason=reason.strip(),
                technical_signals=copy.deepcopy(
                    technical_signals or {}
                ),
                market_context=copy.deepcopy(
                    market_context or {}
                ),
                risk_checks=copy.deepcopy(
                    risk_checks or {}
                ),
                risk_passed=risk_passed,
                metadata=copy.deepcopy(
                    metadata or {}
                ),
            )

            self._decisions.append(
                decision_record
            )

            if (
                len(self._decisions)
                > self._decision_history_limit
            ):
                overflow = (
                    len(self._decisions)
                    - self._decision_history_limit
                )

                del self._decisions[
                    :overflow
                ]

            logger.info(
                "SIP decision recorded | target=%s | "
                "asset=%s | decision=%s | reason=%s",
                decision_record.target_id,
                decision_record.asset,
                decision_record.decision,
                decision_record.reason,
            )

            return copy.deepcopy(
                decision_record
            )

    def get_decisions(
        self,
        target_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[SIPDecision]:
        """
        Return recorded SIP decisions.

        If target_id is supplied, only decisions belonging to that
        target are returned.

        Results are returned oldest-first.
        """

        with self._lock:

            decisions = self._decisions

            if target_id is not None:

                decisions = [
                    decision
                    for decision in decisions
                    if decision.target_id
                    == target_id
                ]

            if limit is not None:

                try:
                    normalized_limit = int(
                        limit
                    )
                except (
                    TypeError,
                    ValueError,
                ) as exc:

                    raise SIPValidationError(
                        "limit must be an integer."
                    ) from exc

                if normalized_limit <= 0:
                    return []

                decisions = decisions[
                    -normalized_limit:
                ]

            return [
                copy.deepcopy(decision)
                for decision in decisions
            ]

    def get_latest_decision(
        self,
        target_id: str,
    ) -> Optional[SIPDecision]:
        """
        Return the latest decision for a target.
        """

        with self._lock:

            for decision in reversed(
                self._decisions
            ):

                if decision.target_id == target_id:
                    return copy.deepcopy(
                        decision
                    )

            return None

    # ------------------------------------------------------------------
    # REFRESH
    # ------------------------------------------------------------------

    def refresh(self) -> List[SIPTarget]:
        """
        Refresh target lifecycle states.

        Returns the current target snapshot.
        """

        with self._lock:

            now = dt.datetime.now(
                IST
            )

            for target in self._targets.values():

                previous_status = target.status

                target.refresh_status(
                    now
                )

                if (
                    previous_status
                    != target.status
                ):
                    logger.info(
                        "SIP target status changed | "
                        "id=%s | %s -> %s",
                        target.target_id,
                        previous_status.value,
                        target.status.value,
                    )

            return [
                copy.deepcopy(target)
                for target in self._targets.values()
            ]

    # ------------------------------------------------------------------
    # STATUS
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """
        Return a complete SIP subsystem status snapshot.
        """

        with self._lock:

            self.refresh()

            active_count = 0
            paused_count = 0

            for target in self._targets.values():

                if target.status == SIPTargetStatus.ACTIVE:
                    active_count += 1

                elif target.status == SIPTargetStatus.PAUSED:
                    paused_count += 1

            return {
                "active": self._active,
                "activated_at": (
                    self._activated_at.isoformat()
                    if self._activated_at is not None
                    else None
                ),
                "target_count": len(
                    self._targets
                ),
                "active_target_count": active_count,
                "paused_target_count": paused_count,
                "decision_count": len(
                    self._decisions
                ),
                "targets": [
                    target.to_dict()
                    for target in self._targets.values()
                ],
            }


# ============================================================================
# GLOBAL SIP ENGINE
# ============================================================================

_SIP_ENGINE: Optional[SIPEngine] = None

_SIP_ENGINE_LOCK = threading.RLock()


# ============================================================================
# GLOBAL ENGINE ACCESS
# ============================================================================

def get_sip_engine() -> SIPEngine:
    """
    Return the global SIP engine instance.

    The engine starts inactive.

    Calling this function does NOT activate SIP.
    """

    global _SIP_ENGINE

    with _SIP_ENGINE_LOCK:

        if _SIP_ENGINE is None:
            _SIP_ENGINE = SIPEngine()

            logger.info(
                "ASTRA V3 SIP engine initialized "
                "in inactive state."
            )

        return _SIP_ENGINE


# ============================================================================
# GLOBAL HELPERS
# ============================================================================

def activate_sip() -> bool:
    """
    Activate the global SIP engine.

    This is intended to be called by Telegram's /SIP command.
    """

    engine = get_sip_engine()

    return engine.activate()


def deactivate_sip(
    reason: str = "MANUAL",
) -> bool:
    """
    Deactivate the global SIP engine.

    Existing targets remain preserved.
    """

    engine = get_sip_engine()

    return engine.deactivate(
        reason=reason
    )


def get_sip_status() -> Dict[str, Any]:
    """
    Return the global SIP engine status.
    """

    engine = get_sip_engine()

    return engine.status()


def create_sip_target(
    asset: str,
    amount: float,
    frequency: str = DEFAULT_FREQUENCY,
    duration_days: int = DEFAULT_DURATION_DAYS,
    start_at: Optional[dt.datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> SIPTarget:
    """
    Create a SIP target through the global SIP engine.
    """

    engine = get_sip_engine()

    return engine.create_target(
        asset=asset,
        amount=amount,
        frequency=frequency,
        duration_days=duration_days,
        start_at=start_at,
        metadata=metadata,
    )


def record_sip_decision(
    target_id: str,
    decision: str,
    reason: str = "",
    *,
    asset: Optional[str] = None,
    price: Optional[float] = None,
    rsi: Optional[float] = None,
    macd: Optional[float] = None,
    trend: Optional[str] = None,
    market_condition: Optional[str] = None,
    allocation: Optional[float] = None,
    technical_signals: Optional[Dict[str, Any]] = None,
    market_context: Optional[Dict[str, Any]] = None,
    risk_checks: Optional[Dict[str, Any]] = None,
    risk_passed: Optional[bool] = None,
    metadata: Optional[Dict[str, Any]] = None,
    timestamp: Optional[dt.datetime] = None,
) -> SIPDecision:
    """
    Record a decision through the global SIP engine.
    """

    engine = get_sip_engine()

    return engine.record_decision(
        target_id=target_id,
        decision=decision,
        reason=reason,
        asset=asset,
        price=price,
        rsi=rsi,
        macd=macd,
        trend=trend,
        market_condition=market_condition,
        allocation=allocation,
        technical_signals=technical_signals,
        market_context=market_context,
        risk_checks=risk_checks,
        risk_passed=risk_passed,
        metadata=metadata,
        timestamp=timestamp,
    )


def refresh_sip() -> List[SIPTarget]:
    """
    Refresh the global SIP engine's target states.
    """

    engine = get_sip_engine()

    return engine.refresh()

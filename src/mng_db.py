"""
ASTRA SQLite Database Manager
=============================

Responsibilities
----------------
- Initialize and maintain the ASTRA SQLite schema.
- Persist normal ASTRA signal history.
- Persist portfolio snapshots.
- Persist system logs.
- Persist intraday sessions and recommendations.
- Persist V3 SIP targets.
- Persist V3 SIP contribution history.
- Persist V3 SIP decision telemetry.

Design goals
------------
- Existing ASTRA V1 database tables remain compatible.
- Existing V2 intraday database functionality remains compatible.
- V3 SIP state is isolated from V1 and V2 tables.
- All SQL is parameterized.
- Connections are short-lived and transaction-scoped.
- UTC timestamps are stored consistently.
- Database failures are logged without corrupting caller state.
- SQLite foreign keys and WAL mode are enabled where supported.
- Flexible SIP decision telemetry is stored as JSON.
- SQLite remains authoritative local persistence.
- Google Sheets remains a downstream human-readable telemetry layer.

Architecture
------------
                    ASTRA
                      |
          +-----------+-----------+
          |           |           |
          V1          V2          V3
        Market      Intraday      SIP
          |           |           |
          +-----------+-----------+
                      |
                    SQLite
                      |
          +-----------+-----------+
          |           |           |
       Signals    Intraday       SIP
                              /       \
                          Targets    Decisions

Google Sheets synchronization is intentionally handled outside this
module, primarily by portfolio_fetcher.py.

This module does NOT:
    - execute broker orders
    - communicate with Telegram
    - write Google Sheets
    - make SIP decisions
    - activate/deactivate the SIP engine

It only persists and retrieves state.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from utils import get_setting


# ============================================================================
# LOGGING / PATHS
# ============================================================================

logger = logging.getLogger("ASTRA_DB")

DB_PATH = (
    Path(__file__).resolve().parent.parent / "astra.db"
)


# ============================================================================
# RUNTIME DATABASE SETTINGS
# ============================================================================


def _setting_int(path: str, default: int) -> int:
    """Read an integer database setting with a safe fallback."""

    try:
        value = get_setting(path, default)
        return int(value)
    except (TypeError, ValueError):
        return default


def _setting_str(path: str, default: str) -> str:
    """Read a string database setting with a safe fallback."""

    try:
        value = get_setting(path, default)
        if value is None:
            return default
        return str(value)
    except (TypeError, ValueError):
        return default


def _configured_db_path() -> Path:
    """Return the configured SQLite path, falling back to ASTRA's default."""

    configured = get_setting(
        "DATABASE.PATH",
        str(DB_PATH),
    )

    if configured is None or not str(configured).strip():
        return DB_PATH

    return Path(str(configured)).expanduser()


# ============================================================================
# DATABASE SCHEMA VERSION
# ============================================================================

# Version 1:
#     Original ASTRA V1/V2 schema.
#
# Version 2:
#     Adds V3 SIP persistence tables.
_SCHEMA_VERSION = 2


# ============================================================================
# DATACLASSES
# ============================================================================

@dataclass(frozen=True)
class IntradaySession:
    """Immutable representation of an intraday engine session."""

    id: int
    session_date: str
    mode: str
    started_at: str
    expires_at: Optional[str]
    ended_at: Optional[str]
    end_reason: Optional[str]


@dataclass(frozen=True)
class SIPTargetRecord:
    """
    Immutable database representation of a SIP target.

    This intentionally mirrors the persistence-relevant fields of
    sip_engine.SIPTarget without importing sip_engine.py.

    Keeping the database layer independent prevents circular imports.
    """

    id: int
    target_id: str
    asset: str
    amount: float
    frequency: str
    start_at: str
    expires_at: str
    status: str
    created_at: str
    next_execution_at: Optional[str]
    completed_contributions: int
    total_invested: float
    metadata: dict[str, Any]


@dataclass(frozen=True)
class SIPContributionRecord:
    """
    Immutable representation of one completed SIP contribution.
    """

    id: int
    target_id: str
    timestamp: str
    amount: float
    execution_price: Optional[float]
    quantity: Optional[float]
    broker_order_id: Optional[str]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class SIPDecisionRecord:
    """
    Immutable representation of one SIP decision snapshot.

    Flexible analytical fields are stored as JSON in SQLite and decoded
    back into dictionaries here.
    """

    id: int
    timestamp: str
    target_id: str
    asset: str
    decision: str
    price: Optional[float]
    rsi: Optional[float]
    macd: Optional[float]
    trend: Optional[str]
    market_condition: Optional[str]
    allocation: Optional[float]
    reason: str
    technical_signals: dict[str, Any]
    market_context: dict[str, Any]
    risk_checks: dict[str, Any]
    risk_passed: Optional[bool]
    metadata: dict[str, Any]


# ============================================================================
# DATABASE MANAGER
# ============================================================================

class DatabaseManager:
    """
    SQLite persistence layer for ASTRA.

    The class deliberately does not keep a connection open. Each operation
    obtains its own connection and transaction.

    This avoids stale connections across long-running Telegram/background
    worker processes and makes the manager safe to use from ASTRA's worker
    threads.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
    ):
        # DB_PATH remains the compatibility/default fallback, while the
        # runtime-configurable DATABASE.PATH setting can override it.
        configured_path = (
            _configured_db_path()
            if db_path is None
            else Path(db_path).expanduser()
        )

        self.db_path = configured_path.resolve()

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._init_db()

    # ========================================================================
    # CONNECTION / TRANSACTION MANAGEMENT
    # ========================================================================

    @contextmanager
    def _connection(
        self,
    ) -> Iterator[sqlite3.Connection]:
        """
        Yield a configured SQLite connection.

        The context manager commits on success and rolls back on failure.
        The connection is always closed afterwards.
        """

        conn: Optional[sqlite3.Connection] = None

        try:
            conn = sqlite3.connect(
                self.db_path,
                timeout=max(0.1, _setting_int("DATABASE.SQLITE_TIMEOUT", 30)),
            )

            conn.row_factory = sqlite3.Row

            # Protect relational integrity.
            conn.execute(
                "PRAGMA foreign_keys = ON"
            )

            # Better behavior for concurrent/background workloads.
            conn.execute(
                f"PRAGMA busy_timeout = {_setting_int('DATABASE.BUSY_TIMEOUT_MS', 30000)}"
            )

            # WAL allows readers to continue while another connection writes.
            journal_mode = _setting_str(
                "DATABASE.JOURNAL_MODE",
                "WAL",
            ).upper()

            if journal_mode not in {
                "DELETE",
                "TRUNCATE",
                "PERSIST",
                "MEMORY",
                "WAL",
                "OFF",
            }:
                journal_mode = "WAL"

            conn.execute(
                f"PRAGMA journal_mode = {journal_mode}"
            )

            # Reasonable durability/performance compromise.
            synchronous = _setting_str(
                "DATABASE.SYNCHRONOUS",
                "NORMAL",
            ).upper()

            if synchronous not in {
                "OFF",
                "NORMAL",
                "FULL",
                "EXTRA",
            }:
                synchronous = "NORMAL"

            conn.execute(
                f"PRAGMA synchronous = {synchronous}"
            )

            yield conn

            conn.commit()

        except sqlite3.Error:
            if conn is not None:
                conn.rollback()

            raise

        finally:
            if conn is not None:
                conn.close()

    def _init_db(self) -> None:
        """
        Create or upgrade the ASTRA database schema.
        """

        try:
            with self._connection() as conn:
                self._create_schema(conn)
                self._set_schema_version(conn)

            logger.info(
                "Database initialized successfully at %s",
                self.db_path,
            )

        except sqlite3.Error:
            logger.exception(
                "Failed to initialize SQLite database at %s",
                self.db_path,
            )
            raise

    @staticmethod
    def _create_schema(
        conn: sqlite3.Connection,
    ) -> None:
        """
        Create all ASTRA tables and indexes.

        CREATE TABLE IF NOT EXISTS keeps existing ASTRA databases
        compatible when upgrading.
        """

        conn.executescript(
            """
            -- ============================================================
            -- ASTRA V1: SIGNAL HISTORY
            -- ============================================================

            CREATE TABLE IF NOT EXISTS signal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL,
                price REAL NOT NULL,
                rsi REAL,
                macd REAL,
                action TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_signal_history_timestamp
                ON signal_history(timestamp);

            CREATE INDEX IF NOT EXISTS idx_signal_history_ticker
                ON signal_history(ticker);


            -- ============================================================
            -- ASTRA V1: PORTFOLIO SNAPSHOTS
            -- ============================================================

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                available_cash REAL NOT NULL,
                total_invested REAL NOT NULL,
                current_value REAL NOT NULL,
                total_pnl REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_timestamp
                ON portfolio_snapshots(timestamp);


            -- ============================================================
            -- ASTRA SYSTEM LOGS
            -- ============================================================

            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_system_logs_timestamp
                ON system_logs(timestamp);


            -- ============================================================
            -- ASTRA V2: INTRADAY SESSIONS
            -- ============================================================

            CREATE TABLE IF NOT EXISTS intraday_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT NOT NULL,
                mode TEXT NOT NULL,
                started_at TEXT NOT NULL,
                expires_at TEXT,
                ended_at TEXT,
                end_reason TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_intraday_sessions_date
                ON intraday_sessions(session_date);

            CREATE INDEX IF NOT EXISTS idx_intraday_sessions_active
                ON intraday_sessions(session_date, ended_at);


            -- ============================================================
            -- ASTRA V2: INTRADAY CANDIDATES
            -- ============================================================

            CREATE TABLE IF NOT EXISTS intraday_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                session_date TEXT NOT NULL,
                ticker TEXT NOT NULL,

                entry_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                allocation REAL NOT NULL,

                rsi REAL,
                macd REAL,
                signals TEXT,

                stop_loss REAL NOT NULL,
                trailing_stop REAL NOT NULL,

                holding_type TEXT NOT NULL,
                expiry TEXT NOT NULL,
                reasoning TEXT,

                status TEXT NOT NULL DEFAULT 'ACTIVE',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                exit_reason TEXT,

                FOREIGN KEY(session_id)
                    REFERENCES intraday_sessions(id)
                    ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_intraday_candidates_date
                ON intraday_candidates(session_date);

            CREATE INDEX IF NOT EXISTS idx_intraday_candidates_status
                ON intraday_candidates(status);

            CREATE INDEX IF NOT EXISTS idx_intraday_candidates_ticker
                ON intraday_candidates(ticker);


            -- ============================================================
            -- ASTRA V3: SIP TARGETS
            -- ============================================================

            CREATE TABLE IF NOT EXISTS sip_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                target_id TEXT NOT NULL UNIQUE,

                asset TEXT NOT NULL,

                amount REAL NOT NULL,

                frequency TEXT NOT NULL,

                start_at TEXT NOT NULL,

                expires_at TEXT NOT NULL,

                status TEXT NOT NULL,

                created_at TEXT NOT NULL,

                next_execution_at TEXT,

                completed_contributions INTEGER NOT NULL DEFAULT 0,

                total_invested REAL NOT NULL DEFAULT 0.0,

                metadata TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_sip_targets_target_id
                ON sip_targets(target_id);

            CREATE INDEX IF NOT EXISTS idx_sip_targets_asset
                ON sip_targets(asset);

            CREATE INDEX IF NOT EXISTS idx_sip_targets_status
                ON sip_targets(status);

            CREATE INDEX IF NOT EXISTS idx_sip_targets_next_execution
                ON sip_targets(next_execution_at);


            -- ============================================================
            -- ASTRA V3: SIP CONTRIBUTIONS
            -- ============================================================

            CREATE TABLE IF NOT EXISTS sip_contributions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                target_id TEXT NOT NULL,

                timestamp TEXT NOT NULL,

                amount REAL NOT NULL,

                execution_price REAL,

                quantity REAL,

                broker_order_id TEXT,

                metadata TEXT,

                FOREIGN KEY(target_id)
                    REFERENCES sip_targets(target_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sip_contributions_target
                ON sip_contributions(target_id);

            CREATE INDEX IF NOT EXISTS idx_sip_contributions_timestamp
                ON sip_contributions(timestamp);

            CREATE INDEX IF NOT EXISTS idx_sip_contributions_order
                ON sip_contributions(broker_order_id);


            -- ============================================================
            -- ASTRA V3: SIP DECISIONS
            -- ============================================================

            CREATE TABLE IF NOT EXISTS sip_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                timestamp TEXT NOT NULL,

                target_id TEXT NOT NULL,

                asset TEXT NOT NULL,

                decision TEXT NOT NULL,

                price REAL,

                rsi REAL,

                macd REAL,

                trend TEXT,

                market_condition TEXT,

                allocation REAL,

                reason TEXT NOT NULL,

                technical_signals TEXT,

                market_context TEXT,

                risk_checks TEXT,

                risk_passed INTEGER,

                metadata TEXT,

                FOREIGN KEY(target_id)
                    REFERENCES sip_targets(target_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sip_decisions_timestamp
                ON sip_decisions(timestamp);

            CREATE INDEX IF NOT EXISTS idx_sip_decisions_target
                ON sip_decisions(target_id);

            CREATE INDEX IF NOT EXISTS idx_sip_decisions_asset
                ON sip_decisions(asset);

            CREATE INDEX IF NOT EXISTS idx_sip_decisions_decision
                ON sip_decisions(decision);
            """
        )

    @staticmethod
    def _set_schema_version(
        conn: sqlite3.Connection,
    ) -> None:
        """
        Record the current schema version.

        PRAGMA user_version is an integer owned by SQLite and avoids
        introducing another metadata table for this project.
        """

        current = conn.execute(
            "PRAGMA user_version"
        ).fetchone()[0]

        if current < _SCHEMA_VERSION:
            conn.execute(
                f"PRAGMA user_version = {_SCHEMA_VERSION}"
            )

            logger.info(
                "SQLite schema upgraded: %d -> %d",
                current,
                _SCHEMA_VERSION,
            )

    # ========================================================================
    # COMMON HELPERS
    # ========================================================================

    @staticmethod
    def _utc_now() -> str:
        """
        Return the current UTC timestamp as ISO-8601 text.
        """

        return datetime.now(
            timezone.utc
        ).isoformat()

    @staticmethod
    def _safe_float(
        value: Any,
        default: float = 0.0,
    ) -> float:
        """
        Safely convert a value to float.
        """

        try:
            return float(value)

        except (
            TypeError,
            ValueError,
        ):
            return default

    @staticmethod
    def _safe_optional_float(
        value: Any,
    ) -> Optional[float]:
        """
        Convert a value to float while preserving None.
        """

        if value is None:
            return None

        try:
            return float(value)

        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _safe_int(
        value: Any,
        default: int = 0,
    ) -> int:
        """
        Safely convert a value to integer.
        """

        try:
            return int(value)

        except (
            TypeError,
            ValueError,
        ):
            return default

    @staticmethod
    def _json_dumps(
        value: Any,
    ) -> str:
        """
        Serialize arbitrary telemetry safely.
        """

        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                default=str,
            )

        except (
            TypeError,
            ValueError,
        ):
            return json.dumps(
                str(value),
                ensure_ascii=False,
            )

    @staticmethod
    def _json_loads(
        value: Optional[str],
        default: Any,
    ) -> Any:
        """
        Deserialize JSON safely.
        """

        if value is None:
            return default

        try:
            result = json.loads(value)

            return result

        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return default

    # ========================================================================
    # SIGNAL HISTORY
    # ========================================================================

    def log_signal(
        self,
        ticker: str,
        strategy: str,
        price: float,
        rsi: float,
        macd: float,
        action: str,
    ) -> bool:
        """
        Persist a generated ASTRA signal.
        """

        try:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO signal_history (
                        timestamp,
                        ticker,
                        strategy,
                        price,
                        rsi,
                        macd,
                        action
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._utc_now(),
                        str(ticker),
                        str(strategy),
                        self._safe_float(price),
                        self._safe_float(rsi),
                        self._safe_float(macd),
                        str(action),
                    ),
                )

            return True

        except sqlite3.Error:
            logger.exception(
                "Failed to persist signal for %s",
                ticker,
            )

            return False

    # ========================================================================
    # PORTFOLIO SNAPSHOTS
    # ========================================================================

    def log_portfolio_snapshot(
        self,
        cash: float,
        invested: float,
        current: float,
        pnl: float,
    ) -> bool:
        """
        Persist a portfolio performance snapshot.
        """

        try:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO portfolio_snapshots (
                        timestamp,
                        available_cash,
                        total_invested,
                        current_value,
                        total_pnl
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        self._utc_now(),
                        self._safe_float(cash),
                        self._safe_float(invested),
                        self._safe_float(current),
                        self._safe_float(pnl),
                    ),
                )

            return True

        except sqlite3.Error:
            logger.exception(
                "Failed to persist portfolio snapshot"
            )

            return False

    # ========================================================================
    # SYSTEM LOGS
    # ========================================================================

    def log_system(
        self,
        level: str,
        message: str,
    ) -> bool:
        """
        Persist an ASTRA system event.
        """

        try:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO system_logs (
                        timestamp,
                        level,
                        message
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        self._utc_now(),
                        str(level).upper(),
                        str(message),
                    ),
                )

            return True

        except sqlite3.Error:
            logger.exception(
                "Failed to persist system log"
            )

            return False

    # ========================================================================
    # INTRADAY SESSIONS
    # ========================================================================

    def create_intraday_session(
        self,
        session_date: str,
        mode: str,
        started_at: str,
        expires_at: Optional[str],
    ) -> Optional[int]:
        """
        Create an intraday session.

        Returns:
            Newly created session ID, or None on failure.
        """

        try:
            with self._connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO intraday_sessions (
                        session_date,
                        mode,
                        started_at,
                        expires_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(session_date),
                        str(mode),
                        str(started_at),
                        expires_at,
                    ),
                )

                return int(
                    cursor.lastrowid
                )

        except sqlite3.Error:
            logger.exception(
                "Failed to create intraday session for %s",
                session_date,
            )

            return None

    def close_intraday_session(
        self,
        session_date: Optional[str],
        reason: str,
    ) -> bool:
        """
        Close the active intraday session for a trading date.
        """

        if not session_date:
            return False

        try:
            with self._connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE intraday_sessions
                    SET ended_at = ?,
                        end_reason = ?
                    WHERE session_date = ?
                      AND ended_at IS NULL
                    """,
                    (
                        self._utc_now(),
                        str(reason),
                        str(session_date),
                    ),
                )

                return cursor.rowcount > 0

        except sqlite3.Error:
            logger.exception(
                "Failed to close intraday session for %s",
                session_date,
            )

            return False

    def get_active_intraday_session(
        self,
        session_date: str,
    ) -> Optional[IntradaySession]:
        """
        Return the active intraday session for a date, if any.
        """

        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT
                        id,
                        session_date,
                        mode,
                        started_at,
                        expires_at,
                        ended_at,
                        end_reason
                    FROM intraday_sessions
                    WHERE session_date = ?
                      AND ended_at IS NULL
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        str(session_date),
                    ),
                ).fetchone()

            if row is None:
                return None

            return IntradaySession(
                id=row["id"],
                session_date=row["session_date"],
                mode=row["mode"],
                started_at=row["started_at"],
                expires_at=row["expires_at"],
                ended_at=row["ended_at"],
                end_reason=row["end_reason"],
            )

        except sqlite3.Error:
            logger.exception(
                "Failed to read active intraday session for %s",
                session_date,
            )

            return None

    # ========================================================================
    # INTRADAY CANDIDATES
    # ========================================================================

    def save_intraday_candidate(
        self,
        candidate: Any,
        session_date: Optional[str],
        session_id: Optional[int] = None,
    ) -> Optional[int]:
        """
        Persist an intraday recommendation.

        The candidate object is intentionally duck-typed so the DB layer
        does not depend on IntradayTradingEngine's implementation.
        """

        if not session_date:
            return None

        now = self._utc_now()

        try:
            signals = getattr(
                candidate,
                "signals",
                [],
            )

            with self._connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO intraday_candidates (
                        session_id,
                        session_date,
                        ticker,
                        entry_price,
                        quantity,
                        allocation,
                        rsi,
                        macd,
                        signals,
                        stop_loss,
                        trailing_stop,
                        holding_type,
                        expiry,
                        reasoning,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        session_id,
                        str(session_date),
                        str(candidate.ticker),
                        self._safe_float(
                            candidate.entry_price
                        ),
                        self._safe_int(
                            candidate.quantity
                        ),
                        self._safe_float(
                            candidate.allocation
                        ),
                        self._safe_float(
                            candidate.rsi
                        ),
                        self._safe_float(
                            candidate.macd
                        ),
                        self._json_dumps(
                            list(signals)
                        ),
                        self._safe_float(
                            candidate.stop_loss
                        ),
                        self._safe_float(
                            candidate.trailing_stop
                        ),
                        str(
                            candidate.holding_type
                        ),
                        str(
                            candidate.expiry
                        ),
                        str(
                            candidate.reasoning
                        ),
                        "ACTIVE",
                        now,
                        now,
                    ),
                )

                return int(
                    cursor.lastrowid
                )

        except sqlite3.Error:
            logger.exception(
                "Failed to persist intraday candidate %s",
                getattr(
                    candidate,
                    "ticker",
                    "<unknown>",
                ),
            )

            return None

    def update_intraday_candidate(
        self,
        session_date: str,
        ticker: str,
        *,
        trailing_stop: Optional[float] = None,
        status: Optional[str] = None,
    ) -> bool:
        """
        Update mutable state for an active intraday recommendation.
        """

        assignments: list[str] = []
        values: list[Any] = []

        if trailing_stop is not None:
            assignments.append(
                "trailing_stop = ?"
            )

            values.append(
                self._safe_float(
                    trailing_stop
                )
            )

        if status is not None:
            assignments.append(
                "status = ?"
            )

            values.append(
                str(status)
            )

        if not assignments:
            return False

        assignments.append(
            "updated_at = ?"
        )

        values.append(
            self._utc_now()
        )

        values.extend(
            [
                str(session_date),
                str(ticker),
            ]
        )

        try:
            with self._connection() as conn:
                cursor = conn.execute(
                    f"""
                    UPDATE intraday_candidates
                    SET {", ".join(assignments)}
                    WHERE session_date = ?
                      AND ticker = ?
                      AND status = 'ACTIVE'
                    """,
                    values,
                )

                return cursor.rowcount > 0

        except sqlite3.Error:
            logger.exception(
                "Failed to update intraday candidate %s",
                ticker,
            )

            return False

    def close_intraday_candidate(
        self,
        session_date: Optional[str],
        ticker: str,
        exit_reason: str,
    ) -> bool:
        """
        Mark an intraday recommendation as closed.
        """

        if not session_date:
            return False

        try:
            now = self._utc_now()

            with self._connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE intraday_candidates
                    SET status = 'CLOSED',
                        closed_at = ?,
                        updated_at = ?,
                        exit_reason = ?
                    WHERE session_date = ?
                      AND ticker = ?
                      AND status = 'ACTIVE'
                    """,
                    (
                        now,
                        now,
                        str(exit_reason),
                        str(session_date),
                        str(ticker),
                    ),
                )

                return cursor.rowcount > 0

        except sqlite3.Error:
            logger.exception(
                "Failed to close intraday candidate %s",
                ticker,
            )

            return False

    def expire_intraday_candidates(
        self,
        session_date: Optional[str],
        reason: str = "DAILY_EXPIRY",
    ) -> int:
        """
        Close every still-active intraday recommendation for a session.

        Useful when the daily 15:30 boundary is reached.
        """

        if not session_date:
            return 0

        try:
            now = self._utc_now()

            with self._connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE intraday_candidates
                    SET status = 'EXPIRED',
                        closed_at = ?,
                        updated_at = ?,
                        exit_reason = ?
                    WHERE session_date = ?
                      AND status = 'ACTIVE'
                    """,
                    (
                        now,
                        now,
                        str(reason),
                        str(session_date),
                    ),
                )

                return cursor.rowcount

        except sqlite3.Error:
            logger.exception(
                "Failed to expire intraday candidates for %s",
                session_date,
            )

            return 0

    def get_active_intraday_candidates(
        self,
        session_date: str,
    ) -> list[sqlite3.Row]:
        """
        Return all active intraday recommendations for a date.
        """

        try:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM intraday_candidates
                    WHERE session_date = ?
                      AND status = 'ACTIVE'
                    ORDER BY created_at ASC
                    """,
                    (
                        str(session_date),
                    ),
                ).fetchall()

            return list(rows)

        except sqlite3.Error:
            logger.exception(
                "Failed to read intraday candidates for %s",
                session_date,
            )

            return []

    # ========================================================================
    # SIP TARGETS
    # ========================================================================

    def save_sip_target(
        self,
        target: Any,
    ) -> bool:
        """
        Persist a SIP target.

        The target object is intentionally duck-typed so this database
        layer does not import sip_engine.py.

        Expected attributes:
            target_id
            asset
            amount
            frequency
            start_at
            expires_at
            status
            created_at
            next_execution_at
            completed_contributions
            total_invested
            metadata
        """

        try:
            target_id = str(
                target.target_id
            )

            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO sip_targets (
                        target_id,
                        asset,
                        amount,
                        frequency,
                        start_at,
                        expires_at,
                        status,
                        created_at,
                        next_execution_at,
                        completed_contributions,
                        total_invested,
                        metadata
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(target_id)
                    DO UPDATE SET
                        asset = excluded.asset,
                        amount = excluded.amount,
                        frequency = excluded.frequency,
                        start_at = excluded.start_at,
                        expires_at = excluded.expires_at,
                        status = excluded.status,
                        created_at = excluded.created_at,
                        next_execution_at = excluded.next_execution_at,
                        completed_contributions =
                            excluded.completed_contributions,
                        total_invested =
                            excluded.total_invested,
                        metadata = excluded.metadata
                    """,
                    (
                        target_id,
                        str(target.asset),
                        self._safe_float(
                            target.amount
                        ),
                        str(target.frequency),
                        self._datetime_to_text(
                            target.start_at
                        ),
                        self._datetime_to_text(
                            target.expires_at
                        ),
                        self._status_to_text(
                            target.status
                        ),
                        self._datetime_to_text(
                            target.created_at
                        ),
                        self._datetime_to_text(
                            target.next_execution_at
                        ),
                        self._safe_int(
                            target.completed_contributions
                        ),
                        self._safe_float(
                            target.total_invested
                        ),
                        self._json_dumps(
                            getattr(
                                target,
                                "metadata",
                                {},
                            )
                        ),
                    ),
                )

            return True

        except sqlite3.Error:
            logger.exception(
                "Failed to persist SIP target %s",
                getattr(
                    target,
                    "target_id",
                    "<unknown>",
                ),
            )

            return False

    def get_sip_target(
        self,
        target_id: str,
    ) -> Optional[SIPTargetRecord]:
        """
        Retrieve one SIP target by its public target ID.
        """

        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT
                        id,
                        target_id,
                        asset,
                        amount,
                        frequency,
                        start_at,
                        expires_at,
                        status,
                        created_at,
                        next_execution_at,
                        completed_contributions,
                        total_invested,
                        metadata
                    FROM sip_targets
                    WHERE target_id = ?
                    LIMIT 1
                    """,
                    (
                        str(target_id),
                    ),
                ).fetchone()

            if row is None:
                return None

            return self._row_to_sip_target(
                row
            )

        except sqlite3.Error:
            logger.exception(
                "Failed to read SIP target %s",
                target_id,
            )

            return None

    def get_sip_targets(
        self,
        *,
        status: Optional[str] = None,
        asset: Optional[str] = None,
        include_terminal: bool = True,
    ) -> list[SIPTargetRecord]:
        """
        Retrieve SIP targets.

        Optional filters:
            status
            asset
            include_terminal

        Terminal states:
            CANCELLED
            EXPIRED
            COMPLETED
        """

        clauses: list[str] = []
        values: list[Any] = []

        if status is not None:
            clauses.append(
                "status = ?"
            )

            values.append(
                str(status).upper()
            )

        if asset is not None:
            clauses.append(
                "asset = ?"
            )

            values.append(
                str(asset).upper()
            )

        if not include_terminal:
            clauses.append(
                """
                status NOT IN (
                    'CANCELLED',
                    'EXPIRED',
                    'COMPLETED'
                )
                """
            )

        where_clause = ""

        if clauses:
            where_clause = (
                "WHERE "
                + " AND ".join(clauses)
            )

        try:
            with self._connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT
                        id,
                        target_id,
                        asset,
                        amount,
                        frequency,
                        start_at,
                        expires_at,
                        status,
                        created_at,
                        next_execution_at,
                        completed_contributions,
                        total_invested,
                        metadata
                    FROM sip_targets
                    {where_clause}
                    ORDER BY created_at ASC
                    """,
                    values,
                ).fetchall()

            return [
                self._row_to_sip_target(row)
                for row in rows
            ]

        except sqlite3.Error:
            logger.exception(
                "Failed to read SIP targets"
            )

            return []

    def update_sip_target(
        self,
        target_id: str,
        *,
        status: Optional[str] = None,
        next_execution_at: Optional[Any] = None,
        completed_contributions: Optional[int] = None,
        total_invested: Optional[float] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        Update mutable SIP target state.

        Only explicitly supplied fields are modified.
        """

        assignments: list[str] = []
        values: list[Any] = []

        if status is not None:
            assignments.append(
                "status = ?"
            )

            values.append(
                str(status).upper()
            )

        if next_execution_at is not None:
            assignments.append(
                "next_execution_at = ?"
            )

            values.append(
                self._datetime_to_text(
                    next_execution_at
                )
            )

        if completed_contributions is not None:
            assignments.append(
                "completed_contributions = ?"
            )

            values.append(
                self._safe_int(
                    completed_contributions
                )
            )

        if total_invested is not None:
            assignments.append(
                "total_invested = ?"
            )

            values.append(
                self._safe_float(
                    total_invested
                )
            )

        if metadata is not None:
            assignments.append(
                "metadata = ?"
            )

            values.append(
                self._json_dumps(
                    metadata
                )
            )

        if not assignments:
            return False

        values.append(
            str(target_id)
        )

        try:
            with self._connection() as conn:
                cursor = conn.execute(
                    f"""
                    UPDATE sip_targets
                    SET {", ".join(assignments)}
                    WHERE target_id = ?
                    """,
                    values,
                )

                return cursor.rowcount > 0

        except sqlite3.Error:
            logger.exception(
                "Failed to update SIP target %s",
                target_id,
            )

            return False

    def delete_sip_target(
        self,
        target_id: str,
    ) -> bool:
        """
        Permanently delete a SIP target and its dependent records.

        This is a database maintenance operation.

        Normal SIP lifecycle should use CANCELLED/COMPLETED rather than
        deleting historical targets.
        """

        try:
            with self._connection() as conn:
                cursor = conn.execute(
                    """
                    DELETE FROM sip_targets
                    WHERE target_id = ?
                    """,
                    (
                        str(target_id),
                    ),
                )

                return cursor.rowcount > 0

        except sqlite3.Error:
            logger.exception(
                "Failed to delete SIP target %s",
                target_id,
            )

            return False

    # ========================================================================
    # SIP CONTRIBUTIONS
    # ========================================================================

    def record_sip_contribution(
        self,
        target_id: str,
        amount: float,
        *,
        timestamp: Optional[Any] = None,
        execution_price: Optional[float] = None,
        quantity: Optional[float] = None,
        broker_order_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[int]:
        """
        Persist one completed SIP contribution.

        This method does NOT execute a broker order.

        The caller should invoke this only after the execution layer has
        established that the contribution actually occurred.
        """

        timestamp_text = (
            self._datetime_to_text(
                timestamp
            )
            if timestamp is not None
            else self._utc_now()
        )

        try:
            with self._connection() as conn:

                # Make sure the target exists before recording a contribution.
                target_exists = conn.execute(
                    """
                    SELECT 1
                    FROM sip_targets
                    WHERE target_id = ?
                    LIMIT 1
                    """,
                    (
                        str(target_id),
                    ),
                ).fetchone()

                if target_exists is None:
                    logger.warning(
                        "Cannot record SIP contribution: "
                        "target %s does not exist",
                        target_id,
                    )

                    return None

                cursor = conn.execute(
                    """
                    INSERT INTO sip_contributions (
                        target_id,
                        timestamp,
                        amount,
                        execution_price,
                        quantity,
                        broker_order_id,
                        metadata
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(target_id),
                        timestamp_text,
                        self._safe_float(
                            amount
                        ),
                        self._safe_optional_float(
                            execution_price
                        ),
                        self._safe_optional_float(
                            quantity
                        ),
                        (
                            str(broker_order_id)
                            if broker_order_id is not None
                            else None
                        ),
                        self._json_dumps(
                            metadata or {}
                        ),
                    ),
                )

                contribution_id = int(
                    cursor.lastrowid
                )

                # Keep the target's aggregate counters synchronized with
                # the contribution history.
                conn.execute(
                    """
                    UPDATE sip_targets
                    SET completed_contributions =
                            completed_contributions + 1,
                        total_invested =
                            total_invested + ?
                    WHERE target_id = ?
                    """,
                    (
                        self._safe_float(
                            amount
                        ),
                        str(target_id),
                    ),
                )

                return contribution_id

        except sqlite3.Error:
            logger.exception(
                "Failed to record SIP contribution for %s",
                target_id,
            )

            return None

    def get_sip_contributions(
        self,
        target_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[SIPContributionRecord]:
        """
        Retrieve SIP contribution history.

        Results are oldest-first unless a limit is supplied, in which case
        the most recent `limit` records are returned in chronological order.
        """

        clauses: list[str] = []
        values: list[Any] = []

        if target_id is not None:
            clauses.append(
                "target_id = ?"
            )

            values.append(
                str(target_id)
            )

        where_clause = ""

        if clauses:
            where_clause = (
                "WHERE "
                + " AND ".join(clauses)
            )

        limit_clause = ""

        normalized_limit: Optional[int] = None

        if limit is not None:
            normalized_limit = self._safe_int(
                limit
            )

            if normalized_limit <= 0:
                return []

            limit_clause = (
                "LIMIT ?"
            )

            values.append(
                normalized_limit
            )

        try:
            with self._connection() as conn:

                if normalized_limit is not None:
                    rows = conn.execute(
                        f"""
                        SELECT
                            id,
                            target_id,
                            timestamp,
                            amount,
                            execution_price,
                            quantity,
                            broker_order_id,
                            metadata
                        FROM sip_contributions
                        {where_clause}
                        ORDER BY timestamp DESC
                        {limit_clause}
                        """,
                        values,
                    ).fetchall()

                    rows = list(
                        reversed(rows)
                    )

                else:
                    rows = conn.execute(
                        f"""
                        SELECT
                            id,
                            target_id,
                            timestamp,
                            amount,
                            execution_price,
                            quantity,
                            broker_order_id,
                            metadata
                        FROM sip_contributions
                        {where_clause}
                        ORDER BY timestamp ASC
                        """,
                        values,
                    ).fetchall()

            return [
                self._row_to_sip_contribution(
                    row
                )
                for row in rows
            ]

        except sqlite3.Error:
            logger.exception(
                "Failed to read SIP contributions"
            )

            return []

    # ========================================================================
    # SIP DECISIONS
    # ========================================================================

    def record_sip_decision(
        self,
        decision: Any,
    ) -> Optional[int]:
        """
        Persist one SIP decision snapshot.

        Expected attributes mirror sip_engine.SIPDecision:

            timestamp
            target_id
            asset
            decision
            price
            rsi
            macd
            trend
            market_condition
            allocation
            reason
            technical_signals
            market_context
            risk_checks
            risk_passed
            metadata

        The object is intentionally duck-typed to prevent a circular
        dependency on sip_engine.py.
        """

        target_id = getattr(
            decision,
            "target_id",
            None,
        )

        if not target_id:
            logger.warning(
                "Cannot persist SIP decision without target_id."
            )

            return None

        timestamp = getattr(
            decision,
            "timestamp",
            None,
        )

        timestamp_text = (
            self._datetime_to_text(
                timestamp
            )
            if timestamp is not None
            else self._utc_now()
        )

        try:
            with self._connection() as conn:

                # The decision belongs to a target. Enforce that relation
                # explicitly before inserting.
                target_exists = conn.execute(
                    """
                    SELECT 1
                    FROM sip_targets
                    WHERE target_id = ?
                    LIMIT 1
                    """,
                    (
                        str(target_id),
                    ),
                ).fetchone()

                if target_exists is None:
                    logger.warning(
                        "Cannot persist SIP decision: "
                        "target %s does not exist",
                        target_id,
                    )

                    return None

                cursor = conn.execute(
                    """
                    INSERT INTO sip_decisions (
                        timestamp,
                        target_id,
                        asset,
                        decision,
                        price,
                        rsi,
                        macd,
                        trend,
                        market_condition,
                        allocation,
                        reason,
                        technical_signals,
                        market_context,
                        risk_checks,
                        risk_passed,
                        metadata
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        timestamp_text,
                        str(target_id),
                        str(
                            getattr(
                                decision,
                                "asset",
                                "",
                            )
                        ),
                        str(
                            getattr(
                                decision,
                                "decision",
                                "",
                            )
                        ),
                        self._safe_optional_float(
                            getattr(
                                decision,
                                "price",
                                None,
                            )
                        ),
                        self._safe_optional_float(
                            getattr(
                                decision,
                                "rsi",
                                None,
                            )
                        ),
                        self._safe_optional_float(
                            getattr(
                                decision,
                                "macd",
                                None,
                            )
                        ),
                        getattr(
                            decision,
                            "trend",
                            None,
                        ),
                        getattr(
                            decision,
                            "market_condition",
                            None,
                        ),
                        self._safe_optional_float(
                            getattr(
                                decision,
                                "allocation",
                                None,
                            )
                        ),
                        str(
                            getattr(
                                decision,
                                "reason",
                                "",
                            )
                        ),
                        self._json_dumps(
                            getattr(
                                decision,
                                "technical_signals",
                                {},
                            )
                        ),
                        self._json_dumps(
                            getattr(
                                decision,
                                "market_context",
                                {},
                            )
                        ),
                        self._json_dumps(
                            getattr(
                                decision,
                                "risk_checks",
                                {},
                            )
                        ),
                        self._bool_to_db(
                            getattr(
                                decision,
                                "risk_passed",
                                None,
                            )
                        ),
                        self._json_dumps(
                            getattr(
                                decision,
                                "metadata",
                                {},
                            )
                        ),
                    ),
                )

                return int(
                    cursor.lastrowid
                )

        except sqlite3.Error:
            logger.exception(
                "Failed to persist SIP decision for %s",
                target_id,
            )

            return None

    def get_sip_decisions(
        self,
        target_id: Optional[str] = None,
        *,
        asset: Optional[str] = None,
        decision: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[SIPDecisionRecord]:
        """
        Retrieve SIP decision history.

        Optional filters:
            target_id
            asset
            decision
            limit

        Results are returned oldest-first.
        """

        clauses: list[str] = []
        values: list[Any] = []

        if target_id is not None:
            clauses.append(
                "target_id = ?"
            )

            values.append(
                str(target_id)
            )

        if asset is not None:
            clauses.append(
                "asset = ?"
            )

            values.append(
                str(asset).upper()
            )

        if decision is not None:
            clauses.append(
                "decision = ?"
            )

            values.append(
                str(decision).upper()
            )

        where_clause = ""

        if clauses:
            where_clause = (
                "WHERE "
                + " AND ".join(clauses)
            )

        limit_clause = ""

        if limit is not None:
            normalized_limit = self._safe_int(
                limit
            )

            if normalized_limit <= 0:
                return []

            limit_clause = (
                "LIMIT ?"
            )

            values.append(
                normalized_limit
            )

        try:
            with self._connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT
                        id,
                        timestamp,
                        target_id,
                        asset,
                        decision,
                        price,
                        rsi,
                        macd,
                        trend,
                        market_condition,
                        allocation,
                        reason,
                        technical_signals,
                        market_context,
                        risk_checks,
                        risk_passed,
                        metadata
                    FROM sip_decisions
                    {where_clause}
                    ORDER BY timestamp ASC
                    {limit_clause}
                    """,
                    values,
                ).fetchall()

            return [
                self._row_to_sip_decision(
                    row
                )
                for row in rows
            ]

        except sqlite3.Error:
            logger.exception(
                "Failed to read SIP decisions"
            )

            return []

    def get_latest_sip_decision(
        self,
        target_id: str,
    ) -> Optional[SIPDecisionRecord]:
        """
        Return the latest decision for one SIP target.
        """

        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT
                        id,
                        timestamp,
                        target_id,
                        asset,
                        decision,
                        price,
                        rsi,
                        macd,
                        trend,
                        market_condition,
                        allocation,
                        reason,
                        technical_signals,
                        market_context,
                        risk_checks,
                        risk_passed,
                        metadata
                    FROM sip_decisions
                    WHERE target_id = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                    """,
                    (
                        str(target_id),
                    ),
                ).fetchone()

            if row is None:
                return None

            return self._row_to_sip_decision(
                row
            )

        except sqlite3.Error:
            logger.exception(
                "Failed to read latest SIP decision for %s",
                target_id,
            )

            return None

    # ========================================================================
    # SIP STATISTICS
    # ========================================================================

    def get_sip_statistics(self) -> dict[str, Any]:
        """
        Return aggregate SIP database statistics.

        This is intended for status dashboards and diagnostics.
        """

        try:
            with self._connection() as conn:

                target_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sip_targets
                    """
                ).fetchone()[0]

                active_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sip_targets
                    WHERE status = 'ACTIVE'
                    """
                ).fetchone()[0]

                paused_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sip_targets
                    WHERE status = 'PAUSED'
                    """
                ).fetchone()[0]

                completed_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sip_targets
                    WHERE status = 'COMPLETED'
                    """
                ).fetchone()[0]

                cancelled_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sip_targets
                    WHERE status = 'CANCELLED'
                    """
                ).fetchone()[0]

                expired_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sip_targets
                    WHERE status = 'EXPIRED'
                    """
                ).fetchone()[0]

                contribution_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sip_contributions
                    """
                ).fetchone()[0]

                total_invested = conn.execute(
                    """
                    SELECT COALESCE(
                        SUM(amount),
                        0
                    )
                    FROM sip_contributions
                    """
                ).fetchone()[0]

                decision_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sip_decisions
                    """
                ).fetchone()[0]

            return {
                "target_count": int(
                    target_count or 0
                ),
                "active_target_count": int(
                    active_count or 0
                ),
                "paused_target_count": int(
                    paused_count or 0
                ),
                "completed_target_count": int(
                    completed_count or 0
                ),
                "cancelled_target_count": int(
                    cancelled_count or 0
                ),
                "expired_target_count": int(
                    expired_count or 0
                ),
                "contribution_count": int(
                    contribution_count or 0
                ),
                "total_invested": self._safe_float(
                    total_invested
                ),
                "decision_count": int(
                    decision_count or 0
                ),
            }

        except sqlite3.Error:
            logger.exception(
                "Failed to calculate SIP statistics"
            )

            return {
                "target_count": 0,
                "active_target_count": 0,
                "paused_target_count": 0,
                "completed_target_count": 0,
                "cancelled_target_count": 0,
                "expired_target_count": 0,
                "contribution_count": 0,
                "total_invested": 0.0,
                "decision_count": 0,
            }

    # ========================================================================
    # SIP ROW CONVERSION
    # ========================================================================

    @classmethod
    def _row_to_sip_target(
        cls,
        row: sqlite3.Row,
    ) -> SIPTargetRecord:
        """
        Convert a SQLite row into SIPTargetRecord.
        """

        return SIPTargetRecord(
            id=int(
                row["id"]
            ),
            target_id=str(
                row["target_id"]
            ),
            asset=str(
                row["asset"]
            ),
            amount=cls._safe_float(
                row["amount"]
            ),
            frequency=str(
                row["frequency"]
            ),
            start_at=str(
                row["start_at"]
            ),
            expires_at=str(
                row["expires_at"]
            ),
            status=str(
                row["status"]
            ),
            created_at=str(
                row["created_at"]
            ),
            next_execution_at=(
                str(
                    row["next_execution_at"]
                )
                if row["next_execution_at"]
                is not None
                else None
            ),
            completed_contributions=cls._safe_int(
                row[
                    "completed_contributions"
                ]
            ),
            total_invested=cls._safe_float(
                row["total_invested"]
            ),
            metadata=cls._json_loads(
                row["metadata"],
                {},
            ),
        )

    @classmethod
    def _row_to_sip_contribution(
        cls,
        row: sqlite3.Row,
    ) -> SIPContributionRecord:
        """
        Convert a SQLite row into SIPContributionRecord.
        """

        return SIPContributionRecord(
            id=int(
                row["id"]
            ),
            target_id=str(
                row["target_id"]
            ),
            timestamp=str(
                row["timestamp"]
            ),
            amount=cls._safe_float(
                row["amount"]
            ),
            execution_price=cls._safe_optional_float(
                row["execution_price"]
            ),
            quantity=cls._safe_optional_float(
                row["quantity"]
            ),
            broker_order_id=(
                str(
                    row["broker_order_id"]
                )
                if row["broker_order_id"]
                is not None
                else None
            ),
            metadata=cls._json_loads(
                row["metadata"],
                {},
            ),
        )

    @classmethod
    def _row_to_sip_decision(
        cls,
        row: sqlite3.Row,
    ) -> SIPDecisionRecord:
        """
        Convert a SQLite row into SIPDecisionRecord.
        """

        return SIPDecisionRecord(
            id=int(
                row["id"]
            ),
            timestamp=str(
                row["timestamp"]
            ),
            target_id=str(
                row["target_id"]
            ),
            asset=str(
                row["asset"]
            ),
            decision=str(
                row["decision"]
            ),
            price=cls._safe_optional_float(
                row["price"]
            ),
            rsi=cls._safe_optional_float(
                row["rsi"]
            ),
            macd=cls._safe_optional_float(
                row["macd"]
            ),
            trend=(
                str(
                    row["trend"]
                )
                if row["trend"] is not None
                else None
            ),
            market_condition=(
                str(
                    row["market_condition"]
                )
                if row["market_condition"] is not None
                else None
            ),
            allocation=cls._safe_optional_float(
                row["allocation"]
            ),
            reason=str(
                row["reason"]
            ),
            technical_signals=cls._json_loads(
                row["technical_signals"],
                {},
            ),
            market_context=cls._json_loads(
                row["market_context"],
                {},
            ),
            risk_checks=cls._json_loads(
                row["risk_checks"],
                {},
            ),
            risk_passed=cls._db_to_bool(
                row["risk_passed"]
            ),
            metadata=cls._json_loads(
                row["metadata"],
                {},
            ),
        )

    # ========================================================================
    # DATETIME / STATUS HELPERS
    # ========================================================================

    @staticmethod
    def _datetime_to_text(
        value: Any,
    ) -> Optional[str]:
        """
        Convert datetime-like values to database text.

        Strings are preserved because the SIP engine already serializes
        its timestamps as ISO-8601 strings.
        """

        if value is None:
            return None

        if isinstance(
            value,
            datetime,
        ):
            return value.isoformat()

        return str(value)

    @staticmethod
    def _status_to_text(
        value: Any,
    ) -> str:
        """
        Convert an Enum-like status to its string value.
        """

        if hasattr(
            value,
            "value",
        ):
            return str(
                value.value
            )

        return str(value)

    @staticmethod
    def _bool_to_db(
        value: Optional[bool],
    ) -> Optional[int]:
        """
        Convert Python bool/None to SQLite representation.
        """

        if value is None:
            return None

        return 1 if bool(value) else 0

    @staticmethod
    def _db_to_bool(
        value: Any,
    ) -> Optional[bool]:
        """
        Convert SQLite integer representation to bool/None.
        """

        if value is None:
            return None

        return bool(
            int(value)
        )

    # ========================================================================
    # MAINTENANCE / DIAGNOSTICS
    # ========================================================================

    def health_check(
        self,
    ) -> bool:
        """
        Return True when SQLite is reachable and responsive.
        """

        try:
            with self._connection() as conn:
                result = conn.execute(
                    "SELECT 1"
                ).fetchone()

            return (
                result is not None
                and result[0] == 1
            )

        except sqlite3.Error:
            logger.exception(
                "SQLite health check failed"
            )

            return False

    def get_schema_version(
        self,
    ) -> int:
        """
        Return the current SQLite schema version.
        """

        try:
            with self._connection() as conn:
                row = conn.execute(
                    "PRAGMA user_version"
                ).fetchone()

            return int(
                row[0]
            )

        except sqlite3.Error:
            logger.exception(
                "Failed to read SQLite schema version"
            )

            return 0

    def close(
        self,
    ) -> None:
        """
        Compatibility method.

        Connections are intentionally not persistent, so there is nothing
        to close.

        Kept so callers can safely call db.close() if desired.
        """

        return None

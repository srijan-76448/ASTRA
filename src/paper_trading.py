"""
ASTRA Paper Trading Simulator
=============================

Local, SQLite-backed virtual account for ASTRA intraday paper trading.

Safety properties:
- This module never imports or calls a broker API.
- All orders are simulated and recorded locally.
- Virtual balance is independent of the real Angel One wallet.
- Repeated client_order_id values are idempotent.
- Prices use an adverse slippage model; fees are explicit configurable estimates.
- Existing account balance is preserved on restart; changing initial_balance does
  not reset an already-created account.

Limitations of V1:
- Full fills only; no queue-position or partial-fill simulation.
- When only last-traded prices are available, the fill model approximates spread
  and slippage with a configurable adverse basis-point adjustment.
- Fee defaults are estimates, not a promise of exact broker charges.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("ASTRA_PAPER_TRADING")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _positive_float(value: Any, name: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        comparator = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be finite and {comparator}")
    return number


@dataclass(frozen=True)
class PaperOrder:
    order_id: str
    client_order_id: str
    ticker: str
    action: str
    quantity: int
    reference_price: float
    fill_price: float
    gross_value: float
    fees: float
    status: str
    reason: str
    session_date: str
    realized_pnl: float
    created_at: str


@dataclass(frozen=True)
class PaperAccountSnapshot:
    account_id: str
    initial_balance: float
    cash_balance: float
    invested_value: float
    unrealized_pnl: float
    realized_pnl: float
    equity: float
    net_pnl: float
    open_positions: int
    as_of: str


class PaperTradingSimulator:
    """Persistent virtual cash and order simulator backed by SQLite."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        account_id: str = "ASTRA_INTRADAY_PAPER",
        initial_balance: float = 100_000.0,
        slippage_bps: float = 5.0,
        brokerage_rate: float = 0.0003,
        brokerage_cap_per_order: float = 20.0,
        transaction_charge_bps: float = 3.5,
        stt_sell_bps: float = 2.5,
        stamp_buy_bps: float = 0.3,
        gst_rate: float = 0.18,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.account_id = str(account_id).strip()
        if not self.account_id:
            raise ValueError("account_id cannot be blank")
        self.initial_balance = _positive_float(initial_balance, "initial_balance")
        self.slippage_bps = _positive_float(slippage_bps, "slippage_bps", allow_zero=True)
        self.brokerage_rate = _positive_float(brokerage_rate, "brokerage_rate", allow_zero=True)
        self.brokerage_cap_per_order = _positive_float(
            brokerage_cap_per_order, "brokerage_cap_per_order", allow_zero=True
        )
        self.transaction_charge_bps = _positive_float(
            transaction_charge_bps, "transaction_charge_bps", allow_zero=True
        )
        self.stt_sell_bps = _positive_float(stt_sell_bps, "stt_sell_bps", allow_zero=True)
        self.stamp_buy_bps = _positive_float(stamp_buy_bps, "stamp_buy_bps", allow_zero=True)
        self.gst_rate = _positive_float(gst_rate, "gst_rate", allow_zero=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    account_id TEXT PRIMARY KEY,
                    initial_balance REAL NOT NULL,
                    cash_balance REAL NOT NULL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_positions (
                    account_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    avg_entry_price REAL NOT NULL CHECK(avg_entry_price > 0),
                    entry_fees_remaining REAL NOT NULL DEFAULT 0,
                    mark_price REAL NOT NULL CHECK(mark_price > 0),
                    session_date TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, ticker),
                    FOREIGN KEY(account_id) REFERENCES paper_accounts(account_id)
                );

                CREATE TABLE IF NOT EXISTS paper_orders (
                    order_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('BUY', 'SELL')),
                    quantity INTEGER NOT NULL,
                    reference_price REAL NOT NULL,
                    fill_price REAL,
                    gross_value REAL NOT NULL DEFAULT 0,
                    fees REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    session_date TEXT NOT NULL,
                    cash_delta REAL NOT NULL DEFAULT 0,
                    realized_pnl_delta REAL NOT NULL DEFAULT 0,
                    position_before TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, client_order_id),
                    FOREIGN KEY(account_id) REFERENCES paper_accounts(account_id)
                );

                CREATE INDEX IF NOT EXISTS idx_paper_orders_account_created
                    ON paper_orders(account_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_paper_orders_session
                    ON paper_orders(account_id, session_date);
                """
            )
            # Additive migration for an account database created by an earlier
            # iteration of this simulator.
            position_columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_positions)")}
            if "session_date" not in position_columns:
                conn.execute("ALTER TABLE paper_positions ADD COLUMN session_date TEXT NOT NULL DEFAULT ''")

            now = _utc_now()
            conn.execute(
                """
                INSERT OR IGNORE INTO paper_accounts
                    (account_id, initial_balance, cash_balance, realized_pnl, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                """,
                (self.account_id, self.initial_balance, self.initial_balance, now, now),
            )

    def _fee(self, action: str, gross_value: float) -> float:
        brokerage = min(self.brokerage_cap_per_order, gross_value * self.brokerage_rate)
        transaction = gross_value * self.transaction_charge_bps / 10_000.0
        stt = gross_value * self.stt_sell_bps / 10_000.0 if action == "SELL" else 0.0
        stamp = gross_value * self.stamp_buy_bps / 10_000.0 if action == "BUY" else 0.0
        gst = (brokerage + transaction) * self.gst_rate
        return round(brokerage + transaction + stt + stamp + gst, 2)

    def _fill_price(self, action: str, reference_price: float) -> float:
        factor = self.slippage_bps / 10_000.0
        # Adverse slippage: buys fill above the reference, sells below it.
        return round(reference_price * (1.0 + factor if action == "BUY" else 1.0 - factor), 4)

    @staticmethod
    def _normalize_intent(intent: Any) -> tuple[str, str, int, float, str, str]:
        action = str(getattr(intent, "action", "")).strip().upper()
        ticker = str(getattr(intent, "ticker", "")).strip().upper()
        quantity_raw = getattr(intent, "quantity", 0)
        price_raw = getattr(intent, "price", 0)
        client_order_id = str(getattr(intent, "intent_id", "")).strip()
        session_date = str(getattr(intent, "session_date", "")).strip()
        if action not in {"BUY", "SELL"}:
            raise ValueError("action must be BUY or SELL")
        if not ticker:
            raise ValueError("ticker cannot be blank")
        if isinstance(quantity_raw, bool):
            raise ValueError("quantity must be a positive integer")
        try:
            quantity = int(quantity_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("quantity must be a positive integer") from exc
        if quantity <= 0 or float(quantity) != float(quantity_raw):
            raise ValueError("quantity must be a positive integer")
        reference_price = _positive_float(price_raw, "price")
        if not client_order_id:
            client_order_id = uuid.uuid4().hex
        if not session_date:
            session_date = datetime.now().date().isoformat()
        return action, ticker, quantity, reference_price, client_order_id, session_date

    def execute(self, intent: Any) -> Optional[str]:
        """Execute a simulated full fill; return the paper order ID on success."""
        try:
            action, ticker, quantity, reference_price, client_id, session_date = self._normalize_intent(intent)
        except ValueError as exc:
            logger.warning("Paper order rejected during validation: %s", exc)
            return None

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT order_id, status FROM paper_orders WHERE account_id=? AND client_order_id=?",
                (self.account_id, client_id),
            ).fetchone()
            if existing:
                return str(existing["order_id"]) if existing["status"] == "FILLED" else None

            order_id = f"PAPER-{action}-{uuid.uuid4().hex[:16].upper()}"
            now = _utc_now()
            fill_price = self._fill_price(action, reference_price)
            gross_value = round(fill_price * quantity, 2)
            fees = self._fee(action, gross_value)
            position = conn.execute(
                "SELECT quantity, avg_entry_price, entry_fees_remaining, mark_price, session_date "
                "FROM paper_positions WHERE account_id=? AND ticker=?",
                (self.account_id, ticker),
            ).fetchone()
            position_before = dict(position) if position else None
            account = conn.execute(
                "SELECT cash_balance, realized_pnl FROM paper_accounts WHERE account_id=?",
                (self.account_id,),
            ).fetchone()
            cash_before = float(account["cash_balance"])
            realized_before = float(account["realized_pnl"])
            status = "FILLED"
            reason = str(getattr(intent, "reason", "") or "")
            cash_delta = 0.0
            realized_delta = 0.0

            if action == "BUY":
                if position:
                    status, reason = "REJECTED", "POSITION_ALREADY_OPEN"
                elif cash_before + 1e-9 < gross_value + fees:
                    status, reason = "REJECTED", "INSUFFICIENT_VIRTUAL_CASH"
                else:
                    conn.execute(
                        "INSERT INTO paper_positions "
                        "(account_id,ticker,quantity,avg_entry_price,entry_fees_remaining,mark_price,session_date,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (self.account_id, ticker, quantity, fill_price, fees, reference_price, session_date, now),
                    )
                    cash_delta = -(gross_value + fees)

            else:
                if not position:
                    status, reason = "REJECTED", "NO_PAPER_POSITION"
                elif quantity > int(position["quantity"]):
                    status, reason = "REJECTED", "SELL_EXCEEDS_PAPER_POSITION"
                else:
                    old_qty = int(position["quantity"])
                    avg_entry = float(position["avg_entry_price"])
                    entry_fees_remaining = float(position["entry_fees_remaining"])
                    allocated_entry_fees = entry_fees_remaining * quantity / old_qty
                    realized_delta = round((fill_price - avg_entry) * quantity - allocated_entry_fees - fees, 2)
                    cash_delta = gross_value - fees
                    remaining_qty = old_qty - quantity
                    if remaining_qty == 0:
                        conn.execute(
                            "DELETE FROM paper_positions WHERE account_id=? AND ticker=?",
                            (self.account_id, ticker),
                        )
                    else:
                        conn.execute(
                            "UPDATE paper_positions SET quantity=?, entry_fees_remaining=?, mark_price=?, updated_at=? "
                            "WHERE account_id=? AND ticker=?",
                            (remaining_qty, entry_fees_remaining - allocated_entry_fees, reference_price, now,
                             self.account_id, ticker),
                        )

            if status == "FILLED":
                conn.execute(
                    "UPDATE paper_accounts SET cash_balance=cash_balance+?, realized_pnl=realized_pnl+?, updated_at=? "
                    "WHERE account_id=?",
                    (cash_delta, realized_delta, now, self.account_id),
                )

            conn.execute(
                """
                INSERT INTO paper_orders (
                    order_id, account_id, client_order_id, ticker, action, quantity,
                    reference_price, fill_price, gross_value, fees, status, reason,
                    session_date, cash_delta, realized_pnl_delta, position_before, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id, self.account_id, client_id, ticker, action, quantity,
                    reference_price, fill_price if status == "FILLED" else None,
                    gross_value if status == "FILLED" else 0.0,
                    fees if status == "FILLED" else 0.0,
                    status, reason, session_date, cash_delta, realized_delta,
                    json.dumps(position_before, sort_keys=True) if position_before is not None else None,
                    now,
                ),
            )
            if status != "FILLED":
                logger.info("PAPER ORDER REJECTED | %s | %s | %s", ticker, action, reason)
                return None
            logger.info(
                "PAPER ORDER FILLED | %s %s qty=%d reference=%.4f fill=%.4f fees=%.2f id=%s",
                action, ticker, quantity, reference_price, fill_price, fees, order_id,
            )
            return order_id

    def buy(self, intent: Any) -> Optional[str]:
        if str(getattr(intent, "action", "")).strip().upper() != "BUY":
            logger.warning("PaperTradingSimulator.buy received a non-BUY intent.")
            return None
        return self.execute(intent)

    def sell(self, intent: Any) -> Optional[str]:
        if str(getattr(intent, "action", "")).strip().upper() != "SELL":
            logger.warning("PaperTradingSimulator.sell received a non-SELL intent.")
            return None
        return self.execute(intent)

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT order_id, client_order_id, ticker, action, quantity, reference_price, "
                "fill_price, gross_value, fees, status, reason, session_date, realized_pnl_delta, created_at "
                "FROM paper_orders WHERE account_id=? AND order_id=?",
                (self.account_id, str(order_id)),
            ).fetchone()
        return dict(row) if row else None

    def get_fill_price(self, order_id: str) -> Optional[float]:
        row = self.get_order(order_id)
        if not row or row["status"] != "FILLED" or row["fill_price"] is None:
            return None
        return float(row["fill_price"])

    def get_order_by_client_id(self, client_order_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT order_id, client_order_id, ticker, action, quantity, reference_price, "
                "fill_price, gross_value, fees, status, reason, session_date, realized_pnl_delta, created_at "
                "FROM paper_orders WHERE account_id=? AND client_order_id=?",
                (self.account_id, str(client_order_id)),
            ).fetchone()
        return dict(row) if row else None

    def get_available_cash(self) -> float:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT cash_balance FROM paper_accounts WHERE account_id=?",
                (self.account_id,),
            ).fetchone()
        if not row:
            raise RuntimeError("Paper account has not been initialized")
        return float(row["cash_balance"])

    def mark_price(self, ticker: str, price: float) -> bool:
        normalized = str(ticker or "").strip().upper()
        if not normalized:
            return False
        try:
            mark = _positive_float(price, "price")
        except ValueError:
            return False
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE paper_positions SET mark_price=?, updated_at=? WHERE account_id=? AND ticker=?",
                (mark, _utc_now(), self.account_id, normalized),
            )
            return cursor.rowcount > 0

    def positions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT ticker, quantity, avg_entry_price, entry_fees_remaining, mark_price, session_date, updated_at "
                "FROM paper_positions WHERE account_id=? ORDER BY ticker",
                (self.account_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self) -> PaperAccountSnapshot:
        with self._lock, self._connect() as conn:
            account = conn.execute(
                "SELECT initial_balance, cash_balance, realized_pnl FROM paper_accounts WHERE account_id=?",
                (self.account_id,),
            ).fetchone()
            rows = conn.execute(
                "SELECT quantity, avg_entry_price, entry_fees_remaining, mark_price, session_date "
                "FROM paper_positions WHERE account_id=?",
                (self.account_id,),
            ).fetchall()
        if not account:
            raise RuntimeError("Paper account has not been initialized")
        invested = sum(float(row["quantity"]) * float(row["avg_entry_price"]) for row in rows)
        unrealized = sum(
            (float(row["mark_price"]) - float(row["avg_entry_price"])) * int(row["quantity"])
            - float(row["entry_fees_remaining"])
            for row in rows
        )
        cash = float(account["cash_balance"])
        equity = cash + sum(float(row["mark_price"]) * int(row["quantity"]) for row in rows)
        initial = float(account["initial_balance"])
        return PaperAccountSnapshot(
            account_id=self.account_id,
            initial_balance=initial,
            cash_balance=cash,
            invested_value=invested,
            unrealized_pnl=round(unrealized, 2),
            realized_pnl=round(float(account["realized_pnl"]), 2),
            equity=round(equity, 2),
            net_pnl=round(equity - initial, 2),
            open_positions=len(rows),
            as_of=_utc_now(),
        )

    def session_pnl(self, session_date: str) -> float:
        """Return realized plus marked unrealized P&L for one paper session."""
        day = str(session_date)
        with self._lock, self._connect() as conn:
            realized = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl_delta), 0) FROM paper_orders "
                "WHERE account_id=? AND session_date=? AND action='SELL' AND status='FILLED'",
                (self.account_id, day),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT quantity, avg_entry_price, entry_fees_remaining, mark_price "
                "FROM paper_positions WHERE account_id=? AND session_date=?",
                (self.account_id, day),
            ).fetchall()
        unrealized = sum(
            (float(row["mark_price"]) - float(row["avg_entry_price"])) * int(row["quantity"])
            - float(row["entry_fees_remaining"])
            for row in rows
        )
        return round(float(realized or 0.0) + unrealized, 2)

    def daily_loss_breached(self, max_daily_loss_fraction: float, session_date: str) -> bool:
        """Check a configured daily loss cap against realized + marked P&L."""
        try:
            fraction = _positive_float(max_daily_loss_fraction, "max_daily_loss_fraction", allow_zero=True)
        except ValueError:
            return True
        if fraction <= 0:
            return False
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT initial_balance FROM paper_accounts WHERE account_id=?",
                (self.account_id,),
            ).fetchone()
        if not row:
            return True
        limit_amount = float(row["initial_balance"]) * fraction
        return self.session_pnl(session_date) <= -limit_amount

    def orders(self, *, limit: int = 100, session_date: Optional[str] = None) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 5000))
        query = (
            "SELECT order_id, client_order_id, ticker, action, quantity, reference_price, "
            "fill_price, gross_value, fees, status, reason, session_date, realized_pnl_delta, created_at "
            "FROM paper_orders WHERE account_id=?"
        )
        params: list[Any] = [self.account_id]
        if session_date:
            query += " AND session_date=?"
            params.append(str(session_date))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(safe_limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def rollback_order(self, order_id: str, reason: str = "LEDGER_SYNC_FAILED") -> bool:
        """Compensate a just-filled order if ASTRA's ownership ledger fails.

        This is intended for immediate integration rollback, not arbitrary
        historical trade editing. The latest order for the account must match.
        """
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE account_id=? AND order_id=?",
                (self.account_id, str(order_id)),
            ).fetchone()
            if not row or row["status"] != "FILLED":
                return False
            latest = conn.execute(
                "SELECT order_id FROM paper_orders WHERE account_id=? AND status='FILLED' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (self.account_id,),
            ).fetchone()
            if not latest or latest["order_id"] != order_id:
                logger.error("Refusing rollback of non-latest paper order %s", order_id)
                return False

            ticker = row["ticker"]
            action = row["action"]
            quantity = int(row["quantity"])
            cash_delta = float(row["cash_delta"])
            realized_delta = float(row["realized_pnl_delta"])
            before = json.loads(row["position_before"]) if row["position_before"] else None
            now = _utc_now()
            conn.execute(
                "UPDATE paper_accounts SET cash_balance=cash_balance-?, realized_pnl=realized_pnl-?, updated_at=? "
                "WHERE account_id=?",
                (cash_delta, realized_delta, now, self.account_id),
            )
            conn.execute("DELETE FROM paper_positions WHERE account_id=? AND ticker=?", (self.account_id, ticker))
            if before:
                conn.execute(
                    "INSERT INTO paper_positions "
                    "(account_id,ticker,quantity,avg_entry_price,entry_fees_remaining,mark_price,session_date,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (self.account_id, ticker, int(before["quantity"]), float(before["avg_entry_price"]),
                     float(before["entry_fees_remaining"]), float(before["mark_price"]), str(before.get("session_date", "")), now),
                )
            conn.execute(
                "UPDATE paper_orders SET status='ROLLED_BACK', reason=? WHERE account_id=? AND order_id=?",
                (str(reason), self.account_id, str(order_id)),
            )
            return True

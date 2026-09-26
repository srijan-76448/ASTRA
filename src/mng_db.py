import sqlite3
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("ASTRA_DB")
DB_PATH = Path(__file__).resolve().parent.parent / "astra.db"

class DatabaseManager:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        """Initializes SQLite tables for local historical persistence."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS signal_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        ticker TEXT,
                        strategy TEXT,
                        price REAL,
                        rsi REAL,
                        macd REAL,
                        action TEXT
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        available_cash REAL,
                        total_invested REAL,
                        current_value REAL,
                        total_pnl REAL
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS system_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        level TEXT,
                        message TEXT
                    )
                """)
                conn.commit()
            logger.info(f"Database initialized successfully at: {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize SQLite database: {e}")

    def log_signal(self, ticker: str, strategy: str, price: float, rsi: float, macd: float, action: str):
        """Persists a generated trade signal."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO signal_history (timestamp, ticker, strategy, price, rsi, macd, action)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (datetime.utcnow().isoformat(), ticker, strategy, price, rsi, macd, action))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to persist signal record for {ticker}: {e}")

    def log_portfolio_snapshot(self, cash: float, invested: float, current: float, pnl: float):
        """Persists portfolio performance metrics."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO portfolio_snapshots (timestamp, available_cash, total_invested, current_value, total_pnl)
                    VALUES (?, ?, ?, ?, ?)
                """, (datetime.utcnow().isoformat(), cash, invested, current, pnl))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to persist portfolio snapshot: {e}")

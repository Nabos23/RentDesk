"""
SQLite Database helper.

Replaces MongoDB with a zero-config local SQLite database (rental_bot.db).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from app.core.logging import get_logger

logger = get_logger(__name__)

DB_PATH = Path("rental_bot.db")


def get_db_connection() -> sqlite3.Connection:
    """Get a connection to the local SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize SQLite database tables if they do not exist."""
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS message_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT UNIQUE,
                    sender_phone TEXT,
                    group_id TEXT,
                    raw_type TEXT,
                    text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        logger.info("sqlite_connected", db_path=str(DB_PATH.resolve()))
    finally:
        conn.close()


async def close_client() -> None:
    """No-op for SQLite connection cleanup."""
    pass

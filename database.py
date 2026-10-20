""""Database operations for storing resource metadata (Turso or local SQLite)."""

import os
import sqlite3
from config import DB_FILE, log

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")


class _Cursor:
    """Wraps a cursor so rows come back as dicts (row["title"] still works)."""

    def __init__(self, cur):
        self._cur = cur

    def _to_dict(self, row):
        cols = [d[0] for d in self._cur.description]
        return dict(zip(cols, row))

    def fetchall(self):
        return [self._to_dict(r) for r in self._cur.fetchall()]

    def fetchone(self):
        row = self._cur.fetchone()
        return self._to_dict(row) if row else None


class _Conn:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        try:
            return _Cursor(self._conn.execute(sql, params))
        except sqlite3.Error:
            raise
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise

    def commit(self):
        self._conn.commit()

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def get_db_connection():
    """Returns a connection (Turso if configured, otherwise local SQLite)."""
    if TURSO_URL and TURSO_TOKEN:
        import libsql
        return _Conn(libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN))
    return _Conn(sqlite3.connect(DB_FILE))


def init_db():
    """Initializes the database schema."""
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            subject TEXT NOT NULL,
            drive_file_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    log.info("Database initialized successfully (%s).",
             "Turso" if TURSO_URL else DB_FILE)


def add_file_record(title: str, subject: str, drive_file_id: str):
    """Inserts a new resource record."""
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO files (title, subject, drive_file_id) VALUES (?, ?, ?)",
        (title, subject, drive_file_id),
    )
    conn.commit()
    conn.close()

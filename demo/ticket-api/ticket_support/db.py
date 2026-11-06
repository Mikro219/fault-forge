"""SQLite storage. Statements autocommit unless they run inside tx_immediate()."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

BUSY_TIMEOUT_S = 5

_conn: sqlite3.Connection | None = None


def connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = os.environ.get("FF_DB_PATH", "/tmp/ticket-api.db")
        _conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def init(initial_stock: int = 1) -> None:
    conn = connection()
    conn.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, stock INTEGER NOT NULL)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            buyer TEXT NOT NULL,
            payment_id TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            buyer TEXT NOT NULL,
            status TEXT NOT NULL
        )"""
    )
    conn.execute("INSERT OR IGNORE INTO items (id, stock) VALUES (1, ?)", (initial_stock,))


def reset(stock: int) -> None:
    with tx_immediate():
        conn = connection()
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM payments")
        conn.execute("DELETE FROM items")
        conn.execute("INSERT INTO items (id, stock) VALUES (1, ?)", (stock,))


@contextmanager
def tx_immediate() -> Iterator[None]:
    """One atomic write transaction. Never hold it across an await."""
    conn = connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_stock(item_id: int) -> int | None:
    row = connection().execute("SELECT stock FROM items WHERE id = ?", (item_id,)).fetchone()
    return None if row is None else row[0]


def set_stock(item_id: int, stock: int) -> None:
    connection().execute("UPDATE items SET stock = ? WHERE id = ?", (stock, item_id))


def claim_stock(item_id: int) -> int:
    """Atomically takes one unit if any remain. Returns the number of units claimed."""
    cursor = connection().execute(
        "UPDATE items SET stock = stock - 1 WHERE id = ? AND stock > 0", (item_id,)
    )
    return cursor.rowcount


def insert_order(item_id: int, buyer: str, payment_id: str | None) -> int:
    cursor = connection().execute(
        "INSERT INTO orders (item_id, buyer, payment_id) VALUES (?, ?, ?)",
        (item_id, buyer, payment_id),
    )
    return cursor.lastrowid


def state() -> dict[str, int]:
    conn = connection()
    stock = conn.execute("SELECT COALESCE(SUM(stock), 0) FROM items").fetchone()[0]
    orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    # A hold is attached when an order references it.
    unattached = conn.execute(
        """SELECT COUNT(*) FROM payments
           WHERE status IN ('authorized', 'void_pending')
           AND id NOT IN (SELECT payment_id FROM orders WHERE payment_id IS NOT NULL)"""
    ).fetchone()[0]
    unpaid = conn.execute(
        """SELECT COUNT(*) FROM orders
           WHERE payment_id IS NULL
           OR payment_id NOT IN (SELECT id FROM payments WHERE status = 'authorized')"""
    ).fetchone()[0]
    return {
        "stock": stock,
        "orders_confirmed": orders,
        "payments_unattached": unattached,
        "orders_without_payment": unpaid,
    }

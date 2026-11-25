"""Website pickup schema.

Lives beside the billing tables and never alters them.
PostgreSQL: schema `website` in the same database.
SQLite: a separate file, data/website.db, so rinse_rise.db is not written.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from db import DbConnection, is_postgres
from paths import data_dir

WEBSITE_SCHEMA = "website"

POSTGRES_STATEMENTS = [
    "CREATE SCHEMA IF NOT EXISTS website",
    """
    CREATE TABLE IF NOT EXISTS website.stores (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        city TEXT NOT NULL,
        state TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS website.store_pincodes (
        pincode TEXT PRIMARY KEY,
        store_code TEXT NOT NULL REFERENCES website.stores(code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS website.pickup_orders (
        id TEXT PRIMARY KEY,
        store_code TEXT REFERENCES website.stores(code),
        customer_name TEXT NOT NULL DEFAULT '',
        customer_phone TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        address_line TEXT NOT NULL DEFAULT '',
        area TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT '',
        pincode TEXT NOT NULL DEFAULT '',
        pickup_date TEXT NOT NULL DEFAULT '',
        pickup_slot TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        subtotal DOUBLE PRECISION NOT NULL DEFAULT 0,
        discount DOUBLE PRECISION NOT NULL DEFAULT 0,
        total DOUBLE PRECISION NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pickup_scheduled',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS website.pickup_order_items (
        id SERIAL PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES website.pickup_orders(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        quantity DOUBLE PRECISION NOT NULL,
        rate DOUBLE PRECISION NOT NULL,
        line_total DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS pickup_orders_store_idx ON website.pickup_orders (store_code, created_at)",
    "CREATE INDEX IF NOT EXISTS pickup_orders_pincode_idx ON website.pickup_orders (pincode)",
    "CREATE INDEX IF NOT EXISTS pickup_order_items_order_idx ON website.pickup_order_items (order_id)",
]

SQLITE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS stores (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        city TEXT NOT NULL,
        state TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS store_pincodes (
        pincode TEXT PRIMARY KEY,
        store_code TEXT NOT NULL REFERENCES stores(code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pickup_orders (
        id TEXT PRIMARY KEY,
        store_code TEXT REFERENCES stores(code),
        customer_name TEXT NOT NULL DEFAULT '',
        customer_phone TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        address_line TEXT NOT NULL DEFAULT '',
        area TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT '',
        pincode TEXT NOT NULL DEFAULT '',
        pickup_date TEXT NOT NULL DEFAULT '',
        pickup_slot TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        subtotal REAL NOT NULL DEFAULT 0,
        discount REAL NOT NULL DEFAULT 0,
        total REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pickup_scheduled',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pickup_order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL REFERENCES pickup_orders(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        quantity REAL NOT NULL,
        rate REAL NOT NULL,
        line_total REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS pickup_orders_store_idx ON pickup_orders (store_code, created_at)",
    "CREATE INDEX IF NOT EXISTS pickup_orders_pincode_idx ON pickup_orders (pincode)",
    "CREATE INDEX IF NOT EXISTS pickup_order_items_order_idx ON pickup_order_items (order_id)",
]

STORES = (
    ("RR001", "Electronic City", "Bengaluru", "Karnataka", "560100"),
    ("RR002", "Whitefield", "Bengaluru", "Karnataka", "560066"),
    ("RR003", "Hinjewadi", "Pune", "Maharashtra", "411057"),
)


def _count(row: Any) -> int:
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(next(iter(row.values())))
    return int(row[0])


def public_table_counts(conn: DbConnection) -> dict[str, int]:
    """Row counts for the existing billing tables. Read-only."""
    counts: dict[str, int] = {}
    for table in ("settings", "customers", "bills", "bill_items", "expenditures"):
        counts[table] = _count(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone())
    return counts


def ensure_website_schema(conn: DbConnection | None = None) -> None:
    """Create the website schema and store map. Does not update billing rows."""
    if is_postgres():
        if conn is None:
            raise RuntimeError("PostgreSQL website schema needs the billing connection")
        for statement in POSTGRES_STATEMENTS:
            conn.execute(statement)
        for code, name, city, state, pincode in STORES:
            conn.execute(
                """
                INSERT INTO website.stores (code, name, city, state)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (code) DO NOTHING
                """,
                (code, name, city, state),
            )
            conn.execute(
                """
                INSERT INTO website.store_pincodes (pincode, store_code)
                VALUES (?, ?)
                ON CONFLICT (pincode) DO NOTHING
                """,
                (pincode, code),
            )
        from catalogue_search import ensure_catalogue_search_schema

        ensure_catalogue_search_schema(conn)
        return

    path = data_dir() / "website.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path)
    try:
        raw.execute("PRAGMA foreign_keys = ON")
        for statement in SQLITE_STATEMENTS:
            raw.execute(statement)
        for code, name, city, state, pincode in STORES:
            raw.execute(
                """
                INSERT INTO stores (code, name, city, state)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (code) DO NOTHING
                """,
                (code, name, city, state),
            )
            raw.execute(
                """
                INSERT INTO store_pincodes (pincode, store_code)
                VALUES (?, ?)
                ON CONFLICT (pincode) DO NOTHING
                """,
                (pincode, code),
            )
        raw.commit()
    finally:
        raw.close()


def record_website_pickup(
    conn: DbConnection,
    *,
    order_id: str,
    customer_name: str,
    customer_phone: str,
    email: str,
    address_line: str,
    area: str,
    city: str,
    state: str,
    pincode: str,
    pickup_date: str,
    pickup_slot: str,
    notes: str,
    subtotal: float,
    discount: float,
    total: float,
    items: list[dict[str, Any]],
) -> str | None:
    """Insert one website booking into the website schema. Returns the store code."""
    if not is_postgres():
        return None
    pin = "".join(ch for ch in pincode if ch.isdigit())[:6]
    store = conn.execute(
        "SELECT store_code FROM website.store_pincodes WHERE pincode = ?",
        (pin,),
    ).fetchone()
    store_code = None
    if store:
        store_code = store["store_code"] if isinstance(store, dict) else store[0]
    created_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO website.pickup_orders (
            id, store_code, customer_name, customer_phone, email,
            address_line, area, city, state, pincode,
            pickup_date, pickup_slot, notes, subtotal, discount, total, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            order_id,
            store_code,
            customer_name,
            customer_phone,
            email,
            address_line,
            area,
            city,
            state,
            pin,
            pickup_date,
            pickup_slot,
            notes,
            subtotal,
            discount,
            total,
            created_at,
        ),
    )
    for item in items:
        conn.execute(
            """
            INSERT INTO website.pickup_order_items (order_id, name, quantity, rate, line_total)
            SELECT ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM website.pickup_order_items
                WHERE order_id = ? AND name = ? AND quantity = ? AND rate = ?
            )
            """,
            (
                order_id,
                item.get("name") or "",
                item.get("quantity") or 0,
                item.get("rate") or 0,
                item.get("line_total") or 0,
                order_id,
                item.get("name") or "",
                item.get("quantity") or 0,
                item.get("rate") or 0,
            ),
        )
    return store_code

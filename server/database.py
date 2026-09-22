"""Database layer for Rinse & Rise billing (PostgreSQL or SQLite)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from db import (
    DbConnection,
    database_backend_name,
    database_config_status,
    date_gte,
    date_lte,
    get_connection,
    is_postgres,
)

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    phone_key TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    name TEXT,
    profile_created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_favorite INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    customer_name TEXT,
    customer_phone TEXT,
    phone_key TEXT,
    delivery_date TEXT,
    delivery_time TEXT,
    delivery_display TEXT,
    service_mode TEXT NOT NULL DEFAULT 'door-pickup',
    home_service_mode TEXT DEFAULT 'door-pickup',
    shop_service_mode TEXT DEFAULT '',
    payment_type TEXT DEFAULT '',
    payment_info TEXT DEFAULT '',
    subtotal REAL NOT NULL DEFAULT 0,
    discount_percent REAL NOT NULL DEFAULT 0,
    discount_amount REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    sent_via TEXT NOT NULL DEFAULT 'saved',
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    completed_at TEXT,
    FOREIGN KEY (phone_key) REFERENCES customers(phone_key)
);

CREATE TABLE IF NOT EXISTS bill_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL,
    item_key TEXT,
    name TEXT NOT NULL,
    service TEXT NOT NULL,
    category TEXT,
    rate REAL NOT NULL,
    qty REAL NOT NULL DEFAULT 1,
    unit TEXT DEFAULT '',
    starch INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (bill_id) REFERENCES bills(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bills_phone ON bills(phone_key);
CREATE INDEX IF NOT EXISTS idx_bills_status ON bills(delivery_status);
CREATE INDEX IF NOT EXISTS idx_bills_created ON bills(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bill_items_bill ON bill_items(bill_id);

CREATE TABLE IF NOT EXISTS expenditures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_expenditures_created ON expenditures(created_at DESC);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    phone_key TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    name TEXT,
    profile_created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_favorite INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bills (
    id SERIAL PRIMARY KEY,
    bill_no TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    customer_name TEXT,
    customer_phone TEXT,
    phone_key TEXT REFERENCES customers(phone_key),
    delivery_date TEXT,
    delivery_time TEXT,
    delivery_display TEXT,
    service_mode TEXT NOT NULL DEFAULT 'door-pickup',
    home_service_mode TEXT DEFAULT 'door-pickup',
    shop_service_mode TEXT DEFAULT '',
    payment_type TEXT DEFAULT '',
    payment_info TEXT DEFAULT '',
    subtotal DOUBLE PRECISION NOT NULL DEFAULT 0,
    discount_percent DOUBLE PRECISION NOT NULL DEFAULT 0,
    discount_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
    total DOUBLE PRECISION NOT NULL DEFAULT 0,
    sent_via TEXT NOT NULL DEFAULT 'saved',
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS bill_items (
    id SERIAL PRIMARY KEY,
    bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    item_key TEXT,
    name TEXT NOT NULL,
    service TEXT NOT NULL,
    category TEXT,
    rate DOUBLE PRECISION NOT NULL,
    qty DOUBLE PRECISION NOT NULL DEFAULT 1,
    unit TEXT DEFAULT '',
    starch INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bills_phone ON bills(phone_key);
CREATE INDEX IF NOT EXISTS idx_bills_status ON bills(delivery_status);
CREATE INDEX IF NOT EXISTS idx_bills_created ON bills(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bill_items_bill ON bill_items(bill_id);

CREATE TABLE IF NOT EXISTS expenditures (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_expenditures_created ON expenditures(created_at DESC);
"""


def normalize_phone_key(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


_db_ready = False


def ensure_database() -> None:
    """Create or migrate tables once per process — not on every API request."""
    global _db_ready
    if _db_ready:
        return
    init_db()
    _db_ready = True


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(POSTGRES_SCHEMA if is_postgres() else SQLITE_SCHEMA)
        if not conn.execute(
            "SELECT 1 FROM settings WHERE key = ?", ("bill_counter",)
        ).fetchone():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                ("bill_counter", "1"),
            )
        _run_schema_migrations(conn)
        _repair_bill_counter(conn)
        conn.commit()


def _run_schema_migrations(conn: DbConnection) -> None:
    """Add columns that may be missing on older hosted databases."""
    if is_postgres():
        pg_alters = [
            "ALTER TABLE bills ADD COLUMN IF NOT EXISTS service_mode TEXT NOT NULL DEFAULT 'door-pickup'",
            "ALTER TABLE bills ADD COLUMN IF NOT EXISTS home_service_mode TEXT DEFAULT 'door-pickup'",
            "ALTER TABLE bills ADD COLUMN IF NOT EXISTS shop_service_mode TEXT DEFAULT ''",
            "ALTER TABLE bills ADD COLUMN IF NOT EXISTS payment_type TEXT DEFAULT ''",
            "ALTER TABLE bills ADD COLUMN IF NOT EXISTS payment_info TEXT DEFAULT ''",
            "ALTER TABLE bills ADD COLUMN IF NOT EXISTS offer_id TEXT DEFAULT ''",
            "ALTER TABLE bills ADD COLUMN IF NOT EXISTS offer_purpose TEXT DEFAULT ''",
            "ALTER TABLE bills ADD COLUMN IF NOT EXISTS offer_details TEXT DEFAULT ''",
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS is_favorite INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE bill_items ADD COLUMN IF NOT EXISTS unit TEXT DEFAULT ''",
            "ALTER TABLE bill_items ADD COLUMN IF NOT EXISTS starch INTEGER NOT NULL DEFAULT 0",
        ]
        for sql in pg_alters:
            conn.execute(sql)
        return

    cols = conn.table_columns("bills")
    if "service_mode" not in cols:
        conn.execute(
            "ALTER TABLE bills ADD COLUMN service_mode TEXT NOT NULL DEFAULT 'door-pickup'"
        )
    cols = conn.table_columns("bills")
    if "home_service_mode" not in cols:
        conn.execute(
            "ALTER TABLE bills ADD COLUMN home_service_mode TEXT DEFAULT 'door-pickup'"
        )
    if "shop_service_mode" not in cols:
        conn.execute(
            "ALTER TABLE bills ADD COLUMN shop_service_mode TEXT DEFAULT ''"
        )
    cols = conn.table_columns("bills")
    if "payment_type" not in cols:
        conn.execute(
            "ALTER TABLE bills ADD COLUMN payment_type TEXT DEFAULT ''"
        )
    if "payment_info" not in cols:
        conn.execute(
            "ALTER TABLE bills ADD COLUMN payment_info TEXT DEFAULT ''"
        )
    cols = conn.table_columns("bills")
    if "offer_id" not in cols:
        conn.execute("ALTER TABLE bills ADD COLUMN offer_id TEXT DEFAULT ''")
    if "offer_purpose" not in cols:
        conn.execute("ALTER TABLE bills ADD COLUMN offer_purpose TEXT DEFAULT ''")
    if "offer_details" not in cols:
        conn.execute("ALTER TABLE bills ADD COLUMN offer_details TEXT DEFAULT ''")
    customer_cols = conn.table_columns("customers")
    if "is_favorite" not in customer_cols:
        conn.execute(
            "ALTER TABLE customers ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0"
        )
    item_cols = conn.table_columns("bill_items")
    if "unit" not in item_cols:
        conn.execute(
            "ALTER TABLE bill_items ADD COLUMN unit TEXT DEFAULT ''"
        )
    item_cols = conn.table_columns("bill_items")
    if "starch" not in item_cols:
        conn.execute(
            "ALTER TABLE bill_items ADD COLUMN starch INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        """
        UPDATE bills SET home_service_mode = service_mode
        WHERE service_mode IN ('door-pickup', 'door-delivery')
          AND COALESCE(home_service_mode, '') = ''
        """
    )
    conn.execute(
        """
        UPDATE bills SET home_service_mode = 'door-pickup'
        WHERE service_mode = 'door-both'
          AND COALESCE(home_service_mode, '') = ''
        """
    )
    conn.execute(
        """
        UPDATE bills SET shop_service_mode = service_mode
        WHERE service_mode IN ('shop-pickup', 'shop-delivery')
          AND COALESCE(shop_service_mode, '') = ''
        """
    )
    conn.execute(
        """
        UPDATE bills SET shop_service_mode = 'shop-pickup'
        WHERE service_mode = 'shop-both'
          AND COALESCE(shop_service_mode, '') = ''
        """
    )
    conn.execute(
        """
        UPDATE bills SET home_service_mode = 'door-pickup'
        WHERE COALESCE(home_service_mode, '') = ''
          AND COALESCE(shop_service_mode, '') = ''
          AND service_mode NOT LIKE 'shop%'
        """
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_setting(key: str, default: str = "") -> str:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def get_bill_counter() -> int:
    with get_connection() as conn:
        return _repair_bill_counter(conn)


def set_bill_counter(value: int) -> None:
    set_setting("bill_counter", str(value))


def _infer_item_unit(item: dict[str, Any]) -> str:
    unit = (item.get("unit") or "").strip().lower()
    if unit in ("kg", "pc"):
        return unit
    name = (item.get("name") or "").lower()
    if "/kg" in name or name in {
        "wash and fold 80/kg",
        "wash and iron 125/kg",
        "premium laundry 200/kg",
    }:
        return "kg"
    return "pc"


def _row_keys(row: Any) -> set[str]:
    if hasattr(row, "keys"):
        return set(row.keys())
    return set()


def _save_bill_item(conn: DbConnection, bill_id: int, item: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO bill_items (bill_id, item_key, name, service, category, rate, qty, unit, starch)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bill_id,
            item.get("key", ""),
            item.get("name", ""),
            item.get("service", ""),
            item.get("category", "") or "",
            item.get("rate", 0),
            float(item.get("qty", 1) or 1),
            item.get("unit") or _infer_item_unit(item),
            _starch_qty_for_save(item),
        ),
    )


def _starch_qty_for_save(item: dict[str, Any]) -> int:
    if item.get("starchQty") is not None:
        return max(0, int(item.get("starchQty") or 0))
    if item.get("starch") is True:
        return max(0, int(float(item.get("qty") or 0)))
    if isinstance(item.get("starch"), (int, float)) and item.get("starch"):
        return max(0, int(item.get("starch")))
    return 0


_bill_item_columns_cache: dict[str, set[str]] = {}


def _bill_item_columns(conn: DbConnection) -> set[str]:
    cache_key = "pg" if conn._is_pg else "sqlite"
    cached = _bill_item_columns_cache.get(cache_key)
    if cached is not None:
        return cached
    cols = conn.table_columns("bill_items")
    _bill_item_columns_cache[cache_key] = cols
    return cols


def _rows_to_items(rows: list[Any], *, has_unit: bool, has_starch: bool) -> list[dict[str, Any]]:
    items = []
    for r in rows:
        keys = _row_keys(r)
        item = {
            "key": r["item_key"] or "",
            "name": r["name"],
            "service": r["service"],
            "category": r["category"] or "",
            "rate": r["rate"],
            "qty": float(r["qty"] or 0),
            "starchQty": int(r["starch"] or 0) if has_starch and "starch" in keys else 0,
        }
        item["unit"] = _infer_item_unit(
            {**item, "unit": (r["unit"] if has_unit and "unit" in keys else "") or ""}
        )
        items.append(item)
    return items


def _fetch_items(conn: DbConnection, bill_id: int) -> list[dict[str, Any]]:
    item_cols = _bill_item_columns(conn)
    has_unit = "unit" in item_cols
    has_starch = "starch" in item_cols
    select_cols = "item_key, name, service, category, rate, qty"
    if has_unit:
        select_cols += ", unit"
    if has_starch:
        select_cols += ", starch"
    rows = conn.execute(
        f"""
        SELECT {select_cols}
        FROM bill_items WHERE bill_id = ? ORDER BY id
        """,
        (bill_id,),
    ).fetchall()
    return _rows_to_items(rows, has_unit=has_unit, has_starch=has_starch)


def _fetch_items_batch(conn: DbConnection, bill_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not bill_ids:
        return {}
    item_cols = _bill_item_columns(conn)
    has_unit = "unit" in item_cols
    has_starch = "starch" in item_cols
    select_cols = "bill_id, item_key, name, service, category, rate, qty"
    if has_unit:
        select_cols += ", unit"
    if has_starch:
        select_cols += ", starch"
    placeholders = ", ".join("?" for _ in bill_ids)
    rows = conn.execute(
        f"""
        SELECT {select_cols}
        FROM bill_items
        WHERE bill_id IN ({placeholders})
        ORDER BY bill_id, id
        """,
        tuple(bill_ids),
    ).fetchall()
    buckets: dict[int, list[Any]] = {bill_id: [] for bill_id in bill_ids}
    for row in rows:
        buckets[int(row["bill_id"])].append(row)
    return {
        bill_id: _rows_to_items(buckets[bill_id], has_unit=has_unit, has_starch=has_starch)
        for bill_id in bill_ids
    }


def _bill_service_modes_from_row(row: Any) -> tuple[str, str]:
    keys = _row_keys(row)
    home = (row["home_service_mode"] if "home_service_mode" in keys else "") or ""
    shop = (row["shop_service_mode"] if "shop_service_mode" in keys else "") or ""

    if not home and not shop and "service_mode" in keys:
        legacy = row["service_mode"] or ""
        if legacy in ("door-pickup", "door-delivery", "door-both"):
            home = legacy
        elif legacy == "door-both":
            home = "door-pickup"
        elif legacy in ("shop-pickup", "shop-delivery", "shop-both"):
            shop = legacy
        elif legacy.startswith("door"):
            home = "door-pickup"

    if shop == "shop-both":
        home = ""
    elif not home and not shop:
        home = "door-pickup"
    return home, shop


def row_to_bill(row: Any, items: list[dict[str, Any]]) -> dict[str, Any]:
    home_mode, shop_mode = _bill_service_modes_from_row(row)
    keys = _row_keys(row)
    return {
        "id": row["id"],
        "billNo": row["bill_no"],
        "createdAt": row["created_at"],
        "customerName": row["customer_name"] or "",
        "customerPhone": row["customer_phone"] or "",
        "deliveryDate": row["delivery_date"] or "",
        "deliveryTime": row["delivery_time"] or "",
        "deliveryDisplay": row["delivery_display"] or "",
        "paymentType": (row["payment_type"] if "payment_type" in keys else "") or "",
        "paymentInfo": (row["payment_info"] if "payment_info" in keys else "") or "",
        "serviceMode": home_mode,
        "homeServiceMode": home_mode,
        "shopServiceMode": shop_mode,
        "subtotal": row["subtotal"],
        "discountPercent": row["discount_percent"],
        "discountAmount": row["discount_amount"],
        "total": row["total"],
        "offerId": (row["offer_id"] if "offer_id" in keys else "") or "",
        "offerPurpose": (row["offer_purpose"] if "offer_purpose" in keys else "") or "",
        "offerDetails": (row["offer_details"] if "offer_details" in keys else "") or "",
        "sentVia": row["sent_via"],
        "deliveryStatus": row["delivery_status"],
        "completedAt": row["completed_at"],
        "items": items,
    }


def get_all_bills() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM bills ORDER BY created_at DESC, id DESC"
        ).fetchall()
        if not rows:
            return []
        bill_ids = [int(r["id"]) for r in rows]
        items_by_bill = _fetch_items_batch(conn, bill_ids)
        return [row_to_bill(r, items_by_bill.get(int(r["id"]), [])) for r in rows]


def get_bill_by_id(bill_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if not row:
            return None
        return row_to_bill(row, _fetch_items(conn, bill_id))


def get_bills_by_phone(phone_key: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM bills
            WHERE phone_key = ?
            ORDER BY created_at DESC, id DESC
            """,
            (phone_key,),
        ).fetchall()
        return [row_to_bill(r, _fetch_items(conn, r["id"])) for r in rows]


T = TypeVar("T")


def _is_unique_violation(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "unique constraint" in msg or "duplicate key" in msg or "bills_bill_no_key" in msg


def _existing_bill_nos(conn: DbConnection) -> set[str]:
    return {
        str(row["bill_no"] or "").strip()
        for row in conn.execute("SELECT bill_no FROM bills").fetchall()
    }


def _shop_sequence_next(existing: set[str]) -> int:
    numbers = [int(n) for n in existing if n.isdigit() and 1 <= int(n) < 1000]
    return (max(numbers) + 1) if numbers else 1


def _repair_bill_counter(conn: DbConnection) -> int:
    """Keep counter on the next free shop bill number (skip 10000+ test numbers)."""
    next_no = _shop_sequence_next(_existing_bill_nos(conn))
    row = conn.execute("SELECT value FROM settings WHERE key = ?", ("bill_counter",)).fetchone()
    stored = int(row["value"]) if row else 0
    if stored != next_no:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (str(next_no), "bill_counter"),
        )
    return next_no


def _allocate_bill_no(conn: DbConnection) -> str:
    _read_bill_counter(conn)  # lock settings row
    existing = _existing_bill_nos(conn)
    n = _shop_sequence_next(existing)
    for _ in range(500):
        bill_no = str(n).zfill(4)
        if bill_no not in existing and str(n) not in existing:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = ?",
                (str(n + 1), "bill_counter"),
            )
            return bill_no
        n += 1
    raise RuntimeError("Could not allocate a new bill number.")


def _retry_on_deadlock(func: Callable[[], T], *, attempts: int = 4) -> T:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return func()
        except Exception as exc:
            last_error = exc
            retryable = "deadlock detected" in str(exc).lower() or _is_unique_violation(exc)
            if is_postgres() and retryable and attempt < attempts - 1:
                time.sleep(0.05 * (2**attempt))
                continue
            raise
    raise last_error or RuntimeError("create_bill failed")


def _read_bill_counter(conn: DbConnection) -> int:
    sql = "SELECT value FROM settings WHERE key = ?"
    if is_postgres():
        sql += " FOR UPDATE"
    row = conn.execute(sql, ("bill_counter",)).fetchone()
    if row:
        return int(row["value"])
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("bill_counter", "1"),
    )
    return 1


def upsert_customer(phone: str, name: str, conn: DbConnection) -> str:
    phone_key = normalize_phone_key(phone)
    if len(phone_key) < 10:
        return phone_key

    now = utc_now_iso()
    if is_postgres():
        conn.execute(
            """
            INSERT INTO customers (phone_key, phone, name, profile_created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (phone_key) DO UPDATE SET
                phone = EXCLUDED.phone,
                name = COALESCE(NULLIF(EXCLUDED.name, ''), customers.name),
                updated_at = EXCLUDED.updated_at
            """,
            (phone_key, phone, name, now, now),
        )
        return phone_key

    existing = conn.execute(
        "SELECT phone_key FROM customers WHERE phone_key = ?", (phone_key,)
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE customers
            SET phone = ?, name = COALESCE(NULLIF(?, ''), name), updated_at = ?
            WHERE phone_key = ?
            """,
            (phone, name, now, phone_key),
        )
    else:
        conn.execute(
            """
            INSERT INTO customers (phone_key, phone, name, profile_created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (phone_key, phone, name, now, now),
        )
    return phone_key


def mark_bill_sent_via(bill_id: int, sent_via: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE bills SET sent_via = ? WHERE id = ?",
            (sent_via, bill_id),
        )
        conn.commit()


def create_bill(payload: dict[str, Any], *, honor_bill_no: bool = False) -> dict[str, Any]:
    def _create() -> dict[str, Any]:
        now = payload.get("createdAt") or utc_now_iso()
        phone = payload.get("customerPhone", "")
        name = payload.get("customerName", "")

        with get_connection() as conn:
            requested = str(payload.get("billNo") or "").strip()
            if honor_bill_no and requested:
                exists = conn.execute(
                    "SELECT 1 FROM bills WHERE bill_no = ?", (requested,)
                ).fetchone()
                bill_no = requested if not exists else _allocate_bill_no(conn)
            else:
                bill_no = _allocate_bill_no(conn)
            phone_key = upsert_customer(phone, name, conn)

            bill_id = conn.insert_returning_id(
                """
                INSERT INTO bills (
                    bill_no, created_at, customer_name, customer_phone, phone_key,
                    delivery_date, delivery_time, delivery_display, service_mode,
                    home_service_mode, shop_service_mode,
                    payment_type, payment_info,
                    subtotal, discount_percent, discount_amount, total,
                    offer_id, offer_purpose, offer_details,
                    sent_via, delivery_status, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bill_no,
                    now,
                    name,
                    phone,
                    phone_key if len(phone_key) >= 10 else None,
                    payload.get("deliveryDate", ""),
                    payload.get("deliveryTime", ""),
                    payload.get("deliveryDisplay", ""),
                    payload.get("homeServiceMode", payload.get("serviceMode", "door-pickup")),
                    payload.get("homeServiceMode", payload.get("serviceMode", "door-pickup")),
                    payload.get("shopServiceMode", ""),
                    payload.get("paymentType", ""),
                    payload.get("paymentInfo", ""),
                    payload.get("subtotal", 0),
                    payload.get("discountPercent", 0),
                    payload.get("discountAmount", 0),
                    payload.get("total", 0),
                    payload.get("offerId", ""),
                    payload.get("offerPurpose", ""),
                    payload.get("offerDetails", ""),
                    payload.get("sentVia", "saved"),
                    payload.get("deliveryStatus", "pending"),
                    payload.get("completedAt"),
                ),
            )

            for item in payload.get("items", []):
                _save_bill_item(conn, bill_id, item)

            conn.commit()

            row = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
            return row_to_bill(row, _fetch_items(conn, bill_id))

    return _retry_on_deadlock(_create)


def update_bill_status(bill_id: int, status: str) -> dict[str, Any] | None:
    completed_at = utc_now_iso() if status == "done" else None
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE bills
            SET delivery_status = ?, completed_at = ?
            WHERE id = ?
            """,
            (status, completed_at, bill_id),
        )
        conn.commit()
    return get_bill_by_id(bill_id)


def update_bill(bill_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    existing = get_bill_by_id(bill_id)
    if not existing:
        return None

    items = payload.get("items") or []
    if not items:
        raise ValueError("Bill must have at least one item")

    phone = payload.get("customerPhone", existing["customerPhone"])
    name = payload.get("customerName", existing["customerName"])

    with get_connection() as conn:
        phone_key = upsert_customer(phone, name, conn)

        conn.execute(
            """
            UPDATE bills SET
                customer_name = ?,
                customer_phone = ?,
                phone_key = ?,
                delivery_date = ?,
                delivery_time = ?,
                delivery_display = ?,
                service_mode = ?,
                home_service_mode = ?,
                shop_service_mode = ?,
                payment_type = ?,
                payment_info = ?,
                subtotal = ?,
                discount_percent = ?,
                discount_amount = ?,
                total = ?,
                offer_id = ?,
                offer_purpose = ?,
                offer_details = ?
            WHERE id = ?
            """,
            (
                name,
                phone,
                phone_key if len(phone_key) >= 10 else None,
                payload.get("deliveryDate", existing["deliveryDate"]),
                payload.get("deliveryTime", existing["deliveryTime"]),
                payload.get("deliveryDisplay", existing["deliveryDisplay"]),
                payload.get("homeServiceMode", existing.get("homeServiceMode", "door-pickup")),
                payload.get("homeServiceMode", existing.get("homeServiceMode", "door-pickup")),
                payload.get("shopServiceMode", existing.get("shopServiceMode", "")),
                payload.get("paymentType", existing.get("paymentType", "")),
                payload.get("paymentInfo", existing.get("paymentInfo", "")),
                payload.get("subtotal", existing["subtotal"]),
                payload.get("discountPercent", existing["discountPercent"]),
                payload.get("discountAmount", existing["discountAmount"]),
                payload.get("total", existing["total"]),
                payload.get("offerId", existing.get("offerId", "")),
                payload.get("offerPurpose", existing.get("offerPurpose", "")),
                payload.get("offerDetails", existing.get("offerDetails", "")),
                bill_id,
            ),
        )

        conn.execute("DELETE FROM bill_items WHERE bill_id = ?", (bill_id,))
        for item in items:
            _save_bill_item(conn, bill_id, item)

        conn.commit()

    return get_bill_by_id(bill_id)


def build_customer_profile(phone_key: str) -> dict[str, Any] | None:
    if len(phone_key) < 10:
        return None

    bills = get_bills_by_phone(phone_key)
    if not bills:
        return None

    customer_row = None
    with get_connection() as conn:
        customer_row = conn.execute(
            "SELECT * FROM customers WHERE phone_key = ?", (phone_key,)
        ).fetchone()

    total_spent = sum(b["total"] for b in bills)
    pending = sum(1 for b in bills if b["deliveryStatus"] != "done")
    done = sum(1 for b in bills if b["deliveryStatus"] == "done")
    total_items = sum(
        sum(i["qty"] for i in b.get("items", [])) for b in bills
    )

    sorted_bills = sorted(bills, key=lambda b: b["createdAt"])
    first_order = sorted_bills[0]["createdAt"]
    last_order = sorted_bills[-1]["createdAt"]

    return {
        "phoneKey": phone_key,
        "phone": customer_row["phone"] if customer_row else bills[0]["customerPhone"],
        "name": (customer_row["name"] if customer_row else bills[0]["customerName"])
        or "Customer",
        "isFavorite": bool(customer_row["is_favorite"]) if customer_row else False,
        "profileCreatedAt": customer_row["profile_created_at"]
        if customer_row
        else first_order,
        "updatedAt": customer_row["updated_at"] if customer_row else last_order,
        "totalOrders": len(bills),
        "totalSpent": total_spent,
        "pendingCount": pending,
        "doneCount": done,
        "avgOrder": round(total_spent / len(bills)),
        "totalItems": total_items,
        "firstOrderAt": first_order,
        "lastOrderAt": last_order,
    }


def get_customer_favorite(phone_key: str) -> bool:
    if len(phone_key) < 10:
        return False
    with get_connection() as conn:
        row = conn.execute(
            "SELECT is_favorite FROM customers WHERE phone_key = ?", (phone_key,)
        ).fetchone()
        return bool(row and row["is_favorite"])


def set_customer_favorite(
    phone_key: str, phone: str, name: str, favorite: bool
) -> dict[str, Any]:
    if len(phone_key) < 10:
        raise ValueError("Valid 10-digit phone number is required")
    with get_connection() as conn:
        upsert_customer(phone, name, conn)
        conn.execute(
            """
            UPDATE customers
            SET is_favorite = ?, updated_at = ?
            WHERE phone_key = ?
            """,
            (1 if favorite else 0, utc_now_iso(), phone_key),
        )
        conn.commit()
    return {"phoneKey": phone_key, "isFavorite": favorite}


def row_to_expenditure(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "amount": row["amount"],
        "createdAt": row["created_at"],
    }


def get_all_expenditures(
    date_from: str | None = None, date_to: str | None = None
) -> list[dict[str, Any]]:
    query = "SELECT * FROM expenditures WHERE 1=1"
    params: list[str] = []
    if date_from:
        query += f" AND {date_gte('created_at')}"
        params.append(date_from)
    if date_to:
        query += f" AND {date_lte('created_at')}"
        params.append(date_to)
    query += " ORDER BY created_at DESC, id DESC"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [row_to_expenditure(r) for r in rows]


def create_expenditure(
    name: str, amount: float, spent_date: str | None = None
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("Expenditure name is required")
    if amount <= 0:
        raise ValueError("Amount must be greater than zero")

    if spent_date and len(spent_date.strip()) >= 10:
        day = spent_date.strip()[:10]
        now = datetime.now(timezone.utc)
        created_at = f"{day}T{now.strftime('%H:%M:%S')}+00:00"
    else:
        created_at = utc_now_iso()

    with get_connection() as conn:
        exp_id = conn.insert_returning_id(
            "INSERT INTO expenditures (name, amount, created_at) VALUES (?, ?, ?)",
            (name, amount, created_at),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM expenditures WHERE id = ?", (exp_id,)
        ).fetchone()
        return row_to_expenditure(row)


def delete_expenditure(expenditure_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM expenditures WHERE id = ?", (expenditure_id,)
        )
        conn.commit()
        return cur.rowcount > 0


def get_profit_loss_summary(
    date_from: str | None = None, date_to: str | None = None
) -> dict[str, Any]:
    bill_query = "SELECT * FROM bills WHERE 1=1"
    bill_params: list[str] = []
    if date_from:
        bill_query += f" AND {date_gte('created_at')}"
        bill_params.append(date_from)
    if date_to:
        bill_query += f" AND {date_lte('created_at')}"
        bill_params.append(date_to)
    bill_query += " ORDER BY created_at DESC, id DESC"

    with get_connection() as conn:
        bill_rows = conn.execute(bill_query, bill_params).fetchall()
        bills = [row_to_bill(r, _fetch_items(conn, r["id"])) for r in bill_rows]

    expenditures = get_all_expenditures(date_from, date_to)

    service_income: dict[str, float] = {}
    for bill in bills:
        for item in bill.get("items", []):
            svc = item.get("service") or "Other"
            amount = (item.get("rate") or 0) * (item.get("qty") or 0)
            service_income[svc] = service_income.get(svc, 0) + amount

    total_revenue = sum(b["total"] for b in bills)
    total_expenditure = sum(e["amount"] for e in expenditures)
    profit_loss = total_revenue - total_expenditure

    return {
        "totalRevenue": total_revenue,
        "orderCount": len(bills),
        "totalExpenditure": total_expenditure,
        "expenditureCount": len(expenditures),
        "profitLoss": profit_loss,
        "isProfit": profit_loss >= 0,
        "expenditureItems": expenditures,
        "serviceIncome": [
            {"name": name, "amount": amount}
            for name, amount in sorted(
                service_income.items(), key=lambda x: x[1], reverse=True
            )
        ],
        "dateFrom": date_from or "",
        "dateTo": date_to or "",
    }


def get_overall_stats() -> dict[str, Any]:
    bills = get_all_bills()

    total_revenue = 0.0
    total_discount = 0.0
    total_items = 0
    pending_orders = 0
    done_orders = 0
    service_revenue: dict[str, float] = {}
    service_items: dict[str, int] = {}
    sent_via_map = {"saved": 0, "print": 0, "whatsapp": 0}

    for bill in bills:
        total_revenue += bill["total"]
        total_discount += bill.get("discountAmount") or 0

        if bill.get("deliveryStatus") == "done":
            done_orders += 1
        else:
            pending_orders += 1

        via = bill.get("sentVia") or "print"
        if via not in sent_via_map:
            via = "print"
        sent_via_map[via] += 1

        for item in bill.get("items", []):
            qty = item.get("qty") or 0
            total_items += qty
            svc = item.get("service") or "Other"
            amount = (item.get("rate") or 0) * qty
            service_revenue[svc] = service_revenue.get(svc, 0) + amount
            service_items[svc] = service_items.get(svc, 0) + qty

    order_count = len(bills)
    avg_order = round(total_revenue / order_count) if order_count else 0

    return {
        "totalOrders": order_count,
        "totalRevenue": total_revenue,
        "totalDiscount": total_discount,
        "totalItems": total_items,
        "pendingOrders": pending_orders,
        "doneOrders": done_orders,
        "avgOrderValue": avg_order,
        "serviceRevenue": [
            {"name": name, "amount": amount}
            for name, amount in sorted(
                service_revenue.items(), key=lambda x: x[1], reverse=True
            )
        ],
        "serviceItems": [
            {"name": name, "count": count}
            for name, count in sorted(
                service_items.items(), key=lambda x: x[1], reverse=True
            )
        ],
        "sentVia": sent_via_map,
    }


def migrate_from_local(payload: dict[str, Any]) -> dict[str, int]:
    imported = 0
    bills = payload.get("bills", [])
    counter = int(payload.get("billCounter") or get_bill_counter())

    existing = {b["billNo"] for b in get_all_bills()}

    for bill in bills:
        bill_no = bill.get("billNo") or bill.get("bill_no")
        if not bill_no or bill_no in existing:
            continue

        create_bill(
            {
                "billNo": bill_no,
                "createdAt": bill.get("createdAt") or bill.get("created_at"),
                "customerName": bill.get("customerName", ""),
                "customerPhone": bill.get("customerPhone", ""),
                "deliveryDate": bill.get("deliveryDate", ""),
                "deliveryTime": bill.get("deliveryTime", ""),
                "deliveryDisplay": bill.get("deliveryDisplay", ""),
                "paymentType": bill.get("paymentType", ""),
                "paymentInfo": bill.get("paymentInfo", ""),
                "homeServiceMode": bill.get("homeServiceMode", bill.get("serviceMode", "door-pickup")),
                "shopServiceMode": bill.get("shopServiceMode", ""),
                "serviceMode": bill.get("homeServiceMode", bill.get("serviceMode", "door-pickup")),
                "subtotal": bill.get("subtotal", 0),
                "discountPercent": bill.get("discountPercent", 0),
                "discountAmount": bill.get("discountAmount", 0),
                "total": bill.get("total", 0),
                "offerId": bill.get("offerId", ""),
                "offerPurpose": bill.get("offerPurpose", ""),
                "offerDetails": bill.get("offerDetails", ""),
                "sentVia": bill.get("sentVia", "saved"),
                "deliveryStatus": bill.get("deliveryStatus", "pending"),
                "completedAt": bill.get("completedAt"),
                "items": bill.get("items", []),
            },
            honor_bill_no=True,
        )
        existing.add(bill_no)
        imported += 1

    if 1 <= counter < 1000 and counter > get_bill_counter():
        set_bill_counter(counter)

    return {"imported": imported, "billCounter": get_bill_counter()}


def list_unique_customer_phones() -> list[dict[str, str]]:
    """Unique 10-digit customer numbers from billing (no duplicates)."""
    with get_connection() as conn:
        customer_rows = conn.execute(
            """
            SELECT phone_key, name, phone FROM customers
            WHERE phone_key IS NOT NULL AND length(phone_key) >= 10
            """
        ).fetchall()
        bill_rows = conn.execute(
            """
            SELECT phone_key, customer_name AS name, customer_phone AS phone FROM bills
            WHERE phone_key IS NOT NULL AND length(phone_key) >= 10
            """
        ).fetchall()

    by_key: dict[str, dict[str, str]] = {}
    for row in list(customer_rows) + list(bill_rows):
        phone_key = normalize_phone_key(str(row["phone_key"] or row["phone"] or ""))
        if len(phone_key) < 10:
            continue
        existing = by_key.get(phone_key)
        name = str(row["name"] or "").strip()
        phone = str(row["phone"] or phone_key).strip()
        if not existing:
            by_key[phone_key] = {"phoneKey": phone_key, "phone": phone, "name": name}
        elif not existing.get("name") and name:
            existing["name"] = name

    return sorted(by_key.values(), key=lambda item: item["phoneKey"])


def clear_all_data() -> dict[str, int]:
    """Remove all bills, customers, expenditures, and generated invoices."""
    from paths import invoice_dir

    with get_connection() as conn:
        if is_postgres():
            conn.execute(
                "TRUNCATE bill_items, bills, customers, expenditures RESTART IDENTITY CASCADE"
            )
        else:
            conn.execute("DELETE FROM bill_items")
            conn.execute("DELETE FROM bills")
            conn.execute("DELETE FROM customers")
            conn.execute("DELETE FROM expenditures")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('bills', 'bill_items', 'expenditures')")
        row = conn.execute(
            "SELECT 1 FROM settings WHERE key = ?", ("bill_counter",)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = ?",
                ("1", "bill_counter"),
            )
        else:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                ("bill_counter", "1"),
            )
        conn.commit()

    removed_invoices = 0
    inv_dir = invoice_dir()
    if inv_dir.is_dir():
        for path in inv_dir.glob("*.pdf"):
            try:
                path.unlink()
                removed_invoices += 1
            except OSError:
                pass

    return {
        "billsDeleted": True,
        "billCounterReset": 1,
        "invoicesRemoved": removed_invoices,
    }
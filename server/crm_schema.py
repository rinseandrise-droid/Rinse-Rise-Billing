"""CRM schema in the same Railway Postgres as billing.

Uses schema `crm` only — never alters public billing tables.
Role-based visibility is enforced in the CRM app (scope/permissions).
"""

from __future__ import annotations

from db import DbConnection, is_postgres

CRM_SCHEMA = "crm"

CRM_STATEMENTS = [
    "CREATE SCHEMA IF NOT EXISTS crm",
    """
    CREATE TABLE IF NOT EXISTS crm.stores (
        id TEXT PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        city TEXT NOT NULL,
        state TEXT NOT NULL,
        address TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        owner_name TEXT NOT NULL DEFAULT '',
        opened_at TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS crm.staff (
        id TEXT PRIMARY KEY,
        login_code TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        store_id TEXT REFERENCES crm.stores(id) ON DELETE SET NULL,
        state TEXT,
        phone TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS crm.bill_counters (
        store_code TEXT PRIMARY KEY,
        last_seq INTEGER NOT NULL DEFAULT 1000
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS crm.bills (
        id TEXT PRIMARY KEY,
        bill_no TEXT NOT NULL,
        store_id TEXT NOT NULL REFERENCES crm.stores(id),
        staff_id TEXT NOT NULL REFERENCES crm.staff(id),
        customer_name TEXT NOT NULL,
        customer_phone TEXT NOT NULL,
        payment_type TEXT NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        subtotal DOUBLE PRECISION NOT NULL DEFAULT 0,
        discount DOUBLE PRECISION NOT NULL DEFAULT 0,
        total DOUBLE PRECISION NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS crm.bill_items (
        id TEXT PRIMARY KEY,
        bill_id TEXT NOT NULL REFERENCES crm.bills(id) ON DELETE CASCADE,
        service_id TEXT NOT NULL,
        service_name TEXT NOT NULL,
        group_name TEXT NOT NULL,
        name TEXT NOT NULL,
        rate DOUBLE PRECISION NOT NULL,
        qty DOUBLE PRECISION NOT NULL,
        unit TEXT NOT NULL,
        line_total DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS crm_staff_login_idx ON crm.staff (login_code)",
    "CREATE INDEX IF NOT EXISTS crm_staff_role_idx ON crm.staff (role)",
    "CREATE INDEX IF NOT EXISTS crm_staff_store_idx ON crm.staff (store_id)",
    "CREATE INDEX IF NOT EXISTS crm_staff_state_idx ON crm.staff (state)",
    "CREATE INDEX IF NOT EXISTS crm_bills_store_idx ON crm.bills (store_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS crm_bills_status_idx ON crm.bills (status)",
    "CREATE INDEX IF NOT EXISTS crm_bill_items_bill_idx ON crm.bill_items (bill_id)",
]


def ensure_crm_schema(conn: DbConnection) -> None:
    if not is_postgres():
        return
    for statement in CRM_STATEMENTS:
        conn.execute(statement)


def crm_table_counts(conn: DbConnection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("stores", "staff", "bills", "bill_items"):
        row = conn.execute(f"SELECT COUNT(*) AS c FROM crm.{table}").fetchone()
        if isinstance(row, dict):
            counts[table] = int(row["c"])
        else:
            counts[table] = int(row[0])
    return counts
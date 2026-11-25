"""Create schema `crm` on Railway Postgres. Does not seed — run rinse-rise-crm db:seed after."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crm_schema import crm_table_counts, ensure_crm_schema  # noqa: E402
from db import database_backend_name, get_connection, is_postgres  # noqa: E402
from website_schema import public_table_counts  # noqa: E402


def main() -> int:
    print("backend:", database_backend_name())
    if not is_postgres():
        print("ERROR: PostgreSQL is required")
        return 1
    with get_connection() as conn:
        before_billing = public_table_counts(conn)
        ensure_crm_schema(conn)
        crm = crm_table_counts(conn)
        after_billing = public_table_counts(conn)
    print("billing before:", before_billing)
    print("billing after:", after_billing)
    print("crm:", crm)
    if before_billing != after_billing:
        print("WARNING: billing row counts changed")
        return 2
    print("OK — run CRM seed: cd rinse-rise-crm && npm run db:seed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
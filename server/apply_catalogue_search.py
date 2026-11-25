"""One-off: create website catalogue/search tables and seed from the Expo rate card."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from catalogue_search import ensure_catalogue_search_schema, seed_catalogue_from_rate_card  # noqa: E402
from db import database_backend_name, get_connection, is_postgres  # noqa: E402
from website_schema import ensure_website_schema, public_table_counts  # noqa: E402


def main() -> int:
    print("backend:", database_backend_name())
    if not is_postgres():
        print("ERROR: PostgreSQL is required")
        return 1
    with get_connection() as conn:
        before = public_table_counts(conn)
        ensure_website_schema(conn)
        ensure_catalogue_search_schema(conn)
        counts = seed_catalogue_from_rate_card(conn)
        after = public_table_counts(conn)
        item_count = conn.execute("SELECT COUNT(*) AS c FROM website.catalogue_items").fetchone()
        kw_count = conn.execute("SELECT COUNT(*) AS c FROM website.search_keywords").fetchone()
    print("billing counts before:", before)
    print("billing counts after:", after)
    print("seed:", counts)
    print("catalogue_items:", item_count["c"] if isinstance(item_count, dict) else item_count[0])
    print("search_keywords:", kw_count["c"] if isinstance(kw_count, dict) else kw_count[0])
    if before != after:
        print("WARNING: billing row counts changed")
        return 2
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

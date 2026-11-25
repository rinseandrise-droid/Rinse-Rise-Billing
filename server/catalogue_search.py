"""Rate-card catalogue + search keywords in schema `website`.

Does not touch public billing tables. Used by the customer app search API.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from db import DbConnection, get_connection, is_postgres

ROOT = Path(__file__).resolve().parents[1]
APP_RATE_CARD = ROOT.parent / "rinse-rise-app" / "src" / "data" / "rate-card.json"

CATALOGUE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS website.catalogue_items (
        id TEXT PRIMARY KEY,
        service_id TEXT NOT NULL,
        service_name TEXT NOT NULL DEFAULT '',
        group_id TEXT NOT NULL DEFAULT '',
        group_name TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        normalized_name TEXT NOT NULL DEFAULT '',
        rate DOUBLE PRECISION NOT NULL DEFAULT 0,
        unit TEXT NOT NULL DEFAULT 'piece',
        rate_text TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS website.search_keywords (
        id SERIAL PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES website.catalogue_items(id) ON DELETE CASCADE,
        keyword TEXT NOT NULL,
        normalized_keyword TEXT NOT NULL,
        keyword_type TEXT NOT NULL DEFAULT 'synonym',
        relevance_weight INTEGER NOT NULL DEFAULT 80,
        language TEXT NOT NULL DEFAULT 'en',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS website.search_events (
        id SERIAL PRIMARY KEY,
        query TEXT NOT NULL,
        normalized_query TEXT NOT NULL DEFAULT '',
        service_id TEXT NOT NULL DEFAULT '',
        result_count INTEGER NOT NULL DEFAULT 0,
        selected_item_id TEXT NOT NULL DEFAULT '',
        match_kind TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS catalogue_items_service_idx ON website.catalogue_items (service_id, group_id)",
    "CREATE INDEX IF NOT EXISTS catalogue_items_norm_idx ON website.catalogue_items (normalized_name)",
    "CREATE INDEX IF NOT EXISTS search_keywords_norm_idx ON website.search_keywords (normalized_keyword)",
    "CREATE INDEX IF NOT EXISTS search_keywords_item_idx ON website.search_keywords (item_id)",
    "CREATE INDEX IF NOT EXISTS search_events_query_idx ON website.search_events (normalized_query, created_at)",
]

# keyword -> (type, weight, name needles). Mirrors the Expo search catalogue.
KEYWORD_SEED: list[tuple[str, str, int, tuple[str, ...]]] = [
    ("rug", "synonym", 100, ("carpet",)),
    ("rugs", "synonym", 100, ("carpet",)),
    ("area rug", "synonym", 90, ("carpet",)),
    ("floor covering", "synonym", 80, ("carpet",)),
    ("bedsheet", "synonym", 100, ("bed sheet",)),
    ("bedsheets", "synonym", 100, ("bed sheet",)),
    ("bed linen", "synonym", 90, ("bed sheet", "pillow cover", "quilt", "blanket")),
    ("pillowcase", "synonym", 100, ("pillow cover",)),
    ("pillow case", "synonym", 100, ("pillow cover",)),
    ("duvet", "synonym", 95, ("quilt",)),
    ("duvet cover", "synonym", 100, ("quilt cover",)),
    ("comforter", "related", 70, ("quilt", "blanket")),
    ("trousers", "synonym", 100, ("pant", "pants", "slacks")),
    ("trouser", "synonym", 100, ("pant", "pants", "slacks")),
    ("denim", "synonym", 100, ("jeans",)),
    ("hoodie", "synonym", 100, ("sweat shirt with hood", "with hood", "with food")),
    ("pullover", "synonym", 95, ("pullover", "pull over", "sweater", "jumper")),
    ("sneakers", "synonym", 100, ("sport shoes",)),
    ("trainers", "synonym", 90, ("sport shoes",)),
    ("running shoes", "synonym", 95, ("sport shoes",)),
    ("tshirt", "synonym", 100, ("t shirt",)),
    ("tee", "synonym", 85, ("t shirt",)),
    ("sari", "synonym", 100, ("saree",)),
    ("lehenga", "synonym", 100, ("lehnga",)),
    ("palazzo", "synonym", 100, ("plazzo",)),
    ("kurti", "synonym", 90, ("kurta",)),
    ("chunni", "synonym", 90, ("dupatta",)),
    ("teddy", "synonym", 90, ("soft toy",)),
    ("teddy bear", "synonym", 95, ("soft toy",)),
    ("luggage", "synonym", 90, ("suit case",)),
    ("purse", "synonym", 80, ("handbag", "wallet")),
    ("bedding", "category", 80, ("bed sheet", "pillow cover", "quilt", "blanket", "mattress", "bed head")),
    ("winter wear", "category", 80, ("sweater", "pullover", "jacket", "coat", "muffler", "shawl", "blanket", "sweat shirt")),
    ("traditional wear", "category", 80, ("saree", "blouse", "lehnga", "kurta", "salwar", "dupatta", "dhoti", "sherwani")),
    ("footwear", "category", 80, ("shoes", "sandal", "slipper", "boot", "suede", "canvass")),
]


def normalize(value: str) -> str:
    text = value.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.strip().lower())).strip("-")


def ensure_catalogue_search_schema(conn: DbConnection) -> None:
    if not is_postgres():
        return
    # Skip CREATE EXTENSION here — it can lock on shared Railway Postgres.
    # Fuzzy matching falls back to LIKE when pg_trgm is unavailable.
    for statement in CATALOGUE_STATEMENTS:
        conn.execute(statement)


def _load_rate_card() -> dict[str, Any]:
    if not APP_RATE_CARD.exists():
        raise FileNotFoundError(f"Rate card not found: {APP_RATE_CARD}")
    return json.loads(APP_RATE_CARD.read_text(encoding="utf-8"))


def seed_catalogue_from_rate_card(conn: DbConnection) -> dict[str, int]:
    """Replace website catalogue + keywords from the Expo rate card. Billing tables untouched."""
    ensure_catalogue_search_schema(conn)
    raw = _load_rate_card()
    services = raw.get("services") or []
    items: list[tuple] = []
    for service in services:
        service_id = service.get("id") or ""
        service_name = service.get("name") or service_id
        for group in service.get("groups") or []:
            group_name = group.get("name") or ""
            group_id = slug(group_name)
            seen: dict[str, int] = {}
            for entry in group.get("items") or []:
                name = entry.get("name") or ""
                base = f"{service_id}.{group_id}.{slug(name)}"
                count = seen.get(base, 0) + 1
                seen[base] = count
                item_id = base if count == 1 else f"{base}-{count}"
                items.append(
                    (
                        item_id,
                        service_id,
                        service_name,
                        group_id,
                        group_name,
                        name,
                        normalize(name),
                        float(entry.get("rate") or 0),
                        entry.get("unit") or "piece",
                        entry.get("rateText") or "",
                    )
                )

    conn.execute("DELETE FROM website.search_keywords")
    conn.execute("DELETE FROM website.catalogue_items")
    _insert_rows_chunked(
        conn,
        """
        INSERT INTO website.catalogue_items (
            id, service_id, service_name, group_id, group_name, name,
            normalized_name, rate, unit, rate_text, active
        ) VALUES
        """,
        items,
        values_sql="(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        cols_per_row=10,
    )

    keyword_params: list[tuple] = []
    for item_id, _sid, _sname, _gid, _gname, name, norm_name, *_rest in items:
        compact = norm_name.replace(" ", "")
        for token in {norm_name, compact, *norm_name.split()}:
            if len(token) < 2:
                continue
            keyword_params.append((item_id, token, token, "exact", 100))
        for keyword, ktype, weight, needles in KEYWORD_SEED:
            if any(_name_matches_needle(norm_name, needle) for needle in needles):
                keyword_params.append(
                    (item_id, keyword, normalize(keyword), ktype, weight)
                )

    _insert_rows_chunked(
        conn,
        """
        INSERT INTO website.search_keywords (
            item_id, keyword, normalized_keyword, keyword_type, relevance_weight
        ) VALUES
        """,
        keyword_params,
        values_sql="(?, ?, ?, ?, ?)",
        cols_per_row=5,
    )

    return {"items": len(items), "keywords": len(keyword_params)}


def _name_matches_needle(norm_name: str, needle: str) -> bool:
    """Word-boundary style match so 't shirt' does not hit 'sweat shirt'."""
    n = normalize(needle)
    if not n:
        return False
    if norm_name == n:
        return True
    padded = f" {norm_name} "
    return f" {n} " in padded


def _insert_rows_chunked(
    conn: DbConnection,
    insert_prefix: str,
    rows: list[tuple],
    *,
    values_sql: str,
    cols_per_row: int,
    chunk_size: int = 100,
) -> None:
    """Multi-row INSERT in chunks — far fewer round-trips than executemany over Railway."""
    if not rows:
        return
    for offset in range(0, len(rows), chunk_size):
        chunk = rows[offset : offset + chunk_size]
        placeholders = ", ".join([values_sql] * len(chunk))
        flat: list[Any] = []
        for row in chunk:
            flat.extend(row[:cols_per_row])
        conn.execute(insert_prefix + " " + placeholders, flat)


def search_catalogue(
    conn: DbConnection,
    *,
    query: str,
    service_id: str | None = None,
    group_id: str | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    q = normalize(query)
    if not q:
        sql = "SELECT * FROM website.catalogue_items WHERE active = 1"
        params: list[Any] = []
        if service_id:
            sql += " AND service_id = ?"
            params.append(service_id)
        if group_id and group_id != "all":
            sql += " AND group_id = ?"
            params.append(group_id)
        sql += " ORDER BY name LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_item(row, "exact", 0) for row in rows]

    like = f"%{q}%"
    sql = """
        SELECT
            ci.*,
            MAX(
                CASE
                    WHEN ci.normalized_name = ? THEN 100
                    WHEN ci.normalized_name LIKE ? THEN 92
                    WHEN sk.keyword_type = 'exact' AND sk.normalized_keyword = ? THEN 96
                    WHEN sk.keyword_type = 'synonym' AND sk.normalized_keyword = ? THEN 78
                    WHEN sk.keyword_type = 'synonym' AND sk.normalized_keyword LIKE ? THEN 70
                    WHEN sk.keyword_type = 'category' AND sk.normalized_keyword = ? THEN 55
                    WHEN sk.keyword_type = 'related' AND sk.normalized_keyword = ? THEN 45
                    WHEN ci.normalized_name % ? THEN 60
                    WHEN sk.normalized_keyword % ? THEN 58
                    ELSE 0
                END
            ) AS score,
            MAX(sk.keyword_type) AS hit_type
        FROM website.catalogue_items ci
        LEFT JOIN website.search_keywords sk
            ON sk.item_id = ci.id AND sk.is_active = 1
        WHERE ci.active = 1
          AND (
            ci.normalized_name = ?
            OR ci.normalized_name LIKE ?
            OR sk.normalized_keyword = ?
            OR sk.normalized_keyword LIKE ?
            OR ci.normalized_name % ?
            OR sk.normalized_keyword % ?
          )
    """
    params = [q, like, q, q, like, q, q, q, q, q, like, q, like, q, q]
    if service_id:
        sql += " AND ci.service_id = ?"
        params.append(service_id)
    if group_id and group_id != "all":
        sql += " AND ci.group_id = ?"
        params.append(group_id)
    sql += """
        GROUP BY ci.id
        HAVING MAX(
                CASE
                    WHEN ci.normalized_name = ? THEN 100
                    WHEN ci.normalized_name LIKE ? THEN 92
                    WHEN sk.keyword_type = 'exact' AND sk.normalized_keyword = ? THEN 96
                    WHEN sk.keyword_type = 'synonym' AND sk.normalized_keyword = ? THEN 78
                    WHEN sk.keyword_type = 'synonym' AND sk.normalized_keyword LIKE ? THEN 70
                    WHEN sk.keyword_type = 'category' AND sk.normalized_keyword = ? THEN 55
                    WHEN sk.keyword_type = 'related' AND sk.normalized_keyword = ? THEN 45
                    WHEN ci.normalized_name % ? THEN 60
                    WHEN sk.normalized_keyword % ? THEN 58
                    ELSE 0
                END
            ) >= 45
        ORDER BY score DESC, ci.name ASC
        LIMIT ?
    """
    params.extend([q, like, q, q, like, q, q, q, q, limit])
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except Exception:
        # Fallback without pg_trgm operator
        sql_fb = """
            SELECT ci.*,
                MAX(
                    CASE
                        WHEN ci.normalized_name = ? THEN 100
                        WHEN ci.normalized_name LIKE ? THEN 92
                        WHEN sk.normalized_keyword = ? THEN 78
                        WHEN sk.normalized_keyword LIKE ? THEN 65
                        ELSE 0
                    END
                ) AS score,
                MAX(sk.keyword_type) AS hit_type
            FROM website.catalogue_items ci
            LEFT JOIN website.search_keywords sk
                ON sk.item_id = ci.id AND sk.is_active = 1
            WHERE ci.active = 1
              AND (
                ci.normalized_name = ?
                OR ci.normalized_name LIKE ?
                OR sk.normalized_keyword = ?
                OR sk.normalized_keyword LIKE ?
              )
        """
        params_fb: list[Any] = [q, like, q, like, q, like, q, like]
        if service_id:
            sql_fb += " AND ci.service_id = ?"
            params_fb.append(service_id)
        if group_id and group_id != "all":
            sql_fb += " AND ci.group_id = ?"
            params_fb.append(group_id)
        sql_fb += " GROUP BY ci.id HAVING MAX(CASE WHEN ci.normalized_name = ? THEN 100 WHEN ci.normalized_name LIKE ? THEN 92 WHEN sk.normalized_keyword = ? THEN 78 WHEN sk.normalized_keyword LIKE ? THEN 65 ELSE 0 END) >= 45 ORDER BY score DESC, ci.name ASC LIMIT ?"
        params_fb.extend([q, like, q, like, limit])
        rows = conn.execute(sql_fb, tuple(params_fb)).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        score = int(row["score"] if isinstance(row, dict) else row[-2] or 0)
        hit_type = str(row["hit_type"] if isinstance(row, dict) else row[-1] or "")
        kind = "exact" if score >= 90 else "similar" if score >= 55 else "related"
        if hit_type == "related" and score < 90:
            kind = "related"
        out.append(_row_to_item(row, kind, score))
    return out


def _row_to_item(row: Any, kind: str, score: int) -> dict[str, Any]:
    data = dict(row) if isinstance(row, dict) else {}
    if not data:
        # positional fallback unlikely with RealDict-like wrappers
        return {"kind": kind, "score": score}
    return {
        "id": data.get("id"),
        "serviceId": data.get("service_id"),
        "serviceName": data.get("service_name"),
        "groupId": data.get("group_id"),
        "groupName": data.get("group_name"),
        "name": data.get("name"),
        "rate": data.get("rate"),
        "unit": data.get("unit"),
        "rateText": data.get("rate_text") or None,
        "kind": kind,
        "label": "Exact Match" if kind == "exact" else "Similar Name" if kind == "similar" else "Related Item",
        "score": score,
    }


def record_search_event(
    conn: DbConnection,
    *,
    query: str,
    service_id: str = "",
    result_count: int = 0,
    selected_item_id: str = "",
    match_kind: str = "",
) -> None:
    if not is_postgres():
        return
    conn.execute(
        """
        INSERT INTO website.search_events (
            query, normalized_query, service_id, result_count, selected_item_id, match_kind
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (query, normalize(query), service_id or "", result_count, selected_item_id or "", match_kind or ""),
    )


def seed_with_own_connection() -> dict[str, Any]:
    with get_connection() as conn:
        ensure_catalogue_search_schema(conn)
        counts = seed_catalogue_from_rate_card(conn)
        return {"backend": "postgresql" if is_postgres() else "sqlite", **counts}

"""Database connection layer — PostgreSQL (Railway) or local SQLite."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote_plus, urlparse

from paths import sqlite_db_path

DB_PATH = sqlite_db_path()

# Full connection strings — public URL first on Railway when internal DNS fails
PG_ENV_KEYS = (
    "DATABASE_PUBLIC_URL",
    "DATABASE_PRIVATE_URL",
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRESQL_URL",
)

# Railway also exposes split Postgres vars when referenced
PG_PARTS_KEYS = (
    ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"),
    ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"),
    ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DATABASE"),
)


def _normalize_postgres_url(url: str) -> str:
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url


def _build_url_from_parts(host: str, port: str, user: str, password: str, database: str) -> str:
    host = host.strip()
    user = user.strip()
    password = password.strip()
    database = database.strip()
    port = (port or "5432").strip()
    if not all([host, user, password, database]):
        return ""
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{quote_plus(database)}"
    )


def _is_unresolved_reference(value: str) -> bool:
    return "${{" in value or value.startswith("${")


def _host_rank(url: str) -> int:
    if _is_unresolved_reference(url):
        return 99
    if is_railway() and "railway.internal" in url:
        return 2
    if "proxy.rlwy.net" in url or "rlwy.net" in url:
        return 0
    return 1


def _postgres_url_hosts() -> list[str]:
    hosts: list[str] = []
    for url in _collect_postgres_urls():
        try:
            hosts.append(urlparse(url).hostname or "unknown")
        except Exception:
            hosts.append("unknown")
    return hosts


def _collect_postgres_urls() -> list[str]:
    """Collect Postgres URLs — on Railway, prefer public proxy over internal DNS."""
    priority = {
        "DATABASE_PUBLIC_URL": 0,
        "DATABASE_PRIVATE_URL": 1,
        "DATABASE_URL": 2,
        "POSTGRES_URL": 3,
        "POSTGRESQL_URL": 4,
    }
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for key in PG_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if _is_unresolved_reference(value):
            continue
        if not value.startswith(("postgres://", "postgresql://")):
            continue
        normalized = _normalize_postgres_url(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        ranked.append((priority.get(key, 9), _host_rank(normalized), normalized))

    for parts in PG_PARTS_KEYS:
        url = _build_url_from_parts(
            os.environ.get(parts[0], ""),
            os.environ.get(parts[1], ""),
            os.environ.get(parts[2], ""),
            os.environ.get(parts[3], ""),
            os.environ.get(parts[4], ""),
        )
        if url and url not in seen and not _is_unresolved_reference(url):
            seen.add(url)
            ranked.append((5, _host_rank(url), url))

    ranked.sort(key=lambda item: (item[1], item[0]))
    urls = [url for _, _, url in ranked]
    if is_railway():
        public_urls = [
            url
            for url in urls
            if "proxy.rlwy.net" in url or ".rlwy.net" in url or ".railway.app" in url
        ]
        if public_urls:
            return public_urls
    return urls


_pg_reachable: bool | None = None
_using_sqlite_fallback = False
_pg_pool = None
_pg_pool_lock = threading.Lock()


def _invalidate_postgres_probe() -> None:
    global _pg_reachable
    _pg_reachable = None


def _postgres_probe(*, force: bool = False) -> bool:
    global _pg_reachable
    if not force and _pg_reachable is not None:
        return _pg_reachable

    urls = _collect_postgres_urls()
    if not urls:
        _pg_reachable = False
        return False

    try:
        import psycopg2
    except ImportError:
        _pg_reachable = False
        return False

    for url in urls:
        try:
            conn = psycopg2.connect(_postgres_connect_url(url), connect_timeout=5)
            conn.close()
            _pg_reachable = True
            return True
        except Exception:
            continue

    _pg_reachable = False
    return False


def postgres_fallback_to_sqlite() -> bool:
    global _using_sqlite_fallback
    return _using_sqlite_fallback


def _should_use_sqlite_fallback() -> bool:
    global _using_sqlite_fallback
    if not is_railway() or not _collect_postgres_urls():
        _using_sqlite_fallback = False
        return False
    if os.environ.get("DATABASE_FALLBACK_SQLITE", "1").lower() in ("0", "false", "no"):
        _using_sqlite_fallback = False
        return False
    if _postgres_probe():
        _using_sqlite_fallback = False
        return False
    _using_sqlite_fallback = True
    return True


def _resolve_database_url() -> str:
    urls = _collect_postgres_urls()
    return urls[0] if urls else ""


def _postgres_env_diagnostics() -> dict[str, str]:
    """Show which Postgres-related env vars exist (never values)."""
    keys = list(PG_ENV_KEYS)
    for parts in PG_PARTS_KEYS:
        keys.extend(parts)
    keys.extend(["RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME"])
    seen: set[str] = set()
    diag: dict[str, str] = {}
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        value = os.environ.get(key, "").strip()
        if not value:
            diag[key] = "missing"
        elif key in PG_ENV_KEYS:
            diag[key] = "set (postgres url)"
        else:
            diag[key] = "set"
    return diag


def is_postgres() -> bool:
    if not _collect_postgres_urls():
        return False
    if _should_use_sqlite_fallback():
        return False
    return True


def is_railway() -> bool:
    return bool(os.environ.get("RAILWAY_ENVIRONMENT", "").strip())


def _postgres_url() -> str:
    return _resolve_database_url()


def database_config_status() -> dict[str, Any]:
    has_pg_env = bool(_collect_postgres_urls())
    using_fallback = _should_use_sqlite_fallback() if has_pg_env else False
    backend = "sqlite" if using_fallback or not has_pg_env else "postgresql"
    status: dict[str, Any] = {
        "backend": backend,
        "railway": is_railway(),
        "postgresConfigured": has_pg_env,
        "postgresReachable": _postgres_probe() if has_pg_env else False,
        "sqliteFallback": using_fallback,
        "postgresEnvDiagnostics": _postgres_env_diagnostics(),
    }
    if has_pg_env:
        for key in PG_ENV_KEYS:
            if os.environ.get(key, "").strip().startswith(("postgres://", "postgresql://")):
                status["postgresEnvVar"] = key
                break
        else:
            for parts in PG_PARTS_KEYS:
                if all(os.environ.get(k, "").strip() for k in parts):
                    status["postgresEnvVar"] = f"{parts[0]}..{parts[-1]}"
                    break
        if using_fallback:
            status["warning"] = (
                "PostgreSQL URL is set but not reachable — using temporary SQLite storage on this server."
            )
            status["fixSteps"] = [
                "Railway → Rinse-RiseBilling → Variables → delete the old manual DATABASE_URL",
                "Add Variable Reference: PostgreSQL service → DATABASE_PRIVATE_URL → name it DATABASE_URL",
                "Also add Variable Reference: PostgreSQL → DATABASE_PUBLIC_URL (fallback)",
                "Redeploy Rinse-RiseBilling — bills will then persist in PostgreSQL",
            ]
        elif not _postgres_probe():
            hosts = _postgres_url_hosts()
            status["postgresUrlHosts"] = hosts
            status["warning"] = "PostgreSQL is configured but connection failed."
            if hosts == ["postgres.railway.internal"] or (
                hosts and all("railway.internal" in (h or "") for h in hosts)
            ):
                status["fixSteps"] = [
                    "Your DATABASE_URL still uses postgres.railway.internal (private DNS not working).",
                    "Open PostgreSQL service → Variables → copy DATABASE_PUBLIC_URL",
                    "On Rinse-RiseBilling → Variables → delete old DATABASE_URL",
                    "Add DATABASE_URL = paste the PUBLIC url (contains proxy.rlwy.net)",
                    "Or use Raw Editor: DATABASE_URL=${{YOUR_POSTGRES_SERVICE_NAME.DATABASE_PUBLIC_URL}}",
                    "Redeploy Rinse-RiseBilling",
                ]
            else:
                status["fixSteps"] = [
                    "Re-link DATABASE_URL using Variable Reference from the Postgres service",
                    "Add DATABASE_PUBLIC_URL from Postgres as a second variable",
                    "Redeploy the web service",
                ]
    elif is_railway():
        status["warning"] = (
            "PostgreSQL is not linked to this service. Fix in Railway dashboard (see RAILWAY_SETUP.md)."
        )
        status["fixSteps"] = [
            "Open Postgres service → Variables → copy DATABASE_PRIVATE_URL",
            "Open Rinse-RiseBilling service → Variables → New Variable",
            "Name: DATABASE_URL  Value: paste the copied URL (or use Variable Reference)",
            "Redeploy Rinse-RiseBilling",
        ]
    return status


def database_backend_name() -> str:
    return "postgresql" if is_postgres() else "sqlite"


def date_gte(column: str) -> str:
    if is_postgres():
        return f"{column}::date >= %s::date"
    return f"date({column}) >= date(?)"


def date_lte(column: str) -> str:
    if is_postgres():
        return f"{column}::date <= %s::date"
    return f"date({column}) <= date(?)"


class DbCursor:
    def __init__(self, cursor: Any, is_pg: bool) -> None:
        self._cursor = cursor
        self._is_pg = is_pg
        self._insert_id: int | None = None

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    @property
    def lastrowid(self) -> int | None:
        if self._is_pg:
            return self._insert_id
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class DbConnection:
    def __init__(self, conn: Any, is_pg: bool) -> None:
        self._conn = conn
        self._is_pg = is_pg

    def _adapt_sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._is_pg else sql

    def execute(self, sql: str, params: tuple | list = ()) -> DbCursor:
        sql = self._adapt_sql(sql)
        cur = self._conn.cursor()
        cur.execute(sql, params)
        wrapper = DbCursor(cur, self._is_pg)
        if self._is_pg and sql.strip().upper().startswith("INSERT") and "RETURNING" in sql.upper():
            row = cur.fetchone()
            if row:
                wrapper._insert_id = row["id"] if isinstance(row, dict) else row[0]
        return wrapper

    def insert_returning_id(self, sql: str, params: tuple | list) -> int:
        if self._is_pg:
            base = self._adapt_sql(sql).rstrip().rstrip(";")
            cur = self._conn.cursor()
            cur.execute(base + " RETURNING id", params)
            row = cur.fetchone()
            if not row:
                raise RuntimeError("Insert did not return id")
            return int(row["id"] if isinstance(row, dict) else row[0])
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return int(cur.lastrowid)

    def commit(self) -> None:
        self._conn.commit()

    def executescript(self, script: str) -> None:
        if self._is_pg:
            statements = [s.strip() for s in script.split(";") if s.strip()]
            cur = self._conn.cursor()
            for statement in statements:
                cur.execute(statement)
        else:
            self._conn.executescript(script)

    def table_columns(self, table: str) -> set[str]:
        allowed = {"bills", "bill_items", "customers", "expenditures", "settings"}
        if table not in allowed:
            raise ValueError(f"Unknown table: {table}")
        if self._is_pg:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                """,
                (table,),
            )
            return {row["column_name"] for row in cur.fetchall()}
        rows = self.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}


def _postgres_connect_url(url: str) -> str:
    if "sslmode=" not in url and "railway.internal" not in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"
    return url


def _get_pg_pool():
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    with _pg_pool_lock:
        if _pg_pool is not None:
            return _pg_pool
        from psycopg2.extras import RealDictCursor
        from psycopg2.pool import ThreadedConnectionPool

        urls = [_postgres_connect_url(u) for u in _collect_postgres_urls()]
        if not urls:
            raise RuntimeError("No PostgreSQL connection URL available")
        last_error: Exception | None = None
        for url in urls:
            try:
                _pg_pool = ThreadedConnectionPool(
                    1,
                    8,
                    url,
                    cursor_factory=RealDictCursor,
                    connect_timeout=10,
                    options="-c statement_timeout=15000 -c lock_timeout=8000",
                )
                return _pg_pool
            except Exception as exc:
                last_error = exc
                continue
        raise last_error or RuntimeError("No PostgreSQL connection URL available")


@contextmanager
def get_connection() -> Iterator[DbConnection]:
    if is_postgres():
        pool = _get_pg_pool()
        conn = pool.getconn()
        wrapper = DbConnection(conn, True)
        try:
            yield wrapper
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(DB_PATH)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        wrapper = DbConnection(raw, False)
        try:
            yield wrapper
        finally:
            raw.close()

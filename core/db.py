"""Postgres access: one pool, tenant-scoped transactions (row-level security) and forward-only migrations."""
import atexit
import contextlib
import os
import pathlib

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

ROOT = pathlib.Path(__file__).resolve().parent.parent
_pool = None


def url():
    return os.environ["DATABASE_URL"]


def pool():
    global _pool
    if _pool is None:
        _pool = ConnectionPool(url(), min_size=1, max_size=int(os.environ.get("DB_POOL", "10")),
                               kwargs={"row_factory": dict_row}, open=True)
        atexit.register(close)
    return _pool


def close():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextlib.contextmanager
def tx(tenant_id=None):
    """One transaction: commit on success, roll back on any exception.

    With a tenant it runs as the unprivileged role with app.tenant_id set, so the RLS policies apply.
    Without one it runs as the owner (migrations, seeding, token lookup, the job claim).
    """
    with pool().connection() as c:
        if tenant_id:
            c.execute("SET LOCAL ROLE app_rw")
            c.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
        yield c


def migrate():
    with tx() as c:
        c.execute("SELECT pg_advisory_xact_lock(4242)")
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["name"] for r in c.execute("SELECT name FROM schema_migrations")}
        for f in sorted((ROOT / "migrations").glob("*.sql")):
            if f.name not in done:
                c.execute(f.read_text())
                c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", [f.name])


def copy(c, table, cols, rows):
    """Bulk insert with COPY (used by the seed scripts and batch ingestion)."""
    with c.cursor().copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)


def many(c, sql, rows):
    c.cursor().executemany(sql, rows)

"""Short PostgreSQL transactions and fenced durable job publication."""

import json
from contextlib import contextmanager
from datetime import timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from quant_platform.domain import digest, now


def jsonb(value):
    return Jsonb(value, dumps=lambda item: json.dumps(item, default=str, allow_nan=False))


class LostLease(RuntimeError):
    pass


class Database:
    def __init__(self, dsn):
        self.pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=8,
            timeout=10,
            open=True,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 5,
                "options": "-c statement_timeout=30000 -c lock_timeout=5000",
            },
        )

    @contextmanager
    def transaction(self):
        with self.pool.connection() as connection:
            with connection.transaction():
                yield connection

    def close(self):
        self.pool.close()

    def rows(self, sql, params=()):
        with self.transaction() as conn:
            return conn.execute(sql, params).fetchall()

    def setting(self, key, default=None):
        rows = self.rows("SELECT value FROM settings WHERE key=%s", (key,))
        return rows[0]["value"] if rows else default

    @staticmethod
    def set_setting(conn, key, value):
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE "
            "SET value=excluded.value,revision=settings.revision+1",
            (key, jsonb(value)),
        )

    @staticmethod
    def enqueue(conn, kind, queue, key, payload=None, priority=0, available=None):
        row = conn.execute(
            "INSERT INTO jobs(kind,queue,dedupe,payload,priority,available_at) VALUES(%s,%s,%s,%s,%s,coalesce(%s,now())) "
            "ON CONFLICT(dedupe) DO UPDATE SET dedupe=excluded.dedupe RETURNING id",
            (kind, queue, key, jsonb(payload or {}), priority, available),
        ).fetchone()
        return row["id"]

    def claim(self, queue, owner):
        with self.transaction() as conn:
            return conn.execute(
                "WITH candidate AS (SELECT id FROM jobs WHERE queue=%s AND available_at<=now() AND "
                "(status='pending' OR (status='running' AND lease_until<now())) "
                "ORDER BY priority DESC,available_at,id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE jobs j SET status='running',owner=%s,fence=fence+1,attempts=attempts+1,"
                "lease_until=now()+interval '60 seconds',updated_at=now() FROM candidate c "
                "WHERE j.id=c.id RETURNING j.*",
                (queue, owner),
            ).fetchone()

    def renew(self, job):
        with self.transaction() as conn:
            result = conn.execute(
                "UPDATE jobs SET lease_until=now()+interval '60 seconds',updated_at=now() "
                "WHERE id=%s AND fence=%s AND owner=%s AND status='running' AND lease_until>now()",
                (job["id"], job["fence"], job["owner"]),
            )
            return result.rowcount == 1

    @staticmethod
    def fence(conn, job):
        row = conn.execute(
            "SELECT *,lease_until>clock_timestamp() AS unexpired FROM jobs WHERE id=%s FOR UPDATE", (job["id"],)
        ).fetchone()
        if (
            not row
            or row["status"] != "running"
            or row["fence"] != job["fence"]
            or row["owner"] != job["owner"]
            or not row["unexpired"]
        ):
            raise LostLease("Job ownership expired; publication rejected.")

    @contextmanager
    def publication(self, job):
        with self.transaction() as conn:
            self.fence(conn, job)
            yield conn
            conn.execute(
                "UPDATE jobs SET status='complete',lease_until=NULL,updated_at=now(),error=NULL WHERE id=%s",
                (job["id"],),
            )

    def finished(self, job):
        rows = self.rows(
            "SELECT 1 FROM jobs WHERE id=%s AND fence=%s AND owner=%s AND status!='running'",
            (job["id"], job["fence"], job["owner"]),
        )
        return bool(rows)

    def defer(self, job, error, delay=1800, blocked=False, transient=False):
        with self.transaction() as conn:
            self.fence(conn, job)
            failures = job.get("failures", 0) + (0 if transient or blocked else 1)
            status = "blocked" if blocked else "failed" if failures >= 10 else "pending"
            conn.execute(
                "UPDATE jobs SET status=%s,error=%s,available_at=now()+(%s * interval '1 second'),failures=%s,"
                "lease_until=NULL,updated_at=now() WHERE id=%s",
                (status, error, delay, failures, job["id"]),
            )

    def checkpoint(self, job, endpoint, code, data):
        with self.transaction() as conn:
            self.fence(conn, job)
            conn.execute(
                "INSERT INTO collection_checkpoints VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (job["id"], endpoint, code, jsonb(data)),
            )

    def heartbeat(self, owner, role, data):
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO heartbeats VALUES(%s,%s,now(),%s) ON CONFLICT(worker) DO UPDATE "
                "SET updated_at=now(),data=excluded.data",
                (owner, role, jsonb(data)),
            )

    @staticmethod
    def dataset(conn, endpoint, scope, rows, metadata, quality="verified"):
        # Serialize allocation through commit so a pinned watermark cannot gain late lower-ID revisions.
        conn.execute("SELECT pg_advisory_xact_lock(77610234)")
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (endpoint + ":" + scope,))
        previous = conn.execute(
            "SELECT id,content_hash FROM datasets WHERE endpoint=%s AND scope=%s ORDER BY id DESC LIMIT 1",
            (endpoint, scope),
        ).fetchone()
        content_hash = digest(rows)
        if previous and previous["content_hash"] == content_hash:
            return previous["id"]
        return conn.execute(
            "INSERT INTO datasets(endpoint,scope,content_hash,quality,metadata) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (endpoint, scope, content_hash, quality, jsonb({"provider": "tushare", **metadata})),
        ).fetchone()["id"]

    def history(self, code, as_of, limit=500, watermark=None):
        return self.rows(
            "WITH b AS (SELECT DISTINCT ON(day) day,data,dataset_id FROM daily_bars "
            "WHERE symbol=%s AND day<=%s AND (%s::bigint IS NULL OR dataset_id<=%s) "
            "ORDER BY day DESC,dataset_id DESC LIMIT %s) "
            "SELECT b.day,b.data,b.dataset_id,f.factor,f.dataset_id AS factor_dataset_id FROM b "
            "LEFT JOIN LATERAL (SELECT factor,dataset_id FROM factors WHERE symbol=%s AND day=b.day "
            "AND (%s::bigint IS NULL OR dataset_id<=%s) ORDER BY dataset_id DESC LIMIT 1) f ON true ORDER BY b.day",
            (code, as_of, watermark, watermark, limit, code, watermark, watermark),
        )

    def basics_window(self, as_of, limit=500, watermark=None):
        rows = self.rows(
            "WITH ranked AS ("
            "SELECT symbol,day,data,dataset_id,row_number() OVER "
            "(PARTITION BY symbol,day ORDER BY dataset_id DESC) AS revision_rank "
            "FROM daily_basics WHERE day<=%s AND (%s::bigint IS NULL OR dataset_id<=%s)"
            "), latest AS ("
            "SELECT symbol,day,data,dataset_id,row_number() OVER "
            "(PARTITION BY symbol ORDER BY day DESC) AS age FROM ranked WHERE revision_rank=1"
            ") SELECT symbol,day,data,dataset_id FROM latest WHERE age<=%s ORDER BY symbol,day",
            (as_of, watermark, watermark, limit),
        )
        result = {}
        for row in rows:
            result.setdefault(row["symbol"], []).append(row)
        return result

    def active_holdings(self):
        return self.rows("SELECT * FROM holdings WHERE NOT archived ORDER BY symbol")

    def active_baskets(self, day):
        return self.rows(
            "SELECT b.id,r.data->>'name' AS name,b.paused,r.revision,r.effective_day,r.data FROM baskets b JOIN LATERAL "
            "(SELECT * FROM basket_revisions WHERE basket_id=b.id AND effective_day<=%s "
            "ORDER BY effective_day DESC,revision DESC LIMIT 1) r ON true WHERE NOT b.archived",
            (day,),
        )

    def calendar_open(self, day):
        rows = self.rows("SELECT is_open FROM calendars WHERE day=%s AND exchange IN ('SSE','SZSE')", (day,))
        return rows[0]["is_open"] if len(rows) == 2 and rows[0]["is_open"] == rows[1]["is_open"] else None

    def capability(self, endpoint, status, data):
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO capabilities VALUES(%s,%s,%s,now()) ON CONFLICT(endpoint) DO UPDATE "
                "SET status=excluded.status,data=excluded.data,updated_at=now()",
                (endpoint, status, jsonb(data)),
            )

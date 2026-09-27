"""Grant the worker exactly the partition DDL it needs, without handing over table ownership.

`maintenance` creates the rolling `quotes` / `alerts` / `minute_bars` partitions and retires the
empty ones it created before. Both operations are ownership-checked in PostgreSQL — ``CREATE TABLE
... PARTITION OF`` needs ownership of the partitioned parent, ``DROP TABLE`` needs ownership of the
partition — and the partitioned parents are owned by the migration user while every worker connects
as ``quant_worker``. The result was that every ``maintenance`` run aborted with
``InsufficientPrivilege`` and no future partition was ever created.

The fix keeps ownership where it is and exposes the two operations as ``SECURITY DEFINER`` functions
owned by the migration user. The caller names a table from a fixed allow-list and a calendar day; the
partition name and its range are derived inside the function, so the elevated block never receives a
caller-supplied identifier and cannot be pointed at another object. ``retire_empty_partition`` also
re-checks the parent/child relationship through ``pg_inherits`` and drops only an empty partition, so
the worker can never retire a partition that still holds data.

This preserves the documented privilege model (``quant_worker`` writes; the migration user owns):
nothing here grants the worker ownership, TRUNCATE or DROP rights of its own.

Two details matter for the elevated block itself. ``search_path`` is pinned to ``pg_catalog`` alone,
so no ``public`` object can shadow a built-in the function relies on — ``quant_worker`` holds
``CREATE`` on ``public`` and could otherwise plant a ``format()``. Every table the function touches
is therefore written as ``public.<name>``; an unqualified ``CREATE TABLE`` would otherwise resolve to
``pg_catalog``, the first writable schema on the path.
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE OR REPLACE FUNCTION ensure_range_partition(table_name text, partition_day date) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
    AS $body$
    DECLARE
      name text;
    BEGIN
      IF table_name NOT IN ('quotes','alerts','minute_bars') THEN
        RAISE EXCEPTION 'Unsupported partitioned table %.', table_name USING ERRCODE = '42501';
      END IF;
      name := format('%s_%s', table_name, to_char(partition_day, 'YYYYMMDD'));
      EXECUTE format(
        'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.%I FOR VALUES FROM (%L) TO (%L)',
        name, table_name, partition_day::timestamptz, (partition_day + 1)::timestamptz
      );
      RETURN name;
    END
    $body$;

    CREATE OR REPLACE FUNCTION retire_empty_partition(table_name text, partition_day date) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
    AS $body$
    DECLARE
      name text;
      attached boolean;
      empty boolean;
    BEGIN
      IF table_name NOT IN ('quotes','alerts','minute_bars') THEN
        RAISE EXCEPTION 'Unsupported partitioned table %.', table_name USING ERRCODE = '42501';
      END IF;
      name := format('%s_%s', table_name, to_char(partition_day, 'YYYYMMDD'));
      SELECT true INTO attached
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_namespace n ON n.oid = p.relnamespace
       WHERE n.nspname = 'public' AND p.relname = table_name AND c.relname = name;
      IF NOT coalesce(attached, false) THEN
        RETURN false;
      END IF;
      EXECUTE format('SELECT NOT EXISTS(SELECT 1 FROM public.%I)', name) INTO empty;
      IF empty THEN
        EXECUTE format('DROP TABLE public.%I', name);
      END IF;
      RETURN empty;
    END
    $body$;

    REVOKE ALL ON FUNCTION ensure_range_partition(text,date) FROM PUBLIC;
    REVOKE ALL ON FUNCTION retire_empty_partition(text,date) FROM PUBLIC;
    DO $grant$
    BEGIN
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quant_worker') THEN
        GRANT EXECUTE ON FUNCTION ensure_range_partition(text,date) TO quant_worker;
        GRANT EXECUTE ON FUNCTION retire_empty_partition(text,date) TO quant_worker;
      END IF;
    END
    $grant$;
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup; destructive downgrade is disabled.")

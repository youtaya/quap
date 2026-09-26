"""Bind every idempotency key, including aliases of a reused Qlib run."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE pipeline_requests (
      request_key text PRIMARY KEY, run_id uuid NOT NULL REFERENCES pipeline_runs(id),
      request jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    INSERT INTO pipeline_requests(request_key,run_id,request)
      SELECT request_key,id,snapshot->'request' FROM pipeline_runs;
    CREATE INDEX pipeline_requests_run ON pipeline_requests(run_id);
    DO $body$ BEGIN
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quant_worker') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON pipeline_requests TO quant_worker;
        GRANT SELECT ON pipeline_requests TO quant_read;
      END IF;
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END $body$;
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup; destructive downgrade is disabled.")

"""Separate retries from deferrals, version controls, and checkpoint large requests."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE jobs ADD COLUMN failures integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE baskets ADD COLUMN control_revision integer NOT NULL DEFAULT 0")
    op.execute(
        "CREATE TABLE collection_checkpoints (job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, "
        "endpoint text NOT NULL, symbol text NOT NULL, data jsonb NOT NULL, PRIMARY KEY(job_id,endpoint,symbol))"
    )
    op.execute(
        "CREATE TABLE qualification_samples (minute timestamptz PRIMARY KEY, day date NOT NULL, "
        "healthy boolean NOT NULL, data jsonb NOT NULL)"
    )


def downgrade():
    raise RuntimeError("Restore a verified backup into a separate database; destructive downgrade is disabled.")

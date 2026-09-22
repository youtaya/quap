"""Relative valuation snapshots and cost-basis holdings."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE daily_basics ("
        "symbol text NOT NULL, day date NOT NULL, dataset_id bigint NOT NULL REFERENCES datasets(id), "
        "data jsonb NOT NULL, PRIMARY KEY(symbol, day, dataset_id))"
    )
    op.execute("CREATE INDEX daily_basics_lookup ON daily_basics(symbol, day DESC, dataset_id DESC)")
    op.execute(
        "CREATE TABLE holdings ("
        "id uuid PRIMARY KEY, symbol text NOT NULL, cost_price double precision NOT NULL CHECK(cost_price > 0), "
        "quantity double precision CHECK(quantity IS NULL OR quantity > 0), note text NOT NULL DEFAULT '', "
        "archived boolean NOT NULL DEFAULT false, revision integer NOT NULL DEFAULT 1, "
        "created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())"
    )
    op.execute("CREATE UNIQUE INDEX holdings_active_symbol ON holdings(symbol) WHERE NOT archived")
    op.execute(
        "CREATE TABLE holding_observations ("
        "holding_id uuid NOT NULL REFERENCES holdings(id), revision integer NOT NULL, "
        "minute timestamptz NOT NULL, data jsonb NOT NULL, "
        "PRIMARY KEY(holding_id, revision, minute))"
    )


def downgrade():
    raise RuntimeError("Restore a verified backup into a separate database; destructive downgrade is disabled.")

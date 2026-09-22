"""Initial canonical store and fenced job queue."""

from pathlib import Path
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    schema = Path(__file__).resolve().parents[2] / "schema.sql"
    for statement in schema.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup into a separate database.")

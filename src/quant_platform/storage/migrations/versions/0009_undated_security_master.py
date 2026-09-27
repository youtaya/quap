"""Allow an undated security master from the tokenless public fallback sources.

Only 东方财富 publishes a listing date (``f26``). 通达信's ``get_security_list`` and 新浪's
``Market_Center.getHQNodeData`` publish identity and name but no listing date, so the go-stock
degradation order can serve the security master with that field unknown. The value is left absent
rather than invented; ``data`` keeps the provider row so the gap stays visible, and the strict
``list_date<=day`` filters become NULL-tolerant so an unknown listing date means "listed as far as
the platform knows" instead of silently dropping the identity.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE instruments ALTER COLUMN list_date DROP NOT NULL;
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup; destructive downgrade is disabled.")

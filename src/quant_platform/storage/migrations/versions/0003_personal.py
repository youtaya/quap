"""Personal research outbox and separate read/write database roles."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE alert_outbox (event_key text PRIMARY KEY REFERENCES alert_keys(event_key) ON DELETE CASCADE, "
        "data jsonb NOT NULL, attempts integer NOT NULL DEFAULT 0, delivered_at timestamptz, last_error text)"
    )
    op.execute("""
        DO $body$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quant_read') THEN
            CREATE ROLE quant_read NOLOGIN;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quant_worker') THEN
            CREATE ROLE quant_worker NOLOGIN;
          END IF;
          GRANT quant_read, quant_worker TO CURRENT_USER;
          GRANT USAGE ON SCHEMA public TO quant_read, quant_worker;
          GRANT SELECT ON ALL TABLES IN SCHEMA public TO quant_read;
          GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO quant_worker;
          GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO quant_worker, quant_read;
          GRANT CREATE ON SCHEMA public TO quant_worker;
          ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public
            GRANT SELECT ON TABLES TO quant_read;
          ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quant_worker;
          ALTER DEFAULT PRIVILEGES FOR ROLE quant_worker IN SCHEMA public
            GRANT SELECT ON TABLES TO quant_read;
        EXCEPTION
          WHEN insufficient_privilege THEN
            RAISE NOTICE 'Database roles were not created; connections keep the login role.';
        END
        $body$
        """)


def downgrade():
    raise RuntimeError("Restore a verified backup into a separate database; destructive downgrade is disabled.")

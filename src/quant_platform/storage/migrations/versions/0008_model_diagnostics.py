"""Append-only diagnostics and frozen observation/source revisions."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE diagnostic_inputs (
      job_id bigint PRIMARY KEY REFERENCES jobs(id), prediction_id uuid NOT NULL REFERENCES prediction_runs(id),
      source_revision text NOT NULL, observed_at timestamptz NOT NULL, data jsonb NOT NULL
    );
    CREATE TABLE model_diagnostics (
      id uuid PRIMARY KEY, model_id uuid NOT NULL REFERENCES model_versions(id),
      prediction_id uuid NOT NULL REFERENCES prediction_runs(id), metric_version text NOT NULL,
      source_revision text NOT NULL, observed_at timestamptz NOT NULL, session date NOT NULL,
      artifact_id uuid NOT NULL REFERENCES research_artifacts(id), data jsonb NOT NULL,
      UNIQUE(model_id,prediction_id,metric_version,source_revision)
    );
    CREATE INDEX model_diagnostic_latest ON model_diagnostics(model_id,session DESC,observed_at DESC);
    DO $body$ BEGIN
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quant_worker') THEN
        REVOKE UPDATE,DELETE,TRUNCATE ON diagnostic_inputs,model_diagnostics FROM quant_worker;
        GRANT SELECT,INSERT ON diagnostic_inputs,model_diagnostics TO quant_worker;
      END IF;
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quant_read') THEN
        GRANT SELECT ON diagnostic_inputs,model_diagnostics TO quant_read;
      END IF;
    END $body$;
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup; destructive downgrade is disabled.")

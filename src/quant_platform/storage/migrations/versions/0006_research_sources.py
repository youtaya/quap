"""Isolated supplemental sources, immutable outcomes, and research artifact references."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE research_requests (
      request_key text PRIMARY KEY, operation text NOT NULL, request_hash text NOT NULL,
      response jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE research_sources (
      source_id text NOT NULL, revision integer NOT NULL CHECK(revision>0),
      enabled boolean NOT NULL DEFAULT false, terms_acknowledged boolean NOT NULL DEFAULT false,
      data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(source_id,revision), CHECK(NOT enabled OR terms_acknowledged)
    );
    CREATE TABLE research_artifacts (
      id uuid PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('snapshot','experiment','diagnostics')),
      path text NOT NULL UNIQUE, manifest jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE research_collections (
      id uuid PRIMARY KEY, source_id text NOT NULL, source_revision integer NOT NULL,
      job_id bigint NOT NULL UNIQUE REFERENCES jobs(id), scope jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(source_id,source_revision) REFERENCES research_sources(source_id,revision)
    );
    CREATE TABLE research_snapshots (
      id uuid PRIMARY KEY, collection_id uuid NOT NULL REFERENCES research_collections(id),
      source_id text NOT NULL, source_revision integer NOT NULL,
      symbol text NOT NULL, status text NOT NULL CHECK(status IN ('available','quarantined','unavailable','error')),
      retrieved_at timestamptz NOT NULL, event_start timestamptz, event_end timestamptz,
      historical_available_at timestamptz CHECK(historical_available_at IS NULL),
      payload_hash text NOT NULL, row_count integer NOT NULL CHECK(row_count>=0),
      artifact_id uuid NOT NULL REFERENCES research_artifacts(id), data jsonb NOT NULL,
      UNIQUE(collection_id,symbol),
      FOREIGN KEY(source_id,source_revision) REFERENCES research_sources(source_id,revision)
    );
    CREATE INDEX research_snapshot_latest ON research_snapshots(source_id,symbol,retrieved_at DESC,id);
    CREATE INDEX research_snapshot_success ON research_snapshots(source_id,symbol,retrieved_at DESC) WHERE status='available';
    CREATE TABLE research_quality_checks (
      id uuid PRIMARY KEY, snapshot_id uuid NOT NULL REFERENCES research_snapshots(id),
      metric_version text NOT NULL, canonical_watermark bigint NOT NULL,
      data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(snapshot_id,metric_version,canonical_watermark)
    );
    CREATE INDEX research_quality_latest ON research_quality_checks(created_at DESC,id);
    CREATE TABLE research_request_attempts (
      id bigserial PRIMARY KEY, collection_id uuid NOT NULL REFERENCES research_collections(id),
      symbol text NOT NULL, attempt integer NOT NULL CHECK(attempt BETWEEN 1 AND 3),
      started_at timestamptz NOT NULL DEFAULT now(), UNIQUE(collection_id,symbol,attempt)
    );
    CREATE INDEX research_quota_window ON research_request_attempts(started_at DESC);
    CREATE TABLE research_attempt_outcomes (
      attempt_id bigint PRIMARY KEY REFERENCES research_request_attempts(id),
      status text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    DO $body$ BEGIN
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quant_worker') THEN
        REVOKE UPDATE,DELETE,TRUNCATE ON research_requests,research_sources,research_artifacts,research_collections,
          research_snapshots,research_quality_checks,research_request_attempts,research_attempt_outcomes FROM quant_worker;
        GRANT SELECT,INSERT ON research_requests,research_sources,research_artifacts,research_collections,
          research_snapshots,research_quality_checks,research_request_attempts,research_attempt_outcomes TO quant_worker;
        GRANT USAGE,SELECT ON SEQUENCE research_request_attempts_id_seq TO quant_worker;
      END IF;
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quant_read') THEN
        GRANT SELECT ON research_requests,research_sources,research_artifacts,research_collections,
          research_snapshots,research_quality_checks,research_request_attempts,research_attempt_outcomes TO quant_read;
      END IF;
    END $body$;
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup; destructive downgrade is disabled.")

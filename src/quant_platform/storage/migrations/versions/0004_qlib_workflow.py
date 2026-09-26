"""Required Qlib workflow, immutable provenance, and explicit model portfolios."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE reports ADD COLUMN engine text NOT NULL DEFAULT 'legacy_native';
    CREATE TABLE scan_policies (
      revision bigserial PRIMARY KEY, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE security_states (
      symbol text NOT NULL, effective_day date NOT NULL, dataset_id bigint NOT NULL REFERENCES datasets(id),
      available_at timestamptz NOT NULL, data jsonb NOT NULL,
      PRIMARY KEY(symbol,effective_day,dataset_id)
    );
    CREATE TABLE market_constraints (
      symbol text NOT NULL, day date NOT NULL, dataset_id bigint NOT NULL REFERENCES datasets(id),
      available_at timestamptz NOT NULL, data jsonb NOT NULL, PRIMARY KEY(symbol,day,dataset_id)
    );
    CREATE TABLE minute_bars (
      symbol text NOT NULL, bar_end timestamptz NOT NULL, dataset_id bigint NOT NULL REFERENCES datasets(id),
      bar_start timestamptz NOT NULL, available_at timestamptz NOT NULL, ingested_at timestamptz NOT NULL DEFAULT now(),
      open double precision NOT NULL, high double precision NOT NULL, low double precision NOT NULL,
      close double precision NOT NULL, volume double precision NOT NULL CHECK(volume>=0),
      amount double precision NOT NULL CHECK(amount>=0), finalized boolean NOT NULL,
      PRIMARY KEY(symbol,bar_end,dataset_id), CHECK(bar_end=bar_start+interval '5 minutes')
    ) PARTITION BY RANGE(bar_end);
    CREATE TABLE minute_bars_default PARTITION OF minute_bars DEFAULT;
    CREATE INDEX minute_lookup ON minute_bars(symbol,bar_end DESC,dataset_id DESC);
    CREATE TABLE normalization_anchors (
      symbol text PRIMARY KEY, anchor double precision NOT NULL CHECK(anchor>0), day date NOT NULL,
      dataset_id bigint NOT NULL REFERENCES datasets(id)
    );
    CREATE TABLE model_portfolios (
      id uuid PRIMARY KEY, name text NOT NULL, revision integer NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE model_portfolio_revisions (
      portfolio_id uuid NOT NULL REFERENCES model_portfolios(id), revision integer NOT NULL,
      effective_from timestamptz NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(portfolio_id,revision)
    );
    INSERT INTO model_portfolios(id,name,revision) SELECT id,name,revision FROM baskets;
    INSERT INTO model_portfolio_revisions(portfolio_id,revision,effective_from,data)
      SELECT basket_id,revision,(effective_day+time '09:30') AT TIME ZONE 'Asia/Shanghai',
      jsonb_build_object('name',data->>'name','weights',data->'members','cash_weight',0,'origin','legacy_basket')
      FROM basket_revisions;
    CREATE TABLE watchlist_revisions (
      revision bigserial PRIMARY KEY, symbols jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE universe_snapshots (
      id uuid PRIMARY KEY, day date NOT NULL, content_hash text NOT NULL UNIQUE,
      data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE qlib_generations (
      id uuid PRIMARY KEY, frequency text NOT NULL CHECK(frequency IN ('day','5min')),
      watermark bigint NOT NULL, as_of timestamptz NOT NULL, contract text NOT NULL,
      path text NOT NULL UNIQUE, manifest jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE model_versions (
      id uuid PRIMARY KEY, frequency text NOT NULL CHECK(frequency IN ('day','5min')),
      generation_id uuid NOT NULL REFERENCES qlib_generations(id),
      state text NOT NULL CHECK(state IN ('challenger','shadow','active','retired','rejected')),
      contract text NOT NULL, path text NOT NULL UNIQUE, metadata jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), approved_at timestamptz, expires_at timestamptz
    );
    CREATE UNIQUE INDEX one_active_model ON model_versions(frequency) WHERE state='active';
    CREATE TABLE model_evaluations (
      model_id uuid PRIMARY KEY REFERENCES model_versions(id), data jsonb NOT NULL,
      passed boolean NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE model_shadow_samples (
      model_id uuid NOT NULL REFERENCES model_versions(id), day date NOT NULL,
      data jsonb NOT NULL, healthy boolean NOT NULL, PRIMARY KEY(model_id,day)
    );
    CREATE TABLE model_release_events (
      id bigserial PRIMARY KEY, model_id uuid NOT NULL REFERENCES model_versions(id),
      action text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE pipeline_runs (
      id uuid PRIMARY KEY, request_key text NOT NULL UNIQUE, input_hash text NOT NULL UNIQUE,
      frequency text NOT NULL CHECK(frequency IN ('day','5min')), purpose text NOT NULL,
      as_of timestamptz NOT NULL, snapshot jsonb NOT NULL, result jsonb NOT NULL DEFAULT '{}',
      state text NOT NULL DEFAULT 'waiting' CHECK(state IN ('waiting','running','blocked','failed','succeeded','superseded')),
      error text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE pipeline_steps (
      run_id uuid NOT NULL REFERENCES pipeline_runs(id), name text NOT NULL,
      job_id bigint NOT NULL UNIQUE REFERENCES jobs(id), result jsonb NOT NULL DEFAULT '{}',
      PRIMARY KEY(run_id,name)
    );
    CREATE TABLE job_dependencies (
      job_id bigint NOT NULL REFERENCES jobs(id), parent_id bigint NOT NULL REFERENCES jobs(id),
      PRIMARY KEY(job_id,parent_id), CHECK(job_id<>parent_id)
    );
    CREATE TABLE prediction_runs (
      id uuid PRIMARY KEY, run_id uuid NOT NULL UNIQUE REFERENCES pipeline_runs(id),
      model_id uuid NOT NULL REFERENCES model_versions(id), generation_id uuid NOT NULL REFERENCES qlib_generations(id),
      data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE recommendations (
      id uuid PRIMARY KEY, prediction_id uuid NOT NULL REFERENCES prediction_runs(id),
      portfolio_id uuid REFERENCES model_portfolios(id), portfolio_revision integer NOT NULL,
      policy_revision bigint NOT NULL REFERENCES scan_policies(revision),
      frequency text NOT NULL CHECK(frequency IN ('day','5min')), as_of timestamptz NOT NULL,
      available_at timestamptz NOT NULL, effective_from timestamptz NOT NULL, valid_until timestamptz NOT NULL,
      state text NOT NULL DEFAULT 'published' CHECK(state IN ('published','superseded')),
      data jsonb NOT NULL, CHECK(valid_until>effective_from), CHECK(effective_from>available_at)
    );
    CREATE INDEX recommendation_latest ON recommendations(frequency,as_of DESC);
    CREATE TABLE recommendation_acceptances (
      request_key text PRIMARY KEY, recommendation_id uuid NOT NULL REFERENCES recommendations(id),
      portfolio_id uuid NOT NULL REFERENCES model_portfolios(id), revision integer NOT NULL,
      request_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE artifact_backups (
      id uuid PRIMARY KEY, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    """)
    op.execute("""
    DO $body$ BEGIN
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quant_worker') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO quant_worker;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO quant_read;
        GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO quant_worker,quant_read;
      END IF;
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END $body$;
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup; destructive downgrade is disabled.")

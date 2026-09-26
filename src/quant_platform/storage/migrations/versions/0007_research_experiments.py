"""Versioned factors, frozen configurations, and append-only research lineage."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE factor_definitions (
      id uuid PRIMARY KEY, name text NOT NULL, revision integer NOT NULL CHECK(revision>0),
      parent_id uuid REFERENCES factor_definitions(id), data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(name,revision)
    );
    CREATE TABLE factor_sets (
      id uuid PRIMARY KEY, name text NOT NULL, revision integer NOT NULL CHECK(revision>0),
      parent_id uuid REFERENCES factor_sets(id), data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(name,revision)
    );
    CREATE TABLE factor_set_members (
      factor_set_id uuid NOT NULL REFERENCES factor_sets(id), ordinal integer NOT NULL CHECK(ordinal>=0),
      factor_id uuid NOT NULL REFERENCES factor_definitions(id), PRIMARY KEY(factor_set_id,ordinal),
      UNIQUE(factor_set_id,factor_id)
    );
    CREATE TABLE training_configurations (
      id uuid PRIMARY KEY, name text NOT NULL, revision integer NOT NULL CHECK(revision>0),
      parent_id uuid REFERENCES training_configurations(id), factor_set_id uuid REFERENCES factor_sets(id),
      frequency text NOT NULL CHECK(frequency IN ('day','5min')), data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(name,revision)
    );
    CREATE TABLE research_experiments (
      id uuid PRIMARY KEY, generation_id uuid NOT NULL REFERENCES qlib_generations(id),
      baseline_id uuid NOT NULL REFERENCES training_configurations(id),
      candidate_id uuid NOT NULL REFERENCES training_configurations(id),
      job_id bigint NOT NULL UNIQUE REFERENCES jobs(id), spec jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE research_trials (
      id uuid PRIMARY KEY, experiment_id uuid NOT NULL REFERENCES research_experiments(id),
      configuration_id uuid NOT NULL REFERENCES training_configurations(id),
      fold integer NOT NULL CHECK(fold BETWEEN 0 AND 2), fence bigint NOT NULL,
      data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(experiment_id,configuration_id,fold,fence)
    );
    CREATE TABLE research_trial_results (
      trial_id uuid PRIMARY KEY REFERENCES research_trials(id),
      state text NOT NULL CHECK(state IN ('succeeded','failed')),
      artifact_id uuid REFERENCES research_artifacts(id), data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX research_trials_experiment ON research_trials(experiment_id,configuration_id,fold);
    CREATE TABLE research_freezes (
      id uuid PRIMARY KEY, experiment_id uuid NOT NULL UNIQUE REFERENCES research_experiments(id),
      configuration_id uuid NOT NULL UNIQUE REFERENCES training_configurations(id),
      generation_id uuid NOT NULL REFERENCES qlib_generations(id), data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE research_test_exposures (
      run_id uuid PRIMARY KEY REFERENCES pipeline_runs(id), freeze_id uuid NOT NULL REFERENCES research_freezes(id),
      generation_id uuid NOT NULL REFERENCES qlib_generations(id), data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    DO $body$ BEGIN
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quant_worker') THEN
        REVOKE UPDATE,DELETE,TRUNCATE ON factor_definitions,factor_sets,factor_set_members,training_configurations,
          research_experiments,research_trials,research_trial_results,research_freezes,research_test_exposures FROM quant_worker;
        GRANT SELECT,INSERT ON factor_definitions,factor_sets,factor_set_members,training_configurations,
          research_experiments,research_trials,research_trial_results,research_freezes,research_test_exposures TO quant_worker;
      END IF;
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quant_read') THEN
        GRANT SELECT ON factor_definitions,factor_sets,factor_set_members,training_configurations,
          research_experiments,research_trials,research_trial_results,research_freezes,research_test_exposures TO quant_read;
      END IF;
    END $body$;
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup; destructive downgrade is disabled.")

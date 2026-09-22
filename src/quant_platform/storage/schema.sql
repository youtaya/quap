CREATE TABLE settings (key text PRIMARY KEY, value jsonb NOT NULL, revision integer NOT NULL DEFAULT 1);
CREATE TABLE instruments (
 symbol text PRIMARY KEY, name text NOT NULL, board text NOT NULL, exchange text NOT NULL,
 list_date date NOT NULL, delist_date date, status text NOT NULL, data jsonb NOT NULL, fetched_at timestamptz NOT NULL
);
CREATE TABLE calendars (
 exchange text NOT NULL, day date NOT NULL, is_open boolean NOT NULL, fetched_at timestamptz NOT NULL,
 PRIMARY KEY(exchange, day)
);
CREATE TABLE datasets (
 id bigserial PRIMARY KEY, endpoint text NOT NULL, scope text NOT NULL, content_hash text NOT NULL,
 fetched_at timestamptz NOT NULL DEFAULT now(), quality text NOT NULL, metadata jsonb NOT NULL
);
CREATE INDEX dataset_scope ON datasets(endpoint, scope, id DESC);
CREATE TABLE daily_bars (
 symbol text NOT NULL, day date NOT NULL, dataset_id bigint NOT NULL REFERENCES datasets(id),
 data jsonb NOT NULL, PRIMARY KEY(symbol, day, dataset_id)
);
CREATE INDEX daily_lookup ON daily_bars(symbol, day DESC, dataset_id DESC);
CREATE TABLE factors (
 symbol text NOT NULL, day date NOT NULL, dataset_id bigint NOT NULL REFERENCES datasets(id),
 factor double precision NOT NULL CHECK(factor > 0), PRIMARY KEY(symbol, day, dataset_id)
);
CREATE TABLE quotes (
 collected_at timestamptz NOT NULL, id bigint NOT NULL REFERENCES datasets(id), data jsonb NOT NULL,
 PRIMARY KEY(collected_at, id)
) PARTITION BY RANGE(collected_at);
CREATE TABLE quotes_default PARTITION OF quotes DEFAULT;
CREATE TABLE latest_quotes (symbol text PRIMARY KEY, dataset_id bigint NOT NULL, data jsonb NOT NULL);
CREATE TABLE baskets (
 id uuid PRIMARY KEY, name text NOT NULL, revision integer NOT NULL, paused boolean NOT NULL DEFAULT false,
 archived boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE basket_revisions (
 basket_id uuid NOT NULL REFERENCES baskets(id), revision integer NOT NULL,
 effective_day date NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(basket_id, revision)
);
CREATE TABLE basket_observations (
 basket_id uuid NOT NULL, revision integer NOT NULL, minute timestamptz NOT NULL,
 data jsonb NOT NULL, PRIMARY KEY(basket_id, revision, minute)
);
CREATE TABLE rules (revision bigserial PRIMARY KEY, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE reports (
 id bigserial PRIMARY KEY, kind text NOT NULL, target text NOT NULL, as_of date NOT NULL,
 input_hash text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(kind, target, as_of, input_hash)
);
CREATE INDEX report_lookup ON reports(kind, target, as_of DESC, id DESC);
CREATE TABLE alerts (
 recorded_at timestamptz NOT NULL DEFAULT now(), event_key text NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(recorded_at, event_key)
) PARTITION BY RANGE(recorded_at);
CREATE TABLE alerts_default PARTITION OF alerts DEFAULT;
CREATE TABLE alert_keys (event_key text PRIMARY KEY, recorded_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE alert_state (
 key text PRIMARY KEY, active boolean NOT NULL, observation text NOT NULL,
 last_alert timestamptz, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE jobs (
 id bigserial PRIMARY KEY, kind text NOT NULL, queue text NOT NULL, dedupe text NOT NULL UNIQUE,
 payload jsonb NOT NULL, status text NOT NULL DEFAULT 'pending', priority integer NOT NULL DEFAULT 0,
 available_at timestamptz NOT NULL DEFAULT now(), attempts integer NOT NULL DEFAULT 0,
 fence bigint NOT NULL DEFAULT 0, lease_until timestamptz, owner text,
 progress jsonb NOT NULL DEFAULT '{}', error text, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(), CHECK(status IN ('pending','running','complete','blocked','failed'))
);
CREATE INDEX job_claim ON jobs(queue, status, available_at, priority DESC);
CREATE TABLE heartbeats (worker text PRIMARY KEY, role text NOT NULL, updated_at timestamptz NOT NULL, data jsonb NOT NULL);
CREATE TABLE capabilities (
 endpoint text PRIMARY KEY, status text NOT NULL, data jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE quota (
 credential text NOT NULL, endpoint text NOT NULL, window_start timestamptz NOT NULL, used integer NOT NULL,
 PRIMARY KEY(credential, endpoint, window_start)
);
CREATE TABLE audit (id bigserial PRIMARY KEY, action text NOT NULL, target text NOT NULL, data jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE imports (source_hash text PRIMARY KEY, imported_at timestamptz NOT NULL DEFAULT now(), data jsonb NOT NULL);
CREATE TABLE incidents (key text PRIMARY KEY, data jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());

-- Domain model. Names follow the blueprint's persistence model; additions are marked (+).

CREATE TABLE sites (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, name text NOT NULL, timezone text NOT NULL,
  sim_clock timestamptz NOT NULL);                                                      -- (+) "now" for the simulated plant
SELECT enable_tenant_rls('sites');

CREATE TABLE assets (id uuid PRIMARY KEY, site_id uuid NOT NULL REFERENCES sites, parent_id uuid REFERENCES assets, asset_type text NOT NULL, manufacturer text, model text,
  criticality int NOT NULL, commissioned_on date,
  name text NOT NULL, overhauled_at timestamptz NOT NULL, economics jsonb NOT NULL, sim jsonb NOT NULL);     -- (+)
CREATE INDEX assets_site ON assets (site_id);

CREATE TABLE sensors (id uuid PRIMARY KEY, asset_id uuid NOT NULL REFERENCES assets, sensor_type text NOT NULL, unit text NOT NULL, sample_rate_hz numeric NOT NULL, axis text, metadata jsonb NOT NULL);

CREATE TABLE waveform_blobs (sha256 text PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, data bytea NOT NULL);       -- (+) stands in for object storage
SELECT enable_tenant_rls('waveform_blobs');

CREATE TABLE signal_windows (id uuid PRIMARY KEY, sensor_id uuid NOT NULL REFERENCES sensors, started_at timestamptz NOT NULL, duration_ms int NOT NULL, object_uri text NOT NULL, checksum text NOT NULL,
  shaft_hz real NOT NULL,                                                               -- (+) tachometer reading for the window
  UNIQUE (sensor_id, started_at));                                                      -- a re-sent window is ignored
CREATE TABLE features (sensor_id uuid REFERENCES sensors, window_id uuid REFERENCES signal_windows ON DELETE CASCADE, feature_set_version text, feature_json jsonb NOT NULL,
  PRIMARY KEY (sensor_id, window_id, feature_set_version));

CREATE TABLE anomaly_scores (id uuid PRIMARY KEY, asset_id uuid NOT NULL REFERENCES assets, scored_at timestamptz NOT NULL, score numeric NOT NULL, model_version text NOT NULL, evidence jsonb NOT NULL);
CREATE INDEX anomaly_asset ON anomaly_scores (asset_id, scored_at);
CREATE TABLE rul_predictions (id uuid PRIMARY KEY, asset_id uuid NOT NULL REFERENCES assets, predicted_at timestamptz NOT NULL, hours_p50 numeric NOT NULL, hours_p10 numeric NOT NULL, hours_p90 numeric NOT NULL,
  failure_mode text, model_version text NOT NULL);
CREATE INDEX rul_asset ON rul_predictions (asset_id, predicted_at);

CREATE TABLE alerts (id uuid PRIMARY KEY, asset_id uuid NOT NULL REFERENCES assets, opened_at timestamptz NOT NULL, severity text NOT NULL, message text NOT NULL,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','closed')), acknowledged_by uuid, acknowledged_at timestamptz);                    -- (+)

CREATE TABLE maintenance_windows (id uuid PRIMARY KEY, asset_id uuid NOT NULL REFERENCES assets, start_at timestamptz NOT NULL, end_at timestamptz NOT NULL, expected_cost numeric NOT NULL,
  avoided_risk numeric NOT NULL, status text NOT NULL);
CREATE TABLE work_orders (id uuid PRIMARY KEY, asset_id uuid NOT NULL REFERENCES assets, external_id text NOT NULL, work_type text NOT NULL, opened_at timestamptz NOT NULL, completed_at timestamptz,
  findings jsonb NOT NULL, maintenance_window_id uuid REFERENCES maintenance_windows);                                                                            -- (+)
CREATE TABLE model_labels (id uuid PRIMARY KEY, asset_id uuid NOT NULL REFERENCES assets, label_type text NOT NULL, failure_mode text, occurred_at timestamptz NOT NULL, source text NOT NULL);

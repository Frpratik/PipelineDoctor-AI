-- Core tables. Re-running this file resets them (DROP first), so setup is repeatable.
-- Later steps append their own tables to this file.
DROP TABLE IF EXISTS agent_steps CASCADE;
DROP TABLE IF EXISTS watch_alerts CASCADE;
DROP TABLE IF EXISTS watch_check_results CASCADE;
DROP TABLE IF EXISTS table_profiles CASCADE;
DROP TABLE IF EXISTS watched_tables CASCADE;
DROP TABLE IF EXISTS connections CASCADE;
DROP TABLE IF EXISTS eval_results CASCADE;
DROP TABLE IF EXISTS jobs CASCADE;
DROP TABLE IF EXISTS incident_memory CASCADE;
DROP TABLE IF EXISTS fix_proposals CASCADE;
DROP TABLE IF EXISTS verdicts CASCADE;
DROP TABLE IF EXISTS diagnoses CASCADE;
DROP TABLE IF EXISTS ground_truth CASCADE;
DROP TABLE IF EXISTS incidents CASCADE;
DROP TABLE IF EXISTS check_results CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS pipeline_runs CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS customers CASCADE;

CREATE TABLE customers (
    customer_id  INT PRIMARY KEY,
    name         TEXT NOT NULL,
    city         TEXT NOT NULL,
    signup_date  DATE NOT NULL
);

CREATE TABLE products (
    product_id  INT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    price_inr   NUMERIC(10, 2) NOT NULL
);

-- One row per ETL execution. The detector (Step 3) compares runs using this.
CREATE TABLE pipeline_runs (
    run_id        SERIAL PRIMARY KEY,
    run_date      DATE NOT NULL,          -- which day's feed was loaded
    rows_in       INT,                    -- rows in the CSV
    rows_loaded   INT,                    -- rows that reached the orders table
    status        TEXT NOT NULL,          -- running / success / failed
    duration_sec  NUMERIC(8, 2),
    started_at    TIMESTAMP NOT NULL DEFAULT now()
);

-- One row per check per run (passed or failed), written by the detector.
CREATE TABLE check_results (
    check_id    SERIAL PRIMARY KEY,
    run_id      INT NOT NULL REFERENCES pipeline_runs(run_id),
    check_name  TEXT NOT NULL,
    passed      BOOLEAN NOT NULL,
    details     TEXT NOT NULL,            -- what the check measured, in plain words
    created_at  TIMESTAMP NOT NULL DEFAULT now()
);

-- Opened by the detector when at least one check fails. Agents work on incidents.
CREATE TABLE incidents (
    incident_id  SERIAL PRIMARY KEY,
    run_id       INT NOT NULL REFERENCES pipeline_runs(run_id),
    status       TEXT NOT NULL DEFAULT 'open',
    summary      TEXT NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT now()
);

-- One row per Investigator attempt (the Verifier can send it back, so an incident may have several).
CREATE TABLE diagnoses (
    diagnosis_id  SERIAL PRIMARY KEY,
    incident_id   INT NOT NULL REFERENCES incidents(incident_id),
    attempt       INT NOT NULL,           -- 1 = first try, 2 = after feedback ...
    diagnosis     JSONB NOT NULL,         -- the Diagnosis model as JSON
    created_at    TIMESTAMP NOT NULL DEFAULT now()
);

-- The Verifier's judgement of each diagnosis attempt.
CREATE TABLE verdicts (
    verdict_id   SERIAL PRIMARY KEY,
    incident_id  INT NOT NULL REFERENCES incidents(incident_id),
    attempt      INT NOT NULL,            -- which diagnosis attempt this judges
    accepted     BOOLEAN NOT NULL,
    problems     JSONB NOT NULL,          -- list of problems found
    feedback     TEXT NOT NULL,           -- what the Investigator should do next
    created_at   TIMESTAMP NOT NULL DEFAULT now()
);

-- The Fixer's proposal. Nothing touches real data until a human approves it.
CREATE TABLE fix_proposals (
    fix_id         SERIAL PRIMARY KEY,
    incident_id    INT NOT NULL REFERENCES incidents(incident_id),
    fix_type       TEXT NOT NULL,         -- update_rows / delete_rows / manual
    description    TEXT NOT NULL,
    sql_or_steps   TEXT NOT NULL,
    risk_notes     TEXT NOT NULL,
    status         TEXT NOT NULL,         -- pending / applied / approved (manual) / rejected / failed
    dry_run_result JSONB,                 -- before/after comparison from the dry run on a copy
    created_at     TIMESTAMP NOT NULL DEFAULT now(),
    decided_at     TIMESTAMP              -- when a human approved or rejected it
);

-- Memory of resolved incidents. The embedding (384 numbers from all-MiniLM-L6-v2) is of the symptoms.
CREATE TABLE incident_memory (
    memory_id    SERIAL PRIMARY KEY,
    incident_id  INT NOT NULL REFERENCES incidents(incident_id),
    symptoms     TEXT NOT NULL,           -- the failed checks, as text
    root_cause   TEXT NOT NULL,
    fix          TEXT NOT NULL,           -- what repaired it
    embedding    vector(384) NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT now()
);

-- One row per background task. The API returns job_id at once; the client polls this row.
CREATE TABLE jobs (
    job_id       SERIAL PRIMARY KEY,
    job_type     TEXT NOT NULL,           -- run_pipeline / investigate_incident / apply_fix / run_eval
    incident_id  INT REFERENCES incidents(incident_id),
    status       TEXT NOT NULL DEFAULT 'queued',   -- queued / running / done / failed
    progress     TEXT,                    -- latest message, e.g. "Investigator: called get_schema_diff"
    error        TEXT,
    created_at   TIMESTAMP NOT NULL DEFAULT now(),
    started_at   TIMESTAMP,
    finished_at  TIMESTAMP
);

-- One row per eval case. eval_id is the job_id of the eval job that produced it.
CREATE TABLE eval_results (
    result_id          SERIAL PRIMARY KEY,
    eval_id            INT NOT NULL,
    model              TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,
    fault_type         TEXT NOT NULL,         -- e.g. currency_unit or currency_unit+null_spike
    case_no            INT NOT NULL,
    incident_id        INT,
    verdict            TEXT NOT NULL,         -- correct / partial / wrong / undetected / error
    judge_reason       TEXT,
    root_cause         TEXT,                  -- the diagnosis that was judged
    verifier_rejections INT,
    tool_calls         INT,
    tokens             INT,
    seconds            NUMERIC(8, 1),
    error              TEXT,
    created_at         TIMESTAMP NOT NULL DEFAULT now()
);

-- ---- Watching a customer's own database (no LLM involved in any of this) ----

-- A database the user connected. The password is stored encrypted (app/vault.py).
CREATE TABLE connections (
    connection_id       SERIAL PRIMARY KEY,
    name                TEXT NOT NULL,
    host                TEXT NOT NULL,
    port                INT NOT NULL,
    database            TEXT NOT NULL,
    username            TEXT NOT NULL,
    password_encrypted  TEXT NOT NULL,
    read_only           BOOLEAN NOT NULL,     -- result of the privilege check at connect time
    warnings            JSONB NOT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT now()
);

-- A table the user chose to watch, with the columns they confirmed.
CREATE TABLE watched_tables (
    watch_id         SERIAL PRIMARY KEY,
    connection_id    INT NOT NULL REFERENCES connections(connection_id) ON DELETE CASCADE,
    schema_name      TEXT NOT NULL,
    table_name       TEXT NOT NULL,
    time_column      TEXT NOT NULL,           -- says when a row was added; days are built from it
    key_column       TEXT,                    -- used to find duplicates
    excluded_columns TEXT[] NOT NULL DEFAULT '{}',   -- never read (personal data)
    columns          JSONB NOT NULL,          -- columns at watch time, to notice schema changes
    created_at       TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (connection_id, schema_name, table_name)
);

-- What each day looked like: row count, null rates, averages ... (the "normal" we compare against).
CREATE TABLE table_profiles (
    profile_id  SERIAL PRIMARY KEY,
    watch_id    INT NOT NULL REFERENCES watched_tables(watch_id) ON DELETE CASCADE,
    day         DATE NOT NULL,
    metrics     JSONB NOT NULL,
    profiled_at TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (watch_id, day)
);

CREATE TABLE watch_check_results (
    result_id   SERIAL PRIMARY KEY,
    watch_id    INT NOT NULL REFERENCES watched_tables(watch_id) ON DELETE CASCADE,
    day         DATE NOT NULL,
    check_name  TEXT NOT NULL,
    passed      BOOLEAN NOT NULL,
    details     TEXT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT now()
);

-- Opened when a watched table fails a check for a day (the watching version of an incident).
CREATE TABLE watch_alerts (
    alert_id    SERIAL PRIMARY KEY,
    watch_id    INT NOT NULL REFERENCES watched_tables(watch_id) ON DELETE CASCADE,
    day         DATE NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    summary     TEXT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (watch_id, day)
);

-- Every step any agent takes while working on an incident (the "flight recorder").
CREATE TABLE agent_steps (
    step_id      SERIAL PRIMARY KEY,
    incident_id  INT NOT NULL REFERENCES incidents(incident_id),
    agent_name   TEXT NOT NULL,           -- investigator / verifier / fixer
    step_no      INT NOT NULL,            -- 1, 2, 3 ... per incident, across all agents
    action       TEXT NOT NULL,           -- tool_call / llm_call / memory_lookup ...
    tool_name    TEXT,
    tool_input   TEXT,
    tool_output  TEXT,
    tokens       INT NOT NULL DEFAULT 0,
    duration_ms  INT NOT NULL DEFAULT 0,
    created_at   TIMESTAMP NOT NULL DEFAULT now()
);

-- The fault we injected into a day's feed. Used ONLY by evals; agents must never read it.
CREATE TABLE ground_truth (
    gt_id       SERIAL PRIMARY KEY,
    run_date    DATE NOT NULL,
    fault_types TEXT NOT NULL,            -- e.g. currency_unit or currency_unit+null_spike
    details     TEXT NOT NULL,            -- plain-English description of what was broken
    created_at  TIMESTAMP NOT NULL DEFAULT now()
);

-- run_id lets us trace every order back to the run that loaded it.
CREATE TABLE orders (
    order_id     INT PRIMARY KEY,
    customer_id  INT NOT NULL REFERENCES customers(customer_id),
    product_id   INT NOT NULL REFERENCES products(product_id),
    quantity     INT NOT NULL,
    amount_inr   NUMERIC(12, 2) NOT NULL,
    order_ts     TIMESTAMP NOT NULL,
    run_id       INT REFERENCES pipeline_runs(run_id)
);

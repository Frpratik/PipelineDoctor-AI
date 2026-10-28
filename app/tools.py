"""Tools the agents can call. Plain Python functions: each takes simple arguments and returns a SHORT string
(JSON or text), so it fits comfortably in an LLM prompt. Errors are returned as text, never raised, so the
agent can read the message and try again.
"""

import json
import re

import pandas as pd

from app.db import get_conn, query_df
from app.detector import get_healthy_runs, read_feed
from app.pipeline import LOG_DIR

MAX_ROWS = 50            # hard cap on rows returned by run_readonly_sql
MAX_LOG_CHARS = 3000     # keep log output small
SQL_TIMEOUT_MS = 5000    # a runaway query is killed after 5 seconds
FORBIDDEN_WORDS = [
    "insert", "update", "delete", "drop", "alter", "create", "truncate", "grant", "revoke",
    "copy", "call", "execute", "merge", "vacuum", "set", "into",
]
HIDDEN_TABLE = "ground_truth"   # answer key for evals; agents must never see it
STATS_TABLES = ["orders", "customers", "products"]


def _json(data) -> str:
    return json.dumps(data, default=str)  # default=str turns dates and Decimals into text


def _get_run(run_id: int):
    rows = query_df("SELECT * FROM pipeline_runs WHERE run_id = %s", (run_id,))
    return None if rows.empty else rows.iloc[0]


def run_readonly_sql(sql: str, limit: int = MAX_ROWS) -> str:
    """Run ONE SELECT query against the database and return up to `limit` rows as JSON."""
    text = sql.strip().rstrip(";").strip()
    lowered = text.lower()
    # Layer 1: reject anything that is not a single SELECT.
    if ";" in text:
        return "ERROR: only a single statement is allowed"
    if not lowered.startswith("select"):
        return "ERROR: only SELECT queries are allowed"
    for word in FORBIDDEN_WORDS:
        if re.search(rf"\b{word}\b", lowered):
            return f"ERROR: '{word}' is not allowed in a read-only query"
    if HIDDEN_TABLE in lowered:
        return f"ERROR: table '{HIDDEN_TABLE}' is not available"

    limit = max(1, min(int(limit), MAX_ROWS))
    conn = get_conn()
    try:
        # Layer 2: even if layer 1 missed something, Postgres itself refuses writes in a read-only session.
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {SQL_TIMEOUT_MS}")
        cur.execute(f"SELECT * FROM ({text}) AS q LIMIT {limit + 1}")  # +1 tells us if rows were cut off
        columns = [c[0] for c in cur.description]
        rows = cur.fetchall()
    except Exception as e:
        return f"ERROR: {str(e).strip().splitlines()[0]}"
    finally:
        conn.rollback()
        conn.close()
    return _json({"columns": columns, "rows": rows[:limit], "row_count": min(len(rows), limit), "truncated": len(rows) > limit})


def get_schema_diff(run_id: int) -> str:
    """Compare the columns of a run's vendor feed with the previous healthy run's feed."""
    run = _get_run(run_id)
    if run is None:
        return f"ERROR: run {run_id} not found"
    previous = get_healthy_runs(run["run_date"])
    if previous.empty:
        return "ERROR: no earlier healthy run to compare with"
    prev_date = previous.iloc[-1]["run_date"]
    current = list(read_feed(run["run_date"]).columns)
    before = list(read_feed(prev_date).columns)
    return _json({
        "run_date": run["run_date"], "compared_with": prev_date,
        "current_columns": current, "previous_columns": before,
        "added": [c for c in current if c not in before], "removed": [c for c in before if c not in current],
    })


def read_pipeline_log(run_id: int) -> str:
    """Return the text log written by the pipeline for one run."""
    files = list(LOG_DIR.glob(f"run_{run_id}_*.log"))
    if not files:
        return f"ERROR: no log found for run {run_id}"
    return files[0].read_text(encoding="utf-8")[:MAX_LOG_CHARS]


def get_recent_runs(n: int = 7) -> str:
    """Return the last n pipeline runs (newest first) with row counts and status."""
    n = max(1, min(int(n), 20))
    runs = query_df(
        "SELECT run_id, run_date, rows_in, rows_loaded, status, duration_sec FROM pipeline_runs "
        "ORDER BY run_id DESC LIMIT %s", (n,))
    return _json(runs.to_dict("records"))


def get_column_stats(table: str, column: str, run_id: int = None) -> str:
    """Summary statistics for one column. For the orders table, run_id limits it to rows loaded by that run."""
    if table not in STATS_TABLES:
        return f"ERROR: table must be one of {STATS_TABLES}"
    types = query_df(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s", (table,))
    types = dict(zip(types["column_name"], types["data_type"]))
    if column not in types:   # also protects the query below from injected column names
        return f"ERROR: column must be one of {list(types)}"

    where, params = "", None
    if run_id is not None and table == "orders":
        where, params = " WHERE run_id = %s", (run_id,)
    values = query_df(f'SELECT "{column}" AS v FROM {table}{where}', params)["v"]

    stats = {"table": table, "column": column, "run_id": run_id, "rows": len(values), "nulls": int(values.isna().sum())}
    if len(values) == stats["nulls"]:
        return _json(stats)
    if types[column] in ("integer", "numeric", "bigint", "double precision"):
        nums = values.dropna().astype(float)
        stats.update(min=nums.min(), max=nums.max(), mean=round(nums.mean(), 2),
                     median=nums.median(), p95=nums.quantile(0.95))
    elif "timestamp" in types[column] or types[column] == "date":
        stats.update(min=values.min(), max=values.max())
    else:
        stats.update(distinct=int(values.nunique()), top_values=values.value_counts().head(5).to_dict())
    return _json(stats)


def _run_summary(run) -> dict:
    """Numbers describing one run: pipeline_runs row + stats of the orders it loaded + its feed columns."""
    orders = query_df(
        "SELECT COUNT(*) AS orders, ROUND(AVG(amount_inr), 2) AS avg_amount, "
        "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount_inr) AS median_amount, "
        "MIN(amount_inr) AS min_amount, MAX(amount_inr) AS max_amount, ROUND(AVG(quantity), 2) AS avg_quantity, "
        "COUNT(DISTINCT customer_id) AS customers, COUNT(DISTINCT product_id) AS products, "
        "MIN(order_ts) AS first_order, MAX(order_ts) AS last_order FROM orders WHERE run_id = %s",
        (int(run["run_id"]),),
    ).iloc[0].to_dict()
    summary = {"run_id": int(run["run_id"]), "run_date": run["run_date"], "status": run["status"],
               "rows_in": run["rows_in"], "rows_loaded": run["rows_loaded"], **orders}
    summary["feed_columns"] = list(read_feed(run["run_date"]).columns)
    return summary


def compare_runs(run_id_a: int, run_id_b: int) -> str:
    """Side-by-side numbers for two runs, e.g. a bad run vs a good one."""
    runs = [_get_run(r) for r in (run_id_a, run_id_b)]
    if any(r is None for r in runs):
        return "ERROR: one of the runs was not found"
    return _json({"a": _run_summary(runs[0]), "b": _run_summary(runs[1])})


# Name -> function. Step 6 gives this list to the Investigator.
TOOLS = {f.__name__: f for f in [
    run_readonly_sql, get_schema_diff, read_pipeline_log, get_recent_runs, get_column_stats, compare_runs]}

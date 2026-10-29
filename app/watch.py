"""Watching a customer's table: save the setup, learn what a normal day looks like, and check the latest day.

No LLM anywhere in this file. The flow is: add_watch -> run_watch (profile the history, then check yesterday).
"""

import json
import statistics

from app import targets, vault
from app.db import get_conn, query_df
from app.targets import NUMERIC_TYPES, quote

HISTORY_DAYS = 30          # how many past days we profile
MIN_BASELINE_DAYS = 5      # fewer healthy days than this = still "learning", no alerts
MAX_METRIC_COLUMNS = 6     # numeric columns we track averages for (keeps the query small)

# Thresholds. A tolerance is the bigger of a fixed minimum and 3x how much the table normally varies.
ROW_COUNT_TOLERANCE = 0.25
VALUE_TOLERANCE = 0.40
VARIATION_FACTOR = 3
NULL_RATE_JUMP = 0.05      # null rate may rise by at most 5 percentage points
DUPLICATE_RATE = 0.01
PARTIAL_HOURS = 2          # the last row may be at most 2 hours earlier than usual


# ---------- connections ----------

def save_connection(name, host, port, database, user, password):
    """Connect (this fails with a clear error if the details are wrong), check safety, save with an encrypted password."""
    conn = targets.connect(host, port, database, user, password)
    try:
        access = targets.check_access(conn)
    finally:
        conn.close()
    db = get_conn()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO connections (name, host, port, database, username, password_encrypted, read_only, warnings) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING connection_id",
        (name, host, port, database, user, vault.encrypt(password), access["read_only"], json.dumps(access["warnings"])))
    connection_id = cur.fetchone()[0]
    db.commit()
    db.close()
    return connection_id, access


def open_target(connection_id: int):
    rows = query_df("SELECT * FROM connections WHERE connection_id = %s", (connection_id,))
    if rows.empty:
        raise ValueError(f"connection {connection_id} not found")
    c = rows.iloc[0]
    return targets.connect(c["host"], int(c["port"]), c["database"], c["username"], vault.decrypt(c["password_encrypted"]))


def scan_connection(connection_id: int) -> list:
    conn = open_target(connection_id)
    try:
        return targets.scan(conn)
    finally:
        conn.close()


# ---------- choosing what to watch ----------

def add_watch(connection_id, schema, table, time_column, key_column=None, excluded=()) -> int:
    """Save a table to watch. Everything is checked against a fresh scan, so names can never be made up."""
    found = [t for t in scan_connection(connection_id) if t["schema"] == schema and t["table"] == table]
    if not found:
        raise ValueError(f"table {schema}.{table} not found")
    columns = {c["name"]: c["type"] for c in found[0]["columns"]}
    if time_column not in columns or not (columns[time_column].startswith("timestamp") or columns[time_column] == "date"):
        raise ValueError(f"time_column must be a date or timestamp column of {table}")
    if key_column and key_column not in columns:
        raise ValueError(f"key_column '{key_column}' not found in {table}")
    unknown = [c for c in excluded if c not in columns]
    if unknown:
        raise ValueError(f"unknown excluded column(s): {unknown}")
    if time_column in excluded or key_column in excluded:
        raise ValueError("the time column and the key column cannot be excluded")

    db = get_conn()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO watched_tables (connection_id, schema_name, table_name, time_column, key_column, excluded_columns, columns) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING watch_id",
        (connection_id, schema, table, time_column, key_column or None, list(excluded), json.dumps(found[0]["columns"])))
    watch_id = cur.fetchone()[0]
    db.commit()
    db.close()
    return watch_id


def get_watch(watch_id: int) -> dict:
    rows = query_df("SELECT * FROM watched_tables WHERE watch_id = %s", (watch_id,))
    if rows.empty:
        raise ValueError(f"watched table {watch_id} not found")
    watch = rows.iloc[0].to_dict()
    return {k: (None if isinstance(v, float) and v != v else v) for k, v in watch.items()}   # pandas NaN -> None


# ---------- profiling: what did each day look like? ----------

def _columns(watch: dict):
    """(columns to track null rates for, numeric columns to track averages for). Excluded columns are never touched."""
    allowed = [c for c in watch["columns"] if c["name"] not in watch["excluded_columns"] and c["name"] != watch["time_column"]]
    numeric = [c["name"] for c in allowed
               if c["type"] in NUMERIC_TYPES and not c["name"].lower().endswith("id") and c["name"] != watch["key_column"]]
    return [c["name"] for c in allowed], numeric[:MAX_METRIC_COLUMNS]


def _profile_sql(watch: dict):
    null_columns, numeric = _columns(watch)
    time_col = quote(watch["time_column"])
    parts = [f"date_trunc('day', {time_col})::date AS day", "COUNT(*) AS row_count"]
    parts += [f"COUNT(*) - COUNT({quote(c)}) AS null_{i}" for i, c in enumerate(null_columns)]
    parts += [f"AVG({quote(c)})::float8 AS mean_{i}" for i, c in enumerate(numeric)]
    if watch["key_column"]:
        parts.append(f"COUNT({quote(watch['key_column'])}) - COUNT(DISTINCT {quote(watch['key_column'])}) AS dup_rows")
    # Look-alike rows: identical on every watched column, even if the key differs (copies that got new ids).
    signature = [f"{quote(c['name'])}::text" for c in watch["columns"]
                 if c["name"] not in watch["excluded_columns"] and c["name"] != watch["key_column"]][:12]
    if signature:
        parts.append(f"COUNT(*) - COUNT(DISTINCT concat_ws('|', {', '.join(signature)})) AS lookalike_rows")
    time_type = next(c["type"] for c in watch["columns"] if c["name"] == watch["time_column"])
    if time_type.startswith("timestamp"):   # a plain date has no time of day, so no "day ended early" check
        parts.append(f"EXTRACT(EPOCH FROM (MAX({time_col}) - date_trunc('day', MAX({time_col})))) / 3600 AS last_hour")
    sql = (f"SELECT {', '.join(parts)} FROM {quote(watch['schema_name'])}.{quote(watch['table_name'])} "
           f"WHERE {time_col} >= date_trunc('day', now()) - interval '{HISTORY_DAYS} days' "
           f"AND {time_col} < date_trunc('day', now()) GROUP BY 1 ORDER BY 1")   # complete days only, today is unfinished
    return sql, null_columns, numeric


def profile_table(watch: dict):
    """Read per-day numbers from the target and save them. Returns yesterday's date on the target's clock."""
    sql, null_columns, numeric = _profile_sql(watch)
    conn = open_target(int(watch["connection_id"]))
    try:
        cur = conn.cursor()
        cur.execute("SELECT now()::date - 1")
        yesterday = cur.fetchone()[0]
        cur.execute(sql)
        names = [c[0] for c in cur.description]
        days = [dict(zip(names, row)) for row in cur.fetchall()]
    finally:
        conn.close()

    db = get_conn()
    cur = db.cursor()
    for d in days:
        n = d["row_count"]
        columns = {c: {"null_rate": d[f"null_{i}"] / n} for i, c in enumerate(null_columns)}
        for i, c in enumerate(numeric):
            columns[c]["mean"] = d[f"mean_{i}"]
        metrics = {"row_count": n, "columns": columns, "last_hour": d.get("last_hour"),
                   "dup_rate": d["dup_rows"] / n if "dup_rows" in d else None,
                   "lookalike_rate": d["lookalike_rows"] / n if "lookalike_rows" in d else None}
        cur.execute(
            "INSERT INTO table_profiles (watch_id, day, metrics) VALUES (%s, %s, %s::jsonb) "
            "ON CONFLICT (watch_id, day) DO UPDATE SET metrics = EXCLUDED.metrics, profiled_at = now()",
            (int(watch["watch_id"]), d["day"], json.dumps(metrics, default=float)))
    db.commit()
    db.close()
    return yesterday


# ---------- checks: is the latest day different from normal? ----------

def _median(values):
    return statistics.median(values)


def _variation(values) -> float:
    """How much the values normally wiggle, relative to their size (robust: a few odd days barely move it)."""
    middle = _median(values)
    if not middle:
        return 0.0
    return 1.4826 * _median([abs(v - middle) for v in values]) / abs(middle)


def _tolerance(minimum: float, values) -> float:
    return max(minimum, VARIATION_FACTOR * _variation(values))


def generic_checks(watch: dict, today: dict, history: list, current_columns: list) -> list:
    """Compare the latest day (`today`) with the healthy days before it. Pure function: no database access."""
    results = []

    def add(name, passed, details):
        results.append({"check_name": name, "passed": bool(passed), "details": details})

    saved = [c["name"] for c in watch["columns"]]
    added, removed = sorted(set(current_columns) - set(saved)), sorted(set(saved) - set(current_columns))
    add("schema_change", not (added or removed),
        f"columns changed. added: {added}, removed: {removed}" if added or removed else "columns unchanged")

    counts = [h["row_count"] for h in history]
    usual = _median(counts)
    if today is None or today["row_count"] == 0:
        add("row_count_change", False, f"no rows at all for the day (usual about {usual:.0f})")
        return results
    if usual:
        tolerance = _tolerance(ROW_COUNT_TOLERANCE, counts)
        change = (today["row_count"] - usual) / usual
        add("row_count_change", abs(change) <= tolerance,
            f"{today['row_count']} rows, usual about {usual:.0f} ({change:+.0%}, allowed +/-{tolerance:.0%})")

    problems = []
    for column, now in today["columns"].items():
        before = [h["columns"][column]["null_rate"] for h in history if column in h["columns"]]
        if before and now["null_rate"] > _median(before) + NULL_RATE_JUMP:
            problems.append(f"{column}: {now['null_rate']:.0%} null (usually {_median(before):.0%})")
    add("null_rate", not problems, "; ".join(problems) if problems else "null rates are normal")

    # Only the mean: it is stable. A median of a small whole-number column (like quantity 1-4) just flips between values.
    problems, notes = [], []
    for column, now in today["columns"].items():
        if now.get("mean") is None:
            continue
        base = [h["columns"][column]["mean"] for h in history if h["columns"].get(column, {}).get("mean") is not None]
        if not base or not _median(base):
            continue
        ratio = now["mean"] / _median(base)
        tolerance = _tolerance(VALUE_TOLERANCE, base)
        if abs(ratio - 1) > tolerance:
            problems.append(f"{column} average {now['mean']:.4g} (usual {_median(base):.4g}, x{ratio:.2f}, allowed +/-{tolerance:.0%})")
        notes.append(column)
    add("value_distribution", not problems, "; ".join(problems) if problems else f"averages normal for {', '.join(notes) or 'no numeric columns'}")

    if watch["key_column"] and today["dup_rate"] is not None:
        add("duplicate_keys", today["dup_rate"] <= DUPLICATE_RATE,
            f"{today['dup_rate']:.1%} of rows repeat a value of {watch['key_column']}")

    if today["lookalike_rate"] is not None:
        usual_rate = _median([h["lookalike_rate"] for h in history if h["lookalike_rate"] is not None] or [0])
        add("lookalike_rows", today["lookalike_rate"] <= usual_rate + DUPLICATE_RATE,
            f"{today['lookalike_rate']:.1%} of rows are copies of another row (usually {usual_rate:.1%}), even if their ids differ")

    hours = [h["last_hour"] for h in history if h["last_hour"] is not None]
    if today["last_hour"] is not None and hours:
        usual_hour = _median(hours)
        add("late_or_partial", today["last_hour"] >= usual_hour - PARTIAL_HOURS,
            f"last row at {int(today['last_hour'])}:{int(today['last_hour'] % 1 * 60):02d}, usually about {int(usual_hour)}:00")
    return results


def _current_columns(watch: dict) -> list:
    conn = open_target(int(watch["connection_id"]))
    try:
        cur = conn.cursor()
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
                    (watch["schema_name"], watch["table_name"]))
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def run_watch(watch_id: int) -> dict:
    """Profile the table's recent history, then check the latest complete day and save the results."""
    watch = get_watch(watch_id)
    yesterday = profile_table(watch)
    profiles = query_df("SELECT day, metrics FROM table_profiles WHERE watch_id = %s ORDER BY day", (watch_id,))
    failed_days = set(query_df("SELECT DISTINCT day FROM watch_check_results WHERE watch_id = %s AND NOT passed", (watch_id,))["day"])

    by_day = dict(zip(profiles["day"], profiles["metrics"]))
    today = by_day.get(yesterday)
    # The baseline is healthy days before the checked day, so a bad day never becomes "normal".
    history = [m for d, m in by_day.items() if d < yesterday and d not in failed_days][-HISTORY_DAYS:]
    if len(history) < MIN_BASELINE_DAYS:
        return {"day": str(yesterday), "status": "learning", "results": [], "alert_id": None,
                "message": f"learning: {len(history)} of {MIN_BASELINE_DAYS} healthy days needed before checking"}

    results = generic_checks(watch, today, history, _current_columns(watch))
    failed = [r["check_name"] for r in results if not r["passed"]]

    db = get_conn()
    cur = db.cursor()
    cur.execute("DELETE FROM watch_check_results WHERE watch_id = %s AND day = %s", (watch_id, yesterday))
    for r in results:
        cur.execute("INSERT INTO watch_check_results (watch_id, day, check_name, passed, details) VALUES (%s, %s, %s, %s, %s)",
                    (watch_id, yesterday, r["check_name"], r["passed"], r["details"]))
    alert_id = None
    if failed:
        summary = f"{watch['schema_name']}.{watch['table_name']} on {yesterday}: {len(failed)} check(s) failed: {', '.join(failed)}"
        cur.execute("INSERT INTO watch_alerts (watch_id, day, summary) VALUES (%s, %s, %s) "
                    "ON CONFLICT (watch_id, day) DO UPDATE SET summary = EXCLUDED.summary RETURNING alert_id",
                    (watch_id, yesterday, summary))
        alert_id = cur.fetchone()[0]
    else:   # the day is fine now (for example after fixing the data), so close any old alert for it
        cur.execute("DELETE FROM watch_alerts WHERE watch_id = %s AND day = %s", (watch_id, yesterday))
    db.commit()
    db.close()
    return {"day": str(yesterday), "status": "alert" if failed else "ok", "results": results, "alert_id": alert_id,
            "message": f"{len(failed)} check(s) failed" if failed else "all checks passed"}

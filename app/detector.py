"""Rule-based data-quality checks (no LLM). Compares the latest run with earlier healthy runs.

Each check is a small pure function: data in -> (passed, details) out. That keeps them easy to test.
detect(run_id) gathers the data, runs every check, saves the results and opens an incident if any failed.
"""

import pandas as pd

from app.db import get_conn, query_df
from app.pipeline import FEED_DIR

# Thresholds. Clean days differ by only a few percent, so these leave plenty of room before alarming.
NULL_RATE_JUMP = 0.05       # null rate may rise by at most 5 percentage points
DUPLICATE_RATE = 0.01       # at most 1% repeated order_ids
ROW_COUNT_CHANGE = 0.25     # rows_loaded may differ from the usual by at most 25%
MEAN_MEDIAN_RATIO = (0.7, 1.4)   # today's mean/median vs the usual
PERCENTILE_RATIO = (0.5, 2.0)    # p95 is noisier (few distinct values), so a wider band
PARTIAL_HOURS = 2           # last order may be at most 2h earlier than on normal days
FANOUT_RATE = 0.01          # at most 1% extra rows sharing the same business key


# ---------- the checks (pure functions) ----------

def check_schema(columns, prev_columns):
    added = sorted(set(columns) - set(prev_columns))
    removed = sorted(set(prev_columns) - set(columns))
    if added or removed:
        return False, f"columns changed. added: {added}, removed: {removed}"
    return True, f"columns unchanged: {list(columns)}"


def check_null_rate(feed, prev_feeds):
    """Null rate per column in the raw feed, compared with the average of earlier feeds."""
    problems = []
    for col in feed.columns:
        today = feed[col].isna().mean()
        usual = sum(f[col].isna().mean() for f in prev_feeds if col in f.columns) / len(prev_feeds)
        if today > usual + NULL_RATE_JUMP:
            problems.append(f"{col}: {today:.0%} null (usually {usual:.0%})")
    if problems:
        return False, "null rate jumped: " + "; ".join(problems)
    return True, "null rates are normal"


def check_duplicate_keys(feed):
    """Repeated order_id values in the raw feed (the pipeline silently drops them, so we look at the feed)."""
    dupes = int(feed.duplicated("order_id").sum())
    rate = dupes / len(feed)
    if rate > DUPLICATE_RATE:
        return False, f"{dupes} of {len(feed)} feed rows ({rate:.0%}) repeat an order_id"
    return True, f"{dupes} duplicate order_ids"


def check_row_count(rows_loaded, rows_in, prev_loaded):
    usual = sum(prev_loaded) / len(prev_loaded)
    change = (rows_loaded - usual) / usual
    details = f"rows_loaded={rows_loaded} (rows_in={rows_in}), usual about {usual:.0f}, change {change:+.0%}"
    return abs(change) <= ROW_COUNT_CHANGE, details


def check_distribution(amounts, prev_amounts):
    """Mean, median and 95th percentile of amount_inr versus all earlier healthy orders."""
    stats = {"mean": (amounts.mean(), prev_amounts.mean(), MEAN_MEDIAN_RATIO),
             "median": (amounts.median(), prev_amounts.median(), MEAN_MEDIAN_RATIO),
             "p95": (amounts.quantile(0.95), prev_amounts.quantile(0.95), PERCENTILE_RATIO)}
    problems, parts = [], []
    for name, (today, usual, (low, high)) in stats.items():
        ratio = today / usual
        parts.append(f"{name} {today:.0f} (usual {usual:.0f}, x{ratio:.2f})")
        if not low <= ratio <= high:
            problems.append(name)
    details = "amount_inr " + ", ".join(parts)
    return not problems, details


def check_date_range(order_ts, run_date, prev_last_hours):
    """Orders should all fall on run_date and run through the whole day."""
    outside = int((order_ts.dt.date != run_date).sum())
    last = order_ts.max()
    last_hour = last.hour + last.minute / 60
    usual_last = min(prev_last_hours)
    problems = []
    if outside:
        problems.append(f"{outside} orders are not dated {run_date} (range {order_ts.min()} to {last})")
    if last_hour < usual_last - PARTIAL_HOURS:
        problems.append(f"last order is at {last:%H:%M}, normal days run until about {int(usual_last)}:00")
    if problems:
        return False, "; ".join(problems)
    return True, f"all orders dated {run_date}, last order at {last:%H:%M}"


def check_join_fanout(orders):
    """Orders that share customer, product, quantity and timestamp but have different order_ids."""
    group_sizes = orders.groupby(["customer_id", "product_id", "quantity", "order_ts"]).size()
    extra = int((group_sizes - 1).sum())
    rate = extra / len(orders)
    if rate > FANOUT_RATE:
        return False, f"{extra} of {len(orders)} orders ({rate:.0%}) are copies of another order under a different order_id"
    return True, f"{extra} look-alike orders"


# ---------- gathering data and saving ----------

def read_feed(run_date) -> pd.DataFrame:
    return pd.read_csv(FEED_DIR / f"orders_{run_date}.csv")


def get_orders(run_ids) -> pd.DataFrame:
    orders = query_df(
        "SELECT run_id, customer_id, product_id, quantity, amount_inr, order_ts FROM orders WHERE run_id = ANY(%s)",
        (list(run_ids),),
    )
    orders["amount_inr"] = orders["amount_inr"].astype(float)  # NUMERIC arrives as Decimal
    orders["order_ts"] = pd.to_datetime(orders["order_ts"])
    return orders


def orders_checks(run, prev_runs, orders, prev_orders):
    """The checks that only need the loaded orders table (the fix dry-run re-runs exactly these on a copy)."""
    if orders.empty:
        return [{"check_name": "row_count_change", "passed": False, "details": "no orders left for this run"}]
    results = []

    def add(name, outcome):
        passed, details = outcome
        results.append({"check_name": name, "passed": bool(passed), "details": details})

    add("row_count_change", check_row_count(run["rows_loaded"], run["rows_in"], prev_runs["rows_loaded"]))
    add("value_distribution", check_distribution(orders["amount_inr"], prev_orders["amount_inr"]))
    last_hours = prev_orders.groupby("run_id")["order_ts"].max().apply(lambda t: t.hour + t.minute / 60)
    add("date_range", check_date_range(orders["order_ts"], run["run_date"], last_hours))
    add("join_fanout", check_join_fanout(orders))
    return results


def run_all_checks(run, prev_runs, orders, prev_orders):
    """`run` is the pipeline_runs row, `prev_runs` the earlier healthy runs. Returns a list of check dicts."""
    feed = read_feed(run["run_date"])
    prev_feeds = [read_feed(d) for d in prev_runs["run_date"]]
    results = []

    def add(name, outcome):
        passed, details = outcome
        results.append({"check_name": name, "passed": bool(passed), "details": details})

    add("schema_change", check_schema(feed.columns, prev_feeds[-1].columns))
    if run["status"] == "failed":
        return results  # the run loaded nothing, so the data checks have nothing to look at

    add("null_rate", check_null_rate(feed, prev_feeds))
    add("duplicate_keys", check_duplicate_keys(feed))
    return results + orders_checks(run, prev_runs, orders, prev_orders)


def get_healthy_runs(run_date) -> pd.DataFrame:
    """Baseline = earlier runs that succeeded and never had a failed check (so bad days don't pollute it)."""
    return query_df(
        "SELECT * FROM pipeline_runs r WHERE r.status = 'success' AND r.run_date < %s "
        "AND NOT EXISTS (SELECT 1 FROM check_results c WHERE c.run_id = r.run_id AND NOT c.passed) "
        "ORDER BY r.run_date",
        (run_date,),
    )


def detect(run_id: int) -> dict:
    """Run all checks for one pipeline run, save them, and open an incident if any failed."""
    run = query_df("SELECT * FROM pipeline_runs WHERE run_id = %s", (run_id,)).iloc[0]
    prev_runs = get_healthy_runs(run["run_date"])
    if prev_runs.empty:
        return {"results": [], "incident_id": None}

    orders = get_orders([run_id])
    prev_orders = get_orders(prev_runs["run_id"])
    results = run_all_checks(run, prev_runs, orders, prev_orders)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM check_results WHERE run_id = %s", (run_id,))  # re-running the detector replaces old results
    for r in results:
        cur.execute(
            "INSERT INTO check_results (run_id, check_name, passed, details) VALUES (%s, %s, %s, %s)",
            (run_id, r["check_name"], r["passed"], r["details"]),
        )

    incident_id = None
    failed = [r["check_name"] for r in results if not r["passed"]]
    if failed:
        cur.execute("SELECT incident_id FROM incidents WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
        if row:
            incident_id = row[0]
        else:
            summary = f"Run {run_id} ({run['run_date']}): {len(failed)} check(s) failed: {', '.join(failed)}"
            cur.execute("INSERT INTO incidents (run_id, summary) VALUES (%s, %s) RETURNING incident_id", (run_id, summary))
            incident_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return {"results": results, "incident_id": incident_id}

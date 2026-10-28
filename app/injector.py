"""Create the next day's vendor feed (clean or broken) and record the injected fault in ground_truth.

Used by scripts/inject_fault.py and by the background pipeline job.
ground_truth is for evals only. Agents must NEVER read it.
"""

from datetime import date, timedelta

import numpy as np

from app.db import get_conn, query_df
from app.faults import inject
from scripts.seed import FEED_DIR, make_feed  # the same generator that made the 7 clean days


def next_run_date() -> str:
    """The day after the latest pipeline run."""
    latest = query_df("SELECT MAX(run_date) AS d FROM pipeline_runs")["d"][0]
    if latest is None:
        raise ValueError("no pipeline runs yet: run scripts.seed and scripts.run_pipeline first")
    return str(latest + timedelta(days=1))


def build_clean_day(day: str):
    """Generate a normal day of orders, continuing the order_id sequence of the loaded days."""
    customers = query_df("SELECT customer_id FROM customers")
    products = query_df("SELECT product_id, price_inr FROM products")
    products["price_inr"] = products["price_inr"].astype(float)  # Postgres NUMERIC arrives as Decimal
    last_id = query_df(
        "SELECT COALESCE(MAX(o.order_id), 100000) AS m FROM orders o JOIN pipeline_runs r USING (run_id) "
        "WHERE r.run_date < %s",
        (day,),
    )["m"][0]
    rng = np.random.default_rng(7)
    return make_feed(date.fromisoformat(day), int(last_id) + 1, rng, customers, products), rng


def save_ground_truth(day: str, fault_types: str, details: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM ground_truth WHERE run_date = %s", (day,))  # one answer per day
    cur.execute(
        "INSERT INTO ground_truth (run_date, fault_types, details) VALUES (%s, %s, %s)",
        (day, fault_types, details),
    )
    conn.commit()
    conn.close()


def create_feed(day: str, fault_names: list = None) -> dict:
    """Write data/feeds/orders_<day>.csv. With fault names the file is broken and ground_truth is saved."""
    clean_df, rng = build_clean_day(day)
    feed, details = (inject(clean_df, fault_names, rng, day) if fault_names else (clean_df, "no fault"))

    path = FEED_DIR / f"orders_{day}.csv"
    feed.to_csv(path, index=False)
    if fault_names:
        save_ground_truth(day, "+".join(fault_names), details)
    return {"path": path, "clean_rows": len(clean_df), "feed_rows": len(feed), "details": details}

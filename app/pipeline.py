"""The ETL pipeline: read one day's vendor CSV -> validate/clean -> join -> load into Postgres."""

import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from psycopg2.extras import execute_values

from app.db import get_conn, query_df

FEED_DIR = Path("data") / "feeds"
LOG_DIR = Path("logs")
REQUIRED_COLUMNS = ["order_id", "customer_id", "product_id", "quantity", "amount_inr", "order_ts"]


def clean(df: pd.DataFrame, log: list) -> pd.DataFrame:
    """Drop bad rows. Each step logs how many rows it removed, so we can see where data was lost."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"feed is missing columns: {missing}")

    def step(name, before, after):
        log.append(f"{name}: dropped {len(before) - len(after)} rows")
        return after

    df = step("null values", df, df.dropna(subset=REQUIRED_COLUMNS))
    df = step("duplicate order_id", df, df.drop_duplicates(subset="order_id"))
    df = step("quantity/amount <= 0", df, df[(df["quantity"] > 0) & (df["amount_inr"] > 0)])
    df["order_ts"] = pd.to_datetime(df["order_ts"], errors="coerce")
    df = step("unparseable order_ts", df, df.dropna(subset=["order_ts"]))
    return df


def join_reference(df: pd.DataFrame, log: list) -> pd.DataFrame:
    """Inner-join with customers and products: orders pointing to unknown ids can't be loaded (foreign keys)."""
    customers = query_df("SELECT customer_id FROM customers")
    products = query_df("SELECT product_id FROM products")
    before = len(df)
    df = df.merge(customers, on="customer_id", how="inner").merge(products, on="product_id", how="inner")
    log.append(f"join with customers/products: dropped {before - len(df)} rows")
    return df


def load(df: pd.DataFrame, run_id: int, cur) -> int:
    """Insert into orders. ON CONFLICT makes re-running the same day safe. Returns rows really inserted."""
    df = df[["order_id", "customer_id", "product_id", "quantity", "amount_inr", "order_ts"]].copy()
    df["run_id"] = run_id
    rows = df.astype(object).values.tolist()  # plain Python types, which psycopg2 understands
    inserted = execute_values(
        cur,
        "INSERT INTO orders (order_id, customer_id, product_id, quantity, amount_inr, order_ts, run_id) "
        "VALUES %s ON CONFLICT (order_id) DO NOTHING RETURNING order_id",
        rows,
        fetch=True,
    )
    return len(inserted)


def run_pipeline(run_date: str) -> int:
    """Run the pipeline for one date (YYYY-MM-DD). Records the run in pipeline_runs and writes a text log."""
    started = time.perf_counter()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO pipeline_runs (run_date, status) VALUES (%s, 'running') RETURNING run_id", (run_date,))
    run_id = cur.fetchone()[0]
    conn.commit()

    log = [f"run {run_id} for {run_date} started at {datetime.now():%H:%M:%S}"]
    rows_in, rows_loaded, status = None, None, "success"
    try:
        df = pd.read_csv(FEED_DIR / f"orders_{run_date}.csv")
        rows_in = len(df)
        log.append(f"read feed: {rows_in} rows, columns {list(df.columns)}")
        df = clean(df, log)
        df = join_reference(df, log)
        rows_loaded = load(df, run_id, cur)
        log.append(f"loaded {rows_loaded} rows into orders")
        conn.commit()
    except Exception as e:
        conn.rollback()  # a failed run must not leave half-loaded data
        status = "failed"
        log.append(f"ERROR: {e}")

    duration = round(time.perf_counter() - started, 2)
    cur.execute(
        "UPDATE pipeline_runs SET rows_in=%s, rows_loaded=%s, status=%s, duration_sec=%s WHERE run_id=%s",
        (rows_in, rows_loaded, status, duration, run_id),
    )
    conn.commit()
    conn.close()

    log.append(f"finished: status={status}, duration={duration}s")
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / f"run_{run_id}_{run_date}.log").write_text("\n".join(log), encoding="utf-8")
    return run_id

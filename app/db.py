"""Small database helpers shared by every script."""

import pandas as pd
import psycopg2

from app import config


def get_conn():
    """Open a new Postgres connection (callers close it)."""
    return psycopg2.connect(config.get_db_url())


def set_incident_status(incident_id: int, status: str) -> None:
    """open -> investigating -> verified/unverified -> fix_proposed -> resolved/fix_rejected"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE incidents SET status = %s WHERE incident_id = %s", (status, incident_id))
    conn.commit()
    conn.close()


def query_df(sql: str, params=None) -> pd.DataFrame:
    """Run a SELECT and return the result as a DataFrame."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        columns = [c[0] for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columns)
    finally:
        conn.close()

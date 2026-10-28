"""Fix proposals: validate the SQL, dry-run it on a COPY of the orders table, then approve or reject.

Nothing here uses an LLM. The Fixer agent (app/agents/fixer.py) only writes the proposal.
"""

import json
import re

import pandas as pd

from app.db import get_conn, query_df, set_incident_status
from app.detector import detect, get_healthy_runs, get_orders, orders_checks
from app.memory import store_incident_memory, symptom_text
from app.trace import record_step

FORBIDDEN_WORDS = ["drop", "truncate", "alter", "grant", "insert", "ground_truth"]
ORDER_COLUMNS = "run_id, customer_id, product_id, quantity, amount_inr, order_ts"


def split_statements(sql: str) -> list:
    return [s.strip() for s in sql.split(";") if s.strip()]


def validate_fix_sql(sql: str, run_id: int) -> list:
    """Only UPDATE/DELETE on orders, each with a WHERE that mentions this run. Returns the statements.
    This is a guard against careless SQL; the dry run and the human approval are the real safety."""
    statements = split_statements(sql)
    if not statements:
        raise ValueError("no SQL statements found")
    for statement in statements:
        text = " ".join(statement.lower().split())
        if not (text.startswith("update orders ") or text.startswith("delete from orders ")):
            raise ValueError(f"only UPDATE orders / DELETE FROM orders are allowed, got: {statement[:60]}")
        if " where " not in text or f"run_id={run_id}" not in text.replace(" ", ""):
            raise ValueError(f"every statement needs a WHERE clause that restricts it to run_id = {run_id}")
        for word in FORBIDDEN_WORDS:
            if re.search(rf"\b{word}\b", text):
                raise ValueError(f"'{word}' is not allowed in a fix")
    return statements


def _run_orders(cur, run_id: int) -> pd.DataFrame:
    """Orders of one run, read through `cur`. After the temp copy exists, 'orders' means the copy."""
    cur.execute(f"SELECT {ORDER_COLUMNS} FROM orders WHERE run_id = %s", (run_id,))
    frame = pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
    frame["amount_inr"] = frame["amount_inr"].astype(float)
    frame["order_ts"] = pd.to_datetime(frame["order_ts"])
    return frame


def _snapshot(cur, run_id: int) -> dict:
    """Small summary of one run's orders: counts, amount stats, time range and 5 sample rows."""
    cur.execute("SELECT COUNT(*), ROUND(AVG(amount_inr), 2), MIN(order_ts), MAX(order_ts) FROM orders WHERE run_id = %s", (run_id,))
    count, mean, first, last = cur.fetchone()
    cur.execute("SELECT order_id, customer_id, product_id, quantity, amount_inr, order_ts FROM orders "
                "WHERE run_id = %s ORDER BY order_id LIMIT 5", (run_id,))
    columns = [c[0] for c in cur.description]
    return {"row_count": count, "amount_mean": mean, "first_order": first, "last_order": last,
            "sample_rows": [dict(zip(columns, row)) for row in cur.fetchall()]}


def dry_run(incident_id: int, run_id: int, sql: str) -> dict:
    """Run the fix on a temporary COPY of orders, compare before/after and re-run the data checks.
    The real table is never touched: the copy and all changes vanish at rollback."""
    statements = validate_fix_sql(sql, run_id)
    run = query_df("SELECT * FROM pipeline_runs WHERE run_id = %s", (run_id,)).iloc[0]
    prev_runs = get_healthy_runs(run["run_date"])
    prev_orders = get_orders(prev_runs["run_id"])

    conn = get_conn()
    try:
        cur = conn.cursor()
        # A temp table named "orders" hides the real one for this connection, so the fix SQL
        # (which says just "orders") runs on the copy without any rewriting.
        cur.execute("CREATE TEMP TABLE orders AS SELECT * FROM public.orders")
        before = _snapshot(cur, run_id)
        rows_affected = []
        for statement in statements:
            cur.execute(statement)
            rows_affected.append(cur.rowcount)
        after = _snapshot(cur, run_id)

        after_run = run.copy()
        after_run["rows_loaded"] = after["row_count"]
        checks = orders_checks(after_run, prev_runs, _run_orders(cur, run_id), prev_orders)
    finally:
        conn.rollback()  # throw the copy and every change away
        conn.close()

    return {
        "rows_affected": rows_affected, "before": before, "after": after, "checks_after_fix": checks,
        "all_rechecked_pass": all(c["passed"] for c in checks),
        "note": "Checks that read the raw vendor file (schema, null rate, duplicate keys) cannot be re-run on a table copy.",
    }


def save_proposal(incident_id: int, proposal, status: str, dry_run_result: dict) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO fix_proposals (incident_id, fix_type, description, sql_or_steps, risk_notes, status, dry_run_result) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING fix_id",
        (incident_id, proposal.fix_type, proposal.description, proposal.sql_or_steps, proposal.risk_notes,
         status, json.dumps(dry_run_result, default=str)))
    fix_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return fix_id


def _get_pending_fix(fix_id: int):
    rows = query_df("SELECT f.*, i.run_id FROM fix_proposals f JOIN incidents i USING (incident_id) WHERE f.fix_id = %s", (fix_id,))
    if rows.empty:
        raise ValueError(f"fix {fix_id} not found")
    fix = rows.iloc[0]
    if fix["status"] != "pending":
        raise ValueError(f"fix {fix_id} is '{fix['status']}', only a pending fix can be decided")
    return fix


def _mark(cur, fix_id: int, status: str) -> None:
    cur.execute("UPDATE fix_proposals SET status = %s, decided_at = now() WHERE fix_id = %s", (status, fix_id))


def approve_fix(fix_id: int) -> dict:
    """Apply a pending fix to the REAL orders table, then re-run the detector on the run."""
    fix = _get_pending_fix(fix_id)
    incident_id, run_id = int(fix["incident_id"]), int(fix["run_id"])
    record_step(incident_id, "user", "approve_fix", tool_output=f"fix {fix_id} approved")

    if fix["fix_type"] == "manual":  # nothing for us to execute; a person does the steps
        conn = get_conn()
        _mark(conn.cursor(), fix_id, "approved")
        conn.commit()
        conn.close()
        return {"applied": False, "message": "Manual fix approved. Carry out the listed steps yourself."}

    statements = validate_fix_sql(fix["sql_or_steps"], run_id)
    symptoms = symptom_text(incident_id)   # read now: re-running the detector below replaces the failed checks
    conn = get_conn()
    try:
        cur = conn.cursor()
        rows_affected = []
        for statement in statements:
            cur.execute(statement)
            rows_affected.append(cur.rowcount)
        if rows_affected != fix["dry_run_result"]["rows_affected"]:  # the data moved since the dry run
            raise ValueError(f"rows affected {rows_affected} differ from the dry run "
                             f"{fix['dry_run_result']['rows_affected']}; nothing was changed")
        # keep the run's bookkeeping in step with the table (a delete changes how many rows the run loaded)
        cur.execute("UPDATE pipeline_runs SET rows_loaded = (SELECT COUNT(*) FROM orders WHERE run_id = %s) "
                    "WHERE run_id = %s", (run_id, run_id))
        _mark(cur, fix_id, "applied")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Did it work? Re-run the detector on the real table.
    outcome = detect(run_id)
    still_failing = [r["check_name"] for r in outcome["results"] if not r["passed"]]
    set_incident_status(incident_id, "fix_applied" if still_failing else "resolved")
    if not still_failing:
        store_incident_memory(incident_id, symptoms)   # so future incidents can learn from this one
    return {"applied": True, "rows_affected": rows_affected, "checks_still_failing": still_failing,
            "incident_status": "fix_applied" if still_failing else "resolved"}


def reject_fix(fix_id: int) -> dict:
    fix = _get_pending_fix(fix_id)
    incident_id = int(fix["incident_id"])
    conn = get_conn()
    _mark(conn.cursor(), fix_id, "rejected")
    conn.commit()
    conn.close()
    record_step(incident_id, "user", "reject_fix", tool_output=f"fix {fix_id} rejected")
    set_incident_status(incident_id, "fix_rejected")
    return {"rejected": True}

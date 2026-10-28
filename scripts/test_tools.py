"""Call every agent tool by hand and print what it returns (no LLM involved).

Run from the project root:  python -m scripts.test_tools
Needs a faulty run in the database (see inject_fault.py), by default the latest run.
"""

from app.db import query_df
from app.tools import (compare_runs, get_column_stats, get_recent_runs, get_schema_diff,
                       read_pipeline_log, run_readonly_sql)


def show(title, output):
    print(f"\n=== {title}")
    print(output[:600] + (" ...[cut for display]" if len(output) > 600 else ""))


if __name__ == "__main__":
    bad = int(query_df("SELECT MAX(run_id) AS m FROM pipeline_runs")["m"][0])
    good = bad - 1

    show("get_recent_runs(3)", get_recent_runs(3))
    show(f"read_pipeline_log({bad})", read_pipeline_log(bad))
    show(f"get_schema_diff({bad})", get_schema_diff(bad))
    show(f"get_column_stats('orders', 'amount_inr', run_id={bad})", get_column_stats("orders", "amount_inr", bad))
    show(f"get_column_stats('orders', 'amount_inr', run_id={good})", get_column_stats("orders", "amount_inr", good))
    show(f"compare_runs({bad}, {good})", compare_runs(bad, good))

    print("\n--- run_readonly_sql: allowed ---")
    show("SELECT with 3 rows", run_readonly_sql("SELECT order_id, amount_inr FROM orders ORDER BY order_id LIMIT 3"))
    show("row limit (asks 1000, gets 50)", run_readonly_sql("SELECT order_id FROM orders", limit=1000)[-60:])

    print("\n--- run_readonly_sql: must be rejected ---")
    show("DELETE", run_readonly_sql("DELETE FROM orders"))
    show("two statements", run_readonly_sql("SELECT 1; DROP TABLE orders"))
    show("write hidden in a CTE", run_readonly_sql("SELECT * FROM (WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x) t"))
    show("answer key", run_readonly_sql("SELECT * FROM ground_truth"))

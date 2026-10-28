"""Run the pipeline for every feed in data/feeds/ (oldest first), or for one date.

Run from the project root:
    python -m scripts.run_pipeline                  # all days
    python -m scripts.run_pipeline 2026-10-21       # one day
"""

import sys
from pathlib import Path

from app.db import query_df
from app.pipeline import FEED_DIR, run_pipeline

if __name__ == "__main__":
    if len(sys.argv) > 1:
        dates = [sys.argv[1]]
    else:
        dates = sorted(p.stem.replace("orders_", "") for p in Path(FEED_DIR).glob("orders_*.csv"))

    for d in dates:
        run_pipeline(d)

    runs = query_df("SELECT run_id, run_date, rows_in, rows_loaded, status, duration_sec FROM pipeline_runs ORDER BY run_id")
    print(runs.to_string(index=False))

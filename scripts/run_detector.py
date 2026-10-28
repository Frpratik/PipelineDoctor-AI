"""Run the detector on one pipeline run (default: the latest run).

Run from the project root:
    python -m scripts.run_detector            # latest run
    python -m scripts.run_detector 8          # a specific run_id
"""

import sys

from app.db import query_df
from app.detector import detect

if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_id = int(sys.argv[1])
    else:
        run_id = int(query_df("SELECT MAX(run_id) AS m FROM pipeline_runs")["m"][0])

    outcome = detect(run_id)
    if not outcome["results"]:
        sys.exit(f"Run {run_id} has no earlier healthy runs to compare with.")

    print(f"Checks for run {run_id}:")
    for r in outcome["results"]:
        print(f"  [{'PASS' if r['passed'] else 'FAIL'}] {r['check_name']:<18} {r['details']}")
    if outcome["incident_id"]:
        print(f"\nIncident {outcome['incident_id']} opened.")
    else:
        print("\nAll checks passed. No incident.")

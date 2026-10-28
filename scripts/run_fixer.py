"""Ask the Fixer for a fix to an incident (default: incident 1), dry-run it, and show the before/after.

Run from the project root:  python -m scripts.run_fixer [incident_id]
Needs GROQ_API_KEY in .env and a diagnosed incident (run scripts.run_team first).
"""

import json
import sys

from app.agents.fixer import fix_incident
from app.db import query_df

if __name__ == "__main__":
    incident_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    fix_id = fix_incident(incident_id)
    fix = query_df("SELECT * FROM fix_proposals WHERE fix_id = %s", (fix_id,)).iloc[0]

    print(f"=== Fix {fix_id} for incident {incident_id}  [{fix['status']}]  type: {fix['fix_type']} ===")
    print("Description:", fix["description"])
    print("SQL / steps:\n ", fix["sql_or_steps"])
    print("Risk notes: ", fix["risk_notes"])

    result = fix["dry_run_result"]
    if "before" in result:
        print("\n=== Dry run on a copy of orders (real data untouched) ===")
        print("Rows affected:", result["rows_affected"])
        for label in ("before", "after"):
            snap = result[label]
            print(f"{label:>6}: {snap['row_count']} rows, mean amount {snap['amount_mean']}, "
                  f"orders from {snap['first_order']} to {snap['last_order']}")
            print("        sample:", json.dumps(snap["sample_rows"][0], default=str))
        print("Checks re-run on the fixed copy:")
        for c in result["checks_after_fix"]:
            print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['check_name']:<18} {c['details']}")
        print(result["note"])
    else:
        print("\nDry run:", result)

    print(f"\nNext: python -m scripts.decide_fix approve {fix_id}   (or reject {fix_id})")

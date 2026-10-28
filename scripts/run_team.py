"""Run Investigator <-> Verifier on one incident (default: incident 1) and show the trace, verdicts and result.

Run from the project root:  python -m scripts.run_team [incident_id]
Needs GROQ_API_KEY in .env. Re-running adds more steps, so reset with setup_db/seed first for a clean trace.
"""

import sys

from app.agents.team import diagnose_incident
from app.db import query_df
from app.trace import format_trace

if __name__ == "__main__":
    incident_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    result = diagnose_incident(incident_id)

    print("=== Agent trace ===")
    print(format_trace(incident_id, width=110))

    print("\n=== Verdicts ===")
    verdicts = query_df("SELECT attempt, accepted, problems, feedback FROM verdicts WHERE incident_id = %s ORDER BY verdict_id", (incident_id,))
    print(verdicts.to_string(index=False))

    status = query_df("SELECT status FROM incidents WHERE incident_id = %s", (incident_id,))["status"][0]
    print(f"\nAttempts: {result['attempts']}   Incident status: {status}")
    print("\n=== Final diagnosis ===")
    print(result["diagnosis"].model_dump_json(indent=2))

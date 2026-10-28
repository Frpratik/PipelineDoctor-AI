"""Run the Investigator agent on one incident (default: incident 1) and show its trace and diagnosis.

Run from the project root:  python -m scripts.run_investigator [incident_id]
Needs GROQ_API_KEY in .env.
"""

import sys

from app.agents.investigator import investigate
from app.trace import format_trace

if __name__ == "__main__":
    incident_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    diagnosis = investigate(incident_id)

    print("=== Agent trace ===")
    print(format_trace(incident_id, width=110))
    print("\n=== Diagnosis ===")
    print(diagnosis.model_dump_json(indent=2))

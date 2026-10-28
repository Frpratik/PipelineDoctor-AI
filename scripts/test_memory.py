"""Show the incident memory and what it would recall for an incident (no LLM involved).

Run from the project root:  python -m scripts.test_memory [incident_id]
Shows every stored memory, then the memories most similar to the given incident's failed checks.
"""

import sys

from app.db import query_df
from app.memory import find_similar, symptom_text

if __name__ == "__main__":
    incident_id = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    stored = query_df("SELECT incident_id, left(symptoms, 70) AS symptoms, left(root_cause, 60) AS root_cause FROM incident_memory ORDER BY incident_id")
    print("=== Stored memories ===")
    print(stored.to_string(index=False) if len(stored) else "(none yet: approve a fix first)")

    symptoms = symptom_text(incident_id)
    print(f"\n=== Failed checks of incident {incident_id} ===\n{symptoms or '(none)'}")
    print("\n=== Most similar past incidents ===")
    for m in find_similar(incident_id, symptoms):
        print(f"- incident {m['incident_id']}  similarity {m['similarity']:.2f}\n  root cause: {m['root_cause']}\n  fix: {m['fix']}")

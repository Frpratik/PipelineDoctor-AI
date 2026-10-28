"""Incident memory: resolved incidents are stored with an embedding in pgvector, and the Investigator
retrieves the most similar past incidents before it starts.

What we embed is the SYMPTOMS (the failed checks), because at investigation time the symptoms are all we know.
The root cause and the fix are stored next to the embedding as the "answer" to hand back.
"""

from app import config
from app.db import get_conn, query_df

TOP_K = 3
MIN_SIMILARITY = 0.5   # cosine similarity (1 = identical); weaker matches are not worth showing the agent

_model = None  # loaded on first use, because importing sentence-transformers is slow


def embed(text: str) -> str:
    """Turn text into a vector, returned in the '[0.1,0.2,...]' form that pgvector accepts."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(config.EMBEDDING_MODEL)
    vector = _model.encode(text, normalize_embeddings=True)
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


def symptom_text(incident_id: int) -> str:
    """The incident's failed checks as text, e.g. 'value_distribution: amount_inr mean 242154 (usual 2574 ...)'."""
    checks = query_df(
        "SELECT c.check_name, c.details FROM check_results c JOIN incidents i USING (run_id) "
        "WHERE i.incident_id = %s AND NOT c.passed ORDER BY c.check_id", (incident_id,))
    return "\n".join(f"{row.check_name}: {row.details}" for row in checks.itertuples())


def store_incident_memory(incident_id: int, symptoms: str) -> None:
    """Save a resolved incident: symptoms (embedded), the root cause that was found, and the fix that worked."""
    diagnosis = query_df("SELECT diagnosis->>'root_cause' AS root_cause FROM diagnoses WHERE incident_id = %s "
                         "ORDER BY attempt DESC LIMIT 1", (incident_id,))
    fix = query_df("SELECT description, sql_or_steps FROM fix_proposals WHERE incident_id = %s AND status IN ('applied', 'approved') "
                   "ORDER BY fix_id DESC LIMIT 1", (incident_id,))
    root_cause = diagnosis["root_cause"][0] if len(diagnosis) else "unknown"
    fix_text = f"{fix['description'][0]} | {fix['sql_or_steps'][0]}" if len(fix) else "unknown"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM incident_memory WHERE incident_id = %s", (incident_id,))   # one memory per incident
    cur.execute(
        "INSERT INTO incident_memory (incident_id, symptoms, root_cause, fix, embedding) VALUES (%s, %s, %s, %s, %s::vector)",
        (incident_id, symptoms, root_cause, fix_text, embed(symptoms)))
    conn.commit()
    conn.close()


def find_similar(incident_id: int, symptoms: str) -> list:
    """The most similar past incidents, best first. `<=>` is pgvector's cosine distance (0 = identical)."""
    vector = embed(symptoms)
    found = query_df(
        "SELECT incident_id, symptoms, root_cause, fix, 1 - (embedding <=> %s::vector) AS similarity "
        "FROM incident_memory WHERE incident_id <> %s ORDER BY embedding <=> %s::vector LIMIT %s",
        (vector, incident_id, vector, TOP_K))
    found = found[found["similarity"] >= MIN_SIMILARITY]
    return found.to_dict("records")

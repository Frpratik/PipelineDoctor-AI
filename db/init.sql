-- Runs once, the first time the postgres container starts with an empty volume.
-- Turns on pgvector so we can store embeddings in a "vector" column (incident memory, Step 9).
CREATE EXTENSION IF NOT EXISTS vector;

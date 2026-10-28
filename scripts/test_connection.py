"""Step 0 check: can we reach Postgres (with pgvector) and Redis from our machine?

Run from the project root:  python -m scripts.test_connection
"""

import psycopg2
import redis

from app import config


def check_postgres() -> None:
    conn = psycopg2.connect(config.get_db_url())
    cur = conn.cursor()
    cur.execute("SELECT version();")
    print("Postgres :", cur.fetchone()[0][:40])
    # init.sql should have enabled this extension
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
    row = cur.fetchone()
    print("pgvector :", row[0] if row else "NOT INSTALLED")
    # prove vectors really work: distance between two tiny vectors
    cur.execute("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector;")
    print("vector distance test:", cur.fetchone()[0])
    conn.close()


def check_redis() -> None:
    client = redis.Redis.from_url(config.REDIS_URL)
    print("Redis    : ping ->", client.ping())


if __name__ == "__main__":
    check_postgres()
    check_redis()
    print("All good.")

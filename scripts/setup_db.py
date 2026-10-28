"""Create (or reset) all tables from db/schema.sql.

Run from the project root:  python -m scripts.setup_db
"""

from pathlib import Path

from app.db import get_conn

SCHEMA_FILE = Path("db") / "schema.sql"


if __name__ == "__main__":
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()

    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1;")
    print("Tables created:")
    for (name,) in cur.fetchall():
        print(" -", name)
    conn.close()

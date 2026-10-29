"""Talking to a customer's database (the "target"): connect, check it is safe, and scan what is in it.

Only reads. Every connection is a read-only session with a short timeout, whatever the user's privileges are.
Postgres only for now (MySQL would need its own driver and SQL dialect).
"""

import re
from datetime import datetime, timedelta

import psycopg2

QUERY_TIMEOUT_MS = 5000
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")
NUMERIC_TYPES = ("integer", "bigint", "smallint", "numeric", "real", "double precision")

# Column names that probably hold personal data. We suggest excluding them: they are never read or sent to an LLM.
SENSITIVE_NAME = re.compile(r"email|phone|mobile|password|passwd|token|secret|ssn|aadhaar|card|address|dob|birth|(^|_)name($|_)", re.I)
# Preferred names for the column that says when a row was added, best first.
TIME_NAME_ORDER = [r"^created", r"_ts$", r"^order", r"^event", r"^sale", r"^paid", r"^logged", r"^inserted", r"_at$", r"date", r"time"]


def quote(name: str) -> str:
    """Quote an identifier for SQL. Names are also checked against the scanned columns before use."""
    return '"' + name.replace('"', '""') + '"'


def connect(host, port, database, user, password):
    """Open a read-only session on the target database."""
    conn = psycopg2.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=5)
    conn.set_session(readonly=True)
    cur = conn.cursor()
    cur.execute(f"SET statement_timeout = {QUERY_TIMEOUT_MS}")
    return conn


def check_access(conn) -> dict:
    """Is this user safe to monitor with? We read privileges from the catalog; we never try to write.
    Returns the warnings to show the user. Our own sessions are read-only anyway, but a read-only USER is the real safeguard."""
    cur = conn.cursor()
    cur.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
    user, is_super = cur.fetchone()
    cur.execute(
        "SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN %s AND n.nspname NOT LIKE 'pg_toast%%' "
        "AND (has_table_privilege(c.oid, 'INSERT') OR has_table_privilege(c.oid, 'UPDATE') "
        "OR has_table_privilege(c.oid, 'DELETE') OR has_table_privilege(c.oid, 'TRUNCATE')) ORDER BY 1",
        (SYSTEM_SCHEMAS,))
    writable = [row[0] for row in cur.fetchall()]
    cur.execute("SELECT nspname FROM pg_namespace n WHERE nspname NOT IN %s AND nspname NOT LIKE 'pg_%%' "
                "AND has_schema_privilege(n.oid, 'CREATE')", (SYSTEM_SCHEMAS,))
    can_create = [row[0] for row in cur.fetchall()]

    warnings = []
    if is_super:
        warnings.append(f"'{user}' is a superuser. Create a user that can only SELECT.")
    if writable:
        warnings.append(f"'{user}' can write to {len(writable)} table(s), e.g. {', '.join(writable[:3])}.")
    if can_create:
        warnings.append(f"'{user}' can create objects in schema(s): {', '.join(can_create)}.")
    return {"user": user, "read_only": not warnings, "warnings": warnings}


def _count_rows(cur, schema: str, table: str, estimate: int) -> int:
    """The planner's estimate is free, but it is -1 or 0 for tables never analysed, so count those exactly."""
    if estimate > 0:
        return estimate
    try:
        cur.execute(f"SELECT COUNT(*) FROM {quote(schema)}.{quote(table)}")
        return cur.fetchone()[0]
    except psycopg2.Error:
        cur.connection.rollback()
        return 0


def _is_recent(latest, days: int = 3) -> bool:
    if latest is None:
        return False
    if not isinstance(latest, datetime):   # a plain date
        latest = datetime.combine(latest, datetime.min.time())
    return datetime.now(latest.tzinfo) - latest < timedelta(days=days)


def _suggest_time_column(columns: list):
    candidates = [c["name"] for c in columns if c["type"].startswith("timestamp") or c["type"] == "date"]
    for pattern in TIME_NAME_ORDER:
        for name in candidates:
            if re.search(pattern, name, re.I):
                return name
    return candidates[0] if candidates else None


def scan(conn) -> list:
    """List every user table with its columns, size and suggestions, best candidates to watch first."""
    cur = conn.cursor()
    cur.execute(
        "SELECT n.nspname, c.relname, c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN %s AND n.nspname NOT LIKE 'pg_toast%%' ORDER BY 1, 2", (SYSTEM_SCHEMAS,))
    tables = {(s, t): {"schema": s, "table": t, "estimate": est, "columns": [], "primary_key": []} for s, t, est in cur.fetchall()}

    cur.execute("SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema NOT IN %s ORDER BY table_schema, table_name, ordinal_position", (SYSTEM_SCHEMAS,))
    for schema, table, column, data_type in cur.fetchall():
        if (schema, table) in tables:
            tables[(schema, table)]["columns"].append({"name": column, "type": data_type})

    cur.execute(
        "SELECT n.nspname, c.relname, a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indisprimary AND n.nspname NOT IN %s", (SYSTEM_SCHEMAS,))
    for schema, table, column in cur.fetchall():
        if (schema, table) in tables:
            tables[(schema, table)]["primary_key"].append(column)

    result = []
    for info in tables.values():
        info["rows"] = _count_rows(cur, info["schema"], info["table"], info.pop("estimate"))
        time_column = _suggest_time_column(info["columns"])
        latest = None
        if time_column and info["rows"]:
            try:
                cur.execute(f"SELECT MAX({quote(time_column)}) FROM {quote(info['schema'])}.{quote(info['table'])}")
                latest = cur.fetchone()[0]
            except psycopg2.Error:
                conn.rollback()
        active = _is_recent(latest)   # a table that keeps receiving rows is worth watching; a static lookup table is not
        info.update(
            suggested_time_column=time_column,
            suggested_key_column=info["primary_key"][0] if len(info["primary_key"]) == 1 else None,
            suggested_excluded=[c["name"] for c in info["columns"] if SENSITIVE_NAME.search(c["name"])],
            latest=str(latest) if latest else None,
            score=(2 if time_column else 0) + (2 if active else 0) + (1 if info["rows"] else 0),
            watchable=bool(time_column and info["rows"]),
        )
        result.append(info)
    return sorted(result, key=lambda t: (-t["score"], -t["rows"], t["schema"], t["table"]))   # best first, bigger first

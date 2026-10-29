"""API for connecting a database and choosing which tables to watch. Included by app/api/main.py.

Passwords go in but never come out: responses contain no secrets.
"""

import json
from typing import Optional

import psycopg2
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import jobs, watch
from app.db import query_df
from app.tasks import run_watch_task

router = APIRouter()


def rows(sql: str, params=None) -> list:
    return json.loads(query_df(sql, params).to_json(orient="records", date_format="iso"))


class ConnectionRequest(BaseModel):
    name: str
    host: str
    port: int = 5432
    database: str
    user: str
    password: str


class WatchRequest(BaseModel):
    schema_name: str = "public"
    table: str
    time_column: str
    key_column: Optional[str] = None
    excluded_columns: list[str] = []


@router.post("/connections")
def create_connection(request: ConnectionRequest):
    try:
        connection_id, access = watch.save_connection(request.name, request.host, request.port, request.database,
                                                      request.user, request.password)
    except psycopg2.Error as e:   # wrong host, port, user or password
        raise HTTPException(400, f"could not connect: {str(e).strip().splitlines()[0]}")
    return {"connection_id": connection_id, **access}


@router.get("/connections")
def list_connections():
    return rows("SELECT connection_id, name, host, port, database, username, read_only, warnings, created_at "
                "FROM connections ORDER BY connection_id DESC")


@router.post("/connections/{connection_id}/scan")
def scan(connection_id: int):
    try:
        return watch.scan_connection(connection_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except psycopg2.Error as e:
        raise HTTPException(400, f"scan failed: {str(e).strip().splitlines()[0]}")


@router.post("/connections/{connection_id}/watch")
def add_watch(connection_id: int, request: WatchRequest):
    try:
        watch_id = watch.add_watch(connection_id, request.schema_name, request.table, request.time_column,
                                   request.key_column, request.excluded_columns)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(409, "this table is already being watched")
    except psycopg2.Error as e:
        raise HTTPException(400, str(e).strip().splitlines()[0])
    job_id = jobs.create_job("watch_check")   # start learning right away
    run_watch_task.delay(job_id, watch_id)
    return {"watch_id": watch_id, "job_id": job_id}


@router.get("/watch")
def list_watched():
    watched = rows(
        "SELECT w.watch_id, w.schema_name, w.table_name, w.time_column, w.key_column, w.excluded_columns, "
        "c.name AS connection_name, c.read_only, "
        "(SELECT COUNT(*) FROM table_profiles p WHERE p.watch_id = w.watch_id) AS profiled_days, "
        "(SELECT MAX(day) FROM watch_check_results r WHERE r.watch_id = w.watch_id) AS last_checked_day, "
        "(SELECT COUNT(*) FROM watch_alerts a WHERE a.watch_id = w.watch_id AND a.status = 'open') AS open_alerts, "
        "(SELECT json_agg(json_build_object('name', r.check_name, 'passed', r.passed) ORDER BY r.result_id) "
        " FROM watch_check_results r WHERE r.watch_id = w.watch_id "
        " AND r.day = (SELECT MAX(day) FROM watch_check_results WHERE watch_id = w.watch_id)) AS checks "
        "FROM watched_tables w JOIN connections c USING (connection_id) ORDER BY w.watch_id DESC")
    for w in watched:
        w["status"] = "alert" if w["open_alerts"] else "ok" if w["last_checked_day"] else "not checked"
    return watched


@router.get("/watch/{watch_id}")
def watch_detail(watch_id: int):
    found = rows("SELECT w.*, c.name AS connection_name FROM watched_tables w JOIN connections c USING (connection_id) "
                 "WHERE w.watch_id = %s", (watch_id,))
    if not found:
        raise HTTPException(404, "watched table not found")
    return {
        "watch": found[0],
        "days": rows("SELECT day, (metrics->>'row_count')::int AS row_count FROM table_profiles WHERE watch_id = %s ORDER BY day", (watch_id,)),
        "checks": rows("SELECT day, check_name, passed, details FROM watch_check_results WHERE watch_id = %s "
                       "AND day = (SELECT MAX(day) FROM watch_check_results WHERE watch_id = %s) ORDER BY result_id", (watch_id, watch_id)),
        "alerts": rows("SELECT alert_id, day, status, summary FROM watch_alerts WHERE watch_id = %s ORDER BY day DESC", (watch_id,)),
    }


@router.post("/watch/{watch_id}/check")
def check_now(watch_id: int):
    if not rows("SELECT 1 FROM watched_tables WHERE watch_id = %s", (watch_id,)):
        raise HTTPException(404, "watched table not found")
    job_id = jobs.create_job("watch_check")
    run_watch_task.delay(job_id, watch_id)
    return {"job_id": job_id}

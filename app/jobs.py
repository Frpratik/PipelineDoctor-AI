"""The jobs table: one row per background task, so the API can report queued -> running -> done / failed."""

from app.db import get_conn, query_df


def _update(job_id: int, sql: str, params: tuple = ()) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE jobs SET {sql} WHERE job_id = %s", (*params, job_id))
    conn.commit()
    conn.close()


def create_job(job_type: str, incident_id: int = None) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO jobs (job_type, incident_id) VALUES (%s, %s) RETURNING job_id", (job_type, incident_id))
    job_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return job_id


def start_job(job_id: int) -> None:
    # COALESCE keeps the first start time when a task is retried
    _update(job_id, "status = 'running', started_at = COALESCE(started_at, now())")


def set_progress(job_id: int, message: str) -> None:
    _update(job_id, "progress = %s", (message,))


def set_incident(job_id: int, incident_id: int) -> None:
    _update(job_id, "incident_id = %s", (incident_id,))


def requeue_job(job_id: int, message: str) -> None:
    """Back to 'queued' while Celery waits to retry the task."""
    _update(job_id, "status = 'queued', progress = %s", (message,))


def finish_job(job_id: int, message: str) -> None:
    _update(job_id, "status = 'done', progress = %s, finished_at = now()", (message,))


def fail_job(job_id: int, error: str) -> None:
    _update(job_id, "status = 'failed', error = %s, finished_at = now()", (error,))


def get_job(job_id: int):
    """The job as a dict, or None."""
    rows = query_df("SELECT * FROM jobs WHERE job_id = %s", (job_id,))
    return None if rows.empty else rows.iloc[0].to_dict()

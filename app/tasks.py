"""Celery tasks for the slow work. Every task gets a job_id and keeps its row in the jobs table up to date."""

import groq

from app import jobs, trace
from app.agents.fixer import fix_incident
from app.agents.team import diagnose_incident
from app.celery_app import celery_app
from app.db import query_df
from app.detector import detect
from app.evals import run_eval
from app.faults import FAULTS
from app.fixes import approve_fix
from app.injector import create_feed, next_run_date
from app.pipeline import FEED_DIR, run_pipeline

MAX_RETRIES = 3
RETRY_DELAY = 10   # seconds between retries
# Errors worth retrying: rate limit, network trouble, Groq server errors. A wrong API key (401) is not retried.
RETRYABLE = (groq.RateLimitError, groq.APIConnectionError, groq.InternalServerError)


def run_job(task, job_id: int, work, retryable=()) -> None:
    """Run work() (returns a final message) as a tracked job: status, progress messages, retries, errors."""
    jobs.start_job(job_id)
    trace.set_progress_callback(lambda message: jobs.set_progress(job_id, message))
    try:
        jobs.finish_job(job_id, work())
    except retryable as e:
        if task.request.retries >= MAX_RETRIES:   # all retries used up
            jobs.fail_job(job_id, f"gave up after {MAX_RETRIES} retries: {type(e).__name__}: {e}")
            raise
        attempt = task.request.retries + 1
        jobs.requeue_job(job_id, f"{type(e).__name__}; retry {attempt}/{MAX_RETRIES} in {RETRY_DELAY}s")
        raise task.retry(exc=e, countdown=RETRY_DELAY, max_retries=MAX_RETRIES)
    except Exception as e:
        jobs.fail_job(job_id, f"{type(e).__name__}: {e}")
        raise
    finally:
        trace.set_progress_callback(None)


# ---------- the four jobs ----------

@celery_app.task(bind=True)
def run_pipeline_task(self, job_id: int, run_date: str = None, fault: str = None):
    """Make the day's feed (broken if a fault is given), run the pipeline and detector, queue an investigation."""
    def work():
        day = run_date or next_run_date()
        names = fault.split("+") if fault else None
        if names and any(n not in FAULTS for n in names):
            raise ValueError(f"unknown fault in '{fault}'")
        if names or not (FEED_DIR / f"orders_{day}.csv").exists():
            jobs.set_progress(job_id, f"Creating feed for {day}" + (f" with fault {fault}" if fault else ""))
            create_feed(day, names)

        jobs.set_progress(job_id, f"Running pipeline for {day}")
        run_id = run_pipeline(day)
        jobs.set_progress(job_id, f"Detector: checking run {run_id}")
        outcome = detect(run_id)
        if not outcome["incident_id"]:
            return f"run {run_id} ({day}) done: all checks passed, no incident"

        incident_id = outcome["incident_id"]
        jobs.set_incident(job_id, incident_id)
        child_job = jobs.create_job("investigate_incident", incident_id)   # chaining: a new incident starts the agents
        investigate_incident_task.delay(child_job, incident_id)
        return f"run {run_id} ({day}) done: incident {incident_id} opened, investigation queued as job {child_job}"

    run_job(self, job_id, work)


@celery_app.task(bind=True)
def investigate_incident_task(self, job_id: int, incident_id: int):
    """Investigator <-> Verifier, then the Fixer and its dry run."""
    def work():
        result = diagnose_incident(incident_id)
        jobs.set_progress(job_id, "Fixer: proposing a fix and dry-running it")
        fix_id = fix_incident(incident_id)
        fix_status = query_df("SELECT status FROM fix_proposals WHERE fix_id = %s", (fix_id,))["status"][0]
        verdict = "verified" if result["verdict"].accepted else "NOT verified"
        return (f"diagnosis {verdict} after {result['attempts']} attempt(s); "
                f"fix {fix_id} is '{fix_status}'" + (", waiting for your approval" if fix_status == "pending" else ""))

    run_job(self, job_id, work, retryable=RETRYABLE)


@celery_app.task(bind=True)
def apply_fix_task(self, job_id: int, fix_id: int):
    """Apply an approved fix to the real table, re-run the detector, store memory if resolved."""
    def work():
        result = approve_fix(fix_id)
        if not result["applied"]:
            return result["message"]
        return f"fix {fix_id} applied to {result['rows_affected']} rows; incident is now '{result['incident_status']}'"

    run_job(self, job_id, work)


@celery_app.task(bind=True)
def run_eval_task(self, job_id: int, faults: list, runs: int, variants: list):
    """Run each fault `runs` times for each variant (model + prompt), judge the diagnoses, save the results."""
    run_job(self, job_id, lambda: run_eval(job_id, faults, runs, variants))

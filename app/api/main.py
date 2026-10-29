"""FastAPI app. Slow endpoints only create a job and return its job_id; the Celery worker does the work.

Run from the project root:  uvicorn app.api.main:app --reload --port 8000
Interactive docs:           http://localhost:8000/docs
"""

import json
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import config, jobs
from app.agents.investigator import PROMPTS
from app.api.watch import router as watch_router
from app.db import query_df
from app.evals import DEFAULT_FAULTS, format_report
from app.faults import FAULTS
from app.tasks import apply_fix_task, run_eval_task, run_pipeline_task
from app.fixes import reject_fix
from app.trace import get_trace

app = FastAPI(title="PipelineDoctor AI")
app.include_router(watch_router)   # connect a database, scan it, choose tables to watch


class PipelineRunRequest(BaseModel):
    run_date: Optional[str] = None   # default: the day after the latest run
    fault: Optional[str] = None      # e.g. "currency_unit" or "currency_unit+null_spike"


def rows(sql: str, params=None) -> list:
    """Run a query and return JSON-safe records (dates, decimals and NaN handled by pandas)."""
    return json.loads(query_df(sql, params).to_json(orient="records", date_format="iso"))


WATCHED_TABLES = ["customers", "products", "orders", "pipeline_runs", "check_results", "incidents",
                  "diagnoses", "verdicts", "fix_proposals", "agent_steps", "incident_memory", "jobs"]


@app.get("/overview")
def overview():
    """Everything the UI's watch screen needs in one call: table sizes, recent runs with check results, incidents, jobs."""
    tables = [{"name": t, "rows": int(query_df(f"SELECT COUNT(*) AS n FROM {t}")["n"][0])} for t in WATCHED_TABLES]
    runs = rows(
        "SELECT r.run_id, r.run_date, r.rows_in, r.rows_loaded, r.status, r.duration_sec, r.started_at, "
        "i.incident_id, i.status AS incident_status, "
        "(SELECT json_agg(json_build_object('name', c.check_name, 'passed', c.passed) ORDER BY c.check_id) "
        " FROM check_results c WHERE c.run_id = r.run_id) AS checks "
        "FROM pipeline_runs r LEFT JOIN incidents i USING (run_id) ORDER BY r.run_id DESC LIMIT 14")
    return {
        "database": {"name": config.POSTGRES_DB, "host": config.POSTGRES_HOST},
        "tables": tables,
        "runs": runs,
        "incidents": list_incidents()[:10],
        "jobs": rows("SELECT * FROM jobs ORDER BY job_id DESC LIMIT 10"),
    }


@app.post("/pipeline/run")
def run_pipeline_endpoint(request: PipelineRunRequest = PipelineRunRequest()):
    if request.fault and any(name not in FAULTS for name in request.fault.split("+")):
        raise HTTPException(400, f"unknown fault. Choose from: {list(FAULTS)}")
    job_id = jobs.create_job("run_pipeline")
    run_pipeline_task.delay(job_id, request.run_date, request.fault)
    return {"job_id": job_id}


@app.get("/incidents")
def list_incidents():
    return rows("SELECT i.incident_id, i.run_id, r.run_date, i.status, i.summary, i.created_at "
                "FROM incidents i JOIN pipeline_runs r USING (run_id) ORDER BY i.incident_id DESC")


@app.get("/incidents/{incident_id}")
def incident_detail(incident_id: int):
    incident = rows("SELECT i.*, r.run_date, r.rows_in, r.rows_loaded, r.status AS run_status FROM incidents i "
                    "JOIN pipeline_runs r USING (run_id) WHERE i.incident_id = %s", (incident_id,))
    if not incident:
        raise HTTPException(404, "incident not found")
    return {
        "incident": incident[0],
        "checks": rows("SELECT check_name, passed, details FROM check_results WHERE run_id = %s ORDER BY check_id", (incident[0]["run_id"],)),
        "diagnoses": rows("SELECT attempt, diagnosis FROM diagnoses WHERE incident_id = %s ORDER BY attempt", (incident_id,)),
        "verdicts": rows("SELECT attempt, accepted, problems, feedback FROM verdicts WHERE incident_id = %s ORDER BY verdict_id", (incident_id,)),
        "fix_proposals": rows("SELECT * FROM fix_proposals WHERE incident_id = %s ORDER BY fix_id", (incident_id,)),  # includes dry_run_result
        "trace": json.loads(json.dumps(get_trace(incident_id), default=str)),
    }


@app.post("/fixes/{fix_id}/approve")
def approve(fix_id: int):
    fix = rows("SELECT incident_id, status FROM fix_proposals WHERE fix_id = %s", (fix_id,))
    if not fix:
        raise HTTPException(404, "fix not found")
    if fix[0]["status"] != "pending":
        raise HTTPException(409, f"fix is '{fix[0]['status']}', only a pending fix can be approved")
    job_id = jobs.create_job("apply_fix", fix[0]["incident_id"])
    apply_fix_task.delay(job_id, fix_id)
    return {"job_id": job_id}


@app.post("/fixes/{fix_id}/reject")
def reject(fix_id: int):
    try:
        return reject_fix(fix_id)   # quick, so it runs right here instead of in a job
    except ValueError as e:
        raise HTTPException(409, str(e))


class EvalRequest(BaseModel):
    faults: Optional[list[str]] = None            # default: the 8 single faults and one combo
    runs_per_fault: int = 1
    models: Optional[list[str]] = None            # default: the model in .env
    prompt_versions: Optional[list[str]] = None   # default: ["v1"]


@app.post("/eval/run")
def run_eval_endpoint(request: EvalRequest = EvalRequest()):
    faults = request.faults or DEFAULT_FAULTS
    if any(name not in FAULTS for fault in faults for name in fault.split("+")):
        raise HTTPException(400, f"unknown fault. Choose from: {list(FAULTS)}")
    prompts = request.prompt_versions or ["v1"]
    if any(p not in PROMPTS for p in prompts):
        raise HTTPException(400, f"unknown prompt version. Choose from: {list(PROMPTS)}")
    if not 1 <= request.runs_per_fault <= 10:
        raise HTTPException(400, "runs_per_fault must be between 1 and 10")
    variants = [{"model": m, "prompt": p} for m in (request.models or [config.LLM_MODEL]) for p in prompts]
    job_id = jobs.create_job("run_eval")
    run_eval_task.delay(job_id, faults, request.runs_per_fault, variants)
    return {"job_id": job_id, "cases": len(faults) * request.runs_per_fault * len(variants)}


@app.get("/eval/{eval_id}")
def eval_report(eval_id: int):
    """The finished (or partial) report for an eval job, plus every case."""
    cases = rows("SELECT * FROM eval_results WHERE eval_id = %s ORDER BY result_id", (eval_id,))
    if not cases:
        raise HTTPException(404, "no results for this eval yet")
    return {"report": format_report(eval_id), "cases": cases}


@app.get("/jobs/{job_id}")
def job_status(job_id: int):
    job = rows("SELECT * FROM jobs WHERE job_id = %s", (job_id,))
    if not job:
        raise HTTPException(404, "job not found")
    return job[0]

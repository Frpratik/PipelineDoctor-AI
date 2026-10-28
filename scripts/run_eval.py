"""Queue an eval on the Celery worker, follow its progress, and print the report.

Run from the project root (the worker must be running):
    python -m scripts.run_eval                                         # all faults x 1 run, current model, prompt v1
    python -m scripts.run_eval --faults currency_unit,timezone_shift --runs 2
    python -m scripts.run_eval --prompts v1,v2                         # compare two prompt versions
    python -m scripts.run_eval --models openai/gpt-oss-120b,llama-3.3-70b-versatile   # compare two models
    python -m scripts.run_eval --report 5                              # print the report of a finished eval job

Every case costs real LLM tokens, and a variant repeats every case. Start small.
"""

import argparse
import time

from app import config
from app.agents.investigator import PROMPTS
from app.db import query_df
from app.evals import DEFAULT_FAULTS, format_report
from app.faults import FAULTS
from app.jobs import create_job
from app.tasks import run_eval_task


def csv(text: str) -> list:
    return [item.strip() for item in text.split(",") if item.strip()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--faults", type=csv, default=DEFAULT_FAULTS, help="comma list; use a+b for a combo")
    parser.add_argument("--runs", type=int, default=1, help="times each fault is run, per variant")
    parser.add_argument("--models", type=csv, default=[config.LLM_MODEL])
    parser.add_argument("--prompts", type=csv, default=["v1"], help=f"Investigator prompt versions: {list(PROMPTS)}")
    parser.add_argument("--report", type=int, help="just print the report for this eval job id")
    args = parser.parse_args()

    if args.report:
        print(format_report(args.report))
        raise SystemExit

    bad = [n for fault in args.faults for n in fault.split("+") if n not in FAULTS]
    if bad or any(p not in PROMPTS for p in args.prompts):
        raise SystemExit(f"Unknown fault {bad} or prompt version. Faults: {list(FAULTS)}  Prompts: {list(PROMPTS)}")

    variants = [{"model": m, "prompt": p} for m in args.models for p in args.prompts]
    total = len(args.faults) * args.runs * len(variants)
    if query_df("SELECT COUNT(*) AS n FROM incident_memory")["n"][0]:
        print("Warning: incident_memory is not empty, so agents may recall past incidents. For a clean eval, reset first.")

    job_id = create_job("run_eval")
    run_eval_task.delay(job_id, args.faults, args.runs, variants)
    print(f"Queued eval job {job_id}: {total} cases ({len(variants)} variant(s)). Watching progress...\n")

    last = None
    while True:
        job = query_df("SELECT status, progress, error FROM jobs WHERE job_id = %s", (job_id,)).iloc[0]
        if job["progress"] != last and job["progress"]:
            last = job["progress"]
            print(f"[{job['status']}] {last}")
        if job["status"] in ("done", "failed"):
            break
        time.sleep(3)

    if job["status"] == "failed":
        print("\nEval job failed:", job["error"])
    print(format_report(job_id))

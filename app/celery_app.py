"""The Celery app. Redis is the queue; job status lives in our own jobs table, so no result backend is needed.

Start a worker (Windows needs --pool=solo):
    celery -A app.celery_app worker --pool=solo --loglevel=info
"""

from celery import Celery

from app import config

celery_app = Celery("pipeline_doctor", broker=config.REDIS_URL, include=["app.tasks"])
celery_app.conf.broker_connection_retry_on_startup = True
celery_app.conf.worker_prefetch_multiplier = 1   # take one job at a time: these jobs are long

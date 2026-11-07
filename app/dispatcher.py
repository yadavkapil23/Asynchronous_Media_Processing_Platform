"""Republish queued database jobs until a worker claims them."""
import logging
import time
from datetime import timedelta

from redis import Redis
from rq import Queue
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import Job, now_utc
from .job_attempts import finish_attempt, ready_for_worker

log = logging.getLogger(__name__)


def recover_stale_jobs(job_id=None):
    cutoff = now_utc() - timedelta(seconds=settings.job_timeout_seconds + settings.job_recovery_grace_seconds)
    with SessionLocal() as db:
        query = select(Job.id, Job.attempt_token, Job.attempt_count).where(
            Job.status == "processing", Job.started_at < cutoff,
        )
        if job_id is not None:
            query = query.where(Job.id == job_id)
        requeued = failed = 0
        for stale_id, token, number in db.execute(query.order_by(Job.started_at, Job.id).limit(100)).all():
            status = finish_attempt(db, stale_id, token, number, "timed_out", "Worker exceeded its processing deadline")
            if status == "queued":
                requeued += 1
            elif status == "failed":
                failed += 1
        db.commit()
        return requeued, failed


def dispatch_once(queue, job_id=None):
    with SessionLocal() as db:
        query = select(Job.id).where(ready_for_worker(now_utc()))
        if job_id is not None:
            query = query.where(Job.id == job_id)
        ids = db.scalars(query.order_by(Job.created_at).limit(100)).all()
    for job_id in ids:
        queue.enqueue("app.worker.process_job", job_id, job_timeout=settings.job_timeout_seconds)
    return len(ids)


def main():
    logging.basicConfig(level=logging.INFO)
    queue = Queue(connection=Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2))
    while True:
        try:
            recover_stale_jobs()
            dispatch_once(queue)
        except Exception:
            log.exception("Job dispatch failed; will retry")
        time.sleep(settings.dispatch_interval_seconds)


if __name__ == "__main__":
    main()

import errno
from datetime import datetime, timedelta
from typing import Literal

from rq.timeouts import JobTimeoutException
from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from .config import settings
from .models import Job, JobAttempt, now_utc
from .webhooks import record_terminal_event


def ready_for_worker(now: datetime):
    return or_(Job.status == "queued", and_(Job.status == "retrying", Job.next_retry_at <= now))


def classify_error(exc: Exception) -> tuple[Literal["failed", "transient_error"], str]:
    network_error = isinstance(exc, (
        ConnectionError, TimeoutError, JobTimeoutException,
    ))
    temporary_io_error = isinstance(exc, OSError) and exc.errno in {
        errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.ETIMEDOUT,
    }
    if network_error or temporary_io_error:
        return "transient_error", f"Temporary processing failure ({type(exc).__name__})"
    return "failed", (str(exc) or type(exc).__name__)[:500]


def retry_delay(attempt_number: int) -> timedelta:
    cap = settings.job_retry_max_seconds
    exponent = min(attempt_number - 1, cap.bit_length())
    seconds = min(cap, settings.job_retry_base_seconds * 2 ** exponent)
    return timedelta(seconds=seconds)


def finish_attempt(
    db: Session,
    job_id: str,
    token: str,
    number: int,
    outcome: Literal["completed", "failed", "transient_error", "timed_out"],
    error: str | None = None,
) -> str | None:
    now = now_utc()
    values = dict(error_message=error, next_retry_at=None, dead_lettered_at=None)
    if outcome == "completed":
        values.update(status="completed", progress=100, completed_at=now)
    elif outcome == "failed":
        values.update(status="failed", attempt_token=None, completed_at=now)
    elif number >= settings.job_max_attempts:
        values.update(status="failed", attempt_token=None, dead_lettered_at=now, completed_at=now)
    else:
        values.update(progress=0, attempt_token=None, started_at=None, completed_at=None)
        if outcome == "timed_out":
            values.update(status="queued")
        else:
            values.update(status="retrying", next_retry_at=now + retry_delay(number))

    changed = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "processing", Job.attempt_token == token)
        .values(**values)
    )
    if changed.rowcount != 1:
        return None
    db.execute(
        update(JobAttempt)
        .where(JobAttempt.token == token, JobAttempt.status == "processing")
        .values(status=outcome, ended_at=now, error_message=error)
    )
    if values["status"] in {"completed", "failed"}:
        record_terminal_event(db, job_id, values["status"], now)
    return values["status"]

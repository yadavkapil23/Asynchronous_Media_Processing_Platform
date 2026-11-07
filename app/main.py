from pathlib import Path
import json
import socket

from uuid import uuid4
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import ApiKey, Job, JobAttempt, Upload, User, WebhookDelivery, WebhookEndpoint, WebhookEvent, now_utc
from .accounts import hash_key
from .schemas import AttemptResponse, EndpointRequest, JobRequest, JobResponse, UploadRequest, UploadResponse, as_utc
from . import uploads, webhooks


Path(settings.local_storage_path).mkdir(parents=True, exist_ok=True)

app = FastAPI(title=settings.app_name, version="0.1.0")


@app.exception_handler(uploads.UploadProblem)
def upload_error(request, exc):
    return JSONResponse(status_code=exc.code, content={"detail": str(exc)})


def require_api_key(x_api_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    if not x_api_key or len(x_api_key) > 256:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")
    api_key = db.query(ApiKey).filter(ApiKey.key_hash == hash_key(x_api_key), ApiKey.revoked_at.is_(None)).first()
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")
    return db.get(User, api_key.user_id)


def resolve_idempotency(existing: Job, payload: JobRequest) -> JobResponse:
    if (existing.upload_id != payload.upload_id or existing.operations != [op.model_dump(exclude_none=True) for op in payload.operations] or existing.webhook_url != (str(payload.webhook_url) if payload.webhook_url else None)):
        raise HTTPException(status_code=409, detail="Idempotency key already used for a different request")
    return JobResponse.model_validate(existing, from_attributes=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/uploads", response_model=UploadResponse, dependencies=[Depends(require_api_key)])
def create_upload(payload: UploadRequest, db: Session = Depends(get_db), user: User = Depends(require_api_key)) -> UploadResponse:
    upload_id = f"upl_{uuid4().hex[:16]}"
    key = f"staging/{upload_id}"
    upload = Upload(
        id=upload_id,
        user_id=user.id,
        filename=payload.filename,
        content_type=payload.content_type,
        object_key=key,
        size=payload.size,
    )
    url = f"/v1/uploads/{upload_id}/content"
    headers = {"Content-Type": payload.content_type}
    db.add(upload)
    db.commit()
    return UploadResponse(upload_id=upload_id, upload_url=url, upload_headers=headers,
                          expires_in=None)


@app.put("/v1/uploads/{upload_id}/content", dependencies=[Depends(require_api_key)])
async def upload_content(upload_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(require_api_key)):
    upload = db.get(Upload, upload_id)
    if upload is None or upload.user_id != user.id:
        raise HTTPException(status_code=404, detail="Upload not found")
    if upload.status != "pending":
        raise HTTPException(status_code=409, detail="Upload already completed")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > uploads.MAX_BYTES:
            raise uploads.UploadProblem(413, "Maximum upload size is 20 MiB")
        body.extend(chunk)
    accepted = await run_in_threadpool(uploads.accept_upload, db, upload, bytes(body))
    return {"upload_id": accepted.id, "size": accepted.size, "status": accepted.status}


@app.post("/v1/webhooks", status_code=201)
def register_webhook(payload: EndpointRequest, response: Response, db: Session = Depends(get_db), user: User = Depends(require_api_key)):
    url = str(payload.url)
    endpoint = WebhookEndpoint(id=f"wh_{uuid4().hex}", user_id=user.id, url=url)
    try:
        secret = webhooks.endpoint_secret(endpoint.id)
        webhooks.destination(url)
    except RuntimeError as exc:
        raise HTTPException(503, detail="Webhook signing is not configured") from exc
    except (ValueError, socket.gaierror) as exc:
        raise HTTPException(422, detail="Endpoint must resolve to a public HTTPS destination on port 443") from exc
    db.add(endpoint)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, detail="This webhook URL is already registered") from exc
    response.headers["Cache-Control"] = "no-store"
    return {"id": endpoint.id, "url": url, "signing_secret": secret}


@app.get("/v1/webhooks")
def list_webhooks(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                  db: Session = Depends(get_db), user: User = Depends(require_api_key)):
    endpoints = db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.user_id == user.id)
                           .order_by(WebhookEndpoint.id).offset(offset).limit(limit))
    return [{"id": e.id, "url": e.url, "enabled": e.enabled} for e in endpoints]


@app.delete("/v1/webhooks/{endpoint_id}")
def disable_webhook(endpoint_id: str, db: Session = Depends(get_db), user: User = Depends(require_api_key)):
    result = db.execute(update(WebhookEndpoint).where(
        WebhookEndpoint.id == endpoint_id, WebhookEndpoint.user_id == user.id,
    ).values(enabled=False))
    if result.rowcount != 1:
        raise HTTPException(404, detail="Endpoint not found")
    db.execute(update(WebhookEvent).where(
        WebhookEvent.endpoint_id == endpoint_id, WebhookEvent.status == "pending",
    ).values(status="cancelled"))
    db.commit()
    return {"id": endpoint_id, "enabled": False}


@app.post("/v1/jobs", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_api_key)])
def create_job(
    payload: JobRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_api_key),
) -> JobResponse:
    upload = db.get(Upload, payload.upload_id)
    if upload is None or upload.user_id != user.id:
        raise HTTPException(status_code=404, detail="Upload not found")
    if upload.status != "uploaded":
        raise HTTPException(status_code=409, detail="Upload content first")
    if idempotency_key:
        existing = db.query(Job).filter(Job.user_id == user.id, Job.idempotency_key == idempotency_key).first()
        if existing:
            return resolve_idempotency(existing, payload)
    if payload.webhook_url is not None:
        endpoint = db.scalar(select(WebhookEndpoint.id).where(
            WebhookEndpoint.user_id == user.id, WebhookEndpoint.url == str(payload.webhook_url),
            WebhookEndpoint.enabled.is_(True),
        ))
        if endpoint is None:
            raise HTTPException(422, detail="Register an enabled webhook URL for this account")
    job = Job(
        user_id=user.id,
        upload_id=payload.upload_id,
        operations=[operation.model_dump(exclude_none=True) for operation in payload.operations],
        webhook_url=str(payload.webhook_url) if payload.webhook_url else None,
        idempotency_key=idempotency_key,
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(Job).filter(Job.user_id == user.id, Job.idempotency_key == idempotency_key).first() if idempotency_key else None
        if existing:
            return resolve_idempotency(existing, payload)
        raise
    db.refresh(job)
    return JobResponse.model_validate(job, from_attributes=True)


def owned_job(db: Session, job_id: str, user_id: str) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/v1/jobs/dead-letters", response_model=list[JobResponse])
def list_dead_letters(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_api_key),
):
    query = (
        select(Job)
        .where(Job.user_id == user.id, Job.dead_lettered_at.is_not(None))
        .order_by(Job.dead_lettered_at.desc(), Job.id)
        .limit(limit).offset(offset)
    )
    return db.scalars(query).all()


@app.get("/v1/jobs/{job_id}", response_model=JobResponse, dependencies=[Depends(require_api_key)])
def get_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(require_api_key)) -> JobResponse:
    job = owned_job(db, job_id, user.id)
    return JobResponse.model_validate(job, from_attributes=True)


@app.get("/v1/jobs/{job_id}/attempts", response_model=list[AttemptResponse])
def list_job_attempts(
    job_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_api_key),
):
    owned_job(db, job_id, user.id)
    query = (
        select(JobAttempt)
        .where(JobAttempt.job_id == job_id)
        .order_by(JobAttempt.started_at.desc(), JobAttempt.token)
        .limit(limit).offset(offset)
    )
    return db.scalars(query).all()


@app.get("/v1/jobs/{job_id}/webhooks")
def webhook_history(job_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                    db: Session = Depends(get_db), user: User = Depends(require_api_key)):
    owned_job(db, job_id, user.id)
    events = db.scalars(select(WebhookEvent).where(WebhookEvent.job_id == job_id)
                        .order_by(WebhookEvent.id).offset(offset).limit(limit)).all()
    attempts = {event.id: [] for event in events}
    for attempt in db.scalars(select(WebhookDelivery).where(WebhookDelivery.event_id.in_(list(attempts)))
                              .order_by(WebhookDelivery.number)):
        attempts[attempt.event_id].append({
            "number": attempt.number, "started_at": as_utc(attempt.started_at),
            "ended_at": as_utc(attempt.ended_at) if attempt.ended_at else None,
            "status_code": attempt.status_code, "detail": attempt.detail,
        })
    return [{"event": json.loads(e.body), "endpoint_id": e.endpoint_id, "status": e.status,
             "next_retry_at": as_utc(e.due_at) if e.status == "pending" else None,
             "deliveries": attempts[e.id]} for e in events]


@app.post("/v1/jobs/{job_id}/retry", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
def retry_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(require_api_key)) -> JobResponse:
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.user_id == user.id, Job.status == "failed")
        .values(status="queued", progress=0, error_message=None, started_at=None, completed_at=None,
                attempt_count=0, attempt_token=None, next_retry_at=None, dead_lettered_at=None)
    )
    db.commit()
    job = owned_job(db, job_id, user.id)
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="Only failed jobs can be retried")
    return JobResponse.model_validate(job, from_attributes=True)


@app.get("/v1/jobs/{job_id}/outputs", dependencies=[Depends(require_api_key)])
def list_outputs(job_id: str, db: Session = Depends(get_db), user: User = Depends(require_api_key)):
    job = owned_job(db, job_id, user.id)
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Job is not completed")
    directory = Path(settings.local_storage_path) / "outputs" / job.id
    if job.attempt_token:
        directory /= job.attempt_token
    return {"outputs": [{"name": p.name, "url": f"/v1/jobs/{job.id}/outputs/{p.name}"} for p in sorted(directory.iterdir()) if p.is_file()]}


@app.get("/v1/jobs/{job_id}/outputs/{filename}", dependencies=[Depends(require_api_key)])
def download_output(job_id: str, filename: str, db: Session = Depends(get_db), user: User = Depends(require_api_key)):
    job = db.get(Job, job_id)
    if job is None or job.user_id != user.id or job.status != "completed":
        raise HTTPException(status_code=404, detail="Output not found")
    directory = (Path(settings.local_storage_path) / "outputs" / job.id).resolve()
    if job.attempt_token:
        directory = directory / job.attempt_token
    path = (directory / filename).resolve()
    if path.parent != directory or not path.is_file():
        raise HTTPException(status_code=404, detail="Output not found")
    return FileResponse(path, filename=path.name)


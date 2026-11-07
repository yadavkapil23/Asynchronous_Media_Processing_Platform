import json
from uuid import uuid4

from PIL import Image
from sqlalchemy import update

from .db import SessionLocal
from .models import Job, JobAttempt, Upload, now_utc
from .job_attempts import classify_error, finish_attempt, ready_for_worker
from .uploads import local_path, worker_source, MAX_PIXELS


def process_job(job_id: str):
    with SessionLocal() as db:
        token = uuid4().hex
        started_at = now_utc()
        claimed = db.execute(
            update(Job)
            .where(Job.id == job_id, ready_for_worker(started_at))
            .values(status="processing", started_at=started_at, attempt_token=token,
                    attempt_count=Job.attempt_count + 1, next_retry_at=None, error_message=None)
        )
        if claimed.rowcount != 1:
            return
        job = db.get(Job, job_id)
        number = job.attempt_count
        db.add(JobAttempt(token=token, job_id=job_id, number=number, status="processing", started_at=started_at))
        db.commit()
        try:
            upload = db.get(Upload, job.upload_id)
            source = worker_source(db, upload)
            output_dir = local_path(f"outputs/{job.id}/{token}")
            output_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(source) as image:
                image.load()
                for index, operation in enumerate(job.operations):
                    kind = operation["type"]
                    if kind == "metadata":
                        (output_dir / f"{index}.json").write_text(json.dumps({"width": image.width, "height": image.height, "format": image.format, "mode": image.mode}), encoding="utf-8")
                        continue
                    result = image.copy()
                    if kind in ("resize", "thumbnail"):
                        width, height = operation.get("width"), operation.get("height")
                        if not width or not height:
                            raise ValueError("width and height are required")
                        if width * height > MAX_PIXELS:
                            raise ValueError("Requested image exceeds the pixel limit")
                        if kind == "thumbnail":
                            result.thumbnail((width, height))
                        else:
                            result = result.resize((width, height))
                    fmt = operation.get("format", "png").lower()
                    if fmt not in ("png", "webp", "jpeg"):
                        raise ValueError("unsupported output format")
                    if fmt == "jpeg":
                        result = result.convert("RGB")
                    result.save(output_dir / f"{index}.{fmt}", format=fmt.upper())
                    db.execute(update(Job).where(Job.id == job_id, Job.status == "processing", Job.attempt_token == token).values(progress=int((index + 1) * 100 / len(job.operations))))
                    db.commit()
            outcome = "completed"
            error = None
        except Exception as exc:
            db.rollback()
            outcome, error = classify_error(exc)
        finish_attempt(db, job_id, token, number, outcome, error)
        db.commit()

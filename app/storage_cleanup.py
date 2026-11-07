"""Inspect or remove old Forge staging objects and abandoned attempt output."""
import argparse
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from .db import SessionLocal
from .models import Job, JobAttempt, Upload, now_utc
from .uploads import local_path, remove_staging

GRACE = timedelta(days=1)
OUTPUT_NAME = re.compile(r"[0-9]+\.(?:jpeg|png|webp|json)")
DIGEST = re.compile(r"[a-f0-9]{64}")


def objects(prefix):
    directory = local_path(prefix)
    if directory.is_dir():
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink():
                yield prefix + path.name, datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def clean_prefix(prefix, name_pattern, cutoff, keep, apply):
    count = 0
    for key, modified in objects(prefix):
        if key == keep or modified >= cutoff or not name_pattern.fullmatch(key.removeprefix(prefix)):
            continue
        if apply:
            remove_staging(key)
        count += 1
    return count


def clean_upload(upload_id, cutoff, apply):
    if not re.fullmatch(r"upl_[0-9a-f]{16}", upload_id):
        return 0
    with SessionLocal() as db:
        if apply:
            db.execute(update(Upload).where(Upload.id == upload_id).values(object_key=Upload.object_key))
        upload = db.get(Upload, upload_id)
        key = upload.staging_key
        expires = upload.staging_expires_at or upload.created_at + timedelta(days=7)
        known_stage = key == f"staging/{upload_id}" or re.fullmatch(r"uploads/[0-9a-f]{32}", key or "")
        count = 0
        if known_stage and expires.replace(tzinfo=timezone.utc) < cutoff:
            if upload.status != "uploaded" or key != upload.object_key:
                if apply:
                    remove_staging(key)
                    upload.staging_key = None
                    if upload.status == "pending":
                        upload.status = "expired"
                count += 1
        count += clean_prefix(f"sources/{upload_id}/", DIGEST, cutoff, upload.object_key, apply)
        db.commit()
        return count


def cleanup(apply=False):
    cutoff = now_utc() - GRACE
    with SessionLocal() as db:
        upload_ids = db.scalars(select(Upload.id).where(Upload.created_at < cutoff)).all()
        attempts = db.execute(select(JobAttempt.job_id, JobAttempt.token).join(Job).where(
            JobAttempt.ended_at < cutoff, JobAttempt.status.in_(("failed", "timed_out", "transient_error")),
            (Job.attempt_token.is_(None) | (Job.attempt_token != JobAttempt.token)),
        )).all()
    count = sum(clean_upload(upload_id, cutoff, apply) for upload_id in upload_ids)
    for job_id, token in attempts:
        if re.fullmatch(r"job_[0-9a-f]{16}", job_id) and re.fullmatch(r"[0-9a-f]{32}", token):
            count += clean_prefix(f"outputs/{job_id}/{token}/", OUTPUT_NAME, cutoff, None, apply)
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean only aged objects referenced by Forge upload or attempt metadata")
    parser.add_argument("--apply", action="store_true")
    apply = parser.parse_args().apply
    print(f"{'Removed' if apply else 'Eligible'} storage entries: {cleanup(apply)}")

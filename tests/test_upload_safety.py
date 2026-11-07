import hashlib
import io
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from support import SessionLocal, test_key
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app import storage_cleanup, uploads
from app.main import app
from app.models import Job, JobAttempt, Upload, User, now_utc
from app.worker import process_job


def picture(color="red", fmt="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (8, 4), color).save(output, fmt)
    return output.getvalue()


class UploadSafetyTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.auth = {"X-API-Key": test_key}

    def ticket(self, **fields):
        response = self.client.post("/v1/uploads", headers=self.auth, json={
            "filename": "input.png", "content_type": "image/png", **fields,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def job(self, upload_id, **fields):
        return self.client.post("/v1/jobs", headers=self.auth, json={
            "upload_id": upload_id, "operations": [{"type": "metadata"}], **fields,
        })

    def test_formats_and_rejected_bytes_leave_no_accepted_source(self):
        for fmt, mime in uploads.FORMATS.items():
            with self.subTest(fmt=fmt):
                ticket = self.ticket(content_type=mime)
                result = self.client.put(ticket["upload_url"], headers=self.auth, content=picture(fmt=fmt))
                self.assertEqual(result.status_code, 200)
        frames = [Image.new("RGB", (8, 4), color) for color in ("red", "blue")]
        animated = io.BytesIO()
        frames[0].save(animated, "PNG", save_all=True, append_images=frames[1:])
        huge = io.BytesIO()
        Image.new("1", (4001, 4000)).save(huge, "PNG")
        cases = [(b"not an image", 422), (picture()[:-12], 422),
                 (picture(fmt="JPEG"), 415), (animated.getvalue(), 415), (huge.getvalue(), 413)]
        for content, expected in cases:
            with self.subTest(expected=expected, length=len(content)):
                ticket = self.ticket()
                result = self.client.put(ticket["upload_url"], headers=self.auth, content=content)
                self.assertEqual(result.status_code, expected, result.text)
                self.assertEqual(self.job(ticket["upload_id"]).status_code, 409)
                self.assertFalse(uploads.local_path(f"sources/{ticket['upload_id']}").exists())

    def test_declared_size_stream_limit_and_operation_limits(self):
        ticket = self.ticket(size=len(picture()) + 1)
        self.assertEqual(self.client.put(ticket["upload_url"], headers=self.auth, content=picture()).status_code, 422)
        ticket = self.ticket()
        oversized = b"x" * (uploads.MAX_BYTES + 1)
        self.assertEqual(self.client.put(ticket["upload_url"], headers=self.auth, content=oversized).status_code, 413)
        self.assertFalse(uploads.local_path(f"sources/{ticket['upload_id']}").exists())
        self.assertEqual(self.client.put(ticket["upload_url"], headers=self.auth, content=picture()).status_code, 200)
        self.assertEqual(self.client.put(ticket["upload_url"], headers=self.auth, content=picture("blue")).status_code, 409)
        self.assertEqual(self.job(ticket["upload_id"], operations=[{"type": "resize", "width": 5000, "height": 5000}]).status_code, 422)
        self.assertEqual(self.job(ticket["upload_id"], operations=[{"type": "metadata"}] * 17).status_code, 422)
    def test_concurrent_uploads_publish_one_source(self):
        ticket = self.ticket()
        barrier = Barrier(2)

        def accept(content):
            with SessionLocal() as db:
                upload = db.get(Upload, ticket["upload_id"])
                barrier.wait(timeout=5)
                try:
                    return uploads.accept_upload(db, upload, content).object_key
                except uploads.UploadProblem as exc:
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(accept, picture())
            second = pool.submit(accept, picture("blue"))
            results = [first.result(timeout=10), second.result(timeout=10)]
        self.assertEqual(results.count(409), 1)
        with SessionLocal() as db:
            accepted = db.get(Upload, ticket["upload_id"])
            data = uploads.read_bytes(accepted)
            self.assertEqual(accepted.object_key, f"sources/{accepted.id}/{hashlib.sha256(data).hexdigest()}")
            self.assertEqual(len(list(uploads.local_path(f"sources/{accepted.id}").iterdir())), 1)

    def test_legacy_source_is_promoted_and_tampering_is_rejected(self):
        old_key = f"uploads/{uuid4().hex}"
        path = uploads.local_path(old_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(picture())
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == "one@example.test"))
            upload = Upload(user_id=user.id, filename="old.png", content_type="image/png", object_key=old_key,
                            staging_key=old_key, size=len(picture()), status="uploaded")
            db.add(upload)
            db.commit()
            upload_id = upload.id
        first = self.job(upload_id).json()["id"]
        process_job(first)
        self.assertEqual(self.client.get(f"/v1/jobs/{first}", headers=self.auth).json()["status"], "completed")
        with SessionLocal() as db:
            upload = db.get(Upload, upload_id)
            self.assertTrue(uploads.sealed(upload))
            source = uploads.local_path(upload.object_key)
        path.write_bytes(b"legacy replacement")
        second = self.job(upload_id).json()["id"]
        process_job(second)
        self.assertEqual(self.client.get(f"/v1/jobs/{second}", headers=self.auth).json()["status"], "completed")
        source.write_bytes(b"x" * len(picture()))
        third = self.job(upload_id).json()["id"]
        process_job(third)
        state = self.client.get(f"/v1/jobs/{third}", headers=self.auth).json()
        self.assertEqual(state["status"], "failed")
        self.assertIn("checksum", state["error_message"])

    def test_storage_paths_reject_traversal_and_links(self):
        for key in ("../secret", "/secret", "C:/secret", "sources/../secret", "sources\\secret"):
            with self.subTest(key=key), self.assertRaises(uploads.UploadProblem):
                uploads.local_path(key)
        directory = uploads.local_path("linked")
        directory.mkdir(exist_ok=True)
        with patch("pathlib.Path.is_symlink", return_value=True), self.assertRaises(uploads.UploadProblem):
            uploads.local_path("linked/file")

    def test_cleanup_preserves_sources_and_active_attempts(self):
        ticket = self.ticket()
        self.client.put(ticket["upload_url"], headers=self.auth, content=picture())
        old = now_utc() - timedelta(days=10)
        job_id = self.job(ticket["upload_id"]).json()["id"]
        abandoned, active = uuid4().hex, uuid4().hex
        with SessionLocal() as db:
            upload = db.get(Upload, ticket["upload_id"])
            accepted_key = upload.object_key
            upload.created_at, upload.staging_expires_at = old, old
            upload.staging_key = f"staging/{upload.id}"
            stage = upload.staging_key
            db.get(Job, job_id).attempt_token = active
            for token in (abandoned, active):
                db.add(JobAttempt(token=token, job_id=job_id, number=1, started_at=old, ended_at=old, status="failed"))
            db.commit()
        orphan = f"sources/{ticket['upload_id']}/{'0' * 64}"
        abandoned_output = f"outputs/{job_id}/{abandoned}/0.png"
        active_output = f"outputs/{job_id}/{active}/0.png"
        unexpected = f"outputs/{job_id}/{abandoned}/keep.txt"
        for key in (orphan, stage, abandoned_output, active_output, unexpected):
            path = uploads.local_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test")
            os.utime(path, (old.timestamp(), old.timestamp()))
        self.assertEqual(storage_cleanup.cleanup(), 3)
        self.assertTrue(uploads.local_path(orphan).exists())
        self.assertEqual(storage_cleanup.cleanup(apply=True), 3)
        for key in (orphan, stage, abandoned_output):
            self.assertFalse(uploads.local_path(key).exists())
        for key in (accepted_key, active_output, unexpected):
            self.assertTrue(uploads.local_path(key).exists())
        self.assertEqual(storage_cleanup.cleanup(apply=True), 0)
        with SessionLocal() as db:
            self.assertEqual(db.get(Upload, ticket["upload_id"]).object_key, accepted_key)


if __name__ == "__main__":
    unittest.main()

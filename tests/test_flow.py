import io
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from PIL import Image

from support import SessionLocal, test_key, other_key

from fastapi.testclient import TestClient
from app.main import app
from app.worker import process_job
from app.accounts import create_account_key, list_account_keys, revoke_account_key
from app.dispatcher import dispatch_once, recover_stale_jobs
from app.config import settings
from app.models import Job

class FlowTest(unittest.TestCase):
    def create_thumbnail_job(self, client, headers, content):
        upload = client.post("/v1/uploads", headers=headers, json={
            "filename": "input.png", "content_type": "image/png",
        }).json()
        self.assertEqual(client.put(upload["upload_url"], headers=headers, content=content).status_code, 200)
        response = client.post("/v1/jobs", headers=headers, json={
            "upload_id": upload["upload_id"],
            "operations": [{"type": "thumbnail", "width": 4, "height": 4}],
        })
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["id"]

    def test_transient_failure_backoff_exhaustion_and_replay(self):
        client = TestClient(app)
        headers = {"X-API-Key": test_key}
        other = {"X-API-Key": other_key}
        data = io.BytesIO()
        Image.new("RGB", (8, 8)).save(data, "PNG")
        job_id = self.create_thumbnail_job(client, headers, data.getvalue())
        url = f"/v1/jobs/{job_id}"
        with (
            patch("app.worker.Image.open", side_effect=TimeoutError()),
            patch.object(settings, "job_retry_base_seconds", 30),
            patch.object(settings, "job_retry_max_seconds", 45),
            patch.object(settings, "job_max_attempts", 3),
        ):
            for number, delay in enumerate((30, 45), start=1):
                process_job(job_id)
                job = client.get(url, headers=headers).json()
                self.assertEqual(job["status"], "retrying")
                self.assertEqual(job["attempt_count"], number)
                last_attempt = client.get(url + "/attempts?limit=1", headers=headers).json()[0]
                due = datetime.fromisoformat(job["next_retry_at"].replace("Z", "+00:00"))
                ended = datetime.fromisoformat(last_attempt["ended_at"].replace("Z", "+00:00"))
                self.assertEqual(due.utcoffset(), timedelta(0))
                self.assertEqual(due - ended, timedelta(seconds=delay))
                self.assertIsNone(job["completed_at"])
                self.assertEqual(dispatch_once(Mock(), job_id), 0)
                process_job(job_id)
                self.assertEqual(client.get(url, headers=headers).json()["attempt_count"], number)
                self.assertEqual(client.post(url + "/retry", headers=headers).status_code, 409)
                with SessionLocal() as db:
                    db.get(Job, job_id).next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    db.commit()
                self.assertEqual(dispatch_once(Mock(), job_id), 1)
            process_job(job_id)

        failed = client.get(url, headers=headers).json()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["attempt_count"], 3)
        self.assertIsNotNone(failed["dead_lettered_at"])
        self.assertIsNone(failed["next_retry_at"])
        self.assertEqual(dispatch_once(Mock(), job_id), 0)
        self.assertIn(job_id, {job["id"] for job in client.get("/v1/jobs/dead-letters", headers=headers).json()})
        self.assertEqual(client.get("/v1/jobs/dead-letters", headers=other).json(), [])
        self.assertEqual(client.get(url + "/attempts", headers=other).status_code, 404)
        self.assertEqual(client.post(url + "/retry", headers=other).status_code, 404)

        replay = client.post(url + "/retry", headers=headers)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["id"], job_id)
        self.assertIsNone(replay.json()["dead_lettered_at"])
        self.assertNotIn(job_id, {job["id"] for job in client.get("/v1/jobs/dead-letters", headers=headers).json()})
        process_job(job_id)
        attempts = client.get(url + "/attempts", headers=headers).json()
        self.assertEqual([a["number"] for a in attempts], [1, 3, 2, 1])
        self.assertEqual([a["status"] for a in attempts], ["completed", "transient_error", "transient_error", "transient_error"])
        self.assertTrue(all(a["ended_at"] for a in attempts))

    def test_invalid_image_fails_once(self):
        client = TestClient(app)
        headers = {"X-API-Key": test_key}
        upload = client.post("/v1/uploads", headers=headers, json={"filename": "bad.png", "content_type": "image/png"}).json()
        self.assertEqual(client.put(upload["upload_url"], headers=headers, content=b"invalid image").status_code, 422)
        response = client.post("/v1/jobs", headers=headers, json={
            "upload_id": upload["upload_id"], "operations": [{"type": "metadata"}],
        })
        self.assertEqual(response.status_code, 409)

    def test_stale_job_recovery_and_fencing(self):
        client = TestClient(app)
        headers = {"X-API-Key": test_key}
        upload = client.post("/v1/uploads", headers=headers, json={"filename": "stale.png", "content_type": "image/png"}).json()
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(image, "PNG")
        client.put(upload["upload_url"], headers=headers, content=image.getvalue())
        job_id = client.post("/v1/jobs", headers=headers, json={"upload_id": upload["upload_id"], "operations": [{"type": "metadata"}]}).json()["id"]
        old_open = Image.open

        def expire_during_processing(*args, **kwargs):
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                job.started_at = datetime.now(timezone.utc) - timedelta(seconds=settings.job_timeout_seconds + settings.job_recovery_grace_seconds + 1)
                db.commit()
            self.assertEqual(recover_stale_jobs(), (1, 0))
            return old_open(*args, **kwargs)

        with patch("app.worker.Image.open", side_effect=expire_during_processing):
            process_job(job_id)
        self.assertEqual(client.get(f"/v1/jobs/{job_id}", headers=headers).json()["status"], "queued")
        process_job(job_id)
        self.assertEqual(client.get(f"/v1/jobs/{job_id}", headers=headers).json()["status"], "completed")
        history = client.get(f"/v1/jobs/{job_id}/attempts", headers=headers).json()
        self.assertEqual([a["status"] for a in history], ["completed", "timed_out"])
        outputs = client.get(f"/v1/jobs/{job_id}/outputs", headers=headers).json()["outputs"]
        self.assertEqual(len(outputs), 1)
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            job.status = "processing"
            job.attempt_count = settings.job_max_attempts
            job.started_at = datetime.now(timezone.utc) - timedelta(seconds=settings.job_timeout_seconds + settings.job_recovery_grace_seconds + 1)
            db.commit()
        self.assertEqual(recover_stale_jobs(), (0, 1))
        exhausted = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        self.assertEqual(exhausted["status"], "failed")
        self.assertIsNotNone(exhausted["dead_lettered_at"])

    def test_retry_failed_job(self):
        client = TestClient(app)
        headers = {"X-API-Key": test_key}
        other = {"X-API-Key": other_key}
        upload = client.post("/v1/uploads", headers=headers, json={"filename": "retry.png", "content_type": "image/png"}).json()
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(image, "PNG")
        self.assertEqual(client.put(upload["upload_url"], headers=headers, content=image.getvalue()).status_code, 200)
        body = {"upload_id": upload["upload_id"], "operations": [{"type": "thumbnail", "width": 4, "height": 4}]}
        job_id = client.post("/v1/jobs", headers=headers, json=body).json()["id"]
        self.assertEqual(client.post(f"/v1/jobs/{job_id}/retry", headers=headers).status_code, 409)
        with patch("app.worker.Image.open", side_effect=ValueError("invalid image data")):
            process_job(job_id)
        self.assertEqual(client.get(f"/v1/jobs/{job_id}", headers=headers).json()["status"], "failed")
        self.assertEqual(client.post(f"/v1/jobs/{job_id}/retry", headers=other).status_code, 404)
        retried = client.post(f"/v1/jobs/{job_id}/retry", headers=headers)
        self.assertEqual(retried.status_code, 202)
        self.assertEqual(retried.json()["id"], job_id)
        self.assertEqual(retried.json()["status"], "queued")
        self.assertIsNone(retried.json()["error_message"])
        self.assertEqual(client.post(f"/v1/jobs/{job_id}/retry", headers=headers).status_code, 409)
        queue = Mock()
        self.assertEqual(dispatch_once(queue, job_id), 1)
        process_job(job_id)
        self.assertEqual(client.get(f"/v1/jobs/{job_id}", headers=headers).json()["status"], "completed")
        self.assertEqual(client.post(f"/v1/jobs/{job_id}/retry", headers=headers).status_code, 409)

    def test_key_revocation(self):
        client = TestClient(app)
        with SessionLocal() as db:
            key = create_account_key(db, "revoke@example.test")
            replacement = create_account_key(db, "revoke@example.test")
            keys = list_account_keys(db, "revoke@example.test")
            self.assertEqual(len(keys), 2)
            self.assertNotIn(key, str(keys))
            key_id = keys[0]["id"]
            with self.assertRaises(ValueError):
                revoke_account_key(db, "two@example.test", key_id)
            revoke_account_key(db, "revoke@example.test", key_id)
            revoke_account_key(db, "revoke@example.test", key_id)
            self.assertIsNotNone(list_account_keys(db, "revoke@example.test")[0]["revoked_at"])
        payload = {"filename": "test.png", "content_type": "image/png"}
        self.assertEqual(client.post("/v1/uploads", headers={"X-API-Key": key}, json=payload).status_code, 401)
        self.assertEqual(client.post("/v1/uploads", headers={"X-API-Key": replacement}, json=payload).status_code, 200)

    def test_upload_process_and_download(self):
        client = TestClient(app)
        headers = {"X-API-Key": test_key}
        response = client.post("/v1/uploads", headers=headers, json={"filename": "a.png", "content_type": "image/png"})
        self.assertEqual(response.status_code, 200)
        upload = response.json()
        other = {"X-API-Key": other_key}
        self.assertEqual(client.put(upload["upload_url"], headers=other, content=b"x").status_code, 404)
        image = io.BytesIO()
        Image.new("RGB", (20, 10), "red").save(image, format="PNG")
        self.assertEqual(client.put(upload["upload_url"], headers=headers, content=image.getvalue()).status_code, 200)
        body = {"upload_id": upload["upload_id"], "operations": [{"type": "thumbnail", "width": 5, "height": 5}, {"type": "metadata"}]}
        response = client.post("/v1/jobs", headers={**headers, "Idempotency-Key": "one"}, json=body)
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["id"]
        self.assertEqual(client.get(f"/v1/jobs/{job_id}", headers=other).status_code, 404)
        self.assertEqual(client.get(f"/v1/jobs/{job_id}/outputs", headers=other).status_code, 404)
        self.assertEqual(client.post("/v1/jobs", headers=other, json=body).status_code, 404)
        self.assertEqual(client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": "dev-key"}).status_code, 401)
        unavailable = Mock()
        unavailable.enqueue.side_effect = ConnectionError("Redis unavailable")
        with self.assertRaises(ConnectionError):
            dispatch_once(unavailable)
        self.assertEqual(client.get(f"/v1/jobs/{job_id}", headers=headers).json()["status"], "queued")
        queue = Mock()
        self.assertEqual(dispatch_once(queue), 1)
        self.assertEqual(dispatch_once(queue), 1)
        self.assertEqual(queue.enqueue.call_count, 2)
        process_job(job_id)
        process_job(job_id)
        self.assertEqual(dispatch_once(queue), 0)
        self.assertEqual(client.get(f"/v1/jobs/{job_id}", headers=headers).json()["status"], "completed")
        outputs = client.get(f"/v1/jobs/{job_id}/outputs", headers=headers).json()["outputs"]
        self.assertEqual(len(outputs), 2)
        result = client.get(outputs[0]["url"], headers=headers)
        self.assertEqual(Image.open(io.BytesIO(result.content)).size, (5, 3))
        self.assertEqual(client.get(outputs[1]["url"], headers=headers).json()["width"], 20)
        self.assertEqual(client.get(outputs[0]["url"], headers=other).status_code, 404)
        other_upload = client.post("/v1/uploads", headers=other, json={"filename": "b.png", "content_type": "image/png"}).json()
        self.assertEqual(client.put(other_upload["upload_url"], headers=other, content=image.getvalue()).status_code, 200)
        other_body = {"upload_id": other_upload["upload_id"], "operations": [{"type": "metadata"}]}
        other_job = client.post("/v1/jobs", headers={**other, "Idempotency-Key": "one"}, json=other_body)
        self.assertEqual(other_job.status_code, 202)
        self.assertNotEqual(other_job.json()["id"], job_id)
        self.assertEqual(client.post("/v1/jobs", headers={**headers, "Idempotency-Key": "one"}, json=body).json()["id"], job_id)
        body["operations"] = [{"type": "metadata"}]
        self.assertEqual(client.post("/v1/jobs", headers={**headers, "Idempotency-Key": "one"}, json=body).status_code, 409)


if __name__ == "__main__":
    unittest.main()

"""Opt-in integration test; requires a Redis instance at TEST_REDIS_URL."""
import io
import os
import subprocess
import sys
import unittest
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from PIL import Image
from fastapi.testclient import TestClient
from redis import Redis
from rq import Queue

from app.main import app
from app.dispatcher import dispatch_once, recover_stale_jobs
from app.accounts import create_account_key
from app.db import SessionLocal
from app.models import Job
from app.config import settings


@unittest.skipUnless(os.getenv("TEST_REDIS_URL"), "set TEST_REDIS_URL to run with Redis")
class RedisIntegrationTest(unittest.TestCase):
    def test_dispatch_and_worker_process(self):
        self.run_flow("normal")

    def test_worker_crash_recovery(self):
        self.run_flow("crash")

    def test_transient_failure_reaches_another_worker(self):
        self.run_flow("transient_error")

    def run_flow(self, scenario):
        redis_url = os.environ["TEST_REDIS_URL"]
        connection = Redis.from_url(redis_url)
        connection.ping()
        queue_name = "forge-test-" + uuid4().hex
        queue = Queue(queue_name, connection=connection)
        client = TestClient(app)
        with SessionLocal() as db:
            api_key = create_account_key(db, f"{uuid4().hex}@example.test")
        headers = {"X-API-Key": api_key}
        upload = client.post("/v1/uploads", headers=headers, json={"filename": "integration.png", "content_type": "image/png"}).json()
        image = io.BytesIO()
        Image.new("RGB", (24, 12), "blue").save(image, "PNG")
        self.assertEqual(client.put(upload["upload_url"], headers=headers, content=image.getvalue()).status_code, 200)
        response = client.post("/v1/jobs", headers=headers, json={"upload_id": upload["upload_id"], "operations": [{"type": "resize", "width": 8, "height": 4}]})
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["id"]
        worker = None
        try:
            self.assertEqual(dispatch_once(queue, job_id), 1)
            if scenario == "crash":
                code = "import time; import app.worker as w; w.Image.open = lambda *a, **k: time.sleep(30); from rq.cli.cli import main; main()"
                worker = subprocess.Popen([sys.executable, "-c", code, "worker", "--url", redis_url, "--worker-class", "rq.worker.SimpleWorker", "--burst", queue_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    state = client.get(f"/v1/jobs/{job_id}", headers=headers).json()["status"]
                    if state == "processing":
                        break
                    if worker.poll() is not None:
                        self.fail("Test worker exited before claiming the job")
                    time.sleep(0.1)
                self.assertEqual(state, "processing")
                worker.kill()
                worker.wait(timeout=5)
                self.assertEqual(recover_stale_jobs(job_id), (0, 0))
                with SessionLocal() as db:
                    job = db.get(Job, job_id)
                    first_token = job.attempt_token
                    job.started_at = datetime.now(timezone.utc) - timedelta(seconds=settings.job_timeout_seconds + settings.job_recovery_grace_seconds + 1)
                    db.commit()
                self.assertEqual(recover_stale_jobs(job_id), (1, 0))
                self.assertEqual(dispatch_once(queue, job_id), 1)
            elif scenario == "transient_error":
                code = (
                    "from unittest.mock import Mock; import app.worker as w; "
                    "w.Image.open = Mock(side_effect=TimeoutError()); from rq.cli.cli import main; main()"
                )
                failed = subprocess.run(
                    [sys.executable, "-c", code, "worker", "--url", redis_url, "--worker-class",
                     "rq.worker.SimpleWorker", "--burst", queue_name],
                    env={**os.environ, "JOB_RETRY_BASE_SECONDS": "2", "JOB_MAX_ATTEMPTS": "3"},
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(failed.returncode, 0, failed.stdout + failed.stderr)
                waiting = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
                self.assertEqual(waiting["status"], "retrying")
                self.assertEqual(waiting["attempt_count"], 1)
                deadline = time.monotonic() + 15
                while not dispatch_once(queue, job_id):
                    self.assertLess(time.monotonic(), deadline, "Scheduled retry never became eligible")
                    time.sleep(0.1)
            result = subprocess.run([sys.executable, "-c", "from rq.cli.cli import main; main()", "worker", "--url", redis_url, "--worker-class", "rq.worker.SimpleWorker", "--burst", queue_name], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(client.get(f"/v1/jobs/{job_id}", headers=headers).json()["status"], "completed", result.stdout + result.stderr)
            outputs = client.get(f"/v1/jobs/{job_id}/outputs", headers=headers).json()["outputs"]
            result = client.get(outputs[0]["url"], headers=headers)
            self.assertEqual(Image.open(io.BytesIO(result.content)).size, (8, 4))
            if scenario == "crash":
                with SessionLocal() as db:
                    job = db.get(Job, job_id)
                    self.assertEqual(job.attempt_count, 2)
                    self.assertNotEqual(job.attempt_token, first_token)
            if scenario == "transient_error":
                attempts = client.get(f"/v1/jobs/{job_id}/attempts", headers=headers).json()
                self.assertEqual([a["status"] for a in attempts], ["completed", "transient_error"])
                self.assertEqual([a["number"] for a in attempts], [2, 1])
        finally:
            if worker is not None and worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
            # The queue has a unique name and is only used by this test.
            queue.delete(delete_jobs=True)


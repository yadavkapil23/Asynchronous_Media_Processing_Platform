import hashlib
import hmac
import io
import json
import socket
import ssl
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from support import SessionLocal, test_key, other_key
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import select
import trustme

from app import webhook_worker, webhooks
from app.config import settings
from app.job_attempts import finish_attempt
from app.main import app
from app.models import Job, JobAttempt, WebhookDelivery, WebhookEvent, now_utc
from app.worker import process_job


def addresses(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


@contextmanager
def receiver(secret, statuses=(500, 204)):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            timestamp = self.headers["X-Webhook-Timestamp"]
            digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
            received.append({"body": body, "id": self.headers["X-Webhook-Id"],
                             "valid": hmac.compare_digest("sha256=" + digest, self.headers["X-Webhook-Signature"]),
                             "recent": abs(time.time() - int(timestamp)) < 300})
            self.send_response(statuses[min(len(received) - 1, len(statuses) - 1)])
            self.send_header("Location", "/redirected")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    ca = trustme.CA()
    certificate = ca.issue_cert("receiver.example.test")
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certificate.configure_cert(server_context)
    client_context = ssl.create_default_context()
    ca.configure_trust(client_context)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield received, server.server_port, client_context
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class WebhookTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.auth, self.other = {"X-API-Key": test_key}, {"X-API-Key": other_key}
        self.master = patch.object(settings, "webhook_master_secret", SecretStr("test-secret-" + "a" * 64))
        self.master.start()
        self.addCleanup(self.master.stop)
        self.endpoints = []

    def tearDown(self):
        for endpoint in self.endpoints:
            self.client.delete(f"/v1/webhooks/{endpoint['id']}", headers=self.auth)

    def endpoint(self):
        with patch("app.webhooks.socket.getaddrinfo", return_value=addresses("93.184.216.34")):
            result = self.client.post("/v1/webhooks", headers=self.auth, json={
                "url": f"https://receiver.example.test/{uuid4().hex}",
            })
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        endpoint = result.json()
        self.endpoints.append(endpoint)
        return endpoint

    def job(self, endpoint):
        content = io.BytesIO()
        Image.new("RGB", (8, 4), "red").save(content, "PNG")
        ticket = self.client.post("/v1/uploads", headers=self.auth, json={
            "filename": "input.png", "content_type": "image/png",
        }).json()
        self.assertEqual(self.client.put(ticket["upload_url"], headers=self.auth, content=content.getvalue()).status_code, 200)
        result = self.client.post("/v1/jobs", headers=self.auth, json={
            "upload_id": ticket["upload_id"], "operations": [{"type": "metadata"}], "webhook_url": endpoint["url"],
        })
        self.assertEqual(result.status_code, 202, result.text)
        return result.json()["id"]

    def history(self, job_id):
        return self.client.get(f"/v1/jobs/{job_id}/webhooks", headers=self.auth).json()

    def make_due(self, event_id):
        with SessionLocal() as db:
            db.get(WebhookEvent, event_id).due_at = now_utc() - timedelta(seconds=1)
            db.commit()

    def test_real_tls_delivery_signature_and_retry(self):
        endpoint = self.endpoint()
        job_id = self.job(endpoint)
        process_job(job_id)
        process_job(job_id)
        history = self.history(job_id)
        self.assertEqual(len(history), 1)
        event_id = history[0]["event"]["event_id"]
        actual_pool = webhooks.urllib3.HTTPSConnectionPool
        with receiver(endpoint["signing_secret"]) as (received, port, context):
            def route(address, **kwargs):
                self.assertEqual(address, "93.184.216.34")
                self.assertEqual(kwargs["server_hostname"], "receiver.example.test")
                self.assertEqual(kwargs["assert_hostname"], "receiver.example.test")
                return actual_pool("127.0.0.1", port=port, server_hostname=kwargs["server_hostname"],
                                   assert_hostname=kwargs["assert_hostname"], ssl_context=context)

            with patch("app.webhooks.destination", return_value=(urlsplit(endpoint["url"]), "93.184.216.34")), \
                    patch("app.webhooks.urllib3.HTTPSConnectionPool", side_effect=route):
                webhook_worker.deliver(event_id)
                self.assertEqual(self.history(job_id)[0]["status"], "pending")
                webhook_worker.deliver(event_id)
                self.assertEqual(len(received), 1)
                self.make_due(event_id)
                webhook_worker.deliver(event_id)
        self.assertEqual(len(received), 2)
        self.assertTrue(all(item["valid"] and item["recent"] for item in received))
        self.assertEqual(received[0]["body"], received[1]["body"])
        self.assertEqual(received[0]["id"], event_id)
        self.assertEqual(json.loads(received[0]["body"])["data"]["outputs_url"], f"/v1/jobs/{job_id}/outputs")
        history = self.history(job_id)[0]
        self.assertEqual(history["status"], "delivered")
        self.assertEqual([delivery["status_code"] for delivery in history["deliveries"]], [500, 204])
        self.assertNotIn(endpoint["signing_secret"], json.dumps(history))
        self.assertEqual(self.client.get(f"/v1/jobs/{job_id}/webhooks", headers=self.other).status_code, 404)

    def test_destination_checks_and_dns_rebinding(self):
        for url in ("http://example.test", "https://example.test:444", "https://a:b@example.test",
                    "https://example.test/#fragment", "https://example.test/\nprivate"):
            with self.subTest(url=url), self.assertRaises(webhooks.UnsafeDestination):
                webhooks.destination(url)
        for ips in [("127.0.0.1",), ("10.0.0.1",), ("169.254.169.254",), ("168.63.129.16",),
                    ("224.0.0.1",), ("93.184.216.34", "192.168.1.1")]:
            with self.subTest(ips=ips), patch("app.webhooks.socket.getaddrinfo", return_value=addresses(*ips)):
                with self.assertRaises(webhooks.UnsafeDestination):
                    webhooks.destination("https://receiver.example.test/")
        endpoint = self.endpoint()
        job_id = self.job(endpoint)
        process_job(job_id)
        event_id = self.history(job_id)[0]["event"]["event_id"]
        with patch("app.webhooks.socket.getaddrinfo", return_value=addresses("127.0.0.1")), \
                patch("app.webhooks.urllib3.HTTPSConnectionPool") as pool:
            webhook_worker.deliver(event_id)
            pool.assert_not_called()
        self.assertEqual(self.history(job_id)[0]["status"], "failed")

    def test_https_redirect_is_not_followed(self):
        endpoint = self.endpoint()
        job_id = self.job(endpoint)
        process_job(job_id)
        event_id = self.history(job_id)[0]["event"]["event_id"]
        actual_pool = webhooks.urllib3.HTTPSConnectionPool
        with receiver(endpoint["signing_secret"], (302, 204)) as (received, port, context):
            def route(address, **kwargs):
                return actual_pool("127.0.0.1", port=port, server_hostname=kwargs["server_hostname"],
                                   assert_hostname=kwargs["assert_hostname"], ssl_context=context)

            with patch("app.webhooks.destination", return_value=(urlsplit(endpoint["url"]), "93.184.216.34")), \
                    patch("app.webhooks.urllib3.HTTPSConnectionPool", side_effect=route):
                webhook_worker.deliver(event_id)
        self.assertEqual(len(received), 1)
        self.assertEqual(self.history(job_id)[0]["status"], "failed")

    def test_concurrent_delivery_claims_send_once(self):
        endpoint = self.endpoint()
        job_id = self.job(endpoint)
        process_job(job_id)
        event_id = self.history(job_id)[0]["event"]["event_id"]
        started, release = threading.Event(), threading.Event()

        def send(*args):
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return 204

        with patch("app.webhooks.post_event", side_effect=send) as sender, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(webhook_worker.deliver, event_id)
            try:
                self.assertTrue(started.wait(timeout=5))
                second = pool.submit(webhook_worker.deliver, event_id)
                second.result(timeout=3)
            finally:
                release.set()
            first.result(timeout=5)
            sender.assert_called_once()
        self.assertEqual(self.history(job_id)[0]["status"], "delivered")

    def test_ownership_disable_and_secret_visibility(self):
        endpoint = self.endpoint()
        self.assertNotIn(endpoint["signing_secret"], self.client.get("/v1/webhooks", headers=self.auth).text)
        self.assertNotIn(endpoint["id"], self.client.get("/v1/webhooks", headers=self.other).text)
        self.assertEqual(self.client.delete(f"/v1/webhooks/{endpoint['id']}", headers=self.other).status_code, 404)
        job_id = self.job(endpoint)
        upload_id = self.client.get(f"/v1/jobs/{job_id}", headers=self.auth).json()["upload_id"]
        with patch("app.webhooks.socket.getaddrinfo", return_value=addresses("93.184.216.34")):
            self.assertEqual(self.client.post("/v1/webhooks", headers=self.auth, json={"url": endpoint["url"]}).status_code, 409)
        self.client.delete(f"/v1/webhooks/{endpoint['id']}", headers=self.auth)
        process_job(job_id)
        self.assertEqual(self.history(job_id)[0]["status"], "cancelled")
        result = self.client.post("/v1/jobs", headers=self.auth, json={
            "upload_id": upload_id, "operations": [{"type": "metadata"}], "webhook_url": endpoint["url"],
        })
        self.assertEqual(result.status_code, 422)
        with patch.object(settings, "webhook_master_secret", SecretStr("")):
            self.assertEqual(self.client.post("/v1/webhooks", headers=self.auth, json={"url": endpoint["url"]}).status_code, 503)

    def test_failed_jobs_notify_once_and_transient_jobs_wait(self):
        endpoint = self.endpoint()
        job_id = self.job(endpoint)
        with patch("app.worker.Image.open", side_effect=TimeoutError("temporary")):
            process_job(job_id)
        self.assertEqual(self.history(job_id), [])
        with SessionLocal() as db:
            db.get(Job, job_id).next_retry_at = now_utc() - timedelta(seconds=1)
            db.commit()
        with patch("app.worker.Image.open", side_effect=ValueError("invalid media")):
            process_job(job_id)
        process_job(job_id)
        history = self.history(job_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["event"]["event"], "job.failed")
        self.assertEqual(history[0]["event"]["data"]["error"], "invalid media")

    def test_outbox_rolls_back_with_job_completion(self):
        endpoint = self.endpoint()
        job_id, token = self.job(endpoint), uuid4().hex
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            job.status, job.attempt_token, job.attempt_count = "processing", token, 1
            db.add(JobAttempt(job_id=job_id, token=token, number=1, status="processing", started_at=now_utc()))
            db.commit()
            self.assertEqual(finish_attempt(db, job_id, token, 1, "completed"), "completed")
            db.flush()
            self.assertIsNotNone(db.scalar(select(WebhookEvent).where(WebhookEvent.job_id == job_id)))
            db.rollback()
            self.assertEqual(db.get(Job, job_id).status, "processing")
            self.assertIsNone(db.scalar(select(WebhookEvent).where(WebhookEvent.job_id == job_id)))
            finish_attempt(db, job_id, token, 1, "completed")
            db.commit()
            self.assertIsNone(finish_attempt(db, job_id, token, 1, "completed"))
        self.assertEqual(len(self.history(job_id)), 1)

    def test_retry_exhaustion_redirect_and_expired_delivery_lease(self):
        endpoint = self.endpoint()
        job_id = self.job(endpoint)
        process_job(job_id)
        event_id = self.history(job_id)[0]["event"]["event_id"]
        with patch("app.webhooks.post_event", return_value=503) as send:
            for _ in range(5):
                self.make_due(event_id)
                webhook_worker.deliver(event_id)
            webhook_worker.deliver(event_id)
            self.assertEqual(send.call_count, 5)
        self.assertEqual(self.history(job_id)[0]["status"], "failed")
        job_id = self.job(endpoint)
        process_job(job_id)
        event_id = self.history(job_id)[0]["event"]["event_id"]
        with SessionLocal() as db:
            event = db.get(WebhookEvent, event_id)
            event.status, event.attempt_count, event.due_at = "sending", 1, now_utc() - timedelta(seconds=1)
            db.add(WebhookDelivery(event_id=event_id, number=1, started_at=now_utc() - timedelta(seconds=70)))
            db.commit()
        with patch("app.webhooks.post_event", return_value=302) as send:
            webhook_worker.deliver(event_id)
            webhook_worker.deliver(event_id)
            send.assert_called_once()
        history = self.history(job_id)[0]
        self.assertEqual(history["status"], "failed")
        self.assertEqual(history["deliveries"][0]["detail"], "Delivery worker lease expired")
        self.assertEqual(history["deliveries"][1]["status_code"], 302)


if __name__ == "__main__":
    unittest.main()

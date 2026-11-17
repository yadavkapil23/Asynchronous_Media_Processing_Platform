import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app import monitoring


class MonitoringTest(unittest.TestCase):
    def setUp(self):
        monitoring._local_windows.clear()
        self.client = TestClient(app)

    def test_rate_limit_returns_headers_and_429(self):
        with patch.object(settings, "rate_limit_requests", 2), \
                patch.object(settings, "rate_limit_window_seconds", 60), \
                patch("redis.Redis.from_url", side_effect=RuntimeError("redis unavailable")):
            first = self.client.get("/v1/jobs/missing")
            second = self.client.get("/v1/jobs/missing")
            limited = self.client.get("/v1/jobs/missing")
        self.assertEqual(first.status_code, 401)
        self.assertEqual(second.status_code, 401)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["Retry-After"], "60")
        self.assertEqual(limited.headers["X-RateLimit-Remaining"], "0")

    def test_metrics_exposes_request_counters(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        metrics = self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("forge_requests_total", metrics.text)
        self.assertIn("forge_request_duration_seconds_sum", metrics.text)


if __name__ == "__main__":
    unittest.main()

import threading
import time
import os
from collections import Counter

from fastapi import HTTPException, Request

from .config import settings

_lock = threading.Lock()
_local_windows = {}
_metrics = Counter()
_redis_client = None


def request_key(request: Request) -> str:
    return request.headers.get("X-API-Key") or (request.client.host if request.client else "unknown")


def allow_request(request: Request) -> tuple[bool, int]:
    now = int(time.time())
    window = now // settings.rate_limit_window_seconds
    key = f"forge:rate:{window}:{request_key(request)}"
    use_redis = not settings.database_url.startswith("sqlite") or bool(os.getenv("TEST_REDIS_URL"))
    try:
        if not use_redis:
            raise RuntimeError("local test mode")
        global _redis_client
        if _redis_client is None:
            from redis import Redis
            _redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=0.1,
                                           socket_timeout=0.1, retry_on_timeout=False)
        count = _redis_client.incr(key)
        if count == 1:
            _redis_client.expire(key, settings.rate_limit_window_seconds + 1)
    except Exception:
        _redis_client = None
        with _lock:
            local_key = (window, request_key(request))
            count = _local_windows.get(local_key, 0) + 1
            _local_windows[local_key] = count
            for old_key in tuple(_local_windows):
                if old_key[0] < window - 1:
                    del _local_windows[old_key]
    remaining = max(0, settings.rate_limit_requests - count)
    return count <= settings.rate_limit_requests, remaining


def enforce_rate_limit(request: Request):
    if not request.url.path.startswith("/v1/"):
        return
    allowed, remaining = allow_request(request)
    record_request(request.method, request.url.path, 200 if allowed else 429, 0)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded",
                            headers={"Retry-After": str(settings.rate_limit_window_seconds),
                                     "X-RateLimit-Limit": str(settings.rate_limit_requests),
                                     "X-RateLimit-Remaining": "0"})


def record_request(method: str, path: str, status_code: int, duration: float):
    with _lock:
        _metrics["requests_total"] += 1
        _metrics[f"requests_{method.lower()}_total"] += 1
        _metrics[f"responses_{status_code // 100}xx_total"] += 1
        _metrics["request_duration_seconds_sum"] += duration


def metrics_text() -> str:
    with _lock:
        snapshot = dict(_metrics)
    lines = [
        "# HELP forge_requests_total Total HTTP requests.",
        "# TYPE forge_requests_total counter",
        f"forge_requests_total {snapshot.get('requests_total', 0)}",
        "# HELP forge_request_duration_seconds_sum Total request duration in seconds.",
        "# TYPE forge_request_duration_seconds_sum counter",
        f"forge_request_duration_seconds_sum {snapshot.get('request_duration_seconds_sum', 0):.6f}",
    ]
    for name, value in sorted(snapshot.items()):
        if name.startswith("responses_") or name.startswith("requests_") and name != "requests_total":
            lines.append(f"forge_{name} {value}")
    return "\n".join(lines) + "\n"

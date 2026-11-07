import hashlib
import hmac
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit
from uuid import uuid4

import urllib3
from sqlalchemy import select

from .config import settings
from .models import Job, WebhookEndpoint, WebhookEvent


class UnsafeDestination(ValueError):
    pass


def destination(url):
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.port not in (None, 443)
            or parts.username is not None or parts.password is not None or parts.fragment
            or len(url) > 2048 or any(ord(c) <= 32 for c in url)):
        raise UnsafeDestination("Use a public HTTPS URL on port 443 without credentials or a fragment")
    hosts = socket.getaddrinfo(parts.hostname, 443, socket.AF_INET, socket.SOCK_STREAM)
    addresses = [ipaddress.IPv4Address(result[4][0]) for result in hosts]
    if not addresses or any(
        not ip.is_global or ip.is_multicast or ip.is_reserved or str(ip) == "168.63.129.16"
        for ip in addresses
    ):
        raise UnsafeDestination("Webhook destination must resolve to public IPv4 addresses")
    return parts, str(addresses[0])


def endpoint_secret(endpoint_id):
    master = settings.webhook_master_secret.get_secret_value().encode()
    if len(master) < 32:
        raise RuntimeError("Configure WEBHOOK_MASTER_SECRET with at least 32 random bytes")
    return hmac.new(master, f"forge-webhook-v1:{endpoint_id}".encode(), hashlib.sha256).hexdigest()


def signature(secret, timestamp, body):
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256)
    return "sha256=" + digest.hexdigest()


def post_event(url, body, headers):
    parts, address = destination(url)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    with urllib3.HTTPSConnectionPool(
        address, port=443, server_hostname=parts.hostname, assert_hostname=parts.hostname,
        ssl_context=ssl.create_default_context(),
    ) as pool:
        response = pool.urlopen(
            "POST", target, body=body, headers={**headers, "Host": parts.hostname},
            timeout=urllib3.Timeout(connect=3, read=7), redirect=False, retries=False, preload_content=False,
        )
        try:
            return response.status
        finally:
            response.close()


def record_terminal_event(db, job_id, status, now):
    job = db.get(Job, job_id)
    if not job.webhook_url:
        return
    endpoint = db.scalar(select(WebhookEndpoint).where(
        WebhookEndpoint.user_id == job.user_id, WebhookEndpoint.url == job.webhook_url,
    ))
    if endpoint is None:
        return
    event_id = f"evt_{uuid4().hex}"
    data = {"job_id": job.id, "upload_id": job.upload_id, "status": status}
    if status == "completed":
        data["outputs_url"] = f"/v1/jobs/{job.id}/outputs"
    else:
        data["error"] = job.error_message
    body = json.dumps({
        "event_id": event_id, "event": f"job.{status}", "created_at": now.isoformat(), "data": data,
    }, separators=(",", ":"))
    db.add(WebhookEvent(id=event_id, job_id=job.id, endpoint_id=endpoint.id, body=body,
                        due_at=now, status="pending" if endpoint.enabled else "cancelled"))

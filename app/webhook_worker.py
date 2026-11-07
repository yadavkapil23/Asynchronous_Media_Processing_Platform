import logging
import time
from datetime import timedelta

from sqlalchemy import select, update
from urllib3.exceptions import HTTPError, SSLError

from . import webhooks
from .db import SessionLocal
from .models import WebhookDelivery, WebhookEndpoint, WebhookEvent, now_utc

DELAYS = (30, 120, 600, 3600)
LEASE_SECONDS = 60
log = logging.getLogger(__name__)


def deliver(event_id):
    now = now_utc()
    with SessionLocal() as db:
        event = db.get(WebhookEvent, event_id)
        if event is None or event.status not in {"pending", "sending"}:
            return
        endpoint = db.get(WebhookEndpoint, event.endpoint_id)
        previous, was_sending = event.attempt_count, event.status == "sending"
        claim = update(WebhookEvent).where(
            WebhookEvent.id == event_id, WebhookEvent.due_at <= now,
            WebhookEvent.status == event.status, WebhookEvent.attempt_count == previous,
        )
        stop = not endpoint.enabled or previous >= len(DELAYS) + 1
        number = previous + 1
        if stop:
            claim = claim.values(status="failed" if endpoint.enabled else "cancelled")
        else:
            secret = webhooks.endpoint_secret(endpoint.id)
            claim = claim.values(status="sending", attempt_count=number, due_at=now + timedelta(seconds=LEASE_SECONDS))
        if db.execute(claim.execution_options(synchronize_session=False)).rowcount != 1:
            return
        if was_sending:
            db.execute(update(WebhookDelivery).where(
                WebhookDelivery.event_id == event_id, WebhookDelivery.number == previous,
                WebhookDelivery.ended_at.is_(None),
            ).values(ended_at=now, detail="Delivery worker lease expired"))
        if stop:
            db.commit()
            return
        body, url = event.body.encode(), endpoint.url
        db.add(WebhookDelivery(event_id=event_id, number=number, started_at=now))
        db.commit()

    timestamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Webhook-Id": event_id, "X-Webhook-Timestamp": timestamp,
               "X-Webhook-Signature": webhooks.signature(secret, timestamp, body)}
    code, detail = None, None
    try:
        code = webhooks.post_event(url, body, headers)
        success = 200 <= code < 300
        retry = code in {408, 425, 429} or 500 <= code < 600
    except (webhooks.UnsafeDestination, SSLError) as exc:
        detail, success, retry = type(exc).__name__, False, False
    except (HTTPError, OSError) as exc:
        detail, success, retry = type(exc).__name__, False, True
    ended = now_utc()
    status, due_at = ("delivered" if success else "failed"), ended
    if retry and number <= len(DELAYS):
        status, due_at = "pending", ended + timedelta(seconds=DELAYS[number - 1])
    with SessionLocal() as db:
        changed = db.execute(update(WebhookEvent).where(
            WebhookEvent.id == event_id, WebhookEvent.status == "sending", WebhookEvent.attempt_count == number,
        ).values(status=status, due_at=due_at))
        if changed.rowcount == 1:
            db.execute(update(WebhookDelivery).where(
                WebhookDelivery.event_id == event_id, WebhookDelivery.number == number,
            ).values(ended_at=ended, status_code=code, detail=detail))
        db.commit()


def run_once():
    with SessionLocal() as db:
        ids = db.scalars(select(WebhookEvent.id).where(
            WebhookEvent.status.in_(("pending", "sending")), WebhookEvent.due_at <= now_utc(),
        ).order_by(WebhookEvent.due_at, WebhookEvent.id).limit(50)).all()
    for event_id in ids:
        deliver(event_id)
    return len(ids)


def main():
    logging.basicConfig(level=logging.INFO)
    webhooks.endpoint_secret("startup-check")
    while True:
        try:
            run_once()
        except Exception:
            log.exception("Webhook delivery pass interrupted")
        time.sleep(1)


if __name__ == "__main__":
    main()

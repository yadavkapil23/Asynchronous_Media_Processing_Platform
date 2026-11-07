# Forge - Async Media Processing Platform

Forge is a backend service for uploading media and processing it asynchronously.

See [ARCHITECTURE.md](ARCHITECTURE.md) for system diagrams, request flows, data models and design tradeoffs.

## Current features

- FastAPI REST API
- SQLite or PostgreSQL database
- Account-scoped API-key authentication
- Upload metadata and job state tracking
- Idempotent job creation
- Local filesystem storage
- Job states include `queued`, `processing`, `retrying`, `completed`, and `failed`
- Verified image uploads with immutable accepted sources
- Signed webhooks with durable delivery history and retries

This development backend supports local filesystem storage and an RQ worker backed by Redis. Local upload URLs are API paths that require the API key. The current processing scope is still images, with resize, thumbnail, conversion and metadata operations.

## Run the full stack with Docker

For a new checkout, copy `.env.example` to `.env` and set `WEBHOOK_MASTER_SECRET` to a random value of at least 32 bytes. Preserve an existing `.env` and its signing secret. Then run these commands from the project directory.

```powershell
docker compose up -d --build
docker compose ps -a
docker compose exec api python -m app.accounts you@example.com
```

Save the API key printed by the account command. The API is available at http://127.0.0.1:8000/docs. Compose runs PostgreSQL, Redis, the API, an RQ worker, the dispatcher and the webhook delivery worker. A one-time migration service must finish successfully before the application services start. Its exited status with code 0 is expected. The Linux processing worker uses RQ's normal process-based worker.

Application containers run as an unprivileged user. The API and processing worker share the `media-data` volume. PostgreSQL and Redis retain their own volumes. Compose uses PostgreSQL and container hostnames independently of host-only database or Redis settings in `.env`. It preserves the existing PostgreSQL volume but does not copy files from host `data/storage` or migrate a host SQLite database. Existing host media must be copied into the media volume before those records can serve files from containers.

The development ports bind to localhost. Public deployment, TLS and production credentials still need separate configuration. Keep the default database password limited to local development. Image builds exclude `.env`, local data, database backups and tests.

```powershell
docker compose logs --tail 100 api worker dispatcher webhooks
docker compose exec api python -m app.storage_cleanup
docker compose stop
docker compose start
```

`docker compose down` removes containers while retaining volumes. Do not add `--volumes` unless you intend to erase stored data. Before upgrading an existing installation, back up PostgreSQL and the media volume, stop the application services, then run `docker compose up -d --build`. Container restarts alone do not pick up code changes.

After installing `requirements-dev.txt` on the host, run `python scripts/compose_smoke.py` to check a real upload, queued image transformation and download. Add `--restart` to also restart the API and worker and verify the output persists. The probe retains its small sample job and files for inspection, but revokes its temporary API key afterward. The restart option briefly interrupts the local API.

## Run locally without application containers

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
alembic upgrade head
python -m app.accounts you@example.com
uvicorn app.main:app --reload
```

Start Redis on localhost:6379 (for Docker Desktop, `docker compose up -d redis`). Run `python -m app.dispatcher` in one terminal and `rq worker --worker-class rq.worker.SimpleWorker` in another. Set `REDIS_URL` for the dispatcher and pass the same URL to the worker with `--url` if using a non-default Redis. The dispatcher republishes queued jobs every 30 seconds; workers atomically claim them, so duplicate messages are ignored. Upload raw bytes with `PUT` to the returned path, including the `X-API-Key` header. Outputs are saved in `data/storage/outputs/<job_id>/<attempt>/`.

Install test dependencies with `pip install -r requirements-dev.txt`. Run `python -m unittest discover -s tests -v` for the local test. To run the Redis integration test separately, set `TEST_REDIS_URL=redis://localhost:6379/0`, `DATABASE_URL` and `LOCAL_STORAGE_PATH` to isolated test locations, then run `python -m unittest discover -s tests -p test_redis_integration.py -v`. The integration test creates records and output files in those locations.

The Redis tests use separate worker processes. One test terminates a worker after it claims a job and advances its database timestamp to simulate timeout expiry. Another injects a temporary failure, waits through a short retry delay, then verifies that a second worker finishes the job.

## Database setup and migrations

New databases require `alembic upgrade head` before starting the API or workers. SQLite remains the default. For local PostgreSQL, run `docker compose up -d postgres`, set `DATABASE_URL=postgresql+psycopg://forge:forge_dev@127.0.0.1:55432/forge` in `.env`, and run the migration. The sample password is for local development only. All processes must share the same database configuration.

Older SQLite databases created by the previous prototype already have the initial tables but no migration history. Back up the database first, verify that its schema matches `migrations/versions/0001_initial.py`, then run `alembic stamp 0001_initial` once, followed by `alembic upgrade head`. Do not stamp an empty or mismatched database. PostgreSQL starts empty and does not require stamping. This migration changes schema only; it does not copy existing SQLite data to PostgreSQL.

This basic dispatcher can accumulate duplicate queue messages during a backlog, and handles at most 100 queued jobs per pass. It is suitable for development; a production dispatcher needs bounded deduplication, fairness and monitoring.

Once a job completes, list outputs at `GET /v1/jobs/{job_id}/outputs` and download them using the returned paths. Metadata operations produce JSON files. Register a webhook endpoint before including its URL in a job.

## Processing retries and dead letters

Connection failures, timeouts and temporary I/O errors put a job in `retrying`. The job exposes its UTC `next_retry_at` and `attempt_count`. By default the second attempt becomes eligible after 30 seconds and the third after another 60 seconds. The dispatcher picks up due jobs on its next pass. Invalid media and other permanent errors go straight to `failed`.

The initial attempt counts toward `JOB_MAX_ATTEMPTS` (default 3). `JOB_RETRY_BASE_SECONDS` (default 30) doubles with each scheduled retry up to `JOB_RETRY_MAX_SECONDS` (default 300). A worker that exceeds `JOB_TIMEOUT_SECONDS` plus `JOB_RECOVERY_GRACE_SECONDS` has already spent its timeout waiting, so the dispatcher can requeue that attempt immediately. All attempts share the same budget. Configure these values consistently across processes and keep host clocks synchronized.

Exhausted jobs stay in the database with status `failed` and a `dead_lettered_at` timestamp. The owner's API key grants access to these operations.

- `GET /v1/jobs/dead-letters` lists exhausted jobs, newest first.
- `GET /v1/jobs/{job_id}/attempts` shows start and end times, outcome and error for each recorded attempt. Both lists accept `limit` (1 to 100, default 50) and `offset`.
- `POST /v1/jobs/{job_id}/retry` requeues a failed job with the same ID and a fresh attempt budget. Replay clears its dead-letter marker and retains the attempt history. Jobs in any other state return 409.

Each attempt writes to its own output location. The current attempt token controls which worker can publish results or close the attempt. The storage cleanup command removes aged files from abandoned attempts.

For an existing installation, stop the API, workers and dispatcher, run `alembic upgrade head`, then restart them together. Migration 0004 imports each last known attempt from 0003 when its token and start time are available. Older completed jobs keep their output paths. Upgrades from 0002 also apply 0003, which marks jobs interrupted during that upgrade as failed for manual retry.

Migration 0005 adds staging metadata and webhook tables without deleting existing uploads or jobs. Existing uploaded images are validated and copied to immutable source keys when first processed by the new worker. Completed jobs retain their existing output paths. Keep the database and storage backups together.

## Upload safety

Only single-frame PNG, JPEG and WebP images are accepted. Files must be at most 20 MiB and 16 million pixels. Validation checks the actual format against the declared MIME type, verifies the file structure and fully decodes the pixels before acceptance. Empty files, truncated images and animations are rejected. Output dimensions are also limited to 16 million pixels, with at most 16 operations per job.

Accepted bytes are saved at `sources/<upload_id>/<sha256>`. The worker checks that checksum before processing. Local PUT accepts content once and returns 409 on replacement. Concurrent uploads publish only one source. A failed local PUT may be corrected using the same pending ticket.

Validation is not a malware scan or a resource quota. Apply request rate limits, per-account storage quotas and worker resource limits before offering public access. Keep the storage directory private to the service.

## Webhooks

Set `WEBHOOK_MASTER_SECRET` to a securely generated value of at least 32 bytes and keep it stable across the API and delivery worker. `python -c "import secrets; print(secrets.token_hex(32))"` generates a suitable value. Store it in the ignored `.env` or your secret manager. Start the delivery process separately after migration.

```powershell
python -m app.webhook_worker
```

Register a destination with `POST /v1/webhooks`, your API key and `{"url":"https://your-domain.example/events"}`. Save the returned `signing_secret` immediately. It is shown only on creation and is derived from the master secret, so changing the master changes all endpoint signatures. Automated secret rotation and recovery are not implemented. URLs are immutable, and a disabled URL remains reserved for its account. Use a new endpoint URL when replacing one.

Use that exact registered URL as `webhook_url` when creating a job. `GET /v1/webhooks` lists your endpoints. `DELETE /v1/webhooks/{id}` disables an endpoint and cancels pending events. A request already in flight may still finish. Endpoints and delivery history are scoped to the API key's account. Registration does not prove domain ownership.

Only public IPv4 HTTPS destinations on port 443 are supported. Private, loopback, link-local, reserved and mixed public/private DNS answers are rejected. Each attempt resolves and checks DNS again, connects to the checked address with TLS certificate and hostname verification, and refuses redirects. Connection and response-header timeouts are three and seven seconds.

The media worker saves a `job.completed` or `job.failed` event in the same transaction as the terminal job state. Intermediate processing retries do not notify. A manually replayed failed job can produce a new terminal event with a new ID. Completion events contain an authenticated relative `outputs_url` path, which the receiver resolves against the Forge API base URL.

Deliveries include `X-Webhook-Id`, `X-Webhook-Timestamp` and `X-Webhook-Signature`. The signature is `sha256=` followed by the hex HMAC-SHA256 of the timestamp string, a dot, and the exact raw request body. Use the returned signing-secret string as UTF-8 bytes, not as hex-decoded bytes. A receiver can verify it as follows, then parse JSON and deduplicate using the event ID.

```python
import hashlib
import hmac
import time

def valid_webhook(secret, timestamp, body, signature):
    try:
        recent = abs(time.time() - int(timestamp)) <= 300
    except (ValueError, TypeError):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return recent and hmac.compare_digest(expected, signature)
```

Return a 2xx response promptly. Transport failures, HTTP 408, 425, 429 and 5xx responses retry after 30 seconds, two minutes, ten minutes and one hour, for five total attempts. Other HTTP errors, redirects, unsafe destinations and TLS certificate failures stop delivery. Each attempt uses a fresh timestamp with the same event ID and body. Worker crashes can cause duplicate delivery, so consumers must deduplicate. A 60-second database lease allows another worker to recover an interrupted delivery.

Use `GET /v1/jobs/{job_id}/webhooks` to inspect event status and each delivery's HTTP status or error. Failed webhook delivery does not change the job's processing result. Manual webhook replay and operational alerts remain future work.

## Storage maintenance

Preview eligible objects before removing anything.

```powershell
python -m app.storage_cleanup
python -m app.storage_cleanup --apply
```

Legacy staging files are eligible for cleanup after their recorded expiry plus one day, or eight days after creation when no expiry exists. Unreferenced source snapshots and files from failed or timed-out attempts must be at least a day old. The command keeps the accepted source, current attempt files, completed attempt files and unknown filenames. It operates only within generated keys tied to database records. It uses upload row locks to coordinate with finalization. Schedule it regularly in deployment. Database records and normal completed outputs have no automatic retention policy yet.

## API keys

Open <http://127.0.0.1:8000/docs> for the interactive API documentation.

The account command prints a newly generated API key once. Save it securely and pass it as `X-API-Key` on API requests. Run the command again with an existing email to issue an additional key. Account management is currently a local administrator operation; do not expose shell or database access to untrusted users.

To rotate a key, issue a replacement, then use `python -m app.accounts you@example.com --list-keys` to find the old key's ID and `python -m app.accounts you@example.com --revoke KEY_ID` to revoke it. Listing shows IDs and timestamps, never secret values. Revocation blocks future authenticated requests.

The accounts migration assigns existing prototype uploads and jobs to an unclaimed legacy account. To access those records, run `python -m app.accounts you@example.com --claim-legacy` once after migration; this assigns the legacy account to that email and prints its first key. New installations may simply create a normal account instead. The old shared `dev-key` no longer works.


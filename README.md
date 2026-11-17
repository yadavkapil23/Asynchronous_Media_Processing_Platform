# Forge

Forge is an asynchronous image-processing backend. It accepts PNG, JPEG and WebP uploads, processes resize, thumbnail, conversion and metadata jobs, and sends signed webhooks for terminal job events.

For the complete design and data flow, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Features

- FastAPI API with account-scoped API keys
- Local filesystem storage
- PostgreSQL or SQLite
- Redis and RQ background processing
- Upload validation and immutable source files
- Retries, crash recovery and dead letters
- Signed webhooks with retries
- Rate limiting and `/metrics`
- Docker Compose with optional HTTPS

## Docker quick start

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"
docker compose up -d --build
docker compose exec api python -m app.accounts you@example.com
```

Put the generated value into `WEBHOOK_MASTER_SECRET` in `.env`. Save the API key printed by the account command. Open <http://127.0.0.1:8000/docs>.

Compose starts the API, PostgreSQL, Redis, image worker, dispatcher and webhook worker. The migration service should exit with code 0.

## Production HTTPS

Set `DOMAIN` in `.env` to a DNS name pointing to the server, open ports 80 and 443, and run:

```bash
docker compose --profile https up -d --build
```

Caddy obtains and renews the certificate and proxies HTTPS traffic to the API. Keep PostgreSQL and Redis private and use a strong database password.

## Local development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
alembic upgrade head
python -m app.accounts you@example.com
uvicorn app.main:app --reload
```

Run Redis, the dispatcher, an RQ worker and the webhook worker as needed:

```powershell
python -m app.dispatcher
rq worker --url redis://localhost:6379/0 --worker-class rq.worker.SimpleWorker
python -m app.webhook_worker
```

## API flow

1. `POST /v1/uploads`
2. `PUT /v1/uploads/{upload_id}/content`
3. `POST /v1/jobs`
4. Poll `GET /v1/jobs/{job_id}`
5. Read `GET /v1/jobs/{job_id}/outputs`

All `/v1` requests require `X-API-Key`. Uploads are limited to 20 MiB and 16 million pixels. Only single-frame PNG, JPEG and WebP images are accepted.

## Rate limits and metrics

The default limit is 120 `/v1` requests per 60 seconds per API key. Configure `RATE_LIMIT_REQUESTS` and `RATE_LIMIT_WINDOW_SECONDS` in `.env`. Rate-limited requests return HTTP 429.

Use `GET /health` for liveness and `GET /metrics` for Prometheus-compatible counters. Keep `/metrics` private in production.

## Tests and CI

```powershell
python -m unittest discover -s tests -v
python scripts/compose_smoke.py
```

Redis integration tests use `TEST_REDIS_URL` with isolated database and storage paths. GitHub Actions runs tests and Docker build checks from `.github/workflows/ci.yml`.

## Backups and restore testing

```bash
BACKUP_DIR=/srv/forge/backups ./scripts/backup.sh
BACKUP_DIR=/srv/forge/backups ./scripts/restore_test.sh \
  /srv/forge/backups/forge-YYYYMMDDTHHMMSSZ.sql.gz \
  /srv/forge/backups/media-YYYYMMDDTHHMMSSZ.tar.gz
```

Use `scripts/backup.cron.example` for daily scheduling. Store backups and secrets outside Git.

## Maintenance

```powershell
python -m app.storage_cleanup
python -m app.storage_cleanup --apply
```

Review the dry-run result before applying cleanup. Never commit `.env`, API keys, database files, media files or backup archives.

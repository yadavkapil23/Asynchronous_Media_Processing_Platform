FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/forge
COPY requirements.txt .
RUN pip install -r requirements.txt && useradd --uid 10001 --create-home forge
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini .
RUN mkdir -p /srv/forge/data/storage && chown -R forge:forge /srv/forge/data
USER forge
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

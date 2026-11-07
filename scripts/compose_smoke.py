"""Verify the running Compose API, queue worker and shared media volume."""
import argparse
import io
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def compose(*args):
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True,
                          check=True, timeout=120).stdout.strip()


def wait_api(client):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(1)
    raise RuntimeError("API did not become ready")


def verify(restart=False):
    email = f"compose-probe-{uuid4().hex}@example.test"
    key = compose("exec", "-T", "api", "python", "-m", "app.accounts", email)
    try:
        with httpx.Client(base_url="http://127.0.0.1:8000", headers={"X-API-Key": key}, timeout=5) as client:
            wait_api(client)
            image = io.BytesIO()
            Image.new("RGB", (24, 12), "blue").save(image, "PNG")
            response = client.post("/v1/uploads", json={"filename": "compose-probe.png", "content_type": "image/png"})
            response.raise_for_status()
            upload = response.json()
            client.put(upload["upload_url"], content=image.getvalue()).raise_for_status()
            response = client.post("/v1/jobs", json={"upload_id": upload["upload_id"],
                                   "operations": [{"type": "resize", "width": 8, "height": 4}]})
            response.raise_for_status()
            job_id = response.json()["id"]
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                response = client.get(f"/v1/jobs/{job_id}")
                response.raise_for_status()
                state = response.json()["status"]
                if state == "completed":
                    break
                if state == "failed":
                    raise RuntimeError("Compose processing job failed")
                time.sleep(1)
            else:
                raise RuntimeError("Compose processing job timed out")
            if restart:
                compose("restart", "api", "worker")
                wait_api(client)
            response = client.get(f"/v1/jobs/{job_id}/outputs")
            response.raise_for_status()
            response = client.get(response.json()["outputs"][0]["url"])
            response.raise_for_status()
            with Image.open(io.BytesIO(response.content)) as output:
                if output.size != (8, 4) or output.getpixel((0, 0)) != (0, 0, 255):
                    raise RuntimeError("Unexpected processed image")
            print(f"Compose upload, queued processing and download passed: {job_id}")
            if restart:
                print("Output remained available after API and worker restart.")
    finally:
        code = (
            "import sys; from app.db import SessionLocal; "
            "from app.accounts import list_account_keys, revoke_account_key; "
            "db=SessionLocal(); "
            "[revoke_account_key(db, sys.argv[1], k['id']) for k in list_account_keys(db, sys.argv[1])]; db.close()"
        )
        compose("exec", "-T", "api", "python", "-c", code, email)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a local probe job and revoke its temporary account key")
    parser.add_argument("--restart", action="store_true", help="Also restart the API and worker to check storage persistence")
    try:
        verify(parser.parse_args().restart)
    except Exception as exc:
        print(f"Compose check failed: {type(exc).__name__}. Inspect docker compose logs.", file=sys.stderr)
        sys.exit(1)

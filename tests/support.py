import atexit
import os
import tempfile
from pathlib import Path

tmp = tempfile.TemporaryDirectory(prefix="forge-tests-")
os.environ.update(DATABASE_URL=os.getenv("TEST_DATABASE_URL") or "sqlite:///" + (Path(tmp.name) / "test.db").as_posix(),
                  LOCAL_STORAGE_PATH=str(Path(tmp.name) / "storage"),
                  WEBHOOK_MASTER_SECRET="")

from alembic import command
from alembic.config import Config
from app.accounts import create_account_key
from app.db import SessionLocal, engine

command.upgrade(Config("alembic.ini"), "head")
with SessionLocal() as session:
    test_key = create_account_key(session, "one@example.test")
    other_key = create_account_key(session, "two@example.test")


def cleanup():
    engine.dispose()
    tmp.cleanup()


atexit.register(cleanup)

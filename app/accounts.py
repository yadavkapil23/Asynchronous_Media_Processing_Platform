"""Local account administration: python -m app.accounts EMAIL [--claim-legacy]."""
import argparse
import hashlib
import secrets
import json
from datetime import datetime, timezone
from sqlalchemy import select
from .db import SessionLocal
from .models import ApiKey, User


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_account_key(db, email: str, claim_legacy: bool = False) -> str:
    email = email.strip().lower()
    if not email or len(email) > 255:
        raise ValueError("Email is required and must fit 255 characters")
    if claim_legacy:
        user = db.get(User, "usr_legacy")
        if user is None or user.email != "legacy@forge.invalid" or db.scalar(select(ApiKey.id).where(ApiKey.user_id == "usr_legacy")):
            raise ValueError("Legacy account is already claimed or unavailable")
        user.email = email
    else:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email)
            db.add(user)
            db.flush()
    key = "forge_" + secrets.token_urlsafe(32)
    db.add(ApiKey(user_id=user.id, key_hash=hash_key(key)))
    db.commit()
    return key


def list_account_keys(db, email: str):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        raise ValueError("Account not found")
    keys = db.scalars(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at)).all()
    return [{"id": key.id, "created_at": key.created_at.isoformat(), "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None} for key in keys]


def revoke_account_key(db, email: str, key_id: str):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    key = db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)) if user else None
    if key is None:
        raise ValueError("Key not found for account")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        db.commit()


def main():
    parser = argparse.ArgumentParser(description="Manage account API keys locally")
    parser.add_argument("email")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--claim-legacy", action="store_true")
    action.add_argument("--list-keys", action="store_true")
    action.add_argument("--revoke", metavar="KEY_ID")
    args = parser.parse_args()
    with SessionLocal() as db:
        try:
            if args.list_keys:
                print(json.dumps(list_account_keys(db, args.email), indent=2))
            elif args.revoke:
                revoke_account_key(db, args.email, args.revoke)
                print("Key revoked")
            else:
                print(create_account_key(db, args.email, args.claim_legacy))
        except ValueError as exc:
            parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

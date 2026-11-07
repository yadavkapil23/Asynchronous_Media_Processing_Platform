"""Add account ownership; preserve existing data under an unclaimed legacy account."""
from datetime import datetime, timezone
from alembic import op
import sqlalchemy as sa

revision = "0002_accounts"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("users", sa.Column("id", sa.String(40), primary_key=True), sa.Column("email", sa.String(255), nullable=False, unique=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("api_keys", sa.Column("id", sa.String(40), primary_key=True), sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False), sa.Column("key_hash", sa.String(64), nullable=False, unique=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.bulk_insert(sa.table("users", sa.column("id", sa.String()), sa.column("email", sa.String()), sa.column("created_at", sa.DateTime(timezone=True))), [{"id": "usr_legacy", "email": "legacy@forge.invalid", "created_at": datetime.now(timezone.utc)}])
    for table in ("uploads", "jobs"):
        op.add_column(table, sa.Column("user_id", sa.String(40), nullable=True))
        op.execute(sa.text(f"UPDATE {table} SET user_id = 'usr_legacy'"))
        with op.batch_alter_table(table) as batch:
            batch.alter_column("user_id", existing_type=sa.String(40), nullable=False)
            batch.create_foreign_key(f"fk_{table}_user_id", "users", ["user_id"], ["id"])
            batch.create_index(f"ix_{table}_user_id", ["user_id"])
            if table == "jobs":
                batch.drop_constraint("uq_jobs_idempotency_key", type_="unique")
                batch.create_unique_constraint("uq_jobs_user_idempotency_key", ["user_id", "idempotency_key"])


def downgrade():
    raise RuntimeError("Downgrade would lose account ownership; restore a backup instead")

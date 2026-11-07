"""Add upload staging metadata and durable webhook delivery records."""
import sqlalchemy as sa
from alembic import op

revision = "0005_uploads_webhooks"
down_revision = "0004_job_retries"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("uploads", sa.Column("staging_key", sa.String(500)))
    op.add_column("uploads", sa.Column("staging_expires_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE uploads SET staging_key = object_key")
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("user_id", "url", name="uq_webhook_owner_url"),
    )
    op.create_index("ix_webhook_endpoints_user_id", "webhook_endpoints", ["user_id"])
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("endpoint_id", sa.String(40), sa.ForeignKey("webhook_endpoints.id"), nullable=False),
        sa.Column("job_id", sa.String(40), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_webhook_events_job_id", "webhook_events", ["job_id"])
    op.create_index("ix_webhook_events_due", "webhook_events", ["status", "due_at"])
    op.create_table(
        "webhook_deliveries",
        sa.Column("event_id", sa.String(40), sa.ForeignKey("webhook_events.id"), primary_key=True),
        sa.Column("number", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("detail", sa.String(200)),
    )


def downgrade():
    raise RuntimeError("Restore a backup to avoid losing webhook history and upload metadata.")

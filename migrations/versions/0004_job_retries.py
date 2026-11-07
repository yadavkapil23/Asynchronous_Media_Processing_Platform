"""Persist retry schedules, exhausted jobs and observed attempts."""
from alembic import op
import sqlalchemy as sa

revision = "0004_job_retries"
down_revision = "0003_job_attempts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("next_retry_at", sa.DateTime(timezone=True)))
    op.add_column("jobs", sa.Column("dead_lettered_at", sa.DateTime(timezone=True)))
    op.create_index("ix_jobs_status_next_retry_at", "jobs", ["status", "next_retry_at"])
    op.create_index("ix_jobs_user_dead_lettered_at", "jobs", ["user_id", "dead_lettered_at"])
    op.create_table(
        "job_attempts",
        sa.Column("token", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(40), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
    )
    op.create_index("ix_job_attempts_job_id", "job_attempts", ["job_id"])
    op.execute(
        "INSERT INTO job_attempts (token, job_id, number, status, started_at, ended_at, error_message) "
        "SELECT attempt_token, id, attempt_count, status, started_at, completed_at, error_message FROM jobs "
        "WHERE attempt_token IS NOT NULL AND started_at IS NOT NULL AND attempt_count > 0 "
        "AND status IN ('processing', 'failed', 'completed')"
    )
    op.execute(
        "UPDATE jobs SET dead_lettered_at = completed_at WHERE status = 'failed' "
        "AND attempt_token IS NULL AND error_message = 'Processing timed out after maximum attempts'"
    )


def downgrade():
    raise RuntimeError("Downgrade would discard retry schedules and attempt history. Restore a backup instead.")

"""Track processing attempts for crash recovery."""
from alembic import op
import sqlalchemy as sa

revision = "0003_job_attempts"
down_revision = "0002_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("jobs", sa.Column("attempt_token", sa.String(32), nullable=True))
    op.execute("UPDATE jobs SET status = 'failed', error_message = 'Interrupted during upgrade; retry manually' WHERE status = 'processing'")


def downgrade():
    raise RuntimeError("Downgrade would discard attempt state; restore a backup instead")

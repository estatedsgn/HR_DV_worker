"""add outbound voice job fields

Revision ID: 0013_outbound_voice_jobs
Revises: 0012_brain_v2_turn_debounce
Create Date: 2026-05-24 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0013_outbound_voice_jobs"
down_revision: Union[str, None] = "0012_brain_v2_turn_debounce"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "outbound_jobs",
        sa.Column("job_type", sa.String(length=50), server_default="text", nullable=False),
    )
    op.add_column("outbound_jobs", sa.Column("media_path", sa.Text(), nullable=True))
    op.add_column("outbound_jobs", sa.Column("media_mime_type", sa.String(length=120), nullable=True))
    op.add_column(
        "outbound_jobs",
        sa.Column("media_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("outbound_jobs", sa.Column("typing_action", sa.String(length=100), nullable=True))
    op.create_index(op.f("ix_outbound_jobs_job_type"), "outbound_jobs", ["job_type"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_outbound_jobs_job_type"), table_name="outbound_jobs")
    op.drop_column("outbound_jobs", "typing_action")
    op.drop_column("outbound_jobs", "media_metadata")
    op.drop_column("outbound_jobs", "media_mime_type")
    op.drop_column("outbound_jobs", "media_path")
    op.drop_column("outbound_jobs", "job_type")

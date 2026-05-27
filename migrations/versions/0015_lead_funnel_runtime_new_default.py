"""set langgraph funnel runtime default stage to new

Revision ID: 0015_funnel_stage_new
Revises: 0014_lead_funnel_runtime
Create Date: 2026-05-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0015_funnel_stage_new"
down_revision: Union[str, None] = "0014_lead_funnel_runtime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE lead_funnel_runtime ALTER COLUMN stage SET DEFAULT 'new'")


def downgrade() -> None:
    op.execute("ALTER TABLE lead_funnel_runtime ALTER COLUMN stage SET DEFAULT 'bootstrap'")

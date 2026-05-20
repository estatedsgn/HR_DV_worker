"""add lease fields for inbound event claiming

Revision ID: 0008_inbound_event_leases
Revises: 0007_inbound_events_retry_fields
Create Date: 2026-05-20 02:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008_inbound_event_leases"
down_revision: Union[str, None] = "0007_inbound_events_retry_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inbound_events", sa.Column("lease_owner", sa.String(length=100), nullable=True))
    op.add_column("inbound_events", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_inbound_events_lease_owner"), "inbound_events", ["lease_owner"], unique=False)
    op.create_index(op.f("ix_inbound_events_lease_expires_at"), "inbound_events", ["lease_expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_inbound_events_lease_expires_at"), table_name="inbound_events")
    op.drop_index(op.f("ix_inbound_events_lease_owner"), table_name="inbound_events")
    op.drop_column("inbound_events", "lease_expires_at")
    op.drop_column("inbound_events", "lease_owner")

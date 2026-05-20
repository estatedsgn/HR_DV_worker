"""add inbound events foreign keys

Revision ID: 0006_inbound_events_foreign_keys
Revises: 0005_inbound_events
Create Date: 2026-05-20 00:30:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0006_inbound_events_foreign_keys"
down_revision: Union[str, None] = "0005_inbound_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_inbound_events_account_id_accounts",
        "inbound_events",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_inbound_events_dialog_id_dialogs",
        "inbound_events",
        "dialogs",
        ["dialog_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_inbound_events_dialog_id_dialogs",
        "inbound_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_inbound_events_account_id_accounts",
        "inbound_events",
        type_="foreignkey",
    )

"""messages.reply_to_message_id (Telegram reply/quote context)

Revision ID: 0018_messages_reply_to
Revises: 0017_polling_skipped
Create Date: 2026-06-13 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0018_messages_reply_to"
down_revision: Union[str, None] = "0017_polling_skipped"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("reply_to_message_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_messages_reply_to_message_id",
        "messages",
        ["reply_to_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_reply_to_message_id", table_name="messages")
    op.drop_column("messages", "reply_to_message_id")

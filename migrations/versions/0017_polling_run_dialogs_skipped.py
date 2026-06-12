"""telegram_polling_runs.dialogs_skipped + skip_details

Revision ID: 0017_polling_skipped
Revises: 0016_account_credentials
Create Date: 2026-06-13 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0017_polling_skipped"
down_revision: Union[str, None] = "0016_account_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telegram_polling_runs",
        sa.Column("dialogs_skipped", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "telegram_polling_runs",
        sa.Column("skip_details", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("telegram_polling_runs", "skip_details")
    op.drop_column("telegram_polling_runs", "dialogs_skipped")

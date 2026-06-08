"""per-account crmchat credentials and daivinchik config

Revision ID: 0016_account_credentials
Revises: 0015_funnel_stage_new
Create Date: 2026-06-07 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0016_account_credentials"
down_revision: Union[str, None] = "0015_funnel_stage_new"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("crmchat_api_base_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("crmchat_api_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "daivinchik_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "accounts",
        sa.Column("daivinchik_config_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "daivinchik_config_json")
    op.drop_column("accounts", "daivinchik_enabled")
    op.drop_column("accounts", "crmchat_api_key")
    op.drop_column("accounts", "crmchat_api_base_url")

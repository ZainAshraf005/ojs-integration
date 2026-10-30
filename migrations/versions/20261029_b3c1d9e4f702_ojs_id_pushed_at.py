"""articles.ojs_id_pushed_at: when OJS confirmed it stores the archive ID

Revision ID: b3c1d9e4f702
Revises: 7ea8f251d260
Create Date: 2026-10-29 12:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b3c1d9e4f702'
down_revision: str | None = '7ea8f251d260'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('articles', sa.Column('ojs_id_pushed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('articles', 'ojs_id_pushed_at')

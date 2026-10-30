"""traders.currency (from the WhatsApp number's country code)

Revision ID: 5b1f0c2a9d17
Revises: e4e9c592f186
Create Date: 2026-10-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5b1f0c2a9d17'
down_revision: Union[str, Sequence[str], None] = 'e4e9c592f186'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Existing traders are all Nigerian, so they get NGN."""
    with op.batch_alter_table('traders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('currency', sa.String(length=3), server_default='NGN', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('traders', schema=None) as batch_op:
        batch_op.drop_column('currency')

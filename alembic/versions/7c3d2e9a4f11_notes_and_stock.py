"""notes, stock_items, stock_moves (purchases, supplier payments and savings reuse `entries`)

Revision ID: 7c3d2e9a4f11
Revises: 5b1f0c2a9d17
Create Date: 2026-11-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7c3d2e9a4f11'
down_revision: Union[str, Sequence[str], None] = '5b1f0c2a9d17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'notes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('trader_id', sa.Integer(), sa.ForeignKey('traders.id'), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('deleted', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_notes_trader_id', 'notes', ['trader_id'])
    op.create_table(
        'stock_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('trader_id', sa.Integer(), sa.ForeignKey('traders.id'), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('normalized_name', sa.String(length=120), nullable=False),
        sa.Column('unit', sa.String(length=32), nullable=True),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('trader_id', 'normalized_name', name='uq_stock_trader_name'),
    )
    op.create_index('ix_stock_items_trader_id', 'stock_items', ['trader_id'])
    op.create_table(
        'stock_moves',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('trader_id', sa.Integer(), sa.ForeignKey('traders.id'), nullable=False),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('stock_items.id'), nullable=False),
        sa.Column('entry_id', sa.Integer(), sa.ForeignKey('entries.id'), nullable=True),
        sa.Column('change', sa.Float(), nullable=False),
        sa.Column('reason', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_stock_moves_trader_id', 'stock_moves', ['trader_id'])
    op.create_index('ix_stock_moves_item_id', 'stock_moves', ['item_id'])
    op.create_index('ix_stock_moves_entry_id', 'stock_moves', ['entry_id'])


def downgrade() -> None:
    op.drop_table('stock_moves')
    op.drop_table('stock_items')
    op.drop_table('notes')

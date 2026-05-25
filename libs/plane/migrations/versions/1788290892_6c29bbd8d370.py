"""auth refresh token

Revision ID: 6c29bbd8d370
Revises: c5e9a2b73f41
Create Date: 2026-09-01 16:28:12.867870

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = '6c29bbd8d370'
down_revision: Union[str, None] = 'c5e9a2b73f41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('auth_refresh_token',
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('seq', sa.Integer(), sa.Identity(always=False, start=1000, cycle=True), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('replaced_by', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_auth_refresh_token_token_hash'), 'auth_refresh_token', ['token_hash'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_auth_refresh_token_token_hash'), table_name='auth_refresh_token')
    op.drop_table('auth_refresh_token')

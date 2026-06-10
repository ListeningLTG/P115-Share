"""add invalid to share_analysis_states

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-05-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a1'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if inspector.has_table('share_analysis_states'):
        columns = [col['name'] for col in inspector.get_columns('share_analysis_states')]
        if 'invalid' not in columns:
            op.add_column(
                'share_analysis_states',
                sa.Column('invalid', sa.Integer(), nullable=False, server_default='0')
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if inspector.has_table('share_analysis_states'):
        columns = [col['name'] for col in inspector.get_columns('share_analysis_states')]
        if 'invalid' in columns:
            op.drop_column('share_analysis_states', 'invalid')

"""add is_invalid to share_analysis_results

Revision ID: a1b2c3d4e5f6
Revises: e478acc400f3
Create Date: 2026-05-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'e478acc400f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if inspector.has_table('share_analysis_results'):
        columns = [col['name'] for col in inspector.get_columns('share_analysis_results')]
        if 'is_invalid' not in columns:
            op.add_column(
                'share_analysis_results',
                sa.Column('is_invalid', sa.Boolean(), nullable=False, server_default=sa.false())
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if inspector.has_table('share_analysis_results'):
        columns = [col['name'] for col in inspector.get_columns('share_analysis_results')]
        if 'is_invalid' in columns:
            op.drop_column('share_analysis_results', 'is_invalid')

"""tmdb_alias_cache composite unique index refactor

将 tmdb_alias_cache 表的 tmdb_id 单列唯一索引改为 (tmdb_id, media_type) 复合唯一索引。

TMDB 的 movie id 和 tv id 是两个独立命名空间，同一数字可同时对应一部电影和一部剧集，
必须按 (tmdb_id, media_type) 分开存储才能避免互相覆盖（alias 污染）。

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a1
Create Date: 2026-09-12 22:48:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """将 tmdb_alias_cache 的唯一约束从单列 tmdb_id 改为 (tmdb_id, media_type) 复合约束。

    由于 SQLite 不支持 DROP CONSTRAINT / ALTER INDEX，
    采用 DROP + CREATE INDEX 的方式原地完成迁移，无需重建表，数据零丢失。
    已通过 init_db()._migrate_tmdb_alias_index() 双重保险兜底。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    TABLE = 'tmdb_alias_cache'
    NEW_IDX = 'uix_tmdb_alias_tmdb_media'
    OLD_IDX = 'ix_tmdb_alias_cache_tmdb_id'

    if not inspector.has_table(TABLE):
        return  # 新库由 create_all 直接建正确结构，跳过

    existing_indexes = {idx['name'] for idx in inspector.get_indexes(TABLE)}

    if NEW_IDX in existing_indexes:
        return  # 复合唯一索引已存在（由 init_db 迁移完成），跳过

    # 删除旧的单列唯一索引（如果存在）
    if OLD_IDX in existing_indexes:
        op.drop_index(OLD_IDX, table_name=TABLE)

    # 建立新的 (tmdb_id, media_type) 复合唯一索引
    op.create_index(NEW_IDX, TABLE, ['tmdb_id', 'media_type'], unique=True)


def downgrade() -> None:
    """回退：恢复单列 tmdb_id 唯一索引（若存在 tmdb_id 重复行则会失败，需手动清理后再降级）。"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    TABLE = 'tmdb_alias_cache'
    NEW_IDX = 'uix_tmdb_alias_tmdb_media'
    OLD_IDX = 'ix_tmdb_alias_cache_tmdb_id'

    existing_indexes = {idx['name'] for idx in inspector.get_indexes(TABLE)}

    if NEW_IDX in existing_indexes:
        op.drop_index(NEW_IDX, table_name=TABLE)

    if OLD_IDX not in existing_indexes:
        op.create_index(OLD_IDX, TABLE, ['tmdb_id'], unique=True)

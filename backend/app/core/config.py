import os
import json
import asyncio
from pydantic_settings import BaseSettings
from typing import Optional, Any
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.core.database import engine, async_session, Base
from app.models.schema import User as UserModel, SystemSettings

class Settings(BaseSettings):
    # Telegram
    TG_BOT_TOKEN: str = ""
    TG_CHANNEL_ID: str = ""
    TG_USER_ID: str = ""
    TG_ALLOW_CHATS: str = "" # Comma separated list of IDs
    TG_CHANNELS: str = "[]"  # JSON list of {id, enabled, concise}
    TG_SKIP_LARGE_PACKAGE: bool = True
    
    # 115
    P115_COOKIE: str = ""
    P115_SAVE_DIR: str = "115-Share"
    
    # Direct save configuration
    DIRECT_SAVE_ACCOUNT_ID: int = 0
    DIRECT_SAVE_DIR: str = "115-Save"
    TG_DEFAULT_COMMAND_MODE: str = "share"
    
    # Sensitive Word Replacement Configuration
    SENSITIVE_REPLACE_ENABLED: bool = False
    SENSITIVE_REPLACE_MAPPING: str = "{}"
    SENSITIVE_REPLACE_PINYIN: bool = False
    SENSITIVE_REPLACE_TMDB: bool = False
    TMDB_API_KEY: str = ""

    
    # App
    WEB_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    
    # Cleanup scheduling
    P115_CLEANUP_DIR_CRON: str = "0 3 * * *"
    P115_CLEANUP_TRASH_CRON: str = "0 4 * * *"
    P115_RECYCLE_PASSWORD: str = ""
    
    # Capacity cleanup
    P115_CLEANUP_CAPACITY_ENABLED: bool = False
    P115_CLEANUP_CAPACITY_LIMIT: float = 0.0  # Threshold value
    P115_CLEANUP_CAPACITY_UNIT: str = "GB"    # GB or TB
    P115_CLEANUP_CAPACITY_TYPE: str = "ENTIRE" # ENTIRE or DIRECTORY

    # Rate limiting & Task Prioritization
    P115_RATE_LIMIT_COUNT: int = 10             # Window count
    P115_RATE_LIMIT_WINDOW: int = 60            # Window size (s)
    P115_RATE_LIMIT_SILENT_DURATION: int = 60   # Silence duration (s)
    P115_BATCH_YIELD_DURATION: int = 10         # Yield to TG links (s)
    
    # Proxy settings
    PROXY_ENABLED: bool = False
    PROXY_HOST: str = ""
    PROXY_PORT: str = ""
    PROXY_USER: str = ""
    PROXY_PASS: str = ""
    PROXY_TYPE: str = "HTTP" # Options: HTTP, SOCKS5

    def _migrate_columns(self, conn):
        """Check all model tables for missing columns and add them via ALTER TABLE"""
        from sqlalchemy import inspect, text
        inspector = inspect(conn)
        
        for table in Base.metadata.sorted_tables:
            table_name = table.name
            if not inspector.has_table(table_name):
                continue
            
            existing_cols = {col['name'] for col in inspector.get_columns(table_name)}
            
            for column in table.columns:
                if column.name not in existing_cols:
                    # Build ALTER TABLE ADD COLUMN statement
                    col_type = column.type.compile(conn.dialect)
                    sql = f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type}"
                    
                    # Add DEFAULT if present
                    if column.server_default is not None:
                        default_val = column.server_default.arg
                        if hasattr(default_val, 'text'):
                            sql += f" DEFAULT {default_val.text}"
                        else:
                            sql += f" DEFAULT '{default_val}'"
                    elif not column.nullable:
                        # Non-nullable without default: add a safe default
                        if "INT" in str(col_type).upper():
                            sql += " DEFAULT 0"
                        elif "BOOL" in str(col_type).upper():
                            sql += " DEFAULT 0"
                        else:
                            sql += " DEFAULT ''"
                    
                    conn.execute(text(sql))
                    logger.info(f"[DB] 数据库迁移: 为表 {table_name} 添加列 {column.name}")

    async def init_db(self):
        """Initialize database tables and ensure schema is up-to-date"""
        async with engine.begin() as conn:
            # Create tables (handles fresh database)
            await conn.run_sync(Base.metadata.create_all)
            # Migrate missing columns for existing databases
            await conn.run_sync(self._migrate_columns)

        async with async_session() as session:
            # Check if admin exists
            result = await session.execute(
                select(UserModel).where(UserModel.username == "admin")
            )
            if not result.scalar_one_or_none():
                from app.services.auth import get_password_hash

                admin = UserModel(
                    username="admin",
                    hashed_password=get_password_hash("admin"),
                    avatar_url="/logo.png",
                )
                session.add(admin)
                logger.info("Default admin user created (admin/admin)")

            # Check if we need to migrate or add missing settings
            await self._ensure_all_settings_exist(session)
            await self._load_from_db(session)

            await session.commit()

    async def _ensure_all_settings_exist(self, session):
        """Ensure all fields defined in Settings exist in the system_settings table"""
        result = await session.execute(select(SystemSettings))
        settings_rows = result.scalars().all()
        existing_keys = {row.key for row in settings_rows}
        
        # 自动将原有的旧默认路径 115-Share/DirectSave 迁移为 115-Save，旧频率限制默认值迁移为新默认值 (仅在首次更新时执行一次，不覆盖用户后续的手动修改)
        migrated_defaults = "MIGRATED_DEFAULTS_TO_115_SAVE" in existing_keys
        if not migrated_defaults:
            for row in settings_rows:
                if row.key == "DIRECT_SAVE_DIR" and row.value == "115-Share/DirectSave":
                    row.value = "115-Save"
                    logger.info("🔄 自动将数据库中的 DIRECT_SAVE_DIR 默认路径迁移至 '115-Save'")
                elif row.key == "P115_RATE_LIMIT_COUNT" and row.value == "30":
                    row.value = "10"
                    logger.info("🔄 自动将数据库中的 P115_RATE_LIMIT_COUNT 默认值迁移至 10")
                elif row.key == "P115_RATE_LIMIT_WINDOW" and row.value == "300":
                    row.value = "60"
                    logger.info("🔄 自动将数据库中的 P115_RATE_LIMIT_WINDOW 默认值迁移至 60")
            
            # 写入已迁移标记，防止后续覆盖用户在前端自主修改的值
            session.add(SystemSettings(key="MIGRATED_DEFAULTS_TO_115_SAVE", value="true"))
            existing_keys.add("MIGRATED_DEFAULTS_TO_115_SAVE")
            logger.info("✅ 已记录系统默认值一键迁移标记。")
          
        added_count = 0
        for field in self.model_fields:
            if field not in existing_keys:
                val = getattr(self, field)
                session.add(SystemSettings(key=field, value=str(val)))
                added_count += 1
        
        if added_count > 0:
            logger.info(f"💾 Added {added_count} missing settings to database.")

    async def _load_from_db(self, session):
        """Load settings from system_settings table"""
        result = await session.execute(select(SystemSettings))
        rows = result.scalars().all()
        for row in rows:
            if hasattr(self, row.key):
                # Type casting
                field_type = self.model_fields[row.key].annotation
                try:
                    if field_type == int:
                        setattr(self, row.key, int(row.value))
                    elif field_type == float:
                        setattr(self, row.key, float(row.value))
                    elif field_type == bool:
                        setattr(self, row.key, row.value.lower() == "true")
                    else:
                        setattr(self, row.key, row.value)
                except Exception as e:
                    logger.error(f"Failed to cast setting {row.key}: {e}")

        # 验证频率控制参数
        if self.P115_RATE_LIMIT_COUNT < 0:
            self.P115_RATE_LIMIT_COUNT = 0
        if self.P115_RATE_LIMIT_WINDOW <= 0:
            self.P115_RATE_LIMIT_WINDOW = 60
        if self.P115_RATE_LIMIT_SILENT_DURATION <= 0:
            self.P115_RATE_LIMIT_SILENT_DURATION = 60
        if self.P115_BATCH_YIELD_DURATION < 0:
            self.P115_BATCH_YIELD_DURATION = 0

    async def save_setting(self, key: str, value: str):
        """Save a single setting to database (Create or Update)."""
        return await self.save_settings_batch({key: value})

    @staticmethod
    def _is_sqlite_locked_error(exc: Exception) -> bool:
        return "database is locked" in str(exc).lower()

    async def save_settings_batch(self, updates: dict[str, Any], max_retries: int = 5) -> bool:
        """Save multiple settings in a single transaction to reduce lock contention."""
        valid_updates = {k: v for k, v in updates.items() if hasattr(self, k)}
        if not valid_updates:
            return False

        for attempt in range(1, max_retries + 1):
            try:
                async with async_session() as session:
                    for key, value in valid_updates.items():
                        stmt = sqlite_insert(SystemSettings).values(key=key, value=str(value))
                        stmt = stmt.on_conflict_do_update(
                            index_elements=[SystemSettings.key],
                            set_={"value": str(value)},
                        )
                        await session.execute(stmt)

                    await session.commit()

                for key, value in valid_updates.items():
                    setattr(self, key, value)
                return True
            except OperationalError as exc:
                if not self._is_sqlite_locked_error(exc) or attempt == max_retries:
                    raise
                delay = 0.1 * (2 ** (attempt - 1))
                logger.warning(
                    f"SQLite busy while saving settings (attempt {attempt}/{max_retries}), retrying in {delay:.2f}s"
                )
                await asyncio.sleep(delay)

        return False

    class Config:
        env_file = ".env"

settings = Settings()

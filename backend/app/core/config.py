import os
import json
from pydantic_settings import BaseSettings
from typing import Optional
from loguru import logger
from sqlalchemy import select, update
from app.core.database import engine, async_session, Base
from app.models.schema import User as UserModel, SystemSettings

class Settings(BaseSettings):
    # Telegram
    TG_BOT_TOKEN: str = ""
    TG_CHANNEL_ID: str = ""
    TG_USER_ID: str = ""
    TG_ALLOW_CHATS: str = "" # Comma separated list of IDs
    TG_CHANNELS: str = "[]"  # JSON list of {id, enabled, concise}
    TG_SKIP_LARGE_PACKAGE: bool = False
    
    # 115
    P115_COOKIE: str = ""
    P115_SAVE_DIR: str = "115-Share"
    
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
    
    # Proxy settings
    PROXY_ENABLED: bool = False
    PROXY_HOST: str = ""
    PROXY_PORT: str = ""
    PROXY_USER: str = ""
    PROXY_PASS: str = ""
    PROXY_TYPE: str = "HTTP" # Options: HTTP, SOCKS5
    HTTP_PROXY: str = ""
    HTTPS_PROXY: str = ""

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
        result = await session.execute(select(SystemSettings.key))
        existing_keys = set(result.scalars().all())
        
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

    async def save_setting(self, key: str, value: str):
        """Save a single setting to database (Create or Update)"""
        if not hasattr(self, key):
            return False
            
        async with async_session() as session:
            # Check if key exists
            result = await session.execute(select(SystemSettings).where(SystemSettings.key == key))
            existing = result.scalar_one_or_none()
            
            if existing:
                existing.value = str(value)
            else:
                session.add(SystemSettings(key=key, value=str(value)))
                
            await session.commit()
            setattr(self, key, value)
            return True

    class Config:
        env_file = ".env"

settings = Settings()

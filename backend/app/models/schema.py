from datetime import datetime
from sqlalchemy import String, Text, DateTime, Integer, Boolean, JSON, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base

class User(Base):
    __tablename__ = "users"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    avatar_url: Mapped[str] = mapped_column(Text, default="/logo.png")  # Stores base64 data URI or path
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class SystemSettings(Base):
    __tablename__ = "system_settings"
    
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(String(255), nullable=True)

class PendingLink(Base):
    __tablename__ = "pending_links"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    share_url: Mapped[str] = mapped_column(String(255))
    metadata_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="auditing") # auditing, failed, completed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_check: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class LinkHistory(Base):
    __tablename__ = "link_history"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    original_url: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    share_link: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class ExcelTask(Base):
    __tablename__ = "excel_tasks"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="wait")  # wait, running, paused, completed, cancelled
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    target_dir: Mapped[str] = mapped_column(String(255), nullable=True)
    target_account_id: Mapped[int] = mapped_column(Integer, nullable=True)
    interval_min: Mapped[int] = mapped_column(Integer, default=5)
    interval_max: Mapped[int] = mapped_column(Integer, default=10)
    skip_count: Mapped[int] = mapped_column(Integer, default=0)
    stop_row: Mapped[int] = mapped_column(Integer, default=0)  # 0 表示处理到最后一行
    current_row: Mapped[int] = mapped_column(Integer, default=0)
    is_waiting: Mapped[bool] = mapped_column(Boolean, default=False)
    target_channels: Mapped[dict] = mapped_column(JSON, nullable=True)  # List of channel IDs to push to
    white_list_keywords: Mapped[str] = mapped_column(Text, nullable=True)  # Comma separated
    black_list_keywords: Mapped[str] = mapped_column(Text, nullable=True)  # Comma separated
    skip_large_package: Mapped[bool] = mapped_column(Boolean, default=False)
    strategy: Mapped[str] = mapped_column(String(20), default="transfer")  # transfer, push
    share_interval: Mapped[int] = mapped_column(Integer, default=0)
    sub_batch_start_row: Mapped[int] = mapped_column(Integer, default=0)
    sub_batch_count: Mapped[int] = mapped_column(Integer, default=0)
    sensitive_replace_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    sensitive_replace_pinyin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    sensitive_replace_tmdb: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class P115Account(Base):
    """115网盘账号配置表"""
    __tablename__ = "p115_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="默认账号")
    cookie: Mapped[str] = mapped_column(Text, default="")
    save_dir: Mapped[str] = mapped_column(String(255), default="115-Share")
    recycle_password: Mapped[str] = mapped_column(String(100), default="")
    priority: Mapped[int] = mapped_column(Integer, default=1)          # 优先级，数字越小越高
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    save_file_limit: Mapped[int] = mapped_column(Integer, default=500)   # 保存文件上限
    share_file_limit: Mapped[int] = mapped_column(Integer, default=10000) # 分享文件上限
    restriction_until: Mapped[float] = mapped_column(Float, default=0.0) # 风控结束时间戳（0 表示无风控）
    last_used_at: Mapped[float] = mapped_column(Float, default=0.0)      # 最后使用时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ShareAnalysisResult(Base):
    """分享链接分析结果表（按账号隔离）"""
    __tablename__ = "share_analysis_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)  # 关联账号 ID
    snap_id: Mapped[str] = mapped_column(String(100), nullable=True)
    share_code: Mapped[str] = mapped_column(String(100), nullable=True)
    share_title: Mapped[str] = mapped_column(Text, nullable=True)
    share_url: Mapped[str] = mapped_column(Text, nullable=True)
    receive_code: Mapped[str] = mapped_column(String(50), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    size_text: Mapped[str] = mapped_column(String(50), default="0B")
    create_time: Mapped[str] = mapped_column(String(30), nullable=True)
    create_timestamp: Mapped[int] = mapped_column(Integer, default=0)
    share_state: Mapped[int] = mapped_column(Integer, default=1)
    status_text: Mapped[str] = mapped_column(String(20), default="正常")
    is_violated: Mapped[bool] = mapped_column(Boolean, default=False)
    is_invalid: Mapped[bool] = mapped_column(Boolean, default=False)
    is_expired: Mapped[bool] = mapped_column(Boolean, default=False)
    is_reviewing: Mapped[bool] = mapped_column(Boolean, default=False)
    receive_count: Mapped[int] = mapped_column(Integer, default=0)
    analysis_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ShareAnalysisState(Base):
    """分享分析任务状态表（按账号存储）"""
    __tablename__ = "share_analysis_states"

    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 账号 ID 作为主键
    is_analyzing: Mapped[bool] = mapped_column(Boolean, default=False)
    total: Mapped[int] = mapped_column(Integer, default=0)
    normal: Mapped[int] = mapped_column(Integer, default=0)
    violated: Mapped[int] = mapped_column(Integer, default=0)
    invalid: Mapped[int] = mapped_column(Integer, default=0)
    expired: Mapped[int] = mapped_column(Integer, default=0)
    reviewing: Mapped[int] = mapped_column(Integer, default=0)
    scanned: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped[str] = mapped_column(String(30), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExcelTaskItem(Base):
    __tablename__ = "excel_task_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    row_index: Mapped[int] = mapped_column(Integer)
    original_url: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255), nullable=True)
    extraction_code: Mapped[str] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="待处理")  # 待处理, 处理中, 成功, 失败, 跳过, 待审核
    new_share_url: Mapped[str] = mapped_column(Text, nullable=True)
    error_msg: Mapped[str] = mapped_column(Text, nullable=True)
    item_metadata: Mapped[dict] = mapped_column(JSON, nullable=True)  # Store original message text and entities
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TMDBConfig(Base):
    """TMDB API 配置表"""
    __tablename__ = "tmdb_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    api_key: Mapped[str] = mapped_column(Text, default="")
    country: Mapped[str] = mapped_column(String(10), default="US")
    certifications: Mapped[dict] = mapped_column(JSON, default=list)  # ["R", "NC-17"]
    keywords: Mapped[str] = mapped_column(Text, default="")  # 关键词过滤，逗号分隔
    use_keyword_filter: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否启用关键词过滤
    last_sync_at: Mapped[str] = mapped_column(String(30), nullable=True)  # 上次全量爬取完成时间
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SensitiveMovie(Base):
    """敏感电影信息表"""
    __tablename__ = "sensitive_movies"

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    original_title: Mapped[str] = mapped_column(String(255), nullable=True)
    chinese_title: Mapped[str] = mapped_column(String(255), nullable=True)  # 中文译名
    alternative_titles: Mapped[dict] = mapped_column(JSON, default=list)  # 别名列表
    release_date: Mapped[str] = mapped_column(String(20), nullable=True)
    certification: Mapped[str] = mapped_column(String(20), nullable=True)
    country: Mapped[str] = mapped_column(String(10), default="US")
    overview: Mapped[str] = mapped_column(Text, nullable=True)
    poster_path: Mapped[str] = mapped_column(String(255), nullable=True)
    keywords: Mapped[dict] = mapped_column(JSON, default=list)  # 电影关键词列表
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SharePushTask(Base):
    """分享链接推送任务表"""
    __tablename__ = "share_push_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    channel_id: Mapped[str] = mapped_column(String(100))
    channel_name: Mapped[str] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running, paused, completed, cancelled
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    interval_min: Mapped[int] = mapped_column(Integer, default=3)
    interval_max: Mapped[int] = mapped_column(Integer, default=5)
    current_index: Mapped[int] = mapped_column(Integer, default=0)
    share_ids: Mapped[dict] = mapped_column(JSON, default=list)  # 要推送的分享ID列表
    failed_ids: Mapped[dict] = mapped_column(JSON, default=list)  # 推送失败的分享ID列表
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScheduledShareTask(Base):
    """定时目录分享任务表"""
    __tablename__ = "scheduled_share_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    dir_path: Mapped[str] = mapped_column(String(255))                               # 115网盘目录路径，例如 /电影
    cron_expression: Mapped[str] = mapped_column(String(50))                         # cron定时表达式，如 0 3 * * *
    clear_files: Mapped[bool] = mapped_column(Boolean, default=True)                 # 定时执行模式：True为移动(剪切)，False为复制(保留)
    share_mode: Mapped[str] = mapped_column(String(20), default="move", server_default="move") # 分享模式：move(移动), copy(复制), direct(直接)
    min_size: Mapped[float] = mapped_column(Float, default=0.0)                      # 最小容量触发阈值，0表示不检测
    min_size_unit: Mapped[str] = mapped_column(String(10), default="GB")             # 容量触发阈值单位 (GB/TB)
    target_channels: Mapped[dict] = mapped_column(JSON, default=list)                # 要推送的TG频道列表，支持多选
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)                      # 任务是否启用
    status: Mapped[str] = mapped_column(String(50), default="waiting")               # 当前运行状态 (waiting, running, success, failed)
    last_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)           # 最近一次执行时间
    sensitive_replace_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    sensitive_replace_pinyin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    sensitive_replace_tmdb: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


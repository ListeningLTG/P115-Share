"""
P115 多账号管理器
负责账号的生命周期管理、负载均衡调度、级联删除等。
"""
import time
import asyncio
from typing import Optional, Dict, List
from loguru import logger
from sqlalchemy import select, delete

from app.core.database import async_session
from app.models.schema import P115Account, ShareAnalysisResult, ShareAnalysisState, SystemSettings


class AccountManager:
    """115网盘账号管理器（单例）"""

    def __init__(self):
        # account_id -> P115Service 实例
        self._services: Dict[int, object] = {}
        self._load_balance_enabled: bool = False
        self._initialized: bool = False
        self._lock = asyncio.Lock()

    # ─────────────────────────────────────────────
    # 初始化 & 迁移
    # ─────────────────────────────────────────────

    async def initialize(self):
        """启动时初始化：加载账号、迁移旧数据"""
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            await self._migrate_legacy_config()
            await self._load_accounts()
            await self._load_global_settings()
            self._initialized = True
            logger.info(f"✅ AccountManager 已初始化，共加载 {len(self._services)} 个账号")

    async def _migrate_legacy_config(self):
        """将旧版单账号配置迁移到多账号表（只迁移一次）"""
        async with async_session() as session:
            # 如果账号表已有数据，跳过迁移
            result = await session.execute(select(P115Account).limit(1))
            if result.scalar_one_or_none():
                return

            # 读取旧配置
            result = await session.execute(
                select(SystemSettings).where(SystemSettings.key.in_([
                    "P115_COOKIE", "P115_SAVE_DIR", "P115_RECYCLE_PASSWORD"
                ]))
            )
            old_cfg: dict = {row.key: row.value for row in result.scalars().all()}

            cookie = old_cfg.get("P115_COOKIE", "")
            save_dir = old_cfg.get("P115_SAVE_DIR", "115-Share")
            recycle_pwd = old_cfg.get("P115_RECYCLE_PASSWORD", "")

            if not cookie:
                logger.info("📦 旧版无账号配置，跳过迁移")
                return

            account = P115Account(
                name="账号1",
                cookie=cookie,
                save_dir=save_dir,
                recycle_password=recycle_pwd,
                priority=1,
                enabled=True,
            )
            session.add(account)
            await session.flush()  # 获取 account.id

            # 迁移 JSON 文件中的分析结果
            import os, json
            json_file = os.path.join("data", "share_analysis_results.json")
            if os.path.exists(json_file):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    results_data = data.get("results", [])

                    # 写入 ShareAnalysisState
                    state = ShareAnalysisState(
                        account_id=account.id,
                        is_analyzing=False,
                        total=data.get("total", 0),
                        normal=data.get("normal", 0),
                        violated=data.get("violated", 0),
                        expired=data.get("expired", 0),
                        reviewing=data.get("reviewing", 0),
                        scanned=data.get("scanned", 0),
                        last_updated=data.get("last_updated"),
                    )
                    session.add(state)

                    # 写入 ShareAnalysisResult
                    for item in results_data:
                        row = ShareAnalysisResult(
                            account_id=account.id,
                            snap_id=item.get("snap_id"),
                            share_code=item.get("share_code"),
                            share_title=item.get("share_title"),
                            share_url=item.get("share_url"),
                            receive_code=item.get("receive_code", ""),
                            file_size=item.get("file_size", 0),
                            size_text=item.get("size_text", "0B"),
                            create_time=item.get("create_time"),
                            create_timestamp=item.get("create_timestamp", 0),
                            share_state=item.get("share_state", 1),
                            status_text=item.get("status_text", "正常"),
                            is_violated=bool(item.get("is_violated", False)),
                            is_expired=bool(item.get("is_expired", False)),
                            is_reviewing=bool(item.get("is_reviewing", False)),
                            receive_count=item.get("receive_count", 0),
                        )
                        session.add(row)

                    logger.info(f"📦 已从 JSON 文件迁移 {len(results_data)} 条分析结果")
                except Exception as e:
                    logger.warning(f"迁移分析结果时出错（不影响账号迁移）: {e}")

            await session.commit()
            logger.info("✅ 旧版账号配置迁移完成 → 多账号表")

    async def _load_accounts(self):
        """从数据库加载所有启用的账号并创建 P115Service 实例"""
        from app.services.p115 import P115Service
        async with async_session() as session:
            result = await session.execute(
                select(P115Account).where(P115Account.enabled == True).order_by(P115Account.priority)
            )
            accounts = result.scalars().all()

        now = time.time()
        for acc in accounts:
            svc = P115Service(account=acc)
            self._services[acc.id] = svc
            if acc.cookie:
                svc.init_client(acc.cookie)
            # 恢复未过期的风控状态
            if acc.restriction_until and acc.restriction_until > now:
                svc._restriction_until = acc.restriction_until
                logger.info(f"⚠️ 账号 [{acc.id}] {acc.name} 恢复风控状态，将于 {time.strftime('%H:%M:%S', time.localtime(acc.restriction_until))} 解除")

    async def _load_global_settings(self):
        """加载全局设置（负载均衡开关）"""
        async with async_session() as session:
            result = await session.execute(
                select(SystemSettings).where(SystemSettings.key == "P115_LOAD_BALANCE_ENABLED")
            )
            row = result.scalar_one_or_none()
            if row:
                self._load_balance_enabled = row.value.lower() == "true"

    # ─────────────────────────────────────────────
    # 账号调度
    # ─────────────────────────────────────────────

    def get_service(self, account_id: int) -> Optional[object]:
        """获取指定账号的 P115Service 实例"""
        return self._services.get(account_id)

    def get_primary_service(self) -> Optional[object]:
        """获取用于默认操作的账号（负载均衡关闭时返回优先级最高的）"""
        if not self._services:
            return None

        if not self._load_balance_enabled:
            # 只用优先级最高（数字最小）的可用账号
            for svc in sorted(self._services.values(), key=lambda s: s.account.priority):
                if not svc.is_restricted and svc.is_connected:
                    return svc
            # 如果没有可用的，返回优先级最高的
            return min(self._services.values(), key=lambda s: s.account.priority)

        # 负载均衡：选择最空闲的账号
        available = [
            svc for svc in self._services.values()
            if svc.account.enabled and not svc.is_restricted and svc.is_connected
        ]
        if not available:
            # 降级：返回优先级最高的
            enabled = [s for s in self._services.values() if s.account.enabled]
            if enabled:
                return min(enabled, key=lambda s: s.account.priority)
            return None

        # 排序：队列大小升序 → 优先级升序（数字越小越优先）→ 最后使用时间升序
        available.sort(key=lambda s: (s.queue_size, s.account.priority, s.account.last_used_at or 0.0))
        return available[0]

    def get_all_services(self) -> List[object]:
        """返回所有账号的 P115Service 列表（按优先级排序）"""
        return sorted(self._services.values(), key=lambda s: s.account.priority)

    async def get_accounts_list(self) -> List[dict]:
        """返回所有账号的摘要信息（含在线状态）"""
        async with async_session() as session:
            result = await session.execute(
                select(P115Account).order_by(P115Account.priority)
            )
            accounts = result.scalars().all()

        out = []
        for acc in accounts:
            svc = self._services.get(acc.id)
            out.append({
                "id": acc.id,
                "name": acc.name,
                "cookie": acc.cookie[:30] + "..." if acc.cookie and len(acc.cookie) > 30 else acc.cookie,
                "save_dir": acc.save_dir,
                "recycle_password": acc.recycle_password,
                "priority": acc.priority,
                "enabled": acc.enabled,
                "save_file_limit": acc.save_file_limit,
                "share_file_limit": acc.share_file_limit,
                "is_restricted": svc.is_restricted if svc else (time.time() < acc.restriction_until),
                "is_connected": svc.is_connected if svc else False,
                "queue_size": svc.queue_size if svc else 0,
                "last_used_at": acc.last_used_at,
                "created_at": acc.created_at.strftime("%Y-%m-%d %H:%M:%S") if acc.created_at else None,
            })
        return out

    # ─────────────────────────────────────────────
    # CRUD
    # ─────────────────────────────────────────────

    async def create_account(self, data: dict) -> dict:
        """创建新账号"""
        from app.services.p115 import P115Service
        async with async_session() as session:
            acc = P115Account(
                name=data.get("name", "新账号"),
                cookie=data.get("cookie", ""),
                save_dir=data.get("save_dir", "115-Share"),
                recycle_password=data.get("recycle_password", ""),
                priority=data.get("priority", 10),
                enabled=data.get("enabled", True),
                save_file_limit=data.get("save_file_limit", 500),
                share_file_limit=data.get("share_file_limit", 10000),
            )
            session.add(acc)
            await session.commit()
            await session.refresh(acc)

        # 创建 Service 实例
        svc = P115Service(account=acc)
        self._services[acc.id] = svc
        if acc.cookie:
            svc.init_client(acc.cookie)

        logger.info(f"✅ 新账号已创建: [{acc.id}] {acc.name}")
        return {"id": acc.id, "name": acc.name}

    async def update_account(self, account_id: int, data: dict) -> bool:
        """更新账号配置"""
        from app.services.p115 import P115Service
        async with async_session() as session:
            result = await session.execute(
                select(P115Account).where(P115Account.id == account_id)
            )
            acc = result.scalar_one_or_none()
            if not acc:
                return False

            cookie_changed = False
            save_dir_changed = False

            if "name" in data:
                acc.name = data["name"]
            if "cookie" in data and data["cookie"] != acc.cookie:
                acc.cookie = data["cookie"]
                cookie_changed = True
            if "save_dir" in data and data["save_dir"] != acc.save_dir:
                acc.save_dir = data["save_dir"]
                save_dir_changed = True
            if "recycle_password" in data:
                acc.recycle_password = data["recycle_password"]
            if "priority" in data:
                acc.priority = data["priority"]
            if "enabled" in data:
                acc.enabled = data["enabled"]
            if "save_file_limit" in data:
                acc.save_file_limit = data["save_file_limit"]
            if "share_file_limit" in data:
                acc.share_file_limit = data["share_file_limit"]

            await session.commit()
            await session.refresh(acc)

        # 更新 Service 实例
        svc = self._services.get(account_id)
        if svc:
            svc.account = acc
            if cookie_changed and acc.cookie:
                svc.init_client(acc.cookie)
            if save_dir_changed:
                svc.clear_save_dir_cache()
        elif acc.enabled:
            # 如果之前没有 service（可能被禁用过），重新创建
            svc = P115Service(account=acc)
            self._services[account_id] = svc
            if acc.cookie:
                svc.init_client(acc.cookie)

        # 如果禁用了账号，从活跃服务中移除
        if not acc.enabled and account_id in self._services:
            del self._services[account_id]

        logger.info(f"✅ 账号已更新: [{account_id}] {acc.name}")
        return True

    async def delete_account(self, account_id: int) -> bool:
        """删除账号及其所有关联数据（级联删除）"""
        async with async_session() as session:
            # 1. 删除分析结果
            await session.execute(
                delete(ShareAnalysisResult).where(ShareAnalysisResult.account_id == account_id)
            )
            # 2. 删除分析状态
            await session.execute(
                delete(ShareAnalysisState).where(ShareAnalysisState.account_id == account_id)
            )
            # 3. 删除账号
            result = await session.execute(
                select(P115Account).where(P115Account.id == account_id)
            )
            acc = result.scalar_one_or_none()
            if not acc:
                await session.rollback()
                return False
            await session.delete(acc)
            await session.commit()

        # 4. 移除 Service 实例
        if account_id in self._services:
            del self._services[account_id]

        logger.info(f"🗑️ 账号已删除: [{account_id}]，关联数据已级联清理")
        return True

    async def test_account_connection(self, account_id: int) -> dict:
        """测试账号连接（验证 Cookie 有效性）"""
        svc = self._services.get(account_id)
        if not svc:
            return {"success": False, "message": "账号不存在或未启用"}
        if not svc.client:
            return {"success": False, "message": "客户端未初始化，请检查 Cookie"}
        ok = await svc.verify_connection()
        if ok:
            return {"success": True, "message": "连接成功，Cookie 有效"}
        else:
            return {"success": False, "message": "连接失败，请检查 Cookie 是否过期"}

    # ─────────────────────────────────────────────
    # 全局配置
    # ─────────────────────────────────────────────

    async def set_load_balance(self, enabled: bool):
        """设置负载均衡开关并持久化"""
        self._load_balance_enabled = enabled
        async with async_session() as session:
            result = await session.execute(
                select(SystemSettings).where(SystemSettings.key == "P115_LOAD_BALANCE_ENABLED")
            )
            row = result.scalar_one_or_none()
            if row:
                row.value = str(enabled)
            else:
                session.add(SystemSettings(key="P115_LOAD_BALANCE_ENABLED", value=str(enabled)))
            await session.commit()

    @property
    def load_balance_enabled(self) -> bool:
        return self._load_balance_enabled

    async def update_last_used(self, account_id: int):
        """更新账号最后使用时间戳（异步，不阻塞主流程）"""
        now = time.time()
        svc = self._services.get(account_id)
        if svc:
            svc.account.last_used_at = now
        async with async_session() as session:
            result = await session.execute(
                select(P115Account).where(P115Account.id == account_id)
            )
            acc = result.scalar_one_or_none()
            if acc:
                acc.last_used_at = now
                await session.commit()


# 全局单例
account_manager = AccountManager()

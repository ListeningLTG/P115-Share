import asyncio
import logging
import os
import time
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from app.core.config import settings
from app.core.database import async_session
from app.models.schema import ScheduledShareTask
from app.services.p115 import p115_service

logger = logging.getLogger(__name__)


def _get_all_svcs():
    """获取所有启用账号的 P115Service 列表，降级到全局单例"""
    try:
        from app.services.account_manager import account_manager
        svcs = account_manager.get_all_services()
        if svcs:
            return svcs
    except Exception:
        pass
    return [p115_service]


async def _scheduled_cleanup_save_dir(wait: bool = False):
    """定时清理保存目录——遍历所有账号分别执行"""
    for svc in _get_all_svcs():
        try:
            await svc.cleanup_save_directory(wait)
        except Exception as e:
            acc_id = svc.account.id if getattr(svc, 'account', None) else '?'
            logger.error(f"[ERROR] 账号 [{acc_id}] 清理保存目录失败: {e}")


async def _scheduled_cleanup_recycle_bin(wait: bool = False):
    """定时清空回收站——遍历所有账号分别执行"""
    for svc in _get_all_svcs():
        try:
            await svc.cleanup_recycle_bin(wait)
        except Exception as e:
            acc_id = svc.account.id if getattr(svc, 'account', None) else '?'
            logger.error(f"[ERROR] 账号 [{acc_id}] 清空回收站失败: {e}")


async def _scheduled_capacity_check():
    """定时容量检测——遍历所有账号分别执行"""
    for svc in _get_all_svcs():
        try:
            await svc.check_capacity_and_cleanup(mode="scheduled")
        except Exception as e:
            acc_id = svc.account.id if getattr(svc, 'account', None) else '?'
            logger.error(f"[ERROR] 账号 [{acc_id}] 容量检测失败: {e}")


async def _scheduled_session_expiry_check():
    """定时检测所有 115 账号的登录 Session 有效性并进行 TG 告警通知"""
    if not settings.TG_BOT_TOKEN:
        logger.debug("[SessionCheck] TG Bot Token 未配置，跳过 Session 失效检测")
        return

    failed_accounts = []
    for svc in _get_all_svcs():
        try:
            # 这里的 verify_connection 会发送请求验证 Cookie 是否有效
            is_ok = await svc.verify_connection()
            if not is_ok:
                acc_name = svc.account.name if (svc.account and svc.account.name) else f"账号(ID: {svc.account.id})" if svc.account else "默认账号"
                failed_accounts.append(acc_name)
        except Exception as e:
            acc_name = svc.account.name if (svc.account and svc.account.name) else f"账号(ID: {svc.account.id})" if svc.account else "默认账号"
            logger.error(f"[SessionCheck] 检测账号 [{acc_name}] 登录状态失败: {e}")
            failed_accounts.append(acc_name)

    if failed_accounts:
        try:
            from app.services.tg_bot import tg_service
            logger.warning(f"[SessionCheck] 检测到有账号登录失效: {failed_accounts}，准备发送通知")
            accounts_str = "\n".join([f"- {name}" for name in failed_accounts])
            msg = (
                f"⚠️ **115 账号登录状态失效提醒**\n\n"
                f"以下账号的登录状态已失效，请及时在 [账号管理] 或设置中更新 Cookie：\n"
                f"{accounts_str}"
            )
            await tg_service.send_admin_msg(msg)
        except Exception as e:
            logger.error(f"[SessionCheck] 发送 TG 账号失效通知失败: {e}")


async def _run_scheduled_share_task(task_id: int):
    """定时复制目录并分享到频道，最后清空临时目录及回收站"""
    # 1. 查询任务信息
    async with async_session() as session:
        result = await session.execute(select(ScheduledShareTask).where(ScheduledShareTask.id == task_id))
        task = result.scalar_one_or_none()
        if not task or not task.enabled:
            return
        
        # 更新状态为运行中
        task.status = "running"
        await session.commit()

    logger.info(f"🚀 开始执行定时分享任务 [{task_id}] '{task.name}'")
    
    try:
        # 2. 获取网盘账号 service
        from app.services.account_manager import account_manager
        svc = account_manager.get_service(task.account_id)
        if not svc:
            raise ValueError(f"网盘账号 (ID: {task.account_id}) 不存在或未启用")
        
        # 3. 解析源目录及其父路径
        src_path = task.dir_path.strip().rstrip('/')
        if not src_path.startswith('/'):
            src_path = '/' + src_path
        if src_path == '/':
            raise ValueError("不能对根目录执行定时分享")
            
        parent_path, folder_name = os.path.split(src_path)
        if not parent_path:
            parent_path = '/'
            
        logger.info(f"📂 源目录: {src_path}, 父路径: {parent_path}, 文件夹名: {folder_name}")
        
        # 4. 获取/创建父目录 CID 与源目录 CID
        parent_cid = await svc._ensure_save_dir(parent_path)
        src_cid = await svc._ensure_save_dir(src_path)
        
        # 5. 获取源目录中所有顶级文件/目录的信息
        items = await svc._get_dir_items(src_cid)
        top_ids = {item["id"] for item in items}
        if not top_ids:
            logger.warning(f"⚠️ 源目录 {src_path} 为空，跳过定时分享。")
            async with async_session() as session:
                task_db = await session.get(ScheduledShareTask, task_id)
                if task_db:
                    task_db.status = "success"
                    task_db.last_run_at = datetime.utcnow()
                    await session.commit()
            return
            
        # 6. 新建带时间戳的目标目录
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        new_folder_name = f"{folder_name}_{timestamp}"
        
        logger.info(f"📁 新建复制目标文件夹: {new_folder_name}")
        sub_dir_resp = await svc._api_call_with_timeout(
            svc.client.fs_makedirs_app, new_folder_name, pid=parent_cid, async_=True,
            **svc._get_ios_ua_kwargs()
        )
        from p115client import check_response
        check_response(sub_dir_resp)
        new_cid = int(sub_dir_resp.get("cid") or sub_dir_resp.get("id") or (sub_dir_resp.get("data") or {}).get("cid") or 0)
        if not new_cid:
            raise RuntimeError("创建目标复制子目录失败")
            
        # 7. 复制顶级文件/目录到新目录
        fids = list(top_ids)
        logger.info(f"📤 正在复制顶级项 {len(fids)} 个到 {new_folder_name} (CID: {new_cid})...")
        copy_resp = await svc._api_call_with_timeout(
            svc.client.fs_copy_app, fids, pid=new_cid, async_=True,
            **svc._get_ios_ua_kwargs()
        )
        check_response(copy_resp)
        
        # 额外休眠以确保 115 后台复制任务有时间初始化
        await asyncio.sleep(5)
        
        # 8. 如果开启了清空原目录文件开关，删除原顶级项（带退避重试，防止 115 复制未完锁定源文件）
        if task.clear_files:
            logger.info("🧹 正在清理原目录中的所有顶级项...")
            max_delete_attempts = 6
            for attempt in range(1, max_delete_attempts + 1):
                try:
                    del_resp = await svc._api_call_with_timeout(
                        svc.client.fs_delete_app, fids, async_=True,
                        **svc._get_ios_ua_kwargs()
                    )
                    check_response(del_resp)
                    logger.info("🧹 原目录顶级项清理完成")
                    break
                except Exception as del_e:
                    # 检查是否为后台复制未完成导致的 Busy 错误 (如 errno 990019)
                    is_busy = "尚未执行完成" in str(del_e) or "请稍后再试" in str(del_e) or "990019" in str(del_e)
                    if is_busy and attempt < max_delete_attempts:
                        wait_sec = attempt * 5
                        logger.warning(f"⚠️ 115 复制操作仍在后台执行中，源文件被锁定。将在 {wait_sec} 秒后重试删除 (尝试 {attempt}/{max_delete_attempts})...")
                        await asyncio.sleep(wait_sec)
                    else:
                        raise del_e
            
        # 9. 对复制出来的新文件夹生成永久分享链接
        logger.info(f"🔗 正在为 {new_folder_name} (CID: {new_cid}) 创建分享链接...")
        await asyncio.sleep(5)
        share_link = await svc._share_fids_direct([new_cid])
        if not share_link:
            raise RuntimeError("创建分享链接失败")
        
        logger.info(f"✅ 生成定时分享链接成功: {share_link}")
        
        # 10. 推送分享链接至 TG 频道
        if task.target_channels:
            from app.services.tg_bot import tg_service
            logger.info(f"📢 正在推送至频道: {task.target_channels}")
            
            # 格式化顶级项结构信息（限 15 项）
            max_show = 15
            # 先文件夹后文件，按名称字母顺序升序排序
            items_sorted = sorted(items, key=lambda x: (not x["is_dir"], x["name"].lower()))
            shown_items = items_sorted[:max_show]
            lines = []
            for item in shown_items:
                icon = "📁" if item["is_dir"] else "📄"
                lines.append(f"  {icon} {item['name']}")
            if len(items_sorted) > max_show:
                lines.append(f"  ...等共 {len(items_sorted)} 个项目")
            
            items_str = "\n".join(lines)
            full_text = f"📁 定时分享: {folder_name}\n"
            if items_str:
                full_text += f"📂 包含项目:\n{items_str}\n"
            full_text += f"🔗 链接: {share_link}"
            
            metadata = {
                "full_text": full_text,
                "entities": [],
                "photo_id": None
            }
            await tg_service.broadcast_to_channels(
                {share_link: share_link},
                metadata,
                channel_ids=task.target_channels
            )
            
        # 11. 清理掉复制出来的新目录 (删除 + 清空回收站)
        logger.info(f"🗑️ 正在删除新创建的临时复制目录 {new_folder_name} (CID: {new_cid})...")
        del_copy_resp = await svc._api_call_with_timeout(
            svc.client.fs_delete_app, [new_cid], async_=True,
            **svc._get_ios_ua_kwargs()
        )
        check_response(del_copy_resp)
        
        # 额外休眠，以确保 115 后台有足够时间将删除的文件夹移入回收站后再执行清空操作
        logger.info("⏳ 等待 5 秒以确保删除项完全移入回收站...")
        await asyncio.sleep(5)
        
        logger.info("🗑️ 正在清空网盘回收站...")
        await svc.cleanup_recycle_bin()
        
        # 12. 更新任务状态为成功
        async with async_session() as session:
            task_db = await session.get(ScheduledShareTask, task_id)
            if task_db:
                task_db.status = "success"
                task_db.last_run_at = datetime.utcnow()
                await session.commit()
        logger.info(f"🎉 定时分享任务 [{task_id}] '{task.name}' 执行完毕！")
                
    except Exception as e:
        logger.error(f"❌ 定时分享任务 [{task_id}] 执行失败: {e}", exc_info=True)
        async with async_session() as session:
            task_db = await session.get(ScheduledShareTask, task_id)
            if task_db:
                task_db.status = f"failed: {str(e)[:100]}"
                task_db.last_run_at = datetime.utcnow()
                await session.commit()


class CleanupScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()

    def start(self):
        """Start the scheduler"""
        self.update_cleanup_dir_job()
        self.update_cleanup_trash_job()
        self.update_cleanup_capacity_job()
        self.update_session_check_job()
        self.scheduler.start()
        logger.info("[TIME] 定时清理任务已启动")

    def update_cleanup_dir_job(self):
        """Update or remove the cleanup save directory job based on config"""
        job_id = "cleanup_save_dir"
        if settings.P115_CLEANUP_DIR_CRON:
            try:
                self.scheduler.add_job(
                    _scheduled_cleanup_save_dir,
                    CronTrigger.from_crontab(settings.P115_CLEANUP_DIR_CRON),
                    id=job_id,
                    name="清理保存目录",
                    replace_existing=True
                )
                logger.info(f"[OK] 已设置清理保存目录定时任务: {settings.P115_CLEANUP_DIR_CRON}")
            except Exception as e:
                logger.error(f"[ERROR] 设置清理保存目录定时任务失败: {e}")
        else:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info("[-] 已移除清理保存目录定时任务")

    def update_cleanup_trash_job(self):
        """Update or remove the cleanup recycle bin job based on config"""
        job_id = "cleanup_recycle_bin"
        if settings.P115_CLEANUP_TRASH_CRON:
            try:
                self.scheduler.add_job(
                    _scheduled_cleanup_recycle_bin,
                    CronTrigger.from_crontab(settings.P115_CLEANUP_TRASH_CRON),
                    id=job_id,
                    name="清空回收站",
                    replace_existing=True
                )
                logger.info(f"[OK] 已设置清空回收站定时任务: {settings.P115_CLEANUP_TRASH_CRON}")
            except Exception as e:
                logger.error(f"[ERROR] 设置清空回收站定时任务失败: {e}")
        else:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info("[-] 已移除清空回收站定时任务")

    def update_cleanup_capacity_job(self):
        """Update or remove the capacity check job based on config"""
        job_id = "cleanup_capacity_check"
        if settings.P115_CLEANUP_CAPACITY_ENABLED:
            try:
                # 每 30 分钟检查一次容量
                self.scheduler.add_job(
                    _scheduled_capacity_check,
                    'interval',
                    minutes=30,
                    id=job_id,
                    name="自动检测网盘容量",
                    replace_existing=True
                )
                logger.info(f"[OK] 已设置容量自动检测任务: 每 30 分钟一次 (阈值: {settings.P115_CLEANUP_CAPACITY_LIMIT} TB)")
            except Exception as e:
                logger.error(f"[ERROR] 设置容量自动检测任务失败: {e}")
        else:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info("[-] 已移除容量自动检测任务")

    def update_session_check_job(self):
        """Update or start the session check job"""
        job_id = "session_expiry_check"
        try:
            # 每 30 分钟检测一次账号登录 Session 有效性
            self.scheduler.add_job(
                _scheduled_session_expiry_check,
                'interval',
                minutes=30,
                id=job_id,
                name="自动检测115账号登录有效性",
                replace_existing=True
            )
            logger.info("[OK] 已设置115账号登录有效性自动检测任务: 每 30 分钟一次")
        except Exception as e:
            logger.error(f"[ERROR] 设置115账号登录有效性自动检测任务失败: {e}")

    def sync_scheduled_share_job(self, task):
        """同步单个定时分享任务"""
        job_id = f"scheduled_share_{task.id}"
        if task.enabled:
            try:
                self.scheduler.add_job(
                    _run_scheduled_share_task,
                    CronTrigger.from_crontab(task.cron_expression),
                    args=[task.id],
                    id=job_id,
                    name=f"定时分享: {task.name}",
                    replace_existing=True
                )
                logger.info(f"[OK] 已设置定时分享任务 [{task.id}] '{task.name}': {task.cron_expression}")
            except Exception as e:
                logger.error(f"[ERROR] 设置定时分享任务 [{task.id}] 调度失败: {e}")
        else:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info(f"[-] 已移除定时分享任务 [{task.id}] '{task.name}'")

    async def sync_all_scheduled_share_jobs(self):
        """加载并同步所有启用的定时分享任务"""
        try:
            async with async_session() as session:
                result = await session.execute(select(ScheduledShareTask))
                tasks = result.scalars().all()
                for task in tasks:
                    self.sync_scheduled_share_job(task)
            logger.info("[TIME] 所有定时分享任务同步完成")
        except Exception as e:
            logger.error(f"[ERROR] 同步定时分享任务失败: {e}")

    def shutdown(self):
        """Shutdown the scheduler"""
        self.scheduler.shutdown()
        logger.info("[TIME] 定时清理任务已停止")



cleanup_scheduler = CleanupScheduler()

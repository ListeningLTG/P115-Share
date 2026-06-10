import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.core.config import settings
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

    def shutdown(self):
        """Shutdown the scheduler"""
        self.scheduler.shutdown()
        logger.info("[TIME] 定时清理任务已停止")


cleanup_scheduler = CleanupScheduler()

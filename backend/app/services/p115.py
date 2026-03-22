from p115client import P115Client, check_response
from p115client.fs import P115FileSystem
from p115client.util import share_extract_payload
from p115client.tool import share_iterdir_walk
from app.core.config import settings
from loguru import logger
import asyncio
import time
from collections import deque
import json
import re
import random
from contextlib import asynccontextmanager
from typing import Literal, Optional, Tuple, Union
from app.core.database import async_session
from app.models.schema import PendingLink, LinkHistory
from sqlalchemy import select, delete

# 默认 API 请求超时（秒）
API_TIMEOUT = 60
# 默认 API 重试次数
API_MAX_RETRIES = 3
# 重试间隔（秒）
API_RETRY_DELAY = 5

# iOS 用户代理
IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 115wangpan_ios/36.2.20"
)


class P115Service:
    def __init__(self, account=None):
        """
        account: P115Account 模型实例（多账号模式）。
                 为 None 时为兼容旧版单例模式，从 settings 读取配置。
        """
        self.account = account  # P115Account ORM 对象，多账号模式下非 None
        self.client = None
        self.fs = None
        self.is_connected = False
        self._task_lock: Optional[asyncio.Lock] = None  # Lazy initialize
        self._current_task: str | None = None  # Track current task type
        self._save_dir_cid: int = 0  # Cached save directory CID
        # 任务队列和监测机制
        self._task_queue = asyncio.Queue()
        self._worker_task = None
        self._worker_lock = asyncio.Lock()
        self._current_task_info = None # 存储当前正在处理的任务信息
        self._restriction_until: float = 0 # 限制结束的时间戳

        # 动态频率限制和任务优先级让步
        self._last_save_times = deque(maxlen=1000)  # 限制最大长度，防止无限增长
        self._last_tg_link_time: float = 0.0 # 最近收到 TG 链接的时间
        self._silent_until: float = 0.0      # 静默期截止时间
        self._batch_yield_count: int = 0     # 批量任务避让次数统计
        self._batch_yield_total_time: float = 0.0  # 批量任务避让累计时间

        if account is None and settings.P115_COOKIE:
            self.init_client(settings.P115_COOKIE)

    # ── 账号专属配置属性（多账号时读 account，否则读 settings）────────────

    @property
    def save_dir(self) -> str:
        if self.account:
            return self.account.save_dir or "115-Share"
        return settings.P115_SAVE_DIR or "115-Share"

    @property
    def recycle_password(self) -> str:
        if self.account:
            return self.account.recycle_password or ""
        return settings.P115_RECYCLE_PASSWORD or ""

    @property
    def save_file_limit(self) -> int:
        """单次保存文件上限（默认 500）"""
        if self.account:
            return self.account.save_file_limit or 500
        return 500

    @property
    def share_file_limit(self) -> int:
        """分享文件总数上限（默认 10000）"""
        if self.account:
            return self.account.share_file_limit or 10000
        return 10000

    @property
    def queue_size(self) -> int:
        """返回当前在队列中等待的任务数量"""
        return self._task_queue.qsize()

    @property
    def is_busy(self) -> bool:
        """如果 Worker 正在处理任务或者处于限制状态则返回 True"""
        return self._current_task_info is not None or self.is_restricted

    @property
    def is_restricted(self) -> bool:
        """检查当前是否处于 115 限制状态"""
        return time.time() < self._restriction_until

    async def set_restriction(self, hours: float = 1.0):
        """设置全局限制状态并持久化到 DB"""
        self._restriction_until = time.time() + (hours * 3600)
        msg = f"🚫 115 服务已进入全局限制模式，预计持续 {hours} 小时 (直到 {time.strftime('%H:%M:%S', time.localtime(self._restriction_until))})。\n系统将自动暂停正在运行的批量任务，并转为后台轮询处理受限链接。"
        logger.warning(msg)

        # 持久化风控截止时间到 DB
        if self.account:
            asyncio.create_task(self._persist_restriction(self._restriction_until))

        # Pause Batch Tasks
        try:
            from app.services.excel_batch import excel_batch_service
            if excel_batch_service and excel_batch_service.active_task_id:
                task_id = excel_batch_service.active_task_id
                asyncio.create_task(excel_batch_service.pause_task(task_id))
        except Exception as e:
            logger.error(f"暂停批量任务失败: {e}")

    async def clear_restriction(self):
        """清除全局限制状态并持久化到 DB"""
        if self._restriction_until > 0:
            self._restriction_until = 0
            msg = "🔓 115 全局限制模式已解除，队列中的任务将按顺序恢复处理。"
            logger.info(msg)
            if self.account:
                asyncio.create_task(self._persist_restriction(0.0))
            try:
                from app.services.tg_bot import tg_service
                if tg_service:
                    asyncio.create_task(tg_service.send_admin_msg(msg))
            except Exception as e:
                logger.warning(f"发送解除限制通知失败: {e}")

    async def _persist_restriction(self, until: float):
        """将风控截止时间戳写入数据库"""
        try:
            from app.core.database import async_session
            from app.models.schema import P115Account
            from sqlalchemy import select
            async with async_session() as session:
                result = await session.execute(
                    select(P115Account).where(P115Account.id == self.account.id)
                )
                acc = result.scalar_one_or_none()
                if acc:
                    acc.restriction_until = until
                    await session.commit()
        except Exception as e:
            logger.warning(f"持久化风控状态失败: {e}")

    def _get_ios_ua_kwargs(self):
        """获取 iOS 用户代理相关的参数"""
        return {
            "headers": {
                "user-agent": IOS_UA,
                "accept-encoding": "gzip, deflate"
            },
            "app": "ios"
        }


    async def _task_worker(self):
        """后台任务处理 Worker"""
        logger.info("🚀 P115 任务队列 Worker 已启动")
        while True:
            # 获取任务：(task_func, args, kwargs, future, task_type, is_batch)
            task_func, args, kwargs, future, task_type, is_batch = await self._task_queue.get()
            self._current_task_info = task_type
            try:
                # 在执行任务前进行频率和优先级检查
                if "save" in task_type: # 只对保存/转存任务实施频率检查
                    await self._handle_dynamic_rate_limit(is_batch)

                logger.info(f"⚡ 队列正在处理任务: {task_type}")
                # 执行具体逻辑
                result = await task_func(*args, **kwargs)
                if not future.done():
                    future.set_result(result)
            except Exception as e:
                logger.error(f"❌ 队列执行任务 {task_type} 出错: {e}")
                if not future.done():
                    future.set_exception(e)
            finally:
                self._task_queue.task_done()
                self._current_task_info = None

    async def _api_call_with_timeout(
        self,
        coro_func,
        *args,
        timeout: int = API_TIMEOUT,
        max_retries: int = API_MAX_RETRIES,
        retry_delay: int = API_RETRY_DELAY,
        label: str = "API",
        **kwargs,
    ):
        """带超时和重试的 API 调用包装器。
        
        Args:
            coro_func: 异步方法（如 self.client.share_snap）
            *args: 传给 coro_func 的位置参数
            timeout: 单次请求超时秒数
            max_retries: 最大重试次数
            retry_delay: 重试间隔秒数
            label: 日志标识
            **kwargs: 传给 coro_func 的关键字参数
        """
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    coro_func(*args, **kwargs),
                    timeout=timeout,
                )
                return result
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"{label} 请求超时 ({timeout}s), 尝试 {attempt}/{max_retries}")
                logger.warning(f"⏱️ {label} 请求超时 (尝试 {attempt}/{max_retries})")
            except Exception as e:
                # 非超时异常直接抛出，不重试
                raise
            
            if attempt < max_retries:
                logger.info(f"🔄 {label} 将在 {retry_delay}s 后重试...")
                await asyncio.sleep(retry_delay)
        
        raise last_error

    def init_client(self, cookie: str):
        try:
            # Apply proxy settings to environment if configured
            import os
            if settings.PROXY_ENABLED and settings.PROXY_HOST and settings.PROXY_PORT:
                proxy_type = settings.PROXY_TYPE.lower()
                auth = f"{settings.PROXY_USER}:{settings.PROXY_PASS}@" if settings.PROXY_USER and settings.PROXY_PASS else ""
                proxy_url = f"{proxy_type}://{auth}{settings.PROXY_HOST}:{settings.PROXY_PORT}"
                
                os.environ['HTTP_PROXY'] = proxy_url
                os.environ['http_proxy'] = proxy_url
                os.environ['HTTPS_PROXY'] = proxy_url
                os.environ['https_proxy'] = proxy_url
                
            self.client = P115Client(cookie, check_for_relogin=True)
            self.fs = P115FileSystem(self.client)
            self.clear_save_dir_cache()
            
            proxy_info = ""
            if settings.PROXY_ENABLED:
                proxy_info = f" (Proxy: {settings.PROXY_TYPE}://{settings.PROXY_HOST}:{settings.PROXY_PORT})"
            logger.info(f"P115Client and FileSystem initialized successfully{proxy_info}")
            # Verify connection asynchronously
            asyncio.create_task(self.verify_connection())
        except Exception as e:
            logger.error(f"Failed to initialize P115Client: {e}")
            self.client = None
            self.fs = None
            self.is_connected = False

    @asynccontextmanager
    async def _acquire_task_lock(self, task_type: Literal["save_share", "cleanup"], wait: bool = True):
        """已废弃：改为使用任务队列排队处理。
        为了兼容性保留接口，实际逻辑改为在队列中排队。
        """
        # 注意：清理任务目前仍可保持同步等待，但建议所有 115 写操作都过队列
        # 这里为了最小化变动，暂时仅针对 share 链接进行队列化
        yield

    async def _enqueue_op(self, task_type: str, func, *args, is_batch: bool = False, **kwargs):
        """将操作放入队列并等待结果"""
        # 确保 Worker 正在运行
        if self._worker_task is None or self._worker_task.done():
            async with self._worker_lock:
                if self._worker_task is None or self._worker_task.done():
                    self._worker_task = asyncio.create_task(self._task_worker())
                    logger.info("⚡ 延迟启动 P115 任务队列 Worker")

        future = asyncio.get_running_loop().create_future()
        # 将任务作为元组放入：(task_func, args, kwargs, future, task_type, is_batch)
        await self._task_queue.put((func, args, kwargs, future, task_type, is_batch))
        return await future

    def update_tg_activity(self):
        """更新最后一次收到 TG 链接的时间戳，用于批量任务让步"""
        self._last_tg_link_time = time.time()

    def _record_save_activity(self):
        """记录一次成功的保存操作并存入频率窗口"""
        self._last_save_times.append(time.time())

    async def _handle_dynamic_rate_limit(self, is_batch: bool):
        """实施动态频率限制和优先级让步逻辑"""
        # 使用循环代替递归，避免栈溢出
        while True:
            now = time.time()

            # 1. 检查是否需要为 TG 消息让步 (仅针对批量任务)
            if is_batch and settings.P115_BATCH_YIELD_DURATION > 0:
                elapsed_since_tg = now - self._last_tg_link_time
                if elapsed_since_tg < settings.P115_BATCH_YIELD_DURATION:
                    # 动态计算等待时间：TG 链接越新，等待时间越长
                    if elapsed_since_tg < 2:
                        # TG 链接刚到达，等待较长时间
                        wait_time = min(8, settings.P115_BATCH_YIELD_DURATION - elapsed_since_tg)
                    elif elapsed_since_tg < 5:
                        # TG 链接到达一段时间，等待中等时间
                        wait_time = min(5, settings.P115_BATCH_YIELD_DURATION - elapsed_since_tg)
                    else:
                        # TG 链接已过去较长时间，等待较短时间
                        wait_time = min(2, settings.P115_BATCH_YIELD_DURATION - elapsed_since_tg)

                    self._batch_yield_count += 1
                    self._batch_yield_total_time += wait_time
                    logger.info(f"🔄 优先为 TG 消息链接让步，批量任务等待 {wait_time:.1f}s (TG链接距今 {elapsed_since_tg:.1f}s，累计避让 {self._batch_yield_count} 次，总计 {self._batch_yield_total_time:.1f}s)")
                    await asyncio.sleep(wait_time)
                    continue  # 重新检查所有条件

            # 2. 检查是否处于静默期
            if now < self._silent_until:
                wait_time = self._silent_until - now
                logger.info(f"P115-Share 处于保存频次限制静默期，剩余 {wait_time:.1f} 秒...")
                await asyncio.sleep(wait_time)
                continue  # 重新检查所有条件

            # 3. 检查窗口期内的保存次数
            if settings.P115_RATE_LIMIT_COUNT > 0 and settings.P115_RATE_LIMIT_WINDOW > 0:
                cutoff = now - settings.P115_RATE_LIMIT_WINDOW

                # 只在队列较长时才清理，提高效率
                if len(self._last_save_times) > settings.P115_RATE_LIMIT_COUNT * 2:
                    while self._last_save_times and self._last_save_times[0] < cutoff:
                        self._last_save_times.popleft()

                # 统计窗口内的有效记录
                recent_count = sum(1 for t in self._last_save_times if t >= cutoff)

                if recent_count >= settings.P115_RATE_LIMIT_COUNT:
                    # 触发静默期
                    self._silent_until = now + settings.P115_RATE_LIMIT_SILENT_DURATION
                    silent_end_time = time.strftime('%H:%M:%S', time.localtime(self._silent_until))
                    logger.warning(
                        f"⚠️ 警告: 检测到保存过于频繁！在 {settings.P115_RATE_LIMIT_WINDOW}s 内执行了 {recent_count} 次转存，"
                        f"触发频率保护。系统将进入静默期 {settings.P115_RATE_LIMIT_SILENT_DURATION}s (至 {silent_end_time})。"
                    )
                    continue  # 重新进入循环处理静默期

            # 所有检查通过，退出循环
            break

    async def verify_connection(self) -> bool:
        """Verify the 115 cookie connection"""
        if not self.client:
            self.is_connected = False
            return False
            
        try:
            # Simple API call to verify cookie
            resp = await self._api_call_with_timeout(
                self.client.user_info, async_=True,
                timeout=30, max_retries=2, label="user_info",
                **self._get_ios_ua_kwargs()
            )
            if resp.get("state"):
                self.is_connected = True
                logger.info("✅ 115 网盘登录验证成功")
                return True
        except Exception as e:
            logger.error(f"❌ 115 网盘登录验证失败: {e}")
            self.is_connected = False
            return False
            
        self.is_connected = False
        return False

    def clear_save_dir_cache(self):
        """Clear the cached save directory CID (e.g. after cleanup)"""
        self._save_dir_cid = 0
        logger.debug("🗑️ 已清除保存目录 CID 缓存")

    async def _ensure_save_dir(self, path: Optional[str] = None):
        """Ensure the save directory exists and return its CID.
        
        Uses a cached CID to avoid repeated API calls for the default path.
        If a custom path is provided, it will always verify/create it.
        """
        is_default = path is None
        path = path or self.save_dir or "/分享保存"
        
        # Return cached CID if available and using default path
        if is_default and self._save_dir_cid > 0:
            logger.debug(f"📂 使用缓存的保存目录 CID: {self._save_dir_cid}")
            return self._save_dir_cid
        
        logger.info(f"🔍 开始检查/创建保存目录: {path}")
        
        if not self.client:
            raise RuntimeError("P115Client 未初始化，无法创建保存目录")
        
        # Retry up to 3 times with timeout
        last_error = None
        for attempt in range(1, 4):
            try:
                logger.info(f"📁 调用 fs_makedirs_app 创建目录... (尝试 {attempt}/3)")
                # Add 30s timeout to prevent indefinite hanging
                resp = await asyncio.wait_for(
                    self.client.fs_makedirs_app(path, pid=0, async_=True, **self._get_ios_ua_kwargs()),
                    timeout=30
                )
                logger.info(f"📋 fs_makedirs_app 响应: {resp}")
                check_response(resp)
                
                # The response structure has 'cid' at the top level (not in 'data')
                # Response format: {'state': True, 'error': '', 'errCode': 0, 'cid': '3358575817564146054'}
                cid = 0
                if "cid" in resp:
                    cid = int(resp["cid"])
                    logger.info(f"🔢 从响应中提取到 CID: {cid}")
                elif "data" in resp:
                    data = resp["data"]
                    cid = int(data.get("category_id") or data.get("cid") or data.get("id") or 0)
                    logger.info(f"🔢 从 data 字段中提取到 CID: {cid}")
                else:
                    logger.error(f"❌ 响应中没有 'cid' 或 'data' 字段: {resp}")
                    
                if cid == 0:
                    raise RuntimeError(f"无法从响应获取有效的 CID: {resp}")
                    
                # Cache the CID only if it's the default path
                if is_default:
                    self._save_dir_cid = cid
                logger.info(f"✅ 保存目录已确认: {path} (CID: {cid})")
                return cid
                
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"fs_makedirs_app 请求超时 (30s), 尝试 {attempt}/3")
                logger.warning(f"⏱️ fs_makedirs_app 请求超时 (尝试 {attempt}/3)")
            except Exception as e:
                last_error = e
                logger.warning(f"⚠️ 创建目录失败 (尝试 {attempt}/3): {e}")
            
            if attempt < 3:
                await asyncio.sleep(3)
        
        # All retries exhausted — raise to prevent saving to root
        raise RuntimeError(f"无法确保保存目录 {path} 存在 (已重试3次): {last_error}")

    async def _handle_already_received(self, to_cid: int, names: list[str], share_url: str, metadata: dict, have_vio_file: int, receive_payload: dict):
        """处理文件已经接收的情况：先检查是否存在，如果不存在则在子目录重试转存"""
        logger.warning(f"⚠️ 115 提示文件该分享已接收过: {share_url}")
        # Verify if files really exist in to_cid
        try:
            # 用 _find_files_in_dir 查找（支持 search + list 双重查找）
            found_files = await self._find_files_in_dir(to_cid, names)
            found_count = len(found_files)
            if found_count > 0:
                logger.info(f"✅ 在保存目录中找到 {found_count} 个同名文件，继续处理")
                await self.clear_restriction()
                return {
                    "status": "success", 
                    "to_cid": to_cid, 
                    "names": names,
                    "share_url": share_url,
                    "recursive_links": [],
                    "metadata": metadata or {},
                    "have_vio": have_vio_file == 1
                }
            else:
                logger.warning("⚠️ 115 提示已接收，但在保存目录未找到文件。尝试创建新目录重试转存...")
                # 创建带时间戳的新目录
                new_folder_name = f"Retry_{int(time.time())}"
                resp = await self._api_call_with_timeout(
                    self.client.fs_makedirs_app, new_folder_name, pid=to_cid, async_=True,
                    **self._get_ios_ua_kwargs()
                )
                check_response(resp)
                new_cid = int(resp.get("cid") or resp.get("id") or (resp.get("data") or {}).get("cid") or 0)
                
                if not new_cid:
                    raise RuntimeError("创建重试目录失败，未获取到有效CID")
                    
                logger.info(f"📁 已创建重试目录: {new_folder_name} (CID: {new_cid})")
                
                # 修改 payload 的 cid 为新创建的目录并重试
                retry_payload = receive_payload.copy()
                retry_payload["cid"] = new_cid
                
                recv_resp = await self._api_call_with_timeout(
                    self.client.share_receive_app, retry_payload, async_=True,
                    timeout=API_TIMEOUT, label="share_receive_retry",
                    **self._get_ios_ua_kwargs()
                )
                check_response(recv_resp)
                logger.info(f"✅ 在新目录转存成功: {share_url} -> CID {new_cid}")
                await self.clear_restriction()
                
                return {
                    "status": "success", 
                    "to_cid": new_cid, 
                    "names": names,
                    "share_url": share_url,
                    "recursive_links": [],
                    "metadata": metadata or {},
                    "have_vio": have_vio_file == 1
                }
                
        except Exception as check_e:
            logger.error(f"❌ 处理已接收逻辑(验证或重试转存)时出错: {check_e}")
            
            # 尝试提取 errno
            errno_val = getattr(check_e, "errno", None)
            if hasattr(check_e, 'args') and len(check_e.args) >= 2 and isinstance(check_e.args[1], dict):
                if not errno_val:
                    errno_val = check_e.args[1].get("errno")
                    
            if errno_val == 4200045 or "4200045" in str(check_e) or "已经接收" in str(check_e) or "已接收" in str(check_e):
                return {
                    "status": "error",
                    "error_type": "already_exists_missing",
                    "message": "该分享链接您已转存过。115 限制同一链接由于文件丢失而无法重复转存，重试转存也失败，请尝试寻找原文件或从回收站还原。"
                }
            # Assume failure to be safe
            return {
                "status": "error", 
                "error_type": "unknown",
                "message": f"保存失败，且重试转存报错: {str(check_e)}"
            }

    async def save_share_link(self, share_url: str, metadata: dict = None, target_dir: Optional[str] = None, skip_large_package: bool = False, is_batch: bool = False):
        """通过队列保存链接"""
        return await self._enqueue_op(f"save_share_link({share_url})", self._save_share_link_internal, share_url, metadata, target_dir, skip_large_package, is_batch=is_batch)

    async def save_and_share(self, share_url: str, metadata: dict = None, target_dir: Optional[str] = None, skip_large_package: bool = False, db_id: Optional[int] = None, is_batch: bool = False):
        """通过队列进行转存并分享"""
        async def _internal_flow():
            save_res = await self._save_share_link_internal(share_url, metadata, target_dir, skip_large_package, db_id=db_id)
            if save_res and save_res.get("status") == "success":
                share_res = await self.create_share_link(save_res)
                if isinstance(share_res, str):
                    return {"status": "success", "share_link": share_res}
                elif isinstance(share_res, dict) and share_res.get("status") == "error":
                    # 将创建分享时的特定错误映射回转存结果
                    return {
                        "status": "error",
                        "error_type": share_res.get("error_type", "share_failed"),
                        "message": share_res.get("message", "生成分享链接失败")
                    }
                return {
                    "status": "error",
                    "error_type": "share_failed",
                    "message": "转存成功但生成分享链接失败"
                }
            return save_res

        return await self._enqueue_op(f"save_and_share({share_url})", _internal_flow, is_batch=is_batch)

    async def _save_share_link_internal(self, share_url: str, metadata: dict = None, target_dir: Optional[str] = None, skip_large_package: bool = False, db_id: Optional[int] = None):
        """Internal logic for saving a 115 share link (no locking)"""
        if not self.client:
            logger.warning("P115Client not initialized, cannot save link")
            return None
        
        logger.info(f"📥 开始处理分享链接: {share_url}")
        try:
            # 1. Extract share/receive codes
            payload = share_extract_payload(share_url)
            
            # 2. Get share snapshot to get file IDs and names (带超时重试)
            snap_resp = await self._api_call_with_timeout(
                self.client.share_snap_app, payload, async_=True,
                timeout=API_TIMEOUT, label="share_snap",
                **self._get_ios_ua_kwargs()
            )
            check_response(snap_resp)
            logger.debug(f"📋 share_snap 响应数据: {snap_resp.get('data')}")

            # Check for audit and violation status
            data = snap_resp.get("data", {})
            if not data:
                logger.error("❌ share_snap 响应中缺少 data 字段")
                return {
                    "status": "error",
                    "error_type": "api_error",
                    "message": "获取分享信息失败：API 响应数据为空"
                }

            share_info = data.get("shareinfo" if "shareinfo" in data else "share_info", {})
            share_state = data.get("share_state", share_info.get("share_state", share_info.get("status"))) # Multiple fallbacks
            if share_state is not None:
                try:
                    share_state = int(share_state)
                except (ValueError, TypeError):
                    pass
            share_title = share_info.get("share_title", "")
            have_vio_file = share_info.get("have_vio_file", 0)
            
            logger.info(f"📊 分享状态: {share_state}, 标题: {share_title}, 违规标志: {have_vio_file}")

            # 🚀 Early Skip Large Package (Check even if auditing)
            if skip_large_package:
                try:
                    # 115 share info often has 'count' or 'file_count'
                    share_count = int(share_info.get("count") or share_info.get("file_count") or 0)
                    if share_count > self.save_file_limit:
                        logger.info(f"⏭️ 预检发现大包 (项目数: {share_count})，且开启了跳过选项，直接跳过处理: {share_url}")
                        return {
                            "status": "skipped",
                            "message": f"检测为大包 (项目数: {share_count})，跳过处理"
                        }
                except (ValueError, TypeError):
                    pass

            # 即使包含违规内容标志，也尝试继续处理，因为很多时候文件列表依然可用
            if have_vio_file == 1:
                logger.warning(f"⚠️ 分享链接包含违规内容标志 (have_vio_file=1): {share_url}")
                # 不再直接返回错误，允许逻辑继续执行以检查 items 列表


            is_snapshotting = "正在生成文件快照" in str(snap_resp)
            if share_state == 0 or is_snapshotting:
                reason = "snapshotting" if is_snapshotting else "auditing"
                logger.info(f"🔍 分享链接处于{ '审核中' if reason == 'auditing' else '快照生成中' }，进入轮询等待队列: {share_url}")
                
                # 如果已经有 db_id，说明是轮询重试场景，不再新建任务
                if db_id:
                    return {
                        "status": "pending",
                        "reason": reason,
                        "share_url": share_url,
                        "metadata": metadata or {},
                        "db_id": db_id
                    }

                # Save to DB for persistence
                async with async_session() as session:
                    new_task = PendingLink(
                        share_url=share_url,
                        metadata_json=metadata or {},
                        status=reason
                    )
                    session.add(new_task)
                    await session.commit()
                    db_id = new_task.id
                
                return {
                    "status": "pending",
                    "reason": reason,
                    "share_url": share_url,
                    "metadata": metadata or {},
                    "db_id": db_id
                }
            
            if share_state == 7:
                logger.warning(f"⚠️ 分享链接已过期: {share_url}")
                return {
                    "status": "error",
                    "error_type": "expired",
                    "message": "链接已过期"
                }
            
            if share_state != 1:
                logger.warning(f"⚠️ 分享链接状态异常 (state={share_state}): {share_url}")
                # Allow attempt if state is unknown but not explicitly pending/expired/prohibited
            
            items = data.get("list", [])
            if not items:
                logger.warning(f"⚠️ 分享链接内没有文件。have_vio_file={have_vio_file}, 状态: {snap_resp.get('state')}")
                if have_vio_file == 1:
                    return {
                        "status": "error",
                        "error_type": "violated",
                        "message": "链接包含违规内容，无法转存分享"
                    }
                return {
                    "status": "error",
                    "error_type": "empty_share",
                    "message": "分享链接内没有可供转存的文件"
                }
            
            # Extract file/folder IDs and names
            # Files use 'fid', folders use 'cid'
            fids = []
            names = []
            for item in items:
                # Try to get fid (file) or cid (folder)
                fid = item.get("fid") or item.get("cid")
                if fid:
                    fids.append(str(fid))
                    # 115 share_snap returns names with unnecessary escapes sometimes (e.g. \' for ')
                    raw_name = item.get("n") or item.get("fn") or item.get("name") or item.get("file_name") or item.get("title")
                    if not raw_name:
                        logger.warning(f"⚠️ 无法从分享项提取文件名，可用的键有: {list(item.keys())}")
                        raw_name = "未知"
                    cleaned_name = raw_name.replace("\\'", "'").replace('\\"', '"')
                    names.append(cleaned_name)
                else:
                    logger.warning(f"Item missing both fid and cid: {item}")
            
            if not fids:
                logger.error(f"❌ 未能从列表项提取到任何有效的文件或文件夹 ID。项目数: {len(items)}")
                return {
                    "status": "error",
                    "error_type": "parse_error",
                    "message": "解析分享文件列表失败，无法提取文件 ID"
                }
            
            logger.info(f"📦 检测到 {len(fids)} 个项目: {', '.join(names[:3])}{'...' if len(names) > 3 else ''}")
            
            # 3. Ensure save directory (with network recovery retry)
            #    If _ensure_save_dir fails (e.g. network issue), pause and retry
            #    for up to 30 minutes instead of discarding the task.
            to_cid = None
            max_network_wait = 1800  # 30 minutes
            network_start = time.time()
            network_attempt = 0
            
            while True:
                try:
                    to_cid = await self._ensure_save_dir(target_dir)
                    if network_attempt > 0:
                        logger.info(f"🎉 网络已恢复，继续处理任务 (等待了 {time.time() - network_start:.0f}s)")
                    break
                except Exception as dir_err:
                    network_attempt += 1
                    elapsed = time.time() - network_start
                    remaining = max_network_wait - elapsed
                    
                    if remaining <= 0:
                        logger.error(f"❌ 网盘网络恢复等待超时 (30分钟)，中止任务: {dir_err}")
                        return {
                            "status": "error",
                            "error_type": "dir_failed",
                            "message": f"网盘网络持续不可用 (已等待30分钟): {dir_err}"
                        }
                    
                    wait_time = min(30, remaining)
                    logger.warning(
                        f"⏸️ 网盘网络异常，任务暂停等待恢复 "
                        f"(第{network_attempt}次重试, 已等待 {elapsed:.0f}s, 剩余 {remaining:.0f}s): {dir_err}"
                    )
                    await asyncio.sleep(wait_time)
            
            # 4. Receive files
            # 💡 增加预检：在大文件保存前尝试清理
            # 提取分享的总大小用于精准容量判断
            try:
                total_size = int(share_info.get("file_size") or 0)
            except (ValueError, TypeError):
                total_size = 0
            await self.check_and_prepare_capacity(file_count=len(fids), total_size=total_size)
            # 重新获取最新的 CID，以防清理逻辑删除了目录并重建了它
            to_cid = await self._ensure_save_dir(target_dir)

            # 为本次任务创建独立子目录，完全隔离不同任务的文件，避免分享时误纳入其他文件
            task_folder_name = f"{int(time.time())}"
            logger.info(f"📁 为本次任务创建独立子目录: {task_folder_name} (父目录 CID: {to_cid})")
            sub_dir_resp = await self._api_call_with_timeout(
                self.client.fs_makedirs_app, task_folder_name, pid=to_cid, async_=True,
                label="fs_makedirs_task_subdir",
                **self._get_ios_ua_kwargs()
            )
            check_response(sub_dir_resp)
            task_cid = int(
                sub_dir_resp.get("cid")
                or sub_dir_resp.get("id")
                or (sub_dir_resp.get("data") or {}).get("cid")
                or 0
            )
            if not task_cid:
                raise RuntimeError(f"创建任务子目录失败，未获取到有效 CID: {sub_dir_resp}")
            logger.info(f"✅ 任务子目录已创建: {task_folder_name} (CID: {task_cid})")

            receive_payload = {
                "share_code": payload["share_code"],
                "receive_code": payload["receive_code"] or "",
                "file_id": ",".join(fids),
                "cid": task_cid
            }

            try:
                recv_resp = await self._api_call_with_timeout(
                    self.client.share_receive_app, receive_payload, async_=True,
                    timeout=API_TIMEOUT, label="share_receive",
                    **self._get_ios_ua_kwargs()
                )
                check_response(recv_resp)
                logger.info(f"✅ 链接转存指令已发送: {share_url} -> CID {task_cid}")
                self._record_save_activity()
                recursive_links = []
            except Exception as recv_error:
                # Check for 500-file limit error (errno 4200044)
                error_info = getattr(recv_error, "args", [None, {}])[1] if hasattr(recv_error, "args") and len(recv_error.args) >= 2 else {}
                errno_val = error_info.get("errno") if isinstance(error_info, dict) else None
                
                if errno_val == 4200044 or "超过当前等级限制" in str(recv_error):
                    if skip_large_package:
                        logger.info(f"⏭️ 检测为大包且开启了跳过选项，跳过处理: {share_url}")
                        return {
                            "status": "skipped",
                            "message": "检测为大包，跳过处理"
                        }
                    logger.warning(f"⚠️ 触发 115 非会员 500 文件保存限制，尝试递归分批保存: {share_url}")
                    recursive_links = await self._save_share_recursive(share_url, task_cid)
                    logger.info(f"✅ 递归分批保存指令已处理完毕: {share_url}")
                    self._record_save_activity()
                # Check if it's a "file already received" error (errno 4200045)
                elif errno_val == 4200045 or "4200045" in str(recv_error) or "已经接收" in str(recv_error) or "已接收" in str(recv_error):
                    return await self._handle_already_received(task_cid, names, share_url, metadata, have_vio_file, receive_payload)
                else:
                    # Other errors, re-raise
                    raise
            
            await self.clear_restriction()
            return {
                "status": "success",
                "to_cid": task_cid if 'task_cid' in locals() and task_cid else to_cid,
                "names": names,
                "share_url": share_url,
                "recursive_links": recursive_links if 'recursive_links' in locals() else [],
                "metadata": metadata or {},
                "have_vio": have_vio_file == 1,
                "original_total_size": total_size,
                "original_file_count": int(share_info.get("count", 0) or share_info.get("file_count", 0)),
                "original_folder_count": int(share_info.get("folder_count", 0))
            }
        except Exception as e:
            # 彻底避免 loguru 格式化异常时可能触发的 KeyError
            try:
                errno_val = getattr(e, "errno", None)
                if hasattr(e, 'args') and len(e.args) >= 2 and isinstance(e.args[1], dict):
                    error_msg = str(e.args[1].get('error', e))
                    if not errno_val:
                        errno_val = e.args[1].get('errno')
                else:
                    error_msg = str(e)
            except:
                error_msg = "未知异常"
                errno_val = None
            
            if "正在生成文件快照" in error_msg:
                logger.info(f"🔍 分享链接正在生成快照，进入轮询等待队列: {share_url}")
                
                if db_id:
                    return {
                        "status": "pending",
                        "reason": "snapshotting",
                        "share_url": share_url,
                        "metadata": metadata or {},
                        "db_id": db_id
                    }

                async with async_session() as session:
                    new_task = PendingLink(
                        share_url=share_url,
                        metadata_json=metadata or {},
                        status="snapshotting"
                    )
                    session.add(new_task)
                    await session.commit()
                    db_id = new_task.id
                
                return {
                    "status": "pending",
                    "reason": "snapshotting",
                    "share_url": share_url,
                    "metadata": metadata or {},
                    "db_id": db_id
                }
            
            # 检查是否由于账号限制导致失败
            if "限制接收" in error_msg:
                logger.warning(f"🚫 触发 115 接收限制: {share_url}")
                await self.set_restriction(hours=1.0) # 设置 1 小时全局限制
                
                if db_id:
                    return {
                        "status": "pending",
                        "reason": "restricted",
                        "share_url": share_url,
                        "metadata": metadata or {},
                        "db_id": db_id
                    }

                async with async_session() as session:
                    new_task = PendingLink(
                        share_url=share_url,
                        metadata_json=metadata or {},
                        status="restricted"
                    )
                    session.add(new_task)
                    await session.commit()
                    db_id = new_task.id
                
                return {
                    "status": "pending",
                    "reason": "restricted",
                    "share_url": share_url,
                    "metadata": metadata or {},
                    "db_id": db_id
                }

            # 检查是否为"已经接收"异常 (errno 4200045)
            # 在某些情况下外层抛出的异常是纯文本，不包含在 errno 里
            if errno_val == 4200045 or "4200045" in error_msg or "已经接收" in error_msg or "已接收" in error_msg:
                # 重新构建 payload，这里可能外层没有 receive_payload，按现有信息构建
                _cid = task_cid if 'task_cid' in locals() and task_cid else to_cid
                retry_payload = {
                    "share_code": payload["share_code"],
                    "receive_code": payload["receive_code"] or "",
                    "file_id": ",".join(fids) if 'fids' in locals() else "",
                    "cid": _cid
                }
                return await self._handle_already_received(_cid, names, share_url, metadata, have_vio_file, retry_payload)

            logger.error("❌ 保存分享链接发生程序异常: {}", error_msg)
            return {
                "status": "error",
                "error_type": "exception",
                "message": f"程序异常: {error_msg}"
            }

    async def _save_share_recursive(self, share_url: str, target_pid: int) -> list[str]:
        """递归分批保存分享内容 (规避 500 文件限制，集成中转清理逻辑)"""
        payload = share_extract_payload(share_url)
        share_code = payload["share_code"]
        receive_code = payload["receive_code"] or ""
        
        # 状态追踪
        cid_map = {0: target_pid}
        share_links = []
        files_saved_total = 0

        # 方案B：在递归开始前快照保存目录的顶层 ID，用于中转分享时精确 diff
        initial_top_ids: set = await self._snapshot_dir_ids(target_pid)

        # 路径重建追踪：share_cid -> (parent_share_cid, name)
        share_structure = {0: (None, "")}
        
        async def reconstruct_path(current_share_cid, current_cid_map):
            """在清理后重建当前所在的文件夹路径"""
            # 1. 确保保存目录存在
            new_root_cid = await self._ensure_save_dir()
            current_cid_map.clear()
            current_cid_map[0] = new_root_cid
            
            # 2. 获取从根到当前的路径名列表
            path_names = []
            temp_cid = current_share_cid
            while temp_cid != 0:
                parent, name = share_structure[temp_cid]
                path_names.append(name)
                temp_cid = parent
            path_names.reverse()
            
            # 3. 逐层创建
            current_share = 0
            current_real = new_root_cid
            for name in path_names:
                # 寻找对应的子 share_cid
                child_share = next(s_cid for s_cid, info in share_structure.items() if info[0] == current_share and info[1] == name)
                resp = await self._api_call_with_timeout(
                    self.client.fs_makedirs_app, name, pid=current_real, async_=True,
                    **self._get_ios_ua_kwargs()
                )
                check_response(resp)
                current_real = int(resp.get("cid") or resp.get("id") or (resp.get("data") or {}).get("cid") or 0)
                current_cid_map[child_share] = current_real
                current_share = child_share
            
            return current_real

        async for pid, dirs, files in share_iterdir_walk(
            self.client, share_code, receive_code, async_=True
        ):
            if pid not in cid_map:
                # 如果因为中转清理丢失了映射，重建它
                logger.info(f"🔄 正在递归深度中重建目录结构 (Share CID: {pid})...")
                cid_map[pid] = await reconstruct_path(pid, cid_map)
                
            current_target_pid = cid_map[pid]
            
            # 1. 记录结构并创建子目录
            for d in dirs:
                share_cid = d["id"]
                name = d["name"]
                share_structure[share_cid] = (pid, name)
                try:
                    resp = await self._api_call_with_timeout(
                        self.client.fs_makedirs_app, name, pid=current_target_pid, async_=True,
                        label=f"fs_makedirs({name})",
                        **self._get_ios_ua_kwargs()
                    )
                    check_response(resp)
                    new_cid = int(resp.get("cid") or resp.get("id") or (resp.get("data") or {}).get("cid") or 0)
                    if new_cid:
                        cid_map[share_cid] = new_cid
                except Exception as e:
                    if "已经存在" in str(e) or "40004" in str(e):
                        found = await self._find_files_in_dir(current_target_pid, [name])
                        if found:
                            cid_map[share_cid] = int(found[0]["fid"])
                    else:
                        logger.error(f"❌ 递归保存过程中创建子目录 {name} 失败: {e}")
            
            # 2. 分批转存该目录下的文件
            fids = [str(f["id"]) for f in files]
            if not fids:
                continue
                
            for i in range(0, len(fids), 500):
                # 🚦 检查是否需要中转清理
                # 条件：已处理超过 10,000 文件，或者容量接近上限 (90%)
                need_cleanup = files_saved_total >= self.share_file_limit
                if not need_cleanup and settings.P115_CLEANUP_CAPACITY_ENABLED:
                    stats = await self.get_storage_stats()
                    used = stats.get("used", 0)
                    total = stats.get("total", 0)
                    if total > 0 and (used / total) > 0.9:
                        need_cleanup = True
                        logger.warning(f"⚠️ 容量逼近上限 ({used/total:.1%})，触发中转清理")

                if need_cleanup:
                    logger.info("📦 触发中转流程：正在生成当前已保存内容的分享链接...")
                    try:
                        # 方案B：snapshot diff，精确找出本轮新增的顶层项目，直接分享，不经过 create_share_link
                        # 使用 target_pid（即 task_cid）而非根目录，避免误纳入其他任务子目录
                        after_top_ids = await self._snapshot_dir_ids(target_pid)
                        new_top_ids = list(after_top_ids - initial_top_ids)
                        if new_top_ids:
                            logger.info(f"📦 中转分享: 找到 {len(new_top_ids)} 个新增顶级项，直接创建分享...")
                            intermediate_link = await self._share_fids_direct(new_top_ids)
                            if intermediate_link:
                                logger.info(f"📤 中转链接已生成: {intermediate_link}")
                                share_links.append(intermediate_link)
                        else:
                            logger.warning("⚠️ 中转分享: 未找到新增顶级项，跳过中转分享")
                    except Exception as share_e:
                        logger.error(f"❌ 中转分享生成失败: {share_e}")

                    # 执行清理
                    await self._do_cleanup_logic()
                    logger.info("🧹 中转清理完成，等待 5 秒恢复...")
                    await asyncio.sleep(5)
                    
                    # 重置计数器并重建当前路径映射
                    files_saved_total = 0
                    initial_top_ids = set()  # 清理后目录已清空，重置快照基线
                    current_target_pid = await reconstruct_path(pid, cid_map)
                
                batch = fids[i:i+500]
                try:
                    receive_payload = {
                        "share_code": share_code,
                        "receive_code": receive_code,
                        "file_id": ",".join(batch),
                        "cid": current_target_pid
                    }
                    recv_resp = await self._api_call_with_timeout(
                        self.client.share_receive_app, receive_payload, async_=True,
                        timeout=API_TIMEOUT, label=f"share_receive_batch({i//500})",
                        **self._get_ios_ua_kwargs()
                    )
                    check_response(recv_resp)
                    files_saved_total += len(batch)
                    logger.info(f"✅ 递归分批转存成功: {len(batch)} 个文件 -> CID {current_target_pid} (本轮累计: {files_saved_total})")
                    self._record_save_activity()
                    
                    await asyncio.sleep(random.randint(2, 3))
                except Exception as e:
                    # 尝试提取 errno
                    errno_val = getattr(e, "errno", None)
                    if hasattr(e, 'args') and len(e.args) >= 2 and isinstance(e.args[1], dict):
                        if not errno_val:
                            errno_val = e.args[1].get("errno")
                            
                    if errno_val == 4200045 or "4200045" in str(e) or "已经接收" in str(e) or "已接收" in str(e):
                        continue
                    logger.error(f"❌ 递归转存文件包失败: {e}")
        
        return share_links

    async def get_share_status(self, share_url: str):
        """Check the current status of a share link
        
        Returns:
            dict: {
                "share_state": int,
                "is_auditing": bool,
                "is_expired": bool,
                "is_prohibited": bool,
                "title": str
            }
        """
        try:
            payload = share_extract_payload(share_url)
            snap_resp = await self._api_call_with_timeout(
                self.client.share_snap_app, payload, async_=True,
                timeout=API_TIMEOUT, label="share_snap(status)",
                **self._get_ios_ua_kwargs()
            )
            check_response(snap_resp)
            
            data = snap_resp.get("data", {})
            share_info = data.get("shareinfo" if "shareinfo" in data else "share_info", {})
            share_state = data.get("share_state", share_info.get("share_state", share_info.get("status")))
            if share_state is not None:
                try:
                    share_state = int(share_state)
                except (ValueError, TypeError):
                    pass
            share_title = share_info.get("share_title", "")
            have_vio_file = share_info.get("have_vio_file", 0)
            
            is_snapshotting = "正在生成文件快照" in str(snap_resp)
            res = {
                "share_state": share_state,
                "is_auditing": share_state == 0,
                "is_snapshotting": is_snapshotting,
                "is_pending": share_state == 0 or is_snapshotting,
                "is_expired": share_state == 7,
                "is_prohibited": have_vio_file == 1,
                "title": share_title
            }
            if is_snapshotting:
                logger.info(f"📊 检查链接发现正在生成快照: {share_url}")
            logger.debug(f"📊 检查链接状态: {share_url} -> {res}")
            return res
        except Exception as e:
            error_msg = str(e)
            # 检查是否为链接失效或取消错误 (errno 4100009 或 4100010)
            if any(code in error_msg for code in ["4100009", "4100010"]) or \
               any(msg in error_msg for msg in ["链接已失效", "分享已取消"]):
                logger.warning(f"⏰ 检查链接状态发现链接已失效或被取消: {share_url}")
                return {
                    "share_state": 7,
                    "is_auditing": False,
                    "is_expired": True,
                    "is_prohibited": False,
                    "title": ""
                }
            if "正在生成文件快照" in error_msg:
                logger.info(f"📊 检查链接状态发现正在生成快照: {share_url}")
                return {
                    "share_state": 0,
                    "is_auditing": False,
                    "is_snapshotting": True,
                    "is_pending": True,
                    "is_expired": False,
                    "is_prohibited": False,
                    "title": ""
                }
            logger.error(f"❌ 检查链接状态失败: {share_url}, 错误: {e}")
            return None

    async def _share_fids_direct(self, fids: list) -> str | None:
        """将指定 file_id 列表直接创建为分享链接（含批次拆分、重试、长期转换）。
        绕开 create_share_link 的 diff 机制，精确分享已知 ID 列表。"""
        if not fids:
            return None
        result_links = []
        fids_str_list = [str(fid) for fid in fids]
        max_share_retries = 3

        for batch_idx, i in enumerate(range(0, len(fids_str_list), 10000), 1):
            batch_fids = fids_str_list[i:i+10000]
            batch_share_code = None
            batch_receive_code = None

            for retry_attempt in range(1, max_share_retries + 1):
                try:
                    logger.info(f"📤 直接创建分享 (分卷 {batch_idx}, 尝试 {retry_attempt}/{max_share_retries})...")
                    send_resp = await self._api_call_with_timeout(
                        self.client.share_send_app, ",".join(batch_fids), async_=True,
                        timeout=API_TIMEOUT, max_retries=1, label=f"share_send_direct_{batch_idx}",
                        **self._get_ios_ua_kwargs()
                    )
                    check_response(send_resp)
                    data = send_resp["data"]
                    batch_share_code = data.get("share_code")
                    batch_receive_code = data.get("receive_code") or data.get("recv_code")
                    logger.info(f"✅ 直接分享分卷 {batch_idx} 创建成功: {batch_share_code}")
                    break
                except Exception as share_error:
                    error_str = str(share_error)
                    if ("4100005" in error_str or "已被移动或删除" in error_str) and retry_attempt < max_share_retries:
                        logger.warning(f"⚠️ 文件尚未就绪，等待 5 秒后重试 (分卷 {batch_idx})...")
                        await asyncio.sleep(5)
                    else:
                        logger.error(f"❌ 直接分享分卷 {batch_idx} 失败: {share_error}")
                        if batch_idx == 1:
                            raise
                        break

            if batch_share_code:
                try:
                    await self._api_call_with_timeout(
                        self.client.share_update_app,
                        {"share_code": batch_share_code, "share_duration": -1},
                        async_=True, timeout=API_TIMEOUT, max_retries=2,
                        label=f"share_update_direct_{batch_idx}",
                        **self._get_ios_ua_kwargs()
                    )
                except Exception as e:
                    logger.warning(f"⚠️ 转换长期分享失败 (分卷 {batch_idx}): {e}")
                full_link = f"https://115.com/s/{batch_share_code}"
                if batch_receive_code:
                    full_link += f"?password={batch_receive_code}"
                result_links.append(full_link)

        if not result_links:
            return None
        if len(result_links) > 1:
            return "\n".join(f"链接 {idx}: {link}" for idx, link in enumerate(result_links, 1))
        return result_links[0]

    async def _snapshot_dir_ids(self, cid: int) -> set:
        """快照目录中所有顶层文件/文件夹的 ID，用于保存前后 diff 找新增项（自动翻页，避免 >1000 条时漏记）"""
        ids = set()
        offset = 0
        limit = 1000
        try:
            while True:
                resp = await self._api_call_with_timeout(
                    self.client.fs_files_app2,
                    {"cid": cid, "limit": limit, "offset": offset, "show_dir": 1},
                    async_=True,
                    timeout=30, max_retries=2, label="fs_files_snapshot",
                    **self._get_ios_ua_kwargs()
                )
                check_response(resp)
                file_list = resp.get("data", [])
                if isinstance(file_list, dict):
                    file_list = file_list.get("list", [])
                for item in file_list:
                    item_id = item.get("fid") or item.get("cid") or item.get("file_id") or item.get("category_id") or item.get("id")
                    if item_id:
                        ids.add(str(item_id))
                if len(file_list) < limit:
                    break  # 已是最后一页
                offset += limit
                logger.debug(f"📄 快照目录 {cid} 翻页: offset={offset}, 已收集 {len(ids)} 个 ID")
        except Exception as e:
            logger.warning(f"⚠️ 快照目录 {cid} 失败: {e}")
        return ids

    async def _find_files_in_dir(self, cid: int, target_names: list, save_start_time: int = 0) -> list:
        """在指定目录中查找文件，使用多种方式确保找到

        优先使用 fs_search（按文件名搜索），失败后回退到 fs_files（列目录）。
        当存在同名文件时，优先选取创建时间 >= save_start_time 的文件（即本次保存的）。

        Args:
            cid: 目录 ID
            target_names: 要查找的文件名列表
            save_start_time: 本次保存操作开始的时间戳（Unix 秒），用于区分同名文件

        Returns:
            匹配的文件列表 [{fid, name, size, time}, ...]
        """
        matched = []

        # 方式 1: 使用 fs_search 按文件名搜索（更可靠，不依赖目录缓存）
        for name in target_names:
            try:
                search_resp = await self._api_call_with_timeout(
                    self.client.fs_search_app2,
                    {"search_value": name, "cid": cid, "limit": 20},
                    async_=True,
                    timeout=30, max_retries=2, label=f"fs_search({name})",
                    **self._get_ios_ua_kwargs()
                )
                check_response(search_resp)
                search_data = search_resp.get("data", [])

                # fs_search 的结果可能在 data 数组或 data.list 中
                if isinstance(search_data, dict):
                    search_items = search_data.get("list", [])
                else:
                    search_items = search_data

                logger.debug(f"🔍 fs_search '{name}' 在 CID:{cid} 返回 {len(search_items)} 条结果")

                # 收集所有同名候选项，再按时间戳优先选最新的
                candidates = []
                for item in search_items:
                    item_name = item.get("n") or item.get("fn") or item.get("name") or item.get("file_name") or item.get("title") or item.get("category_name")
                    if item_name == name:
                        item_id = item.get("fid") or item.get("cid") or item.get("file_id") or item.get("category_id")
                        if item_id:
                            candidates.append({
                                "fid": str(item_id),
                                "name": item_name,
                                "size": item.get("s", item.get("file_size", 0)),
                                "time": item.get("te", 0),
                            })

                if candidates:
                    # 优先选 save_start_time 之后创建的文件；若无则取最新的
                    new_candidates = [c for c in candidates if c["time"] >= save_start_time] if save_start_time else []
                    best = max(new_candidates, key=lambda x: x["time"]) if new_candidates else max(candidates, key=lambda x: x["time"])
                    matched.append(best)
                    logger.info(f"📄 fs_search 找到: {best['name']} (ID: {best['fid']}, time: {best['time']})")
            except Exception as e:
                logger.warning(f"⚠️ fs_search 搜索 '{name}' 失败: {e}")
        
        if len(matched) == len(target_names):
            return matched
        
        # 方式 2: 回退到 fs_files 列目录
        found_names = {m["name"] for m in matched}
        remaining_names = [n for n in target_names if n not in found_names]
        logger.info(f"🔍 fs_search 找到 {len(matched)}/{len(target_names)} 个文件，尝试 fs_files 查找剩余: {remaining_names}")
        
        try:
            resp = await self._api_call_with_timeout(
                self.client.fs_files_app2,
                {"cid": cid, "limit": 500, "show_dir": 1},
                async_=True,
                timeout=30, max_retries=2, label="fs_files",
                **self._get_ios_ua_kwargs()
            )
            check_response(resp)
            file_list = resp.get("data", [])
            
            # 检查 data 的类型，兼容不同响应格式
            if isinstance(file_list, dict):
                file_list = file_list.get("list", [])
            
            # 获取响应中的实际 CID，验证是否正确列出了目标目录
            resp_path = resp.get("path", [])
            resp_cid = None
            if resp_path:
                last_path = resp_path[-1] if isinstance(resp_path, list) else resp_path
                resp_cid = last_path.get("cid") if isinstance(last_path, dict) else None
            
            actual_count = resp.get("count", "?")
            logger.debug(f"📂 fs_files CID:{cid} 返回 {len(file_list)} 项 (总数: {actual_count}, 路径CID: {resp_cid})")
            
            # 验证返回的是否是正确的目录（防止 CID 不存在时回退到根目录）
            if resp_cid is not None and str(resp_cid) != str(cid):
                logger.warning(f"⚠️ fs_files 返回的目录 CID({resp_cid}) 与请求的 CID({cid}) 不匹配！可能目录不存在")
            
            # 日志打印目录中的前10个文件名，便于排查
            if file_list:
                dir_file_names = [(item.get("n") or item.get("fn") or item.get("name") or item.get("file_name") or item.get("title") or item.get("category_name") or f"? (keys: {list(item.keys())})") for item in file_list[:10]]
                logger.debug(f"📋 目录内文件(前10): {dir_file_names}")
            
            for item in file_list:
                item_name = item.get("n") or item.get("fn") or item.get("name") or item.get("file_name") or item.get("title") or item.get("category_name")
                if item_name in remaining_names:
                    item_id = item.get("fid") or item.get("cid") or item.get("file_id") or item.get("category_id") or item.get("id")
                    if item_id:
                        item_time = item.get("te", 0)
                        # 若已有同名结果，优先保留时间更新的（本次保存的）
                        existing = next((m for m in matched if m["name"] == item_name), None)
                        if existing:
                            if item_time > existing["time"]:
                                matched.remove(existing)
                                matched.append({"fid": str(item_id), "name": item_name, "size": item.get("s", 0), "time": item_time})
                                logger.info(f"📄 fs_files 更新为更新的同名文件: {item_name} (ID: {item_id})")
                        else:
                            matched.append({"fid": str(item_id), "name": item_name, "size": item.get("s", 0), "time": item_time})
                            logger.info(f"📄 fs_files 找到: {item_name} (ID: {item_id})")
                        
        except Exception as e:
            logger.warning(f"⚠️ fs_files 列目录失败: {e}")
        
        return matched

    async def create_share_link(self, save_result: dict):
        if not self.client or not save_result:
            return None

        to_cid = save_result.get("to_cid")
        names = save_result.get("names", [])
        original_total_size = save_result.get("original_total_size", 0)
        original_file_count = save_result.get("original_file_count", 0)
        original_folder_count = save_result.get("original_folder_count", 0)

        # 有时候外层分享根节点如果算数的话，可能有轻微误差（原分享有1个文件夹A包含了3个子文件夹，新转存的到C中只有这一棵树）
        # 所以我们重点打印它，并允许极小误差
        logger.info(f"📊 [基准比对数据] 转存源分享规模: 大小 = {original_total_size} 字节, 文件数 = {original_file_count}, 文件夹数 = {original_folder_count}")

        try:
            logger.info(f"⏳ 等待文件深度属性写入子孙目录 (CID: {to_cid})...")

            new_fids = []
            prev_total_count = -1
            prev_size_str = ""
            stable_times = 0
            max_poll_attempts = 45 # 最多等待约 90s
            min_stable_required = 3 # 稳定不变的次数要求

            for poll_attempt in range(1, max_poll_attempts + 1):
                try:
                    logger.debug(f"🔍 探测子目录属性详情 (第 {poll_attempt}/{max_poll_attempts} 次), CID: {to_cid}")
                    
                    # 1. 尝试用你想要的方案：调用类 `fs_category_get` 获取实时深度体积
                    resp = await self._api_call_with_timeout(
                        self.client.fs_category_get_app, to_cid,
                        async_=True, timeout=10, max_retries=1, label="fs_category_get_poll",
                        **self._get_ios_ua_kwargs()
                    )
                    logger.info(f"📋 [深度属性 RAW API 完整响应] {resp}")
                    
                    # 2. 从原生响应中直接读取（而不是从 data 中读）
                    current_file_count = int(resp.get("count", 0) or resp.get("file_count", 0))
                    current_folder_count = int(resp.get("folder_count", 0))
                    current_total = current_file_count + current_folder_count
                    current_size_str = str(resp.get("size", "0"))

                    logger.info(f"⚖️ [比对进程] 当前挂载层级统计: 大小={current_size_str} (基准: {original_total_size} 字节), 文件数={current_file_count}, 文件夹数={current_folder_count}")

                    # 3. 结合原先的顶部结构验证，确保外层骨架确实建好了
                    current_ids = await self._snapshot_dir_ids(to_cid)

                    # 判断规则：只要检测到当前有合并节点（current_total > 0）
                    if current_total > 0:
                        if current_total == prev_total_count and current_size_str == prev_size_str:
                            stable_times += 1
                            logger.info(f"🔄 目标目录总项数 ({current_total}) 和大小 ({current_size_str}) 保持不变... (连续稳固 {stable_times}/{min_stable_required} 次)")
                            if stable_times >= min_stable_required:
                                logger.info(f"✅ 录像稳定！后台大目录深层挂载确认已彻底完成！最终合集项数: {current_total}")
                                final_ids = await self._snapshot_dir_ids(to_cid)
                                new_fids = list(final_ids)
                                break
                        else:
                            # 数量或层级大小有变化，表示还在激烈复制挂载
                            logger.info(f"📈 目录树深度挂载中... （项数变化 {prev_total_count} -> {current_total}, 尺寸 {prev_size_str} -> {current_size_str}）")
                            stable_times = 0
                    else:
                        # Fallback：深度属性接口失效（被115缓存卡在 0 了），这时候靠最外层的 fallback 检测机制！
                        if current_ids and len(current_ids) >= len(names):
                            if current_total == prev_total_count: # (即 0 == 0)
                                stable_times += 1
                                logger.info(f"⚠️ 深度属性(大小)因 115 缓存一直为 0。但顶层目录已就绪，当前进入强制平稳期等待... ({stable_times}/{min_stable_required})")
                                if stable_times >= min_stable_required:
                                    logger.info(f"✅ 录稳期结束。为防止深层未挂载，安全追加 10 秒死等...")
                                    await asyncio.sleep(10)
                                    new_fids = list(current_ids)
                                    break
                        else:
                            logger.warning(f"⚠️ 顶层子目录仍为空...")
                            stable_times = 0

                    prev_total_count = current_total
                    prev_size_str = current_size_str
                    
                    if poll_attempt < max_poll_attempts:
                        await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"⚠️ 检索目录内容或属性失败 (第 {poll_attempt} 次): {e}", exc_info=True)
                    if poll_attempt < max_poll_attempts:
                        await asyncio.sleep(5)

            if not new_fids:
                logger.warning(f"⚠️ 子目录 {to_cid} 中最终未检测到任何文件，可能 115 处理延迟或转存失败")
                return None

            
            # 7. Create new share with retry mechanism and split if > 10,000 files
            share_links = []
            fids_str_list = [str(fid) for fid in new_fids]
            max_share_retries = 3
            
            # Split fids into batches of 10,000 to respect 115 limits
            for batch_idx, i in enumerate(range(0, len(fids_str_list), 10000), 1):
                batch_fids = fids_str_list[i:i+10000]
                batch_share_code = None
                batch_receive_code = None
                
                for retry_attempt in range(1, max_share_retries + 1):
                    try:
                        logger.info(f"📤 正在创建分享链接 (分卷 {batch_idx}, 尝试 {retry_attempt}/{max_share_retries})...")
                        send_resp = await self._api_call_with_timeout(
                            self.client.share_send_app, ",".join(batch_fids), async_=True,
                            timeout=API_TIMEOUT, max_retries=1, label=f"share_send_batch_{batch_idx}",
                            **self._get_ios_ua_kwargs()
                        )
                        check_response(send_resp)
                        
                        data = send_resp["data"]
                        batch_share_code = data.get("share_code")
                        batch_receive_code = data.get("receive_code") or data.get("recv_code")
                        
                        logger.info(f"✅ 分享分卷 {batch_idx} 创建成功: {batch_share_code}")
                        break
                        
                    except Exception as share_error:
                        error_str = str(share_error)
                        if ("4100005" in error_str or "已被移动或删除" in error_str) and retry_attempt < max_share_retries:
                            logger.warning(f"⚠️ 文件尚未就绪，等待 5 秒后重试...")
                            await asyncio.sleep(5)
                        else:
                            logger.error(f"❌ 创建分享分卷 {batch_idx} 失败: {share_error}")
                            if batch_idx == 1: raise # If even the first batch fails, raise
                            break # Otherwise skip this batch
                
                if batch_share_code:
                    # Update share to permanent
                    try:
                        logger.info(f"🔄 正在将分享链接 {batch_share_code} 转换为长期有效...")
                        await self._api_call_with_timeout(
                            self.client.share_update_app, {"share_code": batch_share_code, "share_duration": -1},
                            async_=True, timeout=API_TIMEOUT, max_retries=2, label=f"share_update_{batch_idx}",
                            **self._get_ios_ua_kwargs()
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ 转换长期分享失败 (分卷 {batch_idx}): {e}")
                    
                    full_link = f"https://115.com/s/{batch_share_code}"
                    if batch_receive_code:
                        full_link += f"?password={batch_receive_code}"
                    share_links.append(full_link)
            
            if not share_links:
                logger.error("❌ 未能生成任何分享链接")
                return None
            
            # Format multi-link response if split occurred
            if len(share_links) > 1:
                formatted_links = []
                for idx, link in enumerate(share_links, 1):
                    formatted_links.append(f"链接 {idx}: {link}")
                result_share = "\n".join(formatted_links)
                logger.info(f"🔗 已生成 {len(share_links)} 个分卷分享链接")
            else:
                result_share = share_links[0]
                logger.info(f"🔗 长期分享链接已生成: {result_share}")
                
            return result_share
            
        except Exception as e:
            logger.error(f"❌ 创建新分享链接失败: {e}")
            # 检查是否是由于违规导致的空文件夹分享失败 (errno 4100016)
            error_info = getattr(e, "args", [None, {}])[1] if hasattr(e, "args") and len(e.args) >= 2 else {}
            errno_val = error_info.get("errno") if isinstance(error_info, dict) else None
            
            if errno_val == 4100016 and save_result.get("have_vio"):
                return {
                    "status": "error",
                    "error_type": "violated",
                    "message": "链接包含违规内容，无法转存分享"
                }
            
            # 检查分享限制
            error_msg = str(e)
            if "限制分享" in error_msg:
                logger.warning(f"🚫 触发 115 分享限制")
                self.set_restriction(hours=1.0)
                return {
                    "status": "pending",
                    "reason": "restricted",
                    "share_url": save_result.get("share_url"),
                    "metadata": save_result.get("metadata", {})
                }

            return None

    async def cleanup_save_directory(self, wait: bool = True):
        """Clean up the save directory by deleting the entire folder (with locking)."""
        try:
            async with self._acquire_task_lock("cleanup", wait=wait):
                return await self._cleanup_save_directory_internal()
        except BlockingIOError:
            return False

    async def _cleanup_save_directory_internal(self) -> bool:
        """Internal logic to clean up the save directory (no locking)."""
        try:
            logger.info(f"🧹 开始清理保存目录: {self.save_dir}")
            cid = await self._ensure_save_dir()
            if not cid:
                return False
            
            resp = await self._api_call_with_timeout(
                self.client.fs_delete, cid, async_=True,
                timeout=API_TIMEOUT, label="fs_delete",
                **self._get_ios_ua_kwargs()
            )
            check_response(resp)
            
            self.clear_save_dir_cache()
            logger.info("✅ 保存目录清理完成")
            return True
        except Exception as e:
            logger.error(f"❌ 内部清理保存目录失败: {e}")
            return False

    async def get_storage_stats(self) -> dict:
        """Get storage stats (used, total) of 115 Drive in bytes.
        Supports both entire cloud drive and specific save directory based on settings.
        """
        if not self.client:
            return {"used": 0, "total": 0}
            
        def extract_size(val) -> int:
            if isinstance(val, dict):
                val = val.get("size") or val.get("size_total") or val.get("size_use") or 0
            
            if val is None:
                return 0
            if isinstance(val, (int, float)):
                return int(val)
            
            # Handle string format (e.g., "2.04TB")
            s_val = str(val).strip().upper()
            if not s_val:
                return 0
            
            import re
            match = re.match(r"^([0-9.]+)\s*([A-Z]*B?)$", s_val)
            if not match:
                try:
                    return int(float(s_val))
                except:
                    return 0
            
            number, unit = match.groups()
            number = float(number)
            
            units = {
                "": 1, "B": 1,
                "K": 1024, "KB": 1024,
                "M": 1024**2, "MB": 1024**2,
                "G": 1024**3, "GB": 1024**3,
                "T": 1024**4, "TB": 1024**4,
                "P": 1024**5, "PB": 1024**5
            }
            
            return int(number * units.get(unit, 1))

        try:
            # 1. Directory Mode
            if hasattr(settings, "P115_CLEANUP_CAPACITY_TYPE") and settings.P115_CLEANUP_CAPACITY_TYPE == "DIRECTORY":
                cid = await self.get_save_dir_cid()
                if not cid:
                    logger.error("❌ 无法获取保存目录 CID，无法进行目录容量检测")
                    return {"used": 0, "total": 0}
                
                resp = await self._api_call_with_timeout(
                    self.client.fs_category_get, cid, async_=True,
                    timeout=API_TIMEOUT, label="fs_category_get",
                    **self._get_ios_ua_kwargs()
                )
                check_response(resp)
                used = extract_size(resp.get("size", 0))
                # For directory mode, total is essentially infinite or not applicable in this context
                return {"used": used, "total": 0}
            
            # 2. Entire Drive Mode (Default)
            resp = await self._api_call_with_timeout(
                self.client.user_space_info, async_=True,
                timeout=API_TIMEOUT, label="user_space_info",
                **self._get_ios_ua_kwargs()
            )
            check_response(resp)
            data = resp.get("data", {})
            used = extract_size(data.get("all_used") or data.get("all_use") or data.get("used") or 0)
            total = extract_size(data.get("all_total") or data.get("total") or 0)
            return {"used": used, "total": total}
            
        except Exception as e:
            logger.error("❌ 获取网盘存储状态失败: {}", str(e))
            return {"used": 0, "total": 0}

    async def get_save_dir_cid(self) -> Optional[int]:
        """Get the CID for the current save directory"""
        try:
            return await self._ensure_save_dir()
        except Exception as e:
            logger.error(f"❌ 获取保存目录 CID 失败: {e}")
            return None

    async def check_and_prepare_capacity(self, file_count: int = 0, total_size: int = 0):
        """Check capacity and optionally clean up before starting a task (internal/no-lock).
        
        Trigger cleanup if:
        1. file_count > 500 AND total_size > remainder (Avoid predictive cleanup if space is enough)
        2. Space is tighter than configured threshold (Target maintenance)
        """
        if not settings.P115_CLEANUP_CAPACITY_ENABLED:
            return

        stats = await self.get_storage_stats()
        used_bytes = stats["used"]
        total_bytes = stats["total"]
        
        # Only perform predictive cleanup if in ENTIRE mode (since DIRECTORY mode doesn't have a fixed remainder in the same sense)
        if hasattr(settings, "P115_CLEANUP_CAPACITY_TYPE") and settings.P115_CLEANUP_CAPACITY_TYPE == "ENTIRE":
            if total_bytes > 0:
                remaining_bytes = total_bytes - used_bytes
                if (file_count > 500 and total_size > remaining_bytes) or (total_size > 0 and total_size > remaining_bytes):
                    logger.info(f"🚀 容量检查：检测到待转存内容较多或空间不足，执行清理...")
                    await self._do_cleanup_logic()
                    await asyncio.sleep(3)

    async def check_capacity_and_cleanup(self, mode: str = "manual"):
        """Check current capacity and trigger cleanup if it exceeds limit.
        """
        # Determine if we should wait for the lock
        wait_for_lock = True
        if mode == "scheduled":
            wait_for_lock = False # Skip if busy
            try:
                # 提前探测锁，避免不必要的阻塞
                async with self._acquire_task_lock("cleanup", wait=False):
                    pass
            except BlockingIOError:
                logger.debug("⏭️ 定时容量检查：检测到任务执行中，按计划跳过")
                return False
        
        # 1. Check current capacity
        stats = await self.get_storage_stats()
        used_bytes = stats["used"]
        
        limit_val = settings.P115_CLEANUP_CAPACITY_LIMIT
        unit = settings.P115_CLEANUP_CAPACITY_UNIT
        limit_bytes = limit_val * (1024**4 if unit == "TB" else 1024**3)
        
        detection_type = getattr(settings, "P115_CLEANUP_CAPACITY_TYPE", "ENTIRE")
        detection_name = "全网盘" if detection_type == "ENTIRE" else f"保存目录({self.save_dir})"
        
        if used_bytes < limit_bytes:
            logger.info(f"✅ {detection_name}容量充足: {used_bytes / (1024**3 if unit=='GB' else 1024**4):.2f}/{limit_val:.2f} {unit}")
            return False

        logger.warning(f"⚠️ {detection_name}容量不足: {used_bytes / (1024**3 if unit=='GB' else 1024**4):.2f} {unit} > 限制 {limit_val:.2f} {unit}")
        
        # Execute cleanup with non-blocking support for scheduled tasks
        try:
            # We don't acquire the lock here directly, but pass wait down to atomic cleanup methods
            # which DO acquire the lock. 
            # Actually, check_capacity_and_cleanup held lock in original version.
            # Let's wrap the actual cleanup calls in the lock.
            async with self._acquire_task_lock("cleanup", wait=wait_for_lock):
                logger.info(f"🧹 执行容量管理清理 (模式: {mode})...")
                # Note: we call internal versions or handle logic here to avoid re-acquiring lock
                # But cleanup_save_directory has its own lock. So we need a way to bypass it or透传.
                # Best is to have an internal _cleanup method.
                await self._do_cleanup_logic()
                return True
        except BlockingIOError:
            if mode == "scheduled":
                # 理论上这里由于之前的 probe 不会轻易触发，但作为安全兜底保留
                logger.info("⏭️ 定时容量检查：转存锁获取冲突，按计划跳过任务")
            return False

    async def _do_cleanup_logic(self):
        """Helper to execute both cleanup tasks without lock acquisition."""
        await self._cleanup_save_directory_internal()
        await self._cleanup_recycle_bin_internal()

    async def get_history_link(self, original_url: str) -> Optional[Union[str, list[str]]]:
        """Check if a link has been processed before. Returns string or list of strings."""
        try:
            import json
            from app.models.schema import LinkHistory
            async with async_session() as session:
                result = await session.execute(
                    select(LinkHistory).where(LinkHistory.original_url == original_url)
                )
                record = result.scalar_one_or_none()
                if record:
                    link_val = record.share_link
                    if link_val.startswith("[") and link_val.endswith("]"):
                        try:
                            return json.loads(link_val)
                        except:
                            return link_val
                    return link_val
            return None
        except Exception as e:
            logger.error(f"查询历史记录失败: {e}")
            return None

    async def save_history_link(self, original_url: str, share_link: Union[str, list[str]]):
        """Save processed link(s) to history. share_link can be a list."""
        try:
            import json
            from app.models.schema import LinkHistory
            
            # Convert list to JSON string
            if isinstance(share_link, list):
                if not share_link:
                    return
                # If only one link, store as string, otherwise JSON
                link_to_store = json.dumps(share_link) if len(share_link) > 1 else share_link[0]
            else:
                link_to_store = share_link

            async with async_session() as session:
                existing = await session.execute(
                    select(LinkHistory).where(LinkHistory.original_url == original_url)
                )
                record = existing.scalar_one_or_none()
                if record:
                    record.share_link = link_to_store
                else:
                    new_record = LinkHistory(original_url=original_url, share_link=link_to_store)
                    session.add(new_record)
                await session.commit()
                logger.info(f"已保存历史记录: {original_url} -> {link_to_store[:50]}...")
        except Exception as e:
            logger.error(f"保存历史记录失败: {e}")

    async def delete_all_history_links(self):
        """Clear all history links"""
        try:
            from app.models.schema import LinkHistory
            from sqlalchemy import delete
            async with async_session() as session:
                await session.execute(delete(LinkHistory))
                await session.commit()
                logger.info("已清空所有历史记录")
                return True
        except Exception as e:
            logger.error(f"清空历史记录失败: {e}")
            return False

    async def cleanup_recycle_bin(self, wait: bool = True):
        """Empty the recycle bin (with locking)."""
        try:
            async with self._acquire_task_lock("cleanup", wait=wait):
                return await self._cleanup_recycle_bin_internal()
        except BlockingIOError:
            return False

    async def _cleanup_recycle_bin_internal(self) -> bool:
        """Internal logic to empty the recycle bin (no locking)."""
        try:
            logger.info("🗑️ 开始清空回收站...")
            payload = {}
            if self.recycle_password:
                payload["password"] = self.recycle_password
                logger.debug("使用回收站密码")
            
            resp = await self._api_call_with_timeout(
                self.client.recyclebin_clean_app, payload, async_=True,
                timeout=API_TIMEOUT, label="recyclebin_clean",
                **self._get_ios_ua_kwargs()
            )
            check_response(resp)
            logger.info("✅ 回收站已清空")
            return True
        except Exception as e:
            logger.error("❌ 内部清空回收站失败: {}", e)
            return False

    
p115_service = P115Service()

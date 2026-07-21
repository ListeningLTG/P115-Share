from p115client import P115Client, check_response
from p115client.fs import P115FileSystem
from p115client.util import share_extract_payload, complete_url
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
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from app.core.database import async_session
from app.models.schema import PendingLink, LinkHistory
from sqlalchemy import select, delete
from pathlib import Path
import tempfile

# 默认 API 请求超时（秒）
API_TIMEOUT = 60
# 默认 API 重试次数
API_MAX_RETRIES = 3
# 重试间隔（秒）
API_RETRY_DELAY = 5


def parse_size_to_bytes(val) -> int:
    """将 115 API 返回的 size（字节数或 '91.92GB' 等可读串）转为整数字节。"""
    if isinstance(val, dict):
        val = val.get("size") or val.get("size_total") or val.get("size_use") or 0
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    s_val = str(val).strip().upper()
    if not s_val:
        return 0
    match = re.match(r"^([0-9.]+)\s*([A-Z]*B?)$", s_val)
    if not match:
        try:
            return int(float(s_val))
        except (ValueError, TypeError):
            return 0
    number, unit = match.groups()
    number = float(number)
    units = {
        "": 1, "B": 1,
        "K": 1024, "KB": 1024,
        "M": 1024**2, "MB": 1024**2,
        "G": 1024**3, "GB": 1024**3,
        "T": 1024**4, "TB": 1024**4,
        "P": 1024**5, "PB": 1024**5,
    }
    return int(number * units.get(unit, 1))


def sizes_approximately_equal(a: int, b: int, *, rel_tol: float = 0.001, abs_tol: int = 1024 * 1024) -> bool:
    """可读 size（如 91.92GB）与精确字节比对时允许少量误差。"""
    a, b = int(a or 0), int(b or 0)
    if a == b:
        return True
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= max(abs_tol, int(max(a, b) * rel_tol))


# iOS 用户代理
# app="ios" 对应 ssoent=D1，即「115生活_苹果端」，UA 必须使用 115Life iOS 客户端 UA
# 注意：「115_苹果端（网盘）」的 app="115ios"，ssoent=D3，两者不能混用
IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 115Life/8.7.0"
)

TMDB_ID_PATTERN = re.compile(r"(?i)(?:tmdbid|tmdb)\s*(?:=|:|[-_])?\s*(\d+)")
SEASON_DIR_PATTERN = re.compile(r"(?i)^(?:season\s*0*\d{1,2}|s0*\d{1,2}|第\s*[0-9一二三四五六七八九十百千万]+\s*季)$")
EPISODE_STRONG_PATTERN = re.compile(r"(?i)\bS\d{1,2}E\d{1,3}\b")
EPISODE_MID_PATTERN = re.compile(r"(?i)\b(?:EP?|E)\s*0*\d{1,3}\b")
EPISODE_CN_PATTERN = re.compile(r"第\s*[0-9一二三四五六七八九十百千万]+\s*集")
WEAK_NUMBER_PATTERN = re.compile(r"(?:^|[\s._-])(\d{1,2})(?:[\s._-]|$)")


def extract_tmdb_id_from_name(name: str) -> Optional[int]:
    match = TMDB_ID_PATTERN.search(name or "")
    return int(match.group(1)) if match else None


def _strip_common_tags_for_media(name: str) -> str:
    cleaned = re.sub(r"(?i)(?:tmdbid|tmdb)\s*(?:=|:|[-_])?\s*\d+", "", name or "")
    cleaned = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", cleaned)
    return cleaned


def infer_media_hint_from_name(name: str) -> str:
    name_clean = _strip_common_tags_for_media(name)
    if EPISODE_STRONG_PATTERN.search(name_clean) or EPISODE_CN_PATTERN.search(name_clean):
        return "tv"
    if EPISODE_MID_PATTERN.search(name_clean):
        return "tv"
    return "unknown"


def _collect_weak_episode_numbers(name: str) -> Tuple[str, Optional[int]]:
    name_clean = _strip_common_tags_for_media(name)
    if EPISODE_STRONG_PATTERN.search(name_clean) or EPISODE_MID_PATTERN.search(name_clean):
        return "", None
    matches = WEAK_NUMBER_PATTERN.findall(name_clean)
    if not matches:
        return "", None

    nums = [int(x) for x in matches if x.isdigit() and 1 <= int(x) <= 39]
    if not nums:
        return "", None

    ep_num = nums[-1]
    base = re.sub(r"(?:^|[\s._-])\d{1,2}(?:[\s._-]|$)", " ", name_clean)
    base = re.sub(r"\s+", " ", base).strip().lower()
    return base, ep_num


def infer_media_hint_from_items(items: List[Dict[str, Any]]) -> str:
    tv_score = 0
    weak_groups: Dict[str, set] = {}

    for item in items:
        item_name = (item.get("name") or "").strip()
        if not item_name:
            continue
        if item.get("is_dir"):
            if SEASON_DIR_PATTERN.search(item_name):
                tv_score += 5
            continue

        hint = infer_media_hint_from_name(item_name)
        if hint == "tv":
            tv_score += 2

        base, ep_num = _collect_weak_episode_numbers(item_name)
        if base and ep_num is not None:
            weak_groups.setdefault(base, set()).add(ep_num)

    for nums in weak_groups.values():
        if len(nums) >= 3:
            tv_score += 3
            break

    return "tv" if tv_score >= 3 else "unknown"


def extract_replacement_title_fragment(name: str) -> str:
    """提取用于 TMDB 别名替换的标题片段，优先返回完整中文主标题块。"""
    name_no_ext = (name or "")
    if "." in name_no_ext:
        name_no_ext = name_no_ext.rsplit(".", 1)[0]

    # 先移除 tmdb 标记，避免干扰边界定位
    cleaned = re.sub(r"(?i)(?:\{|\[|\()\s*(?:tmdbid|tmdb)\s*(?:=|:|[-_])?\s*\d+\s*(?:\}|\]|\))", "", name_no_ext)
    cleaned = re.sub(r"(?i)\b(?:tmdbid|tmdb)\s*(?:=|:|[-_])?\s*\d+\b", "", cleaned)

    boundary_patterns = [
        r"\(\d{4}\)",
        r"[._\-\s]\d{4}(?:[._\-\s]|$)",
        r"\bS\d{1,2}E\d{1,3}\b",
        r"\b(?:EP?|E)\s*\d{1,3}\b",
        r"第\s*[0-9一二三四五六七八九十百千万]+\s*[集季]",
        r"\b(?:720|1080|2160|4320)p\b",
    ]

    cut_pos = len(cleaned)
    for p in boundary_patterns:
        m = re.search(p, cleaned, flags=re.IGNORECASE)
        if m:
            cut_pos = min(cut_pos, m.start())

    head = cleaned[:cut_pos].strip(" ._-—–:：")

    # 若前缀中存在中文，优先用完整前缀（保留中文冒号结构，如 A：B）
    if any('\u4e00' <= ch <= '\u9fff' for ch in head):
        return head

    # 回退到最长中文片段
    chinese_segments = re.findall(r"[\u4e00-\u9fff]+", cleaned)
    if chinese_segments:
        return max(chinese_segments, key=len)

    return ""


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
        self._last_verify_failed: bool = False # 记录最后一次验证是否明确失败
        self._batch_yield_count: int = 0     # 批量任务避让次数统计
        self._batch_yield_total_time: float = 0.0  # 批量任务避让累计时间

        # margin 限速重试队列：存放 (save_result, metadata) 元组
        self._margin_retry_queue: deque = deque()
        self._margin_poller_task: Optional[asyncio.Task] = None
        
        # 端点健康度监控（防 405）
        self._endpoint_stats = {
            "share_snap_app": {"success": 0, "fail": 0, "last_405": 0},
            "share_snap_webapi": {"success": 0, "fail": 0, "last_405": 0},
        }
        
        # Cookie 文件管理（用于自动恢复登录态）
        self._cookie_file: Optional[Path] = None
        self.cookies_str: str = ""  # 保存当前 cookie 用于检测更新

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

    @property
    def is_login_failed(self) -> bool:
        """最后一次验证是否明确失败（如密码错误、Cookie失效等）"""
        return self._last_verify_failed

    async def set_restriction(self, hours: float = 1.0):
        """设置全局限制状态并持久化到 DB"""
        self._restriction_until = time.time() + (hours * 3600)
        msg = f"🚫 115 服务已进入全局限制模式，预计持续 {hours} 小时 (直到 {time.strftime('%H:%M:%S', time.localtime(self._restriction_until))})。\n系统将自动暂停正在运行的批量任务，并转为后台轮询处理受限链接。"
        logger.warning(msg)

        # 持久化风控截止时间到 DB
        if self.account:
            asyncio.create_task(self._persist_restriction(self._restriction_until))

        # Pause Batch Tasks（直接推送模式不受全局限制影响，仅暂停转存分享模式）
        try:
            from app.services.excel_batch import excel_batch_service
            if excel_batch_service and excel_batch_service.active_task_id:
                if excel_batch_service.active_task_strategy == "push":
                    logger.info("当前批量任务为直接推送模式，不受全局限制影响，跳过暂停。")
                else:
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

    @staticmethod
    def _is_margin_response(resp) -> bool:
        """检测 115 API 是否返回了限速响应 {"margin": N}。
        这种响应没有 state/data 等正常字段，仅含 margin 秒数。
        """
        return (
            isinstance(resp, dict)
            and "margin" in resp
            and "data" not in resp
            and "state" not in resp
            and "count" not in resp
        )

    def _get_ios_ua_kwargs(self):
        """获取 iOS 用户代理相关的参数"""
        return {
            "headers": {
                "user-agent": IOS_UA,
                "accept-encoding": "gzip, deflate"
            },
            "app": "ios"
        }

    def _record_endpoint_result(self, endpoint: str, success: bool, is_405: bool = False):
        """记录端点调用结果，用于监控与告警"""
        stats = self._endpoint_stats.get(endpoint, {})
        if success:
            stats["success"] = stats.get("success", 0) + 1
        else:
            stats["fail"] = stats.get("fail", 0) + 1
            if is_405:
                stats["last_405"] = time.time()
        
        # 告警阈值：5分钟内连续出现 405
        if is_405:
            last_405_time = stats.get("last_405", 0)
            if last_405_time > 0 and time.time() - last_405_time < 300:
                fail_count = stats.get("fail", 0)
                if fail_count > 3:  # 连续3次以上失败
                    logger.error(f"🚨 端点 {endpoint} 持续被 405 风控 (失败{fail_count}次)，建议检查登录态")

    def _check_cookie_freshness(self):
        """检查并同步最新 cookie（如果文件被外部更新）"""
        try:
            if self._cookie_file and self._cookie_file.exists():
                new_cookie = self._cookie_file.read_text(encoding="utf-8")
                if new_cookie != self.cookies_str and new_cookie.strip():
                    logger.info("🔄 检测到 cookie 更新，重新初始化客户端")
                    self.cookies_str = new_cookie
                    # 更新客户端 cookies
                    if self.client:
                        self.client.cookies = new_cookie
                        logger.info("✅ Cookie 已同步更新")
        except Exception as e:
            logger.warning(f"检查 cookie 更新失败: {e}")

    async def _share_snap_webapi(self, payload: dict, **kwargs) -> dict:
        """
        使用 webapi 的 share_snap（不需要 app 参数，更稳定）
        用作 share_snap_app 的降级备用方案
        """
        api = complete_url("/share/snap", base_url="https://webapi.115.com")
        payload = {"cid": 0, "limit": 32, "offset": 0, **payload}
        
        # 不传 app 参数，使用原生 cookies 请求
        return await self._api_call_with_timeout(
            lambda p, **kw: self.client.request(
                url=api, 
                params=p, 
                async_=True, 
                **kw
            ),
            payload,
            timeout=30,
            label="share_snap_webapi",
            **{k: v for k, v in kwargs.items() if k != "app"}  # 过滤掉 app 参数
        )

    async def _share_snap_with_fallback(self, payload: dict, **kwargs) -> dict:
        """
        多端点容错的 share_snap 调用
        策略：优先 app 接口（proapi），失败自动降级到 webapi
        
        :param payload: share_snap 请求参数
        :return: API 响应字典
        :raises: 所有端点都失败时抛出最后一个异常
        """
        endpoints = [
            {
                "name": "share_snap_app",
                "func": lambda: self.client.share_snap_app(
                    payload, 
                    base_url="https://proapi.115.com",
                    async_=True,
                    **self._get_ios_ua_kwargs(),
                    **kwargs
                ),
                "base_url": "https://proapi.115.com"
            },
            {
                "name": "share_snap_webapi",
                "func": lambda: self._share_snap_webapi(payload, **kwargs),
                "base_url": "https://webapi.115.com"
            },
        ]
        
        last_error = None
        for idx, endpoint_info in enumerate(endpoints, 1):
            endpoint_name = endpoint_info["name"]
            endpoint_func = endpoint_info["func"]
            base_url = endpoint_info["base_url"]
            
            try:
                resp = await asyncio.wait_for(endpoint_func(), timeout=30)
                check_response(resp)
                
                # 记录成功
                self._record_endpoint_result(endpoint_name, success=True, is_405=False)
                
                if idx > 1:
                    logger.warning(f"⚠️ share_snap 主端点失败，使用备用端点 {endpoint_name} 成功")
                
                return resp
                
            except Exception as e:
                error_msg = str(e)
                last_error = e
                is_405 = "405" in error_msg or "Method Not Allowed" in error_msg.lower()
                
                # 记录失败
                self._record_endpoint_result(endpoint_name, success=False, is_405=is_405)
                
                # 405 或风控错误，尝试下一个端点
                if is_405:
                    logger.warning(f"⚠️ 端点 {endpoint_name} ({base_url}) 返回 405，切换到备用端点")
                    if idx < len(endpoints):
                        await asyncio.sleep(1)  # 冷却1秒
                        continue
                else:
                    logger.warning(f"⚠️ 端点 {endpoint_name} ({base_url}) 失败: {error_msg}")
                
                # 如果不是最后一个端点，尝试下一个
                if idx < len(endpoints):
                    await asyncio.sleep(0.5)  # 短暂冷却
                    continue
                
                # 最后一个端点也失败，抛出异常
                break
        
        # 所有端点都失败
        logger.error(f"❌ 所有 share_snap 端点均失败，最后错误: {last_error}")
        raise last_error


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
            # 策略：通过文件路径初始化，支持 cookie 自动更新和持久化
            # 当 p115client 触发重登后，新 cookie 会自动回写到文件
            account_id = self.account.id if self.account else "default"
            
            # 使用临时目录存储 cookie 文件
            cookie_dir = Path(tempfile.gettempdir()) / "p115share_cookies"
            cookie_dir.mkdir(parents=True, exist_ok=True)
            
            self._cookie_file = cookie_dir / f"p115_cookies_{account_id}.txt"
            self._cookie_file.write_text(cookie, encoding="utf-8")
            self.cookies_str = cookie  # 保存副本用于检测更新
            
            # 使用文件路径初始化，支持 p115client 自动管理 cookie
            self.client = P115Client(self._cookie_file)
            self.fs = P115FileSystem(self.client)
            self.clear_save_dir_cache()
            
            logger.info(f"✅ P115Client 初始化成功 (账号ID: {account_id}, cookie文件: {self._cookie_file})")
            # Verify connection asynchronously
            asyncio.create_task(self.verify_connection())
        except Exception as e:
            logger.error(f"❌ P115Client 初始化失败: {e}")
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
                self._last_verify_failed = False
                logger.info("✅ 115 网盘登录验证成功")
                return True
        except Exception as e:
            logger.error(f"❌ 115 网盘登录验证失败: {e}")
            self.is_connected = False
            self._last_verify_failed = True
            return False
        
        self.is_connected = False
        self._last_verify_failed = True
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
        
        # Standardize path and check if it represents root
        path_str = str(path).strip().replace('\\', '/')
        if path_str in ('', '/', '.'):
            logger.info("📂 目标为根目录，直接返回 CID: 0")
            return 0
            
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
                # 🔑 Bug 修复: 检测到 errno=99 (Session 过期) 时，立即清除缓存的目录 CID。
                # 旧 CID 在账号重新登录后可能已失效（目录被重建、CID 变更），
                # 继续使用缓存值会导致后续任务把文件保存到错误目录，进而错误地分享根目录内容。
                err_str = str(e)
                err_errno = getattr(e, 'errno', None)
                if not err_errno and hasattr(e, 'args') and len(e.args) >= 2 and isinstance(e.args[1], dict):
                    err_errno = e.args[1].get('errno')
                if err_errno == 99 or '99' in err_str or '请重新登录' in err_str:
                    if self._save_dir_cid > 0:
                        logger.warning(f"🔑 检测到 Session 过期 (errno=99)，清除保存目录 CID 缓存 (旧值: {self._save_dir_cid})")
                        self._save_dir_cid = 0
                    self.is_connected = False
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

    async def save_share_link(
        self,
        share_url: str,
        metadata: dict = None,
        target_dir: Optional[str] = None,
        skip_large_package: bool = False,
        is_batch: bool = False,
        db_id: Optional[int] = None,
        create_task_subdir: bool = False
    ):
        """通过队列保存链接"""
        return await self._enqueue_op(
            f"save_share_link({share_url})",
            self._save_share_link_internal,
            share_url,
            metadata,
            target_dir,
            skip_large_package,
            db_id,
            create_task_subdir,
            is_batch=is_batch
        )

    async def save_and_share(self, share_url: str, metadata: dict = None, target_dir: Optional[str] = None, skip_large_package: bool = False, db_id: Optional[int] = None, is_batch: bool = False, sensitive_replace_enabled: Optional[bool] = None, sensitive_replace_pinyin: Optional[bool] = None, sensitive_replace_tmdb: Optional[bool] = None):
        """通过队列进行转存并分享"""
        async def _internal_flow():
            save_res = await self._save_share_link_internal(share_url, metadata, target_dir, skip_large_package, db_id=db_id, create_task_subdir=True)
            if save_res and save_res.get("status") == "success":
                # Determine configuration: local override or global fallback
                should_replace = sensitive_replace_enabled if sensitive_replace_enabled is not None else settings.SENSITIVE_REPLACE_ENABLED
                should_pinyin = sensitive_replace_pinyin if sensitive_replace_pinyin is not None else settings.SENSITIVE_REPLACE_PINYIN
                should_tmdb = sensitive_replace_tmdb if sensitive_replace_tmdb is not None else settings.SENSITIVE_REPLACE_TMDB
                # If sensitive replace is enabled, perform replacement inside the task subdir
                if should_replace:
                    await self.replace_sensitive_words_in_dir(save_res["to_cid"], replace_enabled=should_replace, replace_pinyin=should_pinyin, replace_tmdb=should_tmdb)
                share_res = await self.create_share_link(save_res)
                if isinstance(share_res, str):
                    return {"status": "success", "share_link": share_res}
                elif isinstance(share_res, dict) and share_res.get("status") == "margin_limited":
                    # margin 限速 → 入队等待
                    self._enqueue_margin_retry(share_res.get("save_result"), metadata)
                    return {
                        "status": "margin_limited",
                        "message": "分享被限制，将在检测到解除限制后继续分享",
                        "share_url": share_url,
                        "metadata": metadata or {}
                    }
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

    # ── margin 限速排队重试机制 ────────────────────────────────

    def _enqueue_margin_retry(self, save_result: dict, metadata: dict = None):
        """将 margin 限速的任务加入排队队列，并启动后台轮询器"""
        self._margin_retry_queue.append({
            "save_result": save_result,
            "metadata": metadata or {},
            "share_url": save_result.get("share_url", ""),
            "enqueue_time": time.time()
        })
        queue_len = len(self._margin_retry_queue)
        logger.info(f"📋 margin 排队: {save_result.get('share_url', '?')} (队列长度: {queue_len})")
        self._ensure_margin_poller()

    def _ensure_margin_poller(self):
        """确保 margin 轮询器正在运行"""
        if self._margin_poller_task is None or self._margin_poller_task.done():
            self._margin_poller_task = asyncio.create_task(self._margin_retry_poller())
            logger.info("🔄 margin 排队轮询器已启动")

    @property
    def margin_queue_size(self) -> int:
        return len(self._margin_retry_queue)

    async def _margin_retry_poller(self):
        """后台每 5 分钟用队列头部任务探测限速是否解除，解除后逐个处理队列"""
        POLL_INTERVAL = 300  # 5 分钟
        logger.info(f"⏰ margin 轮询器开始运行，队列中有 {len(self._margin_retry_queue)} 个任务")

        while self._margin_retry_queue:
            await asyncio.sleep(POLL_INTERVAL)

            if not self._margin_retry_queue:
                break

            # 用队列第一个任务探测
            probe_item = self._margin_retry_queue[0]
            probe_url = probe_item.get("share_url", "?")
            logger.info(f"🔍 margin 探测: 尝试为 {probe_url} 创建分享链接...")

            try:
                result = await self.create_share_link(probe_item["save_result"])
            except Exception as e:
                logger.error(f"❌ margin 探测异常: {e}")
                result = None

            # 判断探测结果
            if isinstance(result, str):
                # 成功！限速已解除，处理这个任务并继续处理队列
                logger.info(f"✅ margin 限速已解除！探测任务成功: {probe_url}")
                self._margin_retry_queue.popleft()
                await self._margin_task_success(probe_item, result)

                # 通知用户限速解除
                try:
                    from app.services.tg_bot import tg_service
                    if tg_service:
                        remaining = len(self._margin_retry_queue)
                        await tg_service.send_admin_msg(
                            f"🔓 115 分享限速已解除！\n"
                            f"✅ 已完成: {probe_url}\n"
                            f"📋 队列中还有 {remaining} 个待处理任务，正在逐个处理..."
                        )
                except Exception as e:
                    logger.warning(f"发送限速解除通知失败: {e}")

                # 逐个处理剩余队列
                while self._margin_retry_queue:
                    item = self._margin_retry_queue[0]
                    item_url = item.get("share_url", "?")
                    logger.info(f"📤 margin 队列处理: {item_url}")
                    try:
                        item_result = await self.create_share_link(item["save_result"])
                    except Exception as e:
                        logger.error(f"❌ margin 队列处理异常: {item_url}: {e}")
                        item_result = None

                    if isinstance(item_result, str):
                        self._margin_retry_queue.popleft()
                        await self._margin_task_success(item, item_result)
                    elif isinstance(item_result, dict) and item_result.get("status") == "margin_limited":
                        # 又被限速了，停止处理，等下一轮探测
                        logger.warning(f"⚠️ 处理队列时再次触发 margin 限速，暂停处理")
                        try:
                            from app.services.tg_bot import tg_service
                            if tg_service:
                                remaining = len(self._margin_retry_queue)
                                await tg_service.send_admin_msg(
                                    f"⚠️ 处理排队任务时再次触发分享限速，暂停处理。\n"
                                    f"📋 剩余 {remaining} 个任务，将在 5 分钟后继续探测。"
                                )
                        except Exception:
                            pass
                        break
                    else:
                        # 其他错误，跳过该任务
                        self._margin_retry_queue.popleft()
                        logger.warning(f"⚠️ margin 队列任务失败，已跳过: {item_url}, 结果: {item_result}")
                        await self._margin_task_failed(item, item_result)

            elif isinstance(result, dict) and result.get("status") == "margin_limited":
                # 仍然被限速，继续等待
                remaining = len(self._margin_retry_queue)
                logger.info(f"⏳ margin 探测: 仍在限速中，{remaining} 个任务排队等待，{POLL_INTERVAL}s 后再试")
            else:
                # 探测返回其他错误（非 margin），跳过这个任务
                self._margin_retry_queue.popleft()
                logger.warning(f"⚠️ margin 探测返回非 margin 错误，跳过: {probe_url}, 结果: {result}")
                await self._margin_task_failed(probe_item, result)

        logger.info("✅ margin 排队队列已清空，轮询器退出")

    async def _margin_task_success(self, item: dict, share_link: str):
        """margin 排队任务成功后：保存历史 + 推送 TG"""
        share_url = item.get("share_url", "")
        metadata = item.get("metadata", {})
        try:
            await self.save_history_link(share_url, share_link)
        except Exception as e:
            logger.warning(f"保存历史记录失败: {e}")

        try:
            from app.services.tg_bot import tg_service
            if tg_service:
                # 发送给管理员
                await tg_service.send_admin_msg(
                    f"✅ 限速排队任务已完成！\n"
                    f"原链接: {share_url}\n"
                    f"新分享: {share_link}"
                )
                # 推送到频道
                await tg_service.broadcast_to_channels(
                    {share_url: share_link},
                    metadata
                )
        except Exception as e:
            logger.warning(f"margin 成功回调通知失败: {e}")

    async def _margin_task_failed(self, item: dict, result):
        """margin 排队任务最终失败的通知"""
        share_url = item.get("share_url", "")
        error_msg = ""
        if isinstance(result, dict):
            error_msg = result.get("message", str(result))
        else:
            error_msg = str(result) if result else "未知错误"
        try:
            from app.services.tg_bot import tg_service
            if tg_service:
                await tg_service.send_admin_msg(
                    f"❌ 限速排队任务失败\n"
                    f"原链接: {share_url}\n"
                    f"错误: {error_msg}"
                )
        except Exception as e:
            logger.warning(f"margin 失败回调通知失败: {e}")

    async def _save_share_link_internal(
        self,
        share_url: str,
        metadata: dict = None,
        target_dir: Optional[str] = None,
        skip_large_package: bool = False,
        db_id: Optional[int] = None,
        create_task_subdir: bool = True
    ):
        """Internal logic for saving a 115 share link (no locking)"""
        if not self.client:
            logger.warning("P115Client not initialized, cannot save link")
            return None

        # 预检登录态，避免在后续流程中以泛化异常形式暴露“请重新登录”
        if not self.is_connected:
            ok = await self.verify_connection()
            if not ok:
                logger.warning("⚠️ 登录态预检失败，需重新登录后再处理分享链接")
                return {
                    "status": "error",
                    "error_type": "auth_required",
                    "message": "账号登录已失效，请重新登录"
                }
        
        logger.info(f"📥 开始处理分享链接: {share_url}")
        try:
            # 0. RT 加密链接先经 MediaHelper 解密为明文（历史/回传仍用原始 URL）
            from app.services.mh_decrypt import is_rt_encrypted_url, decrypt_rt_share_url, MHDecryptError
            working_url = share_url
            if is_rt_encrypted_url(share_url):
                try:
                    working_url = await decrypt_rt_share_url(share_url)
                    logger.info(f"🔓 RT 链接已解密: {share_url[:80]}... -> {working_url}")
                except MHDecryptError as e:
                    logger.error(f"❌ RT 链接解密失败: {e}")
                    return {
                        "status": "error",
                        "error_type": "mh_decrypt_failed",
                        "message": str(e),
                        "share_url": share_url,
                    }

            # 1. Extract share/receive codes
            payload = share_extract_payload(working_url)
            
            # 2. Get share snapshot to get file IDs and names (使用多端点容错)
            snap_resp = await self._share_snap_with_fallback(payload)
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

            # 🔑 Bug 修复: 拦截 115 系统保留目录作为转存来源。
            # 「最近接收」等系统目录包含用户所有接收文件，若被转存再分享将严重泄露隐私。
            # 通过对比 share_title 拦截这类链接，在任何后续处理之前提前退出。
            _SYSTEM_DIR_BLOCKLIST = {"最近接收", "我的文件", "我的接收", "接收文件", "最近"}
            if share_title and share_title.strip() in _SYSTEM_DIR_BLOCKLIST:
                logger.warning(f"⛔ 拒绝处理：原始分享内容为 115 系统保留目录 [{share_title}]，跳过: {share_url}")
                return {
                    "status": "error",
                    "error_type": "system_dir_blocked",
                    "message": f"原链接分享的是 115 系统目录「{share_title}」，无法转存。请确认分享的是具体文件/文件夹而非系统目录。"
                }

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

                # Save to DB for persistence（先检查是否已存在，避免重复入库）
                from sqlalchemy import select as sa_select
                async with async_session() as session:
                    existing = await session.execute(
                        sa_select(PendingLink).where(PendingLink.share_url == share_url)
                    )
                    existing_task = existing.scalars().first()
                    if existing_task:
                        logger.info(f"♻️ 发现已有等待任务 (id={existing_task.id})，复用现有记录: {share_url}")
                        db_id = existing_task.id
                    else:
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
            if create_task_subdir:
                clean_title = re.sub(r'[\\/:*?"<>|]', '', share_title).strip()
                if clean_title:
                    task_folder_name = f"{clean_title}_{time.strftime('%Y%m%d_%H%M%S')}"
                else:
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
            else:
                task_cid = to_cid
                task_folder_name = ""

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
                    recursive_links = await self._save_share_recursive(working_url, task_cid)
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

                from sqlalchemy import select as sa_select
                async with async_session() as session:
                    existing = await session.execute(
                        sa_select(PendingLink).where(PendingLink.share_url == share_url)
                    )
                    existing_task = existing.scalars().first()
                    if existing_task:
                        logger.info(f"♻️ 发现已有等待任务 (id={existing_task.id})，复用现有记录: {share_url}")
                        db_id = existing_task.id
                    else:
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

                from sqlalchemy import select as sa_select
                async with async_session() as session:
                    existing = await session.execute(
                        sa_select(PendingLink).where(PendingLink.share_url == share_url)
                    )
                    existing_task = existing.scalars().first()
                    if existing_task:
                        logger.info(f"♻️ 发现已有等待任务 (id={existing_task.id})，复用现有记录: {share_url}")
                        db_id = existing_task.id
                    else:
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

            # 检查是否登录失效（常见错误: 请重新登录, errno=99）
            if errno_val == 99 or "请重新登录" in error_msg:
                self.is_connected = False
                logger.warning(f"🔐 检测到账号登录失效: {share_url}")
                return {
                    "status": "error",
                    "error_type": "auth_required",
                    "message": "账号登录已失效，请重新登录"
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

            # 检查是否为"空间不足"异常：触发清理并将任务放入待重试队列
            if "空间不足" in error_msg or "扩容" in error_msg:
                logger.warning(f"⚠️ 检测到 115 账号空间不足，触发紧急清理并将任务放入待重试队列: {share_url}")
                # 清理本次失败留下的空子目录（避免垃圾堆积）
                _failed_cid = task_cid if 'task_cid' in locals() and task_cid else None
                async def _cleanup_and_delete(failed_cid):
                    if failed_cid:
                        try:
                            await self._api_call_with_timeout(
                                self.client.fs_delete, failed_cid, async_=True,
                                timeout=API_TIMEOUT, label="fs_delete_empty_subdir",
                                **self._get_ios_ua_kwargs()
                            )
                            logger.info(f"🗑️ 已清理空间不足时留下的空子目录 (CID: {failed_cid})")
                        except Exception:
                            pass
                    await self._do_cleanup_logic()
                # 异步触发清理，不阻塞当前返回（清理本身耗时较长）
                asyncio.create_task(_cleanup_and_delete(_failed_cid))

                if db_id:
                    return {
                        "status": "pending",
                        "reason": "no_space",
                        "share_url": share_url,
                        "metadata": metadata or {},
                        "db_id": db_id
                    }

                from sqlalchemy import select as sa_select
                async with async_session() as session:
                    existing = await session.execute(
                        sa_select(PendingLink).where(PendingLink.share_url == share_url)
                    )
                    existing_task = existing.scalars().first()
                    if existing_task:
                        logger.info(f"♻️ 发现已有等待任务 (id={existing_task.id})，复用现有记录: {share_url}")
                        db_id = existing_task.id
                    else:
                        new_task = PendingLink(
                            share_url=share_url,
                            metadata_json=metadata or {},
                            status="no_space"
                        )
                        session.add(new_task)
                        await session.commit()
                        db_id = new_task.id

                return {
                    "status": "pending",
                    "reason": "no_space",
                    "share_url": share_url,
                    "metadata": metadata or {},
                    "db_id": db_id
                }

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
            from app.services.mh_decrypt import is_rt_encrypted_url, decrypt_rt_share_url, MHDecryptError
            working_url = share_url
            if is_rt_encrypted_url(share_url):
                try:
                    working_url = await decrypt_rt_share_url(share_url)
                    logger.info(f"🔓 状态检查前 RT 链接已解密: {working_url}")
                except MHDecryptError as e:
                    logger.error(f"❌ 状态检查时 RT 解密失败: {e}")
                    return None

            payload = share_extract_payload(working_url)
            # 使用多端点容错机制获取分享状态
            snap_resp = await self._share_snap_with_fallback(payload)
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
            # 检查是否登录失效（常见错误: 请重新登录, errno=99）
            if "99" in error_msg or "请重新登录" in error_msg:
                self.is_connected = False
                self._last_verify_failed = True
                logger.warning(f"🔐 检测到账号登录失效 (状态检查): {share_url}")

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
                    
                    # 主动检测 115 非标限速响应（仅含 {"margin": N}，无 state/data）
                    if isinstance(send_resp, dict) and "margin" in send_resp and "data" not in send_resp:
                        margin_val = send_resp.get("margin", 10)
                        wait = max(int(margin_val), 5)
                        logger.warning(f"⚠️ 115 分享接口触发限速 (margin={margin_val})，等待 {wait} 秒后重试... (尝试 {retry_attempt}/{max_share_retries})")
                        if retry_attempt < max_share_retries:
                            await asyncio.sleep(wait)
                            continue
                        else:
                            raise KeyError("margin")
                    
                    check_response(send_resp)
                    logger.debug(f"📋 share_send_direct 响应: {send_resp}")
                    
                    data = send_resp.get("data")
                    if not data or not isinstance(data, dict):
                        logger.warning(f"⚠️ share_send_direct 响应缺少 data 字段: {send_resp}")
                        if retry_attempt < max_share_retries:
                            await asyncio.sleep(5)
                            continue
                        else:
                            raise KeyError("data")
                    
                    batch_share_code = data.get("share_code")
                    batch_receive_code = data.get("receive_code") or data.get("recv_code")
                    logger.info(f"✅ 直接分享分卷 {batch_idx} 创建成功: {batch_share_code}")
                    break
                except Exception as share_error:
                    error_str = str(share_error)
                    if "99" in error_str or "请重新登录" in error_str:
                        self.is_connected = False
                        logger.warning(f"🔐 检测到账号登录失效 (直接分享): {share_error}")

                    is_rate_limited = isinstance(share_error, KeyError) and share_error.args and share_error.args[0] in ("margin", "data")
                    if ("4100005" in error_str or "已被移动或删除" in error_str or is_rate_limited) and retry_attempt < max_share_retries:
                        wait = 10 if is_rate_limited else 5
                        logger.warning(f"⚠️ {'115 分享接口触发限速' if is_rate_limited else '文件尚未就绪'}，等待 {wait} 秒后重试 (分卷 {batch_idx})...")
                        await asyncio.sleep(wait)
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

    async def _get_dir_items(self, cid: int, *, strict: bool = False) -> list:
        """获取目录中所有顶层文件/文件夹的详细信息，用于保存前后 diff 或目录树展示（自动翻页，避免 >1000 条时漏记）"""
        items = []
        offset = 0
        limit = 1000
        seen_page_signatures = set()
        try:
            while True:
                resp = await self._api_call_with_timeout(
                    self.client.fs_files_app2,
                    {"cid": cid, "limit": limit, "offset": offset, "show_dir": 1},
                    async_=True,
                    timeout=30, max_retries=2, label="fs_files_get_items",
                    **self._get_ios_ua_kwargs()
                )
                # 🛡️ 检测 115 限速响应
                if self._is_margin_response(resp):
                    margin_val = int(resp.get("margin", 5))
                    logger.warning(f"⚠️ fs_files 获取项触发 115 限速 (margin={margin_val})，等待后重试")
                    await asyncio.sleep(max(margin_val, 3))
                    continue
                check_response(resp)
                file_list = resp.get("data", [])
                if isinstance(file_list, dict):
                    file_list = file_list.get("list", [])
                if not file_list:
                    break
                page_signature = tuple(
                    str(
                        item.get("fid")
                        or item.get("cid")
                        or item.get("file_id")
                        or item.get("category_id")
                        or item.get("id")
                        or ""
                    )
                    for item in file_list
                )
                if page_signature in seen_page_signatures:
                    message = f"目录 {cid} 分页返回重复页面，offset={offset}"
                    if strict:
                        raise RuntimeError(message)
                    logger.warning(message)
                    break
                seen_page_signatures.add(page_signature)
                for item in file_list:
                    item_id = item.get("fid") or item.get("cid") or item.get("file_id") or item.get("category_id") or item.get("id")
                    item_name = item.get("n") or item.get("fn") or item.get("name") or item.get("file_name") or item.get("title") or item.get("category_name")
                    if item_id and item_name:
                        # 文件夹可能带空的 fid 字段，应按有效文件 ID 判断类型。
                        is_dir = not bool(item.get("fid") or item.get("file_id"))
                        items.append({
                            "id": str(item_id),
                            "name": str(item_name),
                            "is_dir": is_dir
                        })
                    elif strict:
                        raise ValueError(f"目录 {cid} 返回缺少 ID 或名称的项目: {item}")
                # 不以“本页少于 limit”作为结束条件：部分端点会使用低于请求值的页大小。
                # 按实际返回数量推进，直到接口明确返回空页。
                offset += len(file_list)
                logger.debug(f"📄 快照目录 {cid} 翻页: offset={offset}, 已收集 {len(items)} 个项")
        except Exception as e:
            logger.warning(f"⚠️ 获取目录 {cid} 顶级项失败: {e}")
            if strict:
                raise
        return items

    async def _snapshot_dir_ids(self, cid: int, *, strict: bool = False) -> set:
        """快照目录中所有顶层文件/文件夹的 ID，用于保存前后 diff 找新增项（自动翻页，避免 >1000 条时漏记）"""
        items = await self._get_dir_items(cid, strict=strict)
        return {item["id"] for item in items}


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

        # 🔑 Bug 修复: 安全断言——禁止对根保存目录或根目录(CID=0)发起分享。
        # 当 _ensure_save_dir 缓存失效后回退到旧/错误 CID 时，to_cid 可能等于根保存目录
        # 本身，导致 _snapshot_dir_ids 枚举出整个 115-Share 目录下的所有文件并分享出去。
        if not to_cid or to_cid == 0:
            logger.error(f"❌ [安全拦截] to_cid 为空或 0，拒绝创建分享链接，避免意外分享根目录内容")
            return {
                "status": "error",
                "error_type": "invalid_cid",
                "message": "任务子目录 CID 无效 (0)，无法创建分享链接"
            }
        if self._save_dir_cid > 0 and to_cid == self._save_dir_cid:
            logger.error(
                f"❌ [安全拦截] to_cid ({to_cid}) 与根保存目录 CID 相同，"
                f"拒绝创建分享链接，避免泄露整个保存目录内容"
            )
            return {
                "status": "error",
                "error_type": "root_dir_share_blocked",
                "message": "检测到即将对保存根目录发起分享，已安全拦截。请检查账号 Session 状态。"
            }

        # 源分享规模是完成判定的硬基准，不能只依赖目标目录暂时稳定。
        logger.info(f"📊 [基准比对数据] 转存源分享规模: 大小 = {original_total_size} 字节, 文件数 = {original_file_count}, 文件夹数 = {original_folder_count}")

        try:
            logger.info(f"⏳ 等待文件深度属性写入子孙目录 (CID: {to_cid})...")

            new_fids = []
            stable_times = 0
            max_poll_attempts = 45 # 最多等待约 90s
            min_stable_required = 3 # 稳定不变的次数要求

            margin_hit_count = 0  # margin 限速不消耗轮询次数，单独计数
            max_margin_hits = 30  # margin 最多容忍 30 次（约 2.5 分钟）

            for poll_attempt in range(1, max_poll_attempts + 1):
                try:
                    logger.debug(f"🔍 探测子目录属性详情 (第 {poll_attempt}/{max_poll_attempts} 次), CID: {to_cid}")
                    
                    # 1. 尝试用你想要的方案：调用类 `fs_category_get` 获取实时深度体积
                    resp = await self._api_call_with_timeout(
                        self.client.fs_category_get_app, to_cid,
                        async_=True, timeout=10, max_retries=1, label="fs_category_get_poll",
                        **self._get_ios_ua_kwargs()
                    )
                    
                    # 🛡️ 检测 115 限速响应 {"margin": N}，不消耗轮询次数
                    if self._is_margin_response(resp):
                        stable_times = 0
                        margin_hit_count += 1
                        margin_val = int(resp.get("margin", 5))
                        wait = max(margin_val, 3)
                        logger.warning(f"⚠️ fs_category_get 触发 115 限速 (margin={margin_val})，等待 {wait}s 后重试 (margin 第 {margin_hit_count}/{max_margin_hits} 次)")
                        if margin_hit_count >= max_margin_hits:
                            logger.warning(f"🚫 轮询阶段 margin 限速持续过久 ({margin_hit_count} 次)，转入 margin 排队")
                            return {
                                "status": "margin_limited",
                                "save_result": save_result,
                                "message": "分享被限制（margin），已加入排队等待"
                            }
                        await asyncio.sleep(wait)
                        continue
                    
                    logger.info(f"📋 [深度属性 RAW API 完整响应] {resp}")
                    
                    # 2. 从原生响应中直接读取（而不是从 data 中读）
                    current_file_count = int(resp.get("count", 0) or resp.get("file_count", 0))
                    current_folder_count = int(resp.get("folder_count", 0))
                    current_total = current_file_count + current_folder_count
                    current_size = parse_size_to_bytes(resp.get("size", 0))

                    logger.info(f"⚖️ [比对进程] 当前挂载层级统计: 大小={current_size} (基准: {original_total_size} 字节), 文件数={current_file_count}, 文件夹数={current_folder_count}")

                    # 3. 结合原先的顶部结构验证，确保外层骨架确实建好了
                    current_items = await self._get_dir_items(to_cid, strict=True)
                    orig_size = int(original_total_size or 0)
                    orig_files = int(original_file_count or 0)
                    orig_folders = int(original_folder_count or 0)
                    size_match = sizes_approximately_equal(current_size, orig_size) if orig_size > 0 else current_size > 0
                    # 源分享常缺深层级文件/文件夹计数（为 0）；此时只按体积判定，避免永远等不到匹配
                    if orig_files == 0 and orig_folders == 0:
                        stats_match = size_match
                    else:
                        stats_match = (
                            size_match
                            and current_file_count == orig_files
                            and current_folder_count == orig_folders
                        )
                    # 敏感词处理可能已修改顶层名称，因此普通分享按顶层数量精确匹配。
                    # 定时移动/复制流程会在改名前执行更严格的名称/ID匹配。
                    top_match = len(current_items) == len(names)

                    if stats_match and top_match:
                        stable_times += 1
                        logger.info(
                            f"🔄 目标目录规模及顶层结构与源分享一致 "
                            f"(连续稳固 {stable_times}/{min_stable_required} 次)"
                        )
                        if stable_times >= min_stable_required:
                            logger.info(f"✅ 目标目录与源分享基准连续一致，确认转存完成")
                            new_fids = [item["id"] for item in current_items]
                            break
                    else:
                        logger.info(
                            "📈 目标目录尚未达到源分享基准: "
                            f"统计匹配={stats_match}, 顶层结构匹配={top_match}, "
                            f"当前总项数={current_total}"
                        )
                        stable_times = 0
                    
                    if poll_attempt < max_poll_attempts:
                        await asyncio.sleep(2)

                except Exception as e:
                    stable_times = 0
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
                        
                        # 主动检测 115 非标限速响应（仅含 {"margin": N}，无 state/data）
                        if self._is_margin_response(send_resp):
                            margin_val = send_resp.get("margin", 10)
                            wait = max(int(margin_val), 5)
                            logger.warning(f"⚠️ 115 分享接口触发限速 (margin={margin_val})，等待 {wait} 秒后重试... (尝试 {retry_attempt}/{max_share_retries})")
                            if retry_attempt < max_share_retries:
                                await asyncio.sleep(wait)
                                continue
                            else:
                                # 3 次都 margin，返回 margin_limited 状态进入排队
                                logger.warning(f"🚫 分享创建阶段 margin 限速 {max_share_retries} 次，转入 margin 排队")
                                return {
                                    "status": "margin_limited",
                                    "save_result": save_result,
                                    "message": "分享被限制（margin），已加入排队等待"
                                }
                        
                        check_response(send_resp)
                        logger.debug(f"📋 share_send 响应: {send_resp}")
                        
                        data = send_resp.get("data")
                        if not data or not isinstance(data, dict):
                            logger.warning(f"⚠️ share_send 响应缺少 data 字段: {send_resp}")
                            if retry_attempt < max_share_retries:
                                await asyncio.sleep(5)
                                continue
                            else:
                                raise KeyError("data")
                        
                        batch_share_code = data.get("share_code")
                        batch_receive_code = data.get("receive_code") or data.get("recv_code")
                        
                        logger.info(f"✅ 分享分卷 {batch_idx} 创建成功: {batch_share_code}")
                        break
                        
                    except Exception as share_error:
                        error_msg = str(share_error)
                        if "99" in error_msg or "请重新登录" in error_msg:
                            self.is_connected = False
                            self._last_verify_failed = True
                            logger.warning(f"🔐 检测到账号登录失效 (创建分享): {share_error}")
                        
                        # data 缺失：115 非标响应，触发重试
                        is_rate_limited = isinstance(share_error, KeyError) and share_error.args and share_error.args[0] == "data"
                        if ("4100005" in error_msg or "已被移动或删除" in error_msg or is_rate_limited) and retry_attempt < max_share_retries:
                            wait = 10 if is_rate_limited else 5
                            logger.warning(f"⚠️ {'115 分享接口触发限速' if is_rate_limited else '文件尚未就绪'}，等待 {wait} 秒后重试...")
                            await asyncio.sleep(wait)
                        else:
                            logger.error(
                                f"❌ 创建分享分卷 {batch_idx} 失败: type={type(share_error).__name__}, "
                                f"args={getattr(share_error, 'args', ())}, detail={share_error}",
                                exc_info=True,
                            )
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
                return {
                    "status": "error",
                    "error_type": "share_failed",
                    "message": "未能生成任何分享链接"
                }
            
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
            logger.error(
                f"❌ 创建新分享链接失败: type={type(e).__name__}, args={getattr(e, 'args', ())}, detail={e}",
                exc_info=True,
            )
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

            # 115 接口偶发返回结构变动时，底层解析可能抛出 KeyError
            if isinstance(e, KeyError):
                missing_key = e.args[0] if e.args else "unknown"
                # margin 类 KeyError 转为 margin_limited 排队
                if missing_key == "margin":
                    return {
                        "status": "margin_limited",
                        "save_result": save_result,
                        "message": "分享被限制（margin），已加入排队等待"
                    }
                return {
                    "status": "error",
                    "error_type": "share_response_parse_error",
                    "message": f"创建分享接口响应缺少关键字段: {missing_key}"
                }

            if isinstance(error_info, dict) and error_info:
                api_msg = error_info.get("error") or error_info.get("msg") or error_info.get("message") or str(e)
                return {
                    "status": "error",
                    "error_type": "share_api_error",
                    "message": f"创建分享接口失败: {api_msg}"
                }

            return {
                "status": "error",
                "error_type": "share_failed",
                "message": f"创建分享失败: {error_msg}"
            }

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
            return parse_size_to_bytes(val)

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

    async def replace_sensitive_words_in_dir(self, cid: int, replace_enabled: Optional[bool] = None, replace_pinyin: Optional[bool] = None, replace_tmdb: Optional[bool] = None):
        """递归遍历指定目录并批量替换敏感词/TMDB别名"""
        should_replace = replace_enabled if replace_enabled is not None else settings.SENSITIVE_REPLACE_ENABLED
        if not should_replace:
            return

        if not self.fs:
            logger.warning("⚠️ P115FileSystem 未初始化，跳过敏感词替换")
            return

        try:
            mapping = json.loads(settings.SENSITIVE_REPLACE_MAPPING) if settings.SENSITIVE_REPLACE_MAPPING else {}
        except Exception as e:
            logger.error(f"❌ 解析敏感词映射表失败: {e}")
            mapping = {}

        should_pinyin = replace_pinyin if replace_pinyin is not None else settings.SENSITIVE_REPLACE_PINYIN
        should_tmdb = replace_tmdb if replace_tmdb is not None else settings.SENSITIVE_REPLACE_TMDB

        if not mapping and not should_pinyin and not should_tmdb:
            return

        logger.info(f"🔍 开始对目录 (CID: {cid}) 执行敏感词替换...")

        # 等待转存文件写入目标目录并稳定 (处理异步转存的时滞)
        try:
            prev_total = -1
            prev_size = ""
            stable_count = 0
            max_wait_attempts = 15
            for attempt in range(1, max_wait_attempts + 1):
                resp = await self._api_call_with_timeout(
                    self.client.fs_category_get_app, cid,
                    async_=True, timeout=10, max_retries=1, label="replace_words_poll",
                    **self._get_ios_ua_kwargs()
                )
                if resp and not self._is_margin_response(resp):
                    cur_file = int(resp.get("count", 0) or resp.get("file_count", 0))
                    cur_folder = int(resp.get("folder_count", 0))
                    cur_total = cur_file + cur_folder
                    cur_size = str(resp.get("size", "0"))
                    
                    if cur_total > 0:
                        if prev_total == -1:
                            prev_total = cur_total
                            prev_size = cur_size
                        elif cur_total == prev_total and cur_size == prev_size:
                            stable_count += 1
                            if stable_count >= 1:
                                break
                        else:
                            stable_count = 0
                            prev_total = cur_total
                            prev_size = cur_size
                    else:
                        stable_count = 0
                        prev_total = 0
                        prev_size = "0"
                
                if attempt < max_wait_attempts:
                    await asyncio.sleep(1.5)
                else:
                    logger.warning(f"⚠️ 目标目录 (CID: {cid}) 在等待 22 秒后依然为空或未稳定，直接执行敏感词检测")
        except Exception as e:
            logger.warning(f"⚠️ 敏感词替换前等待目录稳定失败: {e}")

        import re
        pattern = None
        mapping_lower = {}
        if mapping:
            try:
                # 使用 re.IGNORECASE 进行大小写无关的匹配
                mapping_lower = {k.lower(): v for k, v in mapping.items()}
                pattern = re.compile("|".join(re.escape(k) for k in mapping.keys()), re.IGNORECASE)
            except Exception as e:
                logger.error(f"❌ 编译敏感词正则失败: {e}")
                return

        from app.services.tmdb_service import tmdb_service

        def contains_chinese(text: str) -> bool:
            return any('\u4e00' <= char <= '\u9fff' for char in text)

        async def process_name(old_name: str, parent_replacements: list, media_hint: str) -> Tuple[str, list]:
            new_replacements = list(parent_replacements) if parent_replacements else []
            new_name = old_name

            # 1. 优先：敏感词映射表
            if pattern:
                def replace_func(match):
                    matched_str = match.group(0)
                    return mapping_lower.get(matched_str.lower(), matched_str)
                mapped_name = pattern.sub(replace_func, old_name)
                if mapped_name != old_name:
                    return mapped_name, new_replacements

            # 2. 降级：TMDB ID 别名匹配
            tmdb_success = False
            if should_tmdb and contains_chinese(old_name):
                tmdb_id = extract_tmdb_id_from_name(old_name)
                if tmdb_id:
                    chinese_title = extract_replacement_title_fragment(old_name)
                    preferred_media = media_hint if media_hint in ["tv", "movie"] else None
                    alias = await tmdb_service.get_alias_by_id(
                        tmdb_id,
                        preferred_media=preferred_media,
                        chinese_title_hint=chinese_title,
                    )
                    if alias:
                        if chinese_title:
                            new_name = old_name.replace(chinese_title, alias, 1)
                            if new_name != old_name:
                                tmdb_success = True
                                new_replacements.append((chinese_title, alias))
                                logger.debug(
                                    f"🎯 TMDB 替换命中: name=[{old_name}] tmdb_id={tmdb_id} media_hint={media_hint} alias=[{alias}]"
                                )
                    else:
                        logger.debug(
                            f"ℹ️ TMDB 未命中可用别名: name=[{old_name}] tmdb_id={tmdb_id} media_hint={media_hint}，将尝试后续策略"
                        )

            # 2.5 如果 TMDB 失败或不适用，检查是否有父目录继承来的别名替换
            if not tmdb_success and parent_replacements and contains_chinese(old_name):
                for chinese_title, alias in parent_replacements:
                    if chinese_title in old_name:
                        new_name = old_name.replace(chinese_title, alias, 1)
                        if new_name != old_name:
                            tmdb_success = True
                            break

            if tmdb_success:
                return new_name, new_replacements

            # 3. 兜底：拼音全拼替换（仅转换连续中文段，保留英文/扩展名等）
            if should_pinyin and contains_chinese(old_name):
                try:
                    import pypinyin
                    episode_pattern = re.compile(r"第\s*[0-9一二三四五六七八九十百千万]+\s*[集季]")
                    placeholders = []

                    def encode_episodes(match):
                        placeholder = f"__EPISODE_HOLDER_{len(placeholders)}__"
                        placeholders.append((placeholder, match.group(0)))
                        return placeholder

                    protected_name = episode_pattern.sub(encode_episodes, old_name)
                    segments = re.findall(r"[\u4e00-\u9fff]+|[^\u4e00-\u9fff]+", protected_name)
                    out_parts = []
                    for idx, seg in enumerate(segments):
                        if re.fullmatch(r"[\u4e00-\u9fff]+", seg):
                            py = " ".join(
                                p.capitalize()
                                for p in pypinyin.lazy_pinyin(seg, style=pypinyin.Style.NORMAL)
                            )
                            if out_parts and re.search(r"[A-Za-z0-9]$", out_parts[-1]):
                                py = " " + py
                            if idx + 1 < len(segments) and re.match(r"^[A-Za-z0-9]", segments[idx + 1]):
                                py = py + " "
                            out_parts.append(py)
                        else:
                            out_parts.append(seg)
                    pinyin_name = "".join(out_parts)

                    for placeholder, original in placeholders:
                        pinyin_name = pinyin_name.replace(placeholder, original)
                        pinyin_name = pinyin_name.replace(placeholder.lower(), original)

                    return pinyin_name, new_replacements
                except Exception as pe:
                    logger.error(f"❌ 拼音全拼替换失败: {pe}")

            return old_name, new_replacements

        renamed_count = 0
        try:
            to_rename = []
            visited = set()
            dir_hint_cache: Dict[int, str] = {}

            async def get_dir_hint(target_cid: int) -> str:
                if target_cid in dir_hint_cache:
                    return dir_hint_cache[target_cid]
                try:
                    sub_items = await self.fs.readdir(target_cid, async_=True, **self._get_ios_ua_kwargs())
                    hint = infer_media_hint_from_items(sub_items)
                except Exception:
                    hint = "unknown"
                dir_hint_cache[target_cid] = hint
                return hint

            async def walk_and_collect(current_cid: int, parent_replacements: list = None, inherited_hint: str = "unknown"):
                if parent_replacements is None:
                    parent_replacements = []
                if current_cid in visited:
                    return
                visited.add(current_cid)
                
                try:
                    items = await self.fs.readdir(current_cid, async_=True, **self._get_ios_ua_kwargs())
                except Exception as read_ex:
                    logger.error(f"❌ 读取目录 (CID: {current_cid}) 失败: {read_ex}")
                    return

                dirs = [item for item in items if item.get("is_dir")]
                files = [item for item in items if not item.get("is_dir")]
                dir_structure_hint = infer_media_hint_from_items(items)
                current_hint = dir_structure_hint if dir_structure_hint != "unknown" else inherited_hint

                if dir_structure_hint != "unknown":
                    logger.debug(f"🧭 目录媒体推断: CID={current_cid} hint={dir_structure_hint}")

                # 优先递归子目录并收集重命名
                for d in dirs:
                    sub_cid = d.get("id")
                    old_name = d.get("name")
                    if sub_cid is not None and old_name:
                        name_hint = infer_media_hint_from_name(old_name)
                        child_structure_hint = await get_dir_hint(sub_cid)
                        d_media_hint = (
                            name_hint if name_hint != "unknown"
                            else child_structure_hint if child_structure_hint != "unknown"
                            else current_hint
                        )
                        d_new_name, d_replacements = await process_name(old_name, parent_replacements, d_media_hint)
                        await walk_and_collect(sub_cid, d_replacements, d_media_hint)
                        if d_new_name != old_name:
                            to_rename.append((sub_cid, d_new_name, old_name))

                # 收集文件重命名
                for f in files:
                    fid = f.get("id")
                    old_name = f.get("name")
                    if fid is not None and old_name:
                        file_hint = infer_media_hint_from_name(old_name)
                        f_media_hint = file_hint if file_hint != "unknown" else current_hint
                        f_new_name, _ = await process_name(old_name, parent_replacements, f_media_hint)
                        if f_new_name != old_name:
                            to_rename.append((fid, f_new_name, old_name))

            await walk_and_collect(cid)

            if not to_rename:
                logger.info(f"✅ 目录 (CID: {cid}) 内未检测到敏感词")
                return

            logger.info(f"📂 发现 {len(to_rename)} 个项目包含敏感词，开始重命名...")

            # 批量重命名 (按照 100 个一组)
            batch_size = 100
            for i in range(0, len(to_rename), batch_size):
                batch = to_rename[i:i+batch_size]
                payload_items = [(item[0], item[1]) for item in batch]
                try:
                    logger.debug(f"📤 发送重命名批次 ({i//batch_size + 1}), 数量: {len(payload_items)}")
                    resp = await self._api_call_with_timeout(
                        self.client.fs_rename_app,
                        payload_items,
                        async_=True,
                        **self._get_ios_ua_kwargs()
                    )
                    check_response(resp)
                    renamed_count += len(payload_items)
                    for item in batch:
                        logger.info(f"✏️ 重命名成功: [{item[2]}] -> [{item[1]}]")
                except Exception as ex:
                    logger.error(f"❌ 批量重命名批次失败: {ex}，将尝试逐个重命名...")
                    # 容错降级：单个重命名
                    for item in batch:
                        try:
                            resp = await self._api_call_with_timeout(
                                self.client.fs_rename_app,
                                (item[0], item[1]),
                                async_=True,
                                **self._get_ios_ua_kwargs()
                            )
                            check_response(resp)
                            renamed_count += 1
                            logger.info(f"✏️ 单体容错重命名成功: [{item[2]}] -> [{item[1]}]")
                        except Exception as single_ex:
                            logger.error(f"❌ 容错重命名失败 [{item[2]}]: {single_ex}")

                # 每次批量操作后，休眠 1.0 ~ 2.0 秒，降低风控概率
                await asyncio.sleep(random.uniform(1.0, 2.0))

            logger.info(f"🎉 敏感词替换完成，共替换了 {renamed_count} 个项目")
            # 等待 2 秒让网盘后台完成状态同步
            await asyncio.sleep(2.0)

        except Exception as e:
            logger.error(f"❌ 执行敏感词替换时发生异常: {e}", exc_info=True)


    
p115_service = P115Service()

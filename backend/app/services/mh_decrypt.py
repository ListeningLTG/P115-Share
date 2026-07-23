"""
MediaHelper 加密分享链接解密（旧 RT2Z… / 新长密文 3V9u… 等）
流程: 选 115 账号 → analyze-share-async → 轮询 progress 取明文 URL → cancel
"""
import asyncio
import re
from typing import Optional
from urllib.parse import urlparse

from loguru import logger

from app.core.config import settings
from app.services.mh_client import mh_client

_SHARE_CODE_RE = re.compile(
    r"https?://(?:115\.com|115cdn\.com|anxia\.com)/s/([A-Za-z0-9]+)",
    re.IGNORECASE,
)

# 普通 share_code 约 11 位；加密分享码明显更长（旧 RT2Z…，新约 190 位 base64 风格密文）
_ENCRYPTED_SHARE_CODE_MIN_LEN = 20


class MHDecryptError(Exception):
    """加密分享链接解密失败"""


def extract_share_code(url: str) -> Optional[str]:
    m = _SHARE_CODE_RE.search(url or "")
    return m.group(1) if m else None


def is_rt_encrypted_url(url: str) -> bool:
    """判断是否为 115 加密分享链接（需经 MH 解密后再转存）。

    不再依赖 RT 前缀：旧版以 RT 开头，新版为更长的字母数字密文（如 3V9u…）。
    用长度区分普通 ~11 位 share_code 与加密密文。
    """
    code = extract_share_code(url)
    if not code:
        return False
    return len(code) >= _ENCRYPTED_SHARE_CODE_MIN_LEN


def _is_plaintext_share_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    if is_rt_encrypted_url(url):
        return False
    return bool(extract_share_code(url))


_decrypt_lock = asyncio.Lock()
# RT 密文 → 明文缓存，避免同进程重复解密，也方便日志/TG 展示
_rt_decrypt_cache: dict[str, str] = {}


def get_cached_plaintext_url(url: str) -> Optional[str]:
    """若已解密过该 RT 链接，返回缓存的明文；否则返回 None。"""
    if not url:
        return None
    if not is_rt_encrypted_url(url):
        return url
    return _rt_decrypt_cache.get(url)


def resolve_display_share_url(url: str, save_res: Optional[dict] = None) -> str:
    """优先取处理结果中的明文链接，其次缓存，最后回退原始 URL。"""
    if isinstance(save_res, dict):
        candidate = save_res.get("share_url") or save_res.get("display_url")
        if candidate and not is_rt_encrypted_url(candidate):
            return candidate
    cached = get_cached_plaintext_url(url)
    return cached or url


async def _get_drive115_account_id() -> Optional[str]:
    resp = await mh_client.get("/api/v1/cloud-accounts?active_only=true")
    if not isinstance(resp, dict) or not resp.get("data"):
        logger.error(f"MH 获取网盘账号失败: {resp}")
        return None

    accounts = resp["data"].get("accounts") or []
    for acc in accounts:
        if acc.get("cloud_type") == "drive115" and acc.get("is_default"):
            return acc.get("external_id")
    for acc in accounts:
        if acc.get("cloud_type") == "drive115":
            return acc.get("external_id")

    logger.error(f"MH 中未找到 drive115 账号: {resp}")
    return None


async def _wait_for_previous_analyze(max_wait_seconds: int = 600) -> None:
    """等待上一个分享分析任务结束，避免 409"""
    max_attempts = max(1, max_wait_seconds // 2)
    for attempt in range(max_attempts):
        try:
            resp = await mh_client.get(
                "/api/v1/library-tool/share-analysis/latest?source_type=share_link"
            )
            if isinstance(resp, dict) and resp.get("data"):
                status = resp["data"].get("status")
                if status == "running":
                    logger.info(
                        f"MH: 上一分析任务运行中，等待 2s... ({attempt + 1}/{max_attempts})"
                    )
                    await asyncio.sleep(2)
                    continue
            return
        except Exception as e:
            logger.error(f"MH: 检查上一分析任务异常: {e}")
            return
    logger.warning(f"MH: 等待上一分析任务超时 ({max_wait_seconds}s)，继续执行")


async def _cancel_task(task_id: str) -> None:
    try:
        resp = await mh_client.post(f"/api/v1/library-tool/analysis-task/{task_id}/cancel")
        logger.info(f"MH: 已取消分析任务 {task_id}: {resp}")
    except Exception as e:
        logger.warning(f"MH: 取消分析任务 {task_id} 失败: {e}")


async def decrypt_rt_share_url(url: str, poll_interval: float = 2.0, max_wait: int = 120) -> str:
    """
    解密加密分享链接，返回明文 115 分享 URL（可含 password）。
    拿到明文后立即 cancel，不等待分析完成。
    """
    if not is_rt_encrypted_url(url):
        return url

    address = (settings.MH_ADDRESS or "").strip()
    if not address:
        raise MHDecryptError("未配置 MediaHelper 地址，无法解密加密分享链接")
    if not (settings.MH_USERNAME or "").strip() or not (settings.MH_PASSWORD or "").strip():
        raise MHDecryptError("MediaHelper 用户名或密码未配置，无法解密加密分享链接")

    async with _decrypt_lock:
        if url in _rt_decrypt_cache:
            return _rt_decrypt_cache[url]

        account_id = await _get_drive115_account_id()
        if not account_id:
            raise MHDecryptError("MediaHelper 中未配置可用的 115 网盘账号")

        await _wait_for_previous_analyze()

        analyze_resp = await mh_client.post(
            "/api/v1/library-tool/analyze-share-async",
            json={
                "share_url": url,
                "cloud_type": "drive115",
                "account_identifier": account_id,
                "analysis_mode": "tmdb",
            },
        )
        if not isinstance(analyze_resp, dict) or not (analyze_resp.get("data") or {}).get("task_id"):
            msg = (
                analyze_resp.get("message", "未知错误")
                if isinstance(analyze_resp, dict)
                else str(analyze_resp)
            )
            raise MHDecryptError(f"提交 MH 分析任务失败: {msg}")

        task_id = analyze_resp["data"]["task_id"]
        logger.info(f"MH: 已提交加密链接解密分析任务 {task_id}")

        plaintext: Optional[str] = None
        try:
            attempts = max(1, int(max_wait / poll_interval))
            for i in range(attempts):
                await asyncio.sleep(poll_interval)
                prog = await mh_client.get(
                    f"/api/v1/library-tool/analysis-task/{task_id}/progress"
                )
                if not isinstance(prog, dict) or not prog.get("data"):
                    continue

                data = prog["data"]
                status = data.get("status")
                meta = data.get("metadata") or {}
                candidate = meta.get("share_url") or ""

                if _is_plaintext_share_url(candidate) and candidate != url:
                    plaintext = candidate
                    logger.info(
                        f"MH: 第 {i + 1} 次轮询拿到明文链接 (status={status}): {plaintext}"
                    )
                    break

                if status == "failed":
                    raise MHDecryptError(
                        f"MH 分析失败: {data.get('error') or '未知原因'}"
                    )
                if status == "cancelled":
                    raise MHDecryptError(
                        f"MH 分析已取消: {data.get('error') or '任务被取消'}"
                    )
                if status == "completed":
                    # 完成后仍无明文则失败
                    if _is_plaintext_share_url(candidate):
                        plaintext = candidate
                        break
                    raise MHDecryptError("MH 分析完成但未返回明文分享链接")

            if not plaintext:
                raise MHDecryptError(f"MH 解密超时 ({max_wait}s)，未拿到明文链接")
        finally:
            await _cancel_task(task_id)

        # 规范化：保留 password query
        parsed = urlparse(plaintext)
        if not parsed.scheme or not parsed.netloc:
            raise MHDecryptError(f"MH 返回的明文链接无效: {plaintext}")

        _rt_decrypt_cache[url] = plaintext
        return plaintext


async def normalize_115_share_url(url: str) -> str:
    """若为加密分享链接则解密，否则原样返回。"""
    if is_rt_encrypted_url(url):
        return await decrypt_rt_share_url(url)
    return url

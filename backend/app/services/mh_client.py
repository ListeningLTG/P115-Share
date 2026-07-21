"""
MediaHelper 全局客户端单例
- 统一管理 token 缓存，避免各处重复登录
- 支持 401/403 时自动刷新 token 并重试一次
- 配置变更时主动清除缓存
"""
import asyncio
from typing import Any, Optional, Tuple

import aiohttp
from loguru import logger

from app.core.config import settings


class MHClient:
    def __init__(self):
        self._token: Optional[str] = None
        self._domain: Optional[str] = None
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=60)
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
                    ),
                },
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def login_with(
        self, domain: str, username: str, password: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """使用指定凭证登录，返回 (domain, token)"""
        domain = domain.rstrip("/")
        try:
            session = await self._get_session()
            async with session.post(
                f"{domain}/api/v1/auth/login",
                json={"username": username, "password": password},
            ) as resp:
                data = await resp.json(content_type=None)
            token = (data or {}).get("data", {}).get("access_token") if isinstance(data, dict) else None
            if token:
                self._token = token
                self._domain = domain
                logger.info("MHClient: 登录成功，token 已缓存")
                return domain, token
            logger.warning(f"MHClient: 登录未返回有效 token，响应: {data}")
        except Exception as e:
            logger.error(f"MHClient: 登录异常: {e}")
        return None, None

    async def _do_login(self) -> Tuple[Optional[str], Optional[str]]:
        domain = (settings.MH_ADDRESS or "").rstrip("/")
        username = settings.MH_USERNAME or ""
        password = settings.MH_PASSWORD or ""
        if not domain or not username or not password:
            logger.debug("MH 配置不完整，跳过登录")
            return None, None
        return await self.login_with(domain, username, password)

    async def get_token(self) -> Tuple[Optional[str], Optional[str]]:
        if self._token and self._domain:
            return self._domain, self._token
        async with self._lock:
            if self._token and self._domain:
                return self._domain, self._token
            return await self._do_login()

    async def refresh_token(self) -> Tuple[Optional[str], Optional[str]]:
        async with self._lock:
            self._token = None
            self._domain = None
            return await self._do_login()

    def clear(self):
        self._token = None
        self._domain = None

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        domain, token = await self.get_token()
        if not domain or not token:
            return None

        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        session = await self._get_session()
        url = f"{domain}{path}"

        async def _once(auth_token: str):
            headers["Authorization"] = f"Bearer {auth_token}"
            async with session.request(method, url, headers=headers, **kwargs) as resp:
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    text = await resp.text()
                    return {"code": resp.status, "message": text}

        result = await _once(token)
        if self._is_auth_error(result):
            logger.warning(f"MHClient: {method} {path} 返回权限错误，尝试刷新 token...")
            domain, token = await self.refresh_token()
            if not token:
                return None
            result = await _once(token)
        return result

    async def get(self, path: str, **kwargs) -> Any:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> Any:
        return await self._request("POST", path, **kwargs)

    @staticmethod
    def _is_auth_error(resp: Any) -> bool:
        if not isinstance(resp, dict):
            return False
        code = resp.get("code")
        return code in (401, 403, "401", "403")


mh_client = MHClient()

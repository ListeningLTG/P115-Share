import asyncio
import re
from typing import Optional, List, Dict, Any
from loguru import logger
import aiohttp
from sqlalchemy import select, delete, func
from app.core.database import get_db
from app.models.schema import TMDBConfig, SensitiveMovie


class TMDBService:
    """TMDB API 服务"""

    BASE_URL = "https://api.themoviedb.org/3"
    RATE_LIMIT_DELAY = 0.25   # 页间延迟 250ms
    MOVIE_DELAY = 0.5          # 每部电影处理后延迟 500ms，避免触发限速

    def __init__(self):
        self.api_key: Optional[str] = None
        self.is_fetching = False
        self.fetch_progress: Dict[str, Any] = {
            "status": "idle",  # idle, running, completed, stopped, error
            "current": 0,
            "total": 0,
            "filtered": 0,
            "message": ""
        }
        self._stop_flag = False

    # ------------------------------------------------------------------ #
    #  配置管理
    # ------------------------------------------------------------------ #

    async def get_config(self) -> Optional[Dict[str, Any]]:
        """获取 TMDB 配置"""
        async for db in get_db():
            result = await db.execute(select(TMDBConfig).limit(1))
            config = result.scalar_one_or_none()
            if config:
                return {
                    "api_key": config.api_key,
                    "country": config.country,
                    "certifications": config.certifications,
                    "keywords": config.keywords,
                    "use_keyword_filter": config.use_keyword_filter,
                    "last_sync_at": config.last_sync_at
                }
            return None

    async def _update_last_sync_at(self):
        """更新上次全量同步时间为当前 UTC 时间"""
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            async for db in get_db():
                result = await db.execute(select(TMDBConfig).limit(1))
                config = result.scalar_one_or_none()
                if config:
                    config.last_sync_at = now_str
                    await db.commit()
                    logger.info(f"📅 已更新 last_sync_at = {now_str}")
        except Exception as e:
            logger.error(f"❌ 更新 last_sync_at 失败: {e}")

    async def save_config(self, api_key: str, country: str, certifications: List[str],
                          keywords: str = "", use_keyword_filter: bool = False) -> bool:
        """保存 TMDB 配置"""
        try:
            async for db in get_db():
                result = await db.execute(select(TMDBConfig).limit(1))
                config = result.scalar_one_or_none()
                if config:
                    config.api_key = api_key
                    config.country = country
                    config.certifications = certifications
                    config.keywords = keywords
                    config.use_keyword_filter = use_keyword_filter
                else:
                    config = TMDBConfig(
                        api_key=api_key,
                        country=country,
                        certifications=certifications,
                        keywords=keywords,
                        use_keyword_filter=use_keyword_filter
                    )
                    db.add(config)
                await db.commit()
                self.api_key = api_key
                logger.info(f"✅ TMDB 配置已保存: country={country} certs={certifications} kw_filter={use_keyword_filter}")
                return True
        except Exception as e:
            logger.error(f"❌ 保存 TMDB 配置失败: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  连接测试
    # ------------------------------------------------------------------ #

    async def test_connection(self, api_key: str) -> tuple[bool, str]:
        """测试 TMDB API 连接"""
        try:
            url = f"{self.BASE_URL}/configuration"
            params = {"api_key": api_key}
            data, status = await self._request_with_retry(url, params, timeout=10)
            if status == 200:
                return True, "连接成功"
            elif status == 401:
                return False, "API Key 无效"
            else:
                return False, f"连接失败: HTTP {status}"
        except Exception as e:
            return False, f"连接失败: {str(e)}"

    async def _request_with_retry(self, url: str, params: Dict[str, Any] = None, 
                             method: str = "GET", timeout: int = 15, 
                             max_retries: int = 3) -> tuple[Optional[Dict], int]:
        """通用的带重试机制的请求方法"""
        last_error = None
        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    timeout_obj = aiohttp.ClientTimeout(total=timeout)
                    async with session.request(method, url, params=params, timeout=timeout_obj) as resp:
                        if resp.status == 200:
                            return await resp.json(), 200
                        elif resp.status in [429, 500, 502, 503, 504]:
                            # 触发限速或服务器错误，稍后重试
                            wait = (attempt + 1) * 2
                            logger.warning(f"⚠️ TMDB 请求返回 {resp.status}, 正在进行第 {attempt+1} 次重试 (等待 {wait}s)...")
                            await asyncio.sleep(wait)
                            continue
                        else:
                            return None, resp.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                wait = (attempt + 1) * 2
                logger.warning(f"⚠️ TMDB 请求异常 ({type(e).__name__}), 正在进行第 {attempt+1} 次重试 (等待 {wait}s)...")
                await asyncio.sleep(wait)
        
        logger.error(f"❌ TMDB 请求多次重试失败: {last_error}")
        raise last_error if last_error else Exception("Request failed after retries")

    # ------------------------------------------------------------------ #
    #  爬取入口（智能路由）
    # ------------------------------------------------------------------ #

    async def fetch_movies_by_certification(self, country: str, certifications: List[str]) -> bool:
        """
        全量爬取电影 – 智能路由：
          - 有关键词过滤 + 有关键词列表  → 关键词 ID 模式（精准，快速）
          - 只有分级                      → 分级模式（全量）
          - 两者都有                      → 关键词 ID + 分级叠加过滤
        完成后更新 last_sync_at。
        """
        if self.is_fetching:
            return False

        self.is_fetching = True
        self._stop_flag = False
        self.fetch_progress = {
            "status": "running",
            "current": 0,
            "total": 0,
            "filtered": 0,
            "message": "正在初始化..."
        }

        try:
            config = await self.get_config()
            if not config or not config["api_key"]:
                raise ValueError("未配置 TMDB API Key")

            self.api_key = config["api_key"]
            use_keyword_filter = config.get("use_keyword_filter", False)
            keywords_str = config.get("keywords", "")

            # 判断走哪条路
            has_certifications = bool(certifications)
            has_keywords = use_keyword_filter and bool(keywords_str.strip())

            if has_keywords:
                # 关键词模式（精准）
                await self._fetch_by_keywords(keywords_str, country, certifications if has_certifications else [])
            elif has_certifications:
                # 纯分级模式（全量）
                await self._fetch_by_certifications(country, certifications)
            else:
                raise ValueError("请至少配置分级或关键词过滤之一")

            if self._stop_flag:
                self.fetch_progress["status"] = "stopped"
                self.fetch_progress["message"] = f"已停止，已保存 {self.fetch_progress['current']} 部"
                logger.warning("⚠️ 爬取任务已被用户停止")
            else:
                self.fetch_progress["status"] = "completed"
                saved = self.fetch_progress["current"]
                self.fetch_progress["message"] = f"完成，共保存 {saved} 部电影"
                logger.info(f"✅ 全量爬取完成，共保存 {saved} 部电影")
                # 全量完成后记录同步时间
                await self._update_last_sync_at()

            return True

        except Exception as e:
            logger.error(f"❌ 爬取失败: {e}")
            self.fetch_progress["status"] = "error"
            self.fetch_progress["message"] = f"错误: {str(e)}"
            return False
        finally:
            self.is_fetching = False

    async def incremental_sync(self, country: str) -> bool:
        """
        增量同步：按上映日期倒序爬取，遇到已存在记录即停止。
        适合在全量爬取过后，定期同步 TMDB 新增电影。
        """
        if self.is_fetching:
            return False

        self.is_fetching = True
        self._stop_flag = False
        self.fetch_progress = {
            "status": "running",
            "current": 0,
            "total": 0,
            "filtered": 0,
            "message": "正在初始化增量同步..."
        }

        try:
            config = await self.get_config()
            if not config or not config["api_key"]:
                raise ValueError("未配置 TMDB API Key")

            self.api_key = config["api_key"]
            keywords_str = config.get("keywords", "")
            last_sync_at = config.get("last_sync_at")

            if not keywords_str.strip():
                raise ValueError("请先配置关键词再执行增量同步")

            logger.info(f"🔄 开始增量同步，上次同步时间: {last_sync_at or '从未'}")
            self.fetch_progress["message"] = f"增量同步中（上次: {last_sync_at or '从未'}）..."

            # 解析关键词 ID
            keyword_names = [k.strip() for k in keywords_str.split(",") if k.strip()]
            self.fetch_progress["message"] = f"正在解析关键词 ID（共 {len(keyword_names)} 个）..."

            keyword_ids: List[int] = []
            for kw in keyword_names:
                if self._stop_flag:
                    break
                kid = await self._resolve_keyword_id(kw)
                if kid:
                    keyword_ids.append(kid)
                await asyncio.sleep(self.RATE_LIMIT_DELAY)

            if not keyword_ids:
                raise ValueError("所有关键词均未找到对应 TMDB ID")

            with_keywords = "|".join(str(kid) for kid in keyword_ids)

            # 增量分页爬取（按上映日期倒序，遇到已有记录停止）
            new_count = await self._paginate_incremental(with_keywords, country)

            if self._stop_flag:
                self.fetch_progress["status"] = "stopped"
                self.fetch_progress["message"] = f"已停止，本次新增 {new_count} 部"
                logger.warning("⚠️ 增量同步已被用户停止")
            else:
                self.fetch_progress["status"] = "completed"
                self.fetch_progress["message"] = f"增量同步完成，本次新增 {new_count} 部电影"
                logger.info(f"✅ 增量同步完成，新增 {new_count} 部电影")
                await self._update_last_sync_at()

            return True

        except Exception as e:
            logger.error(f"❌ 增量同步失败: {e}")
            self.fetch_progress["status"] = "error"
            self.fetch_progress["message"] = f"错误: {str(e)}"
            return False
        finally:
            self.is_fetching = False

    async def _paginate_incremental(self, with_keywords: str, country: str) -> int:
        """
        增量分页：按上映日期倒序，遇到数据库中已存在的 tmdb_id 则停止。
        返回本次新增数量。
        """
        page = 1
        total_pages = 1
        new_count = 0

        while page <= total_pages and not self._stop_flag:
            try:
                url = f"{self.BASE_URL}/discover/movie"
                params = {
                    "api_key": self.api_key,
                    "with_keywords": with_keywords,
                    "sort_by": "primary_release_date.desc",
                    "page": page
                }
                data, status = await self._request_with_retry(url, params)
                if status != 200:
                    logger.error(f"❌ 增量请求失败: HTTP {status}")
                    break

                total_pages = min(data.get("total_pages", 1), 500)
                movies = data.get("results", [])

                if page == 1:
                    total = data.get("total_results", 0)
                    self.fetch_progress["total"] = total
                    logger.info(f"🔄 增量模式共 {total} 条结果，按日期倒序检查新增...")

                self.fetch_progress["message"] = (
                    f"增量同步第 {page}/{total_pages} 页"
                    f"（新增 {new_count} 部）"
                )

                stop_early = False
                for movie in movies:
                    if self._stop_flag:
                        return new_count

                    tmdb_id = movie.get("id")
                    if not tmdb_id:
                        continue

                    # 检查是否已存在
                    already_exists = await self._movie_exists(tmdb_id)
                    if already_exists:
                        # 按日期倒序，遇到已有的就说明后面都是旧数据
                        logger.info(f"📌 遇到已存在记录 tmdb_id={tmdb_id}，增量同步结束")
                        stop_early = True
                        break

                    await self._save_movie_keyword_mode(movie, country)
                    new_count += 1
                    self.fetch_progress["current"] = new_count

                if stop_early:
                    return new_count

                page += 1
                await asyncio.sleep(self.RATE_LIMIT_DELAY)

            except Exception as e:
                logger.error(f"❌ 增量爬取第 {page} 页失败: {e}")
                break

        return new_count

    async def _movie_exists(self, tmdb_id: int) -> bool:
        """判断 tmdb_id 是否已在数据库中"""
        try:
            async for db in get_db():
                result = await db.execute(
                    select(SensitiveMovie.id).where(SensitiveMovie.tmdb_id == tmdb_id)
                )
                return result.scalar_one_or_none() is not None
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  关键词 ID 模式（核心优化）
    # ------------------------------------------------------------------ #

    async def _fetch_by_keywords(self, keywords_str: str, country: str, certifications: List[str]):
        """
        关键词 ID 模式：
        1. 把关键词名称批量解析为 TMDB keyword ID
        2. 用 with_keywords=ID1|ID2|... 调用 Discover API（OR 关系）
        3. 可选叠加分级过滤
        """
        keyword_names = [k.strip() for k in keywords_str.split(",") if k.strip()]
        logger.info(f"🔍 正在解析 {len(keyword_names)} 个关键词的 TMDB ID...")
        self.fetch_progress["message"] = f"正在解析关键词 ID（共 {len(keyword_names)} 个）..."

        # 批量查询关键词 ID
        keyword_ids: List[int] = []
        resolved: List[str] = []
        failed: List[str] = []

        for kw in keyword_names:
            if self._stop_flag:
                return
            kid = await self._resolve_keyword_id(kw)
            if kid:
                keyword_ids.append(kid)
                resolved.append(kw)
            else:
                failed.append(kw)
            # 解析关键词 ID 本身频率限制较低，稍微等待即可
            await asyncio.sleep(self.RATE_LIMIT_DELAY)

        if failed:
            logger.warning(f"⚠️ 以下关键词未找到对应 ID，将跳过: {', '.join(failed)}")
        if not keyword_ids:
            raise ValueError(f"所有关键词均未找到对应 TMDB ID，请检查关键词拼写")

        logger.info(f"✅ 成功解析 {len(keyword_ids)}/{len(keyword_names)} 个关键词 ID: {resolved}")

        # 构建 with_keywords 参数（| = OR）
        with_keywords = "|".join(str(kid) for kid in keyword_ids)

        # 获取总数
        total = await self._get_keyword_total(with_keywords, country, certifications)
        self.fetch_progress["total"] = total

        cert_info = f" + 分级 {certifications}" if certifications else ""
        logger.info(f"🎬 开始关键词精准爬取{cert_info}，共 {total} 部电影")
        self.fetch_progress["message"] = f"共找到 {total} 部电影，开始爬取..."

        # 分页爬取
        await self._paginate_discover(with_keywords, country, certifications)

    async def _resolve_keyword_id(self, keyword: str) -> Optional[int]:
        """将关键词名称解析为 TMDB keyword ID"""
        try:
            url = f"{self.BASE_URL}/search/keyword"
            params = {"api_key": self.api_key, "query": keyword}
            data, status = await self._request_with_retry(url, params)
            if status == 200:
                results = data.get("results", [])
                # 精确匹配（忽略大小写）
                for r in results:
                    if r.get("name", "").lower() == keyword.lower():
                        logger.debug(f"🔑 关键词 '{keyword}' → ID {r['id']}")
                        return r["id"]
                # 若无精确匹配，取第一个近似结果
                if results:
                    first = results[0]
                    logger.debug(f"🔑 关键词 '{keyword}' 近似匹配 '{first['name']}' → ID {first['id']}")
                    return first["id"]
            return None
        except Exception as e:
            logger.error(f"❌ 解析关键词 '{keyword}' 失败: {e}")
            return None

    async def _get_keyword_total(self, with_keywords: str, country: str, certifications: List[str]) -> int:
        """获取关键词模式下的电影总数"""
        try:
            url = f"{self.BASE_URL}/discover/movie"
            params = self._build_discover_params(with_keywords, country, certifications, page=1)
            data, status = await self._request_with_retry(url, params)
            if status == 200:
                return data.get("total_results", 0)
            return 0
        except Exception as e:
            logger.error(f"❌ 获取总数失败: {e}")
            return 0

    def _build_discover_params(self, with_keywords: str, country: str,
                               certifications: List[str], page: int) -> Dict:
        """构建 Discover API 参数"""
        params: Dict[str, Any] = {
            "api_key": self.api_key,
            "with_keywords": with_keywords,
            "sort_by": "popularity.desc",
            "page": page
        }
        if certifications and country:
            params["certification_country"] = country
            # 多个分级用 | 分隔（OR 关系）
            params["certification"] = "|".join(certifications)
        return params

    async def _paginate_discover(self, with_keywords: str, country: str, certifications: List[str]):
        """通用分页爬取（关键词模式）"""
        page = 1
        total_pages = 1

        while page <= total_pages and not self._stop_flag:
            try:
                url = f"{self.BASE_URL}/discover/movie"
                params = self._build_discover_params(with_keywords, country, certifications, page)
                data, status = await self._request_with_retry(url, params)
                if status != 200:
                    logger.error(f"❌ Discover 请求失败: HTTP {status}")
                    break

                total_pages = min(data.get("total_pages", 1), 500)  # TMDB 最多返回 500 页
                movies = data.get("results", [])

                self.fetch_progress["message"] = (
                    f"正在爬取第 {page}/{total_pages} 页"
                    f"（已保存 {self.fetch_progress['current']} 部）"
                )

                for movie in movies:
                    if self._stop_flag:
                        return
                    await self._save_movie_keyword_mode(movie, country)

                page += 1
                await asyncio.sleep(self.RATE_LIMIT_DELAY)

            except Exception as e:
                logger.error(f"❌ 爬取第 {page} 页失败: {e}")
                break

    async def _save_movie_keyword_mode(self, movie_data: Dict, country: str):
        """
        关键词模式下的保存逻辑：
        - 电影本身已经由 TMDB 关键词 ID 筛选过，直接保存
        - 合并 alternative_titles 和 chinese_title 为一次请求，减少 API 调用
        """
        try:
            tmdb_id = movie_data.get("id")
            if not tmdb_id:
                return

            # 3 个接口并发（alternative_titles 同时提取中文译名，不再单独请求）
            alt_result, movie_keywords, us_cert = await asyncio.gather(
                self._get_titles(tmdb_id),
                self._get_movie_keywords(tmdb_id),
                self._get_us_certification(tmdb_id)
            )
            alt_titles, chinese_title = alt_result

            async for db in get_db():
                result = await db.execute(
                    select(SensitiveMovie).where(SensitiveMovie.tmdb_id == tmdb_id)
                )
                existing = result.scalar_one_or_none()

                if existing:
                    existing.title = movie_data.get("title", "")
                    existing.original_title = movie_data.get("original_title")
                    existing.chinese_title = chinese_title
                    existing.alternative_titles = alt_titles
                    existing.release_date = movie_data.get("release_date")
                    existing.certification = us_cert or ""
                    existing.country = country
                    existing.overview = movie_data.get("overview")
                    existing.poster_path = movie_data.get("poster_path")
                    existing.keywords = movie_keywords
                else:
                    db.add(SensitiveMovie(
                        tmdb_id=tmdb_id,
                        title=movie_data.get("title", ""),
                        original_title=movie_data.get("original_title"),
                        chinese_title=chinese_title,
                        alternative_titles=alt_titles,
                        release_date=movie_data.get("release_date"),
                        certification=us_cert or "",
                        country=country,
                        overview=movie_data.get("overview"),
                        poster_path=movie_data.get("poster_path"),
                        keywords=movie_keywords
                    ))

                await db.commit()
                self.fetch_progress["current"] += 1
                logger.debug(f"✅ 保存: {movie_data.get('title')} | 分级: {us_cert} | 关键词: {', '.join(movie_keywords[:3])}")

            # 每部电影处理完后等待，避免触发 TMDB 限速
            await asyncio.sleep(self.MOVIE_DELAY)

        except Exception as e:
            logger.error(f"❌ 保存电影 {movie_data.get('title')} 失败: {e}")

    # ------------------------------------------------------------------ #
    #  纯分级模式（原有逻辑，保留兼容）
    # ------------------------------------------------------------------ #

    async def _fetch_by_certifications(self, country: str, certifications: List[str]):
        """纯分级模式：按分级拉取所有电影"""
        total_results = 0
        for cert in certifications:
            if self._stop_flag:
                return
            count = await self._get_cert_total(country, cert)
            total_results += count

        self.fetch_progress["total"] = total_results
        logger.info(f"🎬 开始分级模式爬取 {country} {certifications}，共 {total_results} 部")
        self.fetch_progress["message"] = f"共 {total_results} 部电影，开始爬取..."

        for cert in certifications:
            if self._stop_flag:
                return
            await self._paginate_by_cert(country, cert)

    async def _get_cert_total(self, country: str, certification: str) -> int:
        try:
            url = f"{self.BASE_URL}/discover/movie"
            params = {
                "api_key": self.api_key,
                "certification_country": country,
                "certification": certification,
                "sort_by": "popularity.desc",
                "page": 1
            }
            data, status = await self._request_with_retry(url, params)
            if status == 200:
                return data.get("total_results", 0)
            return 0
        except Exception as e:
            logger.error(f"❌ 获取分级总数失败: {e}")
            return 0

    async def _paginate_by_cert(self, country: str, certification: str):
        page = 1
        total_pages = 1
        while page <= total_pages and not self._stop_flag:
            try:
                url = f"{self.BASE_URL}/discover/movie"
                params = {
                    "api_key": self.api_key,
                    "certification_country": country,
                    "certification": certification,
                    "sort_by": "popularity.desc",
                    "page": page
                }
                data, status = await self._request_with_retry(url, params)
                if status != 200:
                    logger.error(f"❌ 请求失败: HTTP {status}")
                    break
                total_pages = data.get("total_pages", 1)
                movies = data.get("results", [])

                self.fetch_progress["message"] = (
                    f"分级 {certification}：第 {page}/{total_pages} 页"
                    f"（已保存 {self.fetch_progress['current']} 部）"
                )

                for movie in movies:
                    if self._stop_flag:
                        return
                    await self._save_movie_cert_mode(movie, country, certification)

                page += 1
                await asyncio.sleep(self.RATE_LIMIT_DELAY)
            except Exception as e:
                logger.error(f"❌ 分级爬取第 {page} 页失败: {e}")
                break

    async def _save_movie_cert_mode(self, movie_data: Dict, country: str, certification: str):
        """分级模式保存"""
        try:
            tmdb_id = movie_data.get("id")
            if not tmdb_id:
                return

            alt_result, movie_keywords = await asyncio.gather(
                self._get_titles(tmdb_id),
                self._get_movie_keywords(tmdb_id)
            )
            alt_titles, chinese_title = alt_result

            async for db in get_db():
                result = await db.execute(
                    select(SensitiveMovie).where(SensitiveMovie.tmdb_id == tmdb_id)
                )
                existing = result.scalar_one_or_none()

                if existing:
                    existing.title = movie_data.get("title", "")
                    existing.original_title = movie_data.get("original_title")
                    existing.chinese_title = chinese_title
                    existing.alternative_titles = alt_titles
                    existing.release_date = movie_data.get("release_date")
                    existing.certification = certification
                    existing.country = country
                    existing.overview = movie_data.get("overview")
                    existing.poster_path = movie_data.get("poster_path")
                    existing.keywords = movie_keywords
                else:
                    db.add(SensitiveMovie(
                        tmdb_id=tmdb_id,
                        title=movie_data.get("title", ""),
                        original_title=movie_data.get("original_title"),
                        chinese_title=chinese_title,
                        alternative_titles=alt_titles,
                        release_date=movie_data.get("release_date"),
                        certification=certification,
                        country=country,
                        overview=movie_data.get("overview"),
                        poster_path=movie_data.get("poster_path"),
                        keywords=movie_keywords
                    ))

                await db.commit()
                self.fetch_progress["current"] += 1

            await asyncio.sleep(self.MOVIE_DELAY)

        except Exception as e:
            logger.error(f"❌ 保存电影 {movie_data.get('title')} 失败: {e}")

    # ------------------------------------------------------------------ #
    #  底层 API 辅助
    # ------------------------------------------------------------------ #

    async def _get_titles(self, tmdb_id: int) -> tuple[List[str], Optional[str]]:
        """
        一次请求同时获取所有别名列表和中文译名，避免重复调用同一接口。
        返回 (alt_titles, chinese_title)
        """
        try:
            url = f"{self.BASE_URL}/movie/{tmdb_id}/alternative_titles"
            data, status = await self._request_with_retry(url, params={"api_key": self.api_key})
            if status == 200:
                titles = data.get("titles", [])
                alt_titles = [t["title"] for t in titles if t.get("title")]
                chinese_title = next(
                    (t["title"] for t in titles if t.get("iso_3166_1") in ["CN", "TW", "HK"]),
                    None
                )
                return alt_titles, chinese_title
            return [], None
        except Exception as e:
            logger.error(f"❌ 获取别名失败: {e}")
            return [], None

    async def _get_movie_keywords(self, tmdb_id: int) -> List[str]:
        """获取电影关键词"""
        try:
            url = f"{self.BASE_URL}/movie/{tmdb_id}/keywords"
            data, status = await self._request_with_retry(url, params={"api_key": self.api_key})
            if status == 200:
                return [k["name"] for k in data.get("keywords", []) if k.get("name")]
            return []
        except Exception as e:
            logger.error(f"❌ 获取关键词失败: {e}")
            return []

    async def _get_us_certification(self, tmdb_id: int) -> Optional[str]:
        """获取美国分级"""
        try:
            url = f"{self.BASE_URL}/movie/{tmdb_id}/release_dates"
            data, status = await self._request_with_retry(url, params={"api_key": self.api_key})
            if status == 200:
                results = data.get("results", [])
                for r in results:
                    if r.get("iso_3166_1") == "US":
                        for rd in r.get("release_dates", []):
                            cert = rd.get("certification", "").strip()
                            if cert:
                                return cert
            return None
        except Exception as e:
            logger.error(f"❌ 获取美国分级失败: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  数据库操作
    # ------------------------------------------------------------------ #

    async def get_movies(self, page: int = 1, page_size: int = 20, search: str = "",
                         sort_field: str = "created_at", sort_order: str = "desc") -> Dict:
        """获取电影列表（分页 + 搜索 + 排序）"""
        try:
            async for db in get_db():
                def _where(q):
                    if search:
                        # 支持按 tmdb_id 搜索（纯数字时）
                        if search.isdigit():
                            return q.where(
                                SensitiveMovie.title.contains(search) |
                                SensitiveMovie.original_title.contains(search) |
                                SensitiveMovie.chinese_title.contains(search) |
                                (SensitiveMovie.tmdb_id == int(search))
                            )
                        return q.where(
                            SensitiveMovie.title.contains(search) |
                            SensitiveMovie.original_title.contains(search) |
                            SensitiveMovie.chinese_title.contains(search)
                        )
                    return q

                # 排序字段映射
                sort_col_map = {
                    "tmdb_id": SensitiveMovie.tmdb_id,
                    "title": SensitiveMovie.title,
                    "chinese_title": SensitiveMovie.chinese_title,
                    "original_title": SensitiveMovie.original_title,
                    "release_date": SensitiveMovie.release_date,
                    "created_at": SensitiveMovie.created_at,
                }
                sort_col = sort_col_map.get(sort_field, SensitiveMovie.created_at)
                order_expr = sort_col.asc() if sort_order == "asc" else sort_col.desc()

                total = (await db.execute(_where(select(func.count()).select_from(SensitiveMovie)))).scalar()
                movies = (await db.execute(
                    _where(select(SensitiveMovie))
                    .order_by(order_expr)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )).scalars().all()

                return {
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "items": [
                        {
                            "id": m.id,
                            "tmdb_id": m.tmdb_id,
                            "title": m.title,
                            "original_title": m.original_title,
                            "chinese_title": m.chinese_title,
                            "alternative_titles": m.alternative_titles,
                            "release_date": m.release_date,
                            "certification": m.certification,
                            "country": m.country,
                            "overview": m.overview,
                            "poster_path": m.poster_path,
                            "keywords": m.keywords,
                            "created_at": m.created_at.isoformat() if m.created_at else None
                        }
                        for m in movies
                    ]
                }
        except Exception as e:
            logger.error(f"❌ 获取电影列表失败: {e}")
            return {"total": 0, "page": page, "page_size": page_size, "items": []}

    async def delete_movie(self, movie_id: int) -> bool:
        """删除单条电影记录"""
        try:
            async for db in get_db():
                result = await db.execute(select(SensitiveMovie).where(SensitiveMovie.id == movie_id))
                movie = result.scalar_one_or_none()
                if movie:
                    await db.delete(movie)
                    await db.commit()
                    logger.info(f"🗑️ 已删除电影: {movie.title}")
                    return True
                return False
        except Exception as e:
            logger.error(f"❌ 删除电影失败: {e}")
            return False

    async def clear_all_movies(self) -> bool:
        """清空所有电影数据"""
        try:
            async for db in get_db():
                await db.execute(delete(SensitiveMovie))
                await db.commit()
                logger.info("🗑️ 已清空所有敏感电影数据")
                return True
        except Exception as e:
            logger.error(f"❌ 清空数据失败: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  控制
    # ------------------------------------------------------------------ #

    def stop_fetching(self):
        """停止爬取任务"""
        self._stop_flag = True
        logger.info("⏹️ 正在停止爬取任务...")

    def get_progress(self) -> Dict:
        """获取爬取进度"""
        return self.fetch_progress.copy()

    # ------------------------------------------------------------------ #
    #  别名获取与验证
    # ------------------------------------------------------------------ #

    async def _ensure_api_key(self) -> Optional[str]:
        """确保 API Key 已加载"""
        from app.core.config import settings
        if settings.TMDB_API_KEY:
            self.api_key = settings.TMDB_API_KEY
        return self.api_key

    @staticmethod
    def _contains_chinese(text: str) -> bool:
        return any('\u4e00' <= char <= '\u9fff' for char in (text or ""))

    @staticmethod
    def _contains_english(text: str) -> bool:
        return bool(re.search(r"[A-Za-z]", text or ""))

    @classmethod
    def _build_media_query_order(cls, preferred_media: Optional[str]) -> List[str]:
        if preferred_media == "tv":
            return ["tv", "movie"]
        if preferred_media == "movie":
            return ["movie", "tv"]
        # 未知类型时优先按剧集尝试，降低剧集误识别为电影的概率
        return ["tv", "movie"]

    async def _fetch_english_alias(self, tmdb_id: int, media_type: str, params: Dict[str, Any]) -> Optional[str]:
        alt_url = f"{self.BASE_URL}/{media_type}/{tmdb_id}/alternative_titles"
        alt_data, alt_status = await self._request_with_retry(alt_url, params, max_retries=2)
        if not alt_data or alt_status != 200:
            return None

        titles = alt_data.get("titles", []) if media_type == "movie" else alt_data.get("results", [])

        # 第一优先级：US/GB 且包含英文字符
        for t in titles:
            title = (t.get("title") or "").strip()
            if not title:
                continue
            if t.get("iso_3166_1") in ["US", "GB"] and self._contains_english(title) and not self._contains_chinese(title):
                return title

        # 第二优先级：任意非中文且包含英文字符
        for t in titles:
            title = (t.get("title") or "").strip()
            if not title:
                continue
            if self._contains_english(title) and not self._contains_chinese(title):
                return title

        return None

    async def get_alias_by_id(self, tmdb_id: int, preferred_media: Optional[str] = None) -> Optional[str]:
        """根据 TMDB ID 获取替换名（支持媒体类型提示与 tv/movie 回退）。"""
        api_key = await self._ensure_api_key()
        if not api_key:
            logger.warning("⚠️ TMDB API Key 未配置，跳过别名替换")
            return None

        params = {"api_key": api_key}
        media_order = self._build_media_query_order(preferred_media)

        detail = None
        detail_media = None
        status = None
        for media_type in media_order:
            url = f"{self.BASE_URL}/{media_type}/{tmdb_id}"
            d, s = await self._request_with_retry(url, params, max_retries=2)
            if d and s == 200:
                detail = d
                detail_media = media_type
                break
            status = s

        if not detail or not detail_media:
            logger.warning(
                f"⚠️ 无法获取 TMDB ID {tmdb_id} 的详情 (preferred={preferred_media}, last_http={status})"
            )
            return None

        original_title = (detail.get("original_title") if detail_media == "movie" else detail.get("original_name") or "").strip()
        english_alias = await self._fetch_english_alias(tmdb_id, detail_media, params)

        # 名称策略：
        # 1) 原名本身英文 -> 直接使用原名
        # 2) 原名非英文且非中文 -> 优先英文别名，没有则原名
        # 3) 原名中文 -> 优先英文别名，没有则返回 None（让上层走拼音）
        alias = None
        source = ""

        if original_title and self._contains_english(original_title) and not self._contains_chinese(original_title):
            alias = original_title
            source = "original_english"
        elif original_title and (not self._contains_english(original_title)) and (not self._contains_chinese(original_title)):
            if english_alias:
                alias = english_alias
                source = "english_alias"
            else:
                alias = original_title
                source = "original_non_chinese"
        else:
            if english_alias:
                alias = english_alias
                source = "english_alias"

        if not alias:
            logger.warning(
                f"⚠️ TMDB ID {tmdb_id} ({detail_media}) 原名 [{original_title}] 未能提取到可用英文名，交由上层兜底"
            )
            return None

        if self._contains_chinese(alias):
            logger.warning(f"⚠️ TMDB ID {tmdb_id} 获取的别名 [{alias}] 含有中文字符，判定为替换失败")
            return None

        logger.info(
            f"🎉 成功获取 TMDB ID {tmdb_id} 的替换名: [{alias}] (media={detail_media}, source={source}, preferred={preferred_media})"
        )
        return alias


tmdb_service = TMDBService()

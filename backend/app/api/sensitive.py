from fastapi import APIRouter, Depends, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, List
from app.api.auth import get_current_user
from app.services.tmdb_service import tmdb_service
from loguru import logger

router = APIRouter(prefix="/sensitive", tags=["sensitive"])


class TMDBConfigUpdate(BaseModel):
    api_key: str
    country: str = "US"
    certifications: List[str] = ["R", "NC-17"]
    keywords: str = ""
    use_keyword_filter: bool = False


class TestConnectionRequest(BaseModel):
    api_key: str


class FetchRequest(BaseModel):
    country: str = "US"
    certifications: List[str] = ["R", "NC-17"]


@router.get("/config")
async def get_config(user=Depends(get_current_user)):
    """获取 TMDB 配置"""
    config = await tmdb_service.get_config()
    if config:
        return {"status": "success", "data": config}
    return {"status": "success", "data": {
        "api_key": "",
        "country": "US",
        "certifications": ["R", "NC-17"],
        "keywords": "",
        "use_keyword_filter": False
    }}


@router.post("/config")
async def save_config(cfg: TMDBConfigUpdate, user=Depends(get_current_user)):
    """保存 TMDB 配置"""
    success = await tmdb_service.save_config(
        cfg.api_key,
        cfg.country,
        cfg.certifications,
        cfg.keywords,
        cfg.use_keyword_filter
    )
    if success:
        return {"status": "success", "message": "配置已保存"}
    return {"status": "error", "message": "保存失败"}


@router.post("/test-connection")
async def test_connection(req: TestConnectionRequest, user=Depends(get_current_user)):
    """测试 TMDB API 连接"""
    success, message = await tmdb_service.test_connection(req.api_key)
    return {"status": "success" if success else "error", "message": message}


@router.post("/fetch")
async def fetch_movies(req: FetchRequest, background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    """开始全量爬取电影"""
    if tmdb_service.is_fetching:
        return {"status": "error", "message": "已有爬取任务正在运行"}

    # 在后台任务中执行爬取
    background_tasks.add_task(
        tmdb_service.fetch_movies_by_certification,
        req.country,
        req.certifications
    )

    logger.info(f"🚀 开始全量爬取 {req.country} {req.certifications} 分级电影")
    return {"status": "success", "message": "爬取任务已启动"}


@router.post("/incremental-sync")
async def incremental_sync(background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    """增量同步：按上映日期倒序，遇到已有记录即停止"""
    if tmdb_service.is_fetching:
        return {"status": "error", "message": "已有爬取任务正在运行"}

    background_tasks.add_task(
        tmdb_service.incremental_sync,
        "US"
    )

    logger.info("🔄 开始增量同步")
    return {"status": "success", "message": "增量同步已启动"}


@router.post("/stop")
async def stop_fetching(user=Depends(get_current_user)):
    """停止爬取任务"""
    tmdb_service.stop_fetching()
    return {"status": "success", "message": "正在停止爬取任务"}


@router.get("/status")
async def get_status(user=Depends(get_current_user)):
    """获取爬取任务状态"""
    progress = tmdb_service.get_progress()
    return {"status": "success", "data": progress}


@router.get("/movies")
async def get_movies(
    page: int = 1,
    page_size: int = 20,
    search: str = "",
    sort_field: str = "created_at",
    sort_order: str = "desc",
    user=Depends(get_current_user)
):
    """获取电影列表"""
    result = await tmdb_service.get_movies(page, page_size, search, sort_field, sort_order)
    return {"status": "success", "data": result}


@router.delete("/movies/{movie_id}")
async def delete_movie(movie_id: int, user=Depends(get_current_user)):
    """删除单个电影"""
    success = await tmdb_service.delete_movie(movie_id)
    if success:
        return {"status": "success", "message": "删除成功"}
    return {"status": "error", "message": "删除失败"}


@router.post("/clear")
async def clear_all_movies(user=Depends(get_current_user)):
    """清空所有电影数据"""
    success = await tmdb_service.clear_all_movies()
    if success:
        return {"status": "success", "message": "已清空所有数据"}
    return {"status": "error", "message": "清空失败"}

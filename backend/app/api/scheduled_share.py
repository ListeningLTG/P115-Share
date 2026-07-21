"""
定时分享任务管理 API
"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from typing import Optional, List
from sqlalchemy import select
from app.api.auth import get_current_user
from app.core.database import async_session
from app.models.schema import ScheduledShareTask

router = APIRouter(prefix="/scheduled-share", tags=["scheduled-share"])


class ScheduledShareTaskCreate(BaseModel):
    name: str
    account_id: int
    dir_path: str
    cron_expression: str
    clear_files: bool = True
    share_mode: str = "move"
    cleanup_temp_dir: bool = True
    min_size: float = 0.0
    min_size_unit: str = "GB"
    target_channels: List[str] = []
    enabled: bool = True
    sensitive_replace_enabled: bool = False
    sensitive_replace_pinyin: bool = False
    sensitive_replace_tmdb: bool = False

    @field_validator('min_size', mode='before')
    @classmethod
    def coerce_min_size(cls, v):
        if v is None or v == "":
            return 0.0
        return v


class ScheduledShareTaskUpdate(BaseModel):
    name: Optional[str] = None
    account_id: Optional[int] = None
    dir_path: Optional[str] = None
    cron_expression: Optional[str] = None
    clear_files: Optional[bool] = None
    share_mode: Optional[str] = None
    cleanup_temp_dir: Optional[bool] = None
    min_size: Optional[float] = None
    min_size_unit: Optional[str] = None
    target_channels: Optional[List[str]] = None
    enabled: Optional[bool] = None
    sensitive_replace_enabled: Optional[bool] = None
    sensitive_replace_pinyin: Optional[bool] = None
    sensitive_replace_tmdb: Optional[bool] = None

    @field_validator('min_size', mode='before')
    @classmethod
    def coerce_min_size(cls, v):
        if v is None or v == "":
            return 0.0
        return v


@router.get("/")
async def list_tasks(user=Depends(get_current_user)):
    """获取所有定时分享任务列表"""
    async with async_session() as session:
        result = await session.execute(select(ScheduledShareTask).order_by(ScheduledShareTask.id.desc()))
        tasks = result.scalars().all()
        return {
            "state": True,
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "account_id": t.account_id,
                    "dir_path": t.dir_path,
                    "cron_expression": t.cron_expression,
                    "clear_files": t.clear_files,
                    "share_mode": t.share_mode or ("move" if t.clear_files else "copy"),
                    "cleanup_temp_dir": t.cleanup_temp_dir,
                    "min_size": t.min_size,
                    "min_size_unit": t.min_size_unit,
                    "target_channels": t.target_channels,
                    "enabled": t.enabled,
                    "status": t.status,
                    "last_run_at": (t.last_run_at.isoformat() + "Z") if t.last_run_at else None,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "sensitive_replace_enabled": t.sensitive_replace_enabled,
                    "sensitive_replace_pinyin": t.sensitive_replace_pinyin,
                    "sensitive_replace_tmdb": t.sensitive_replace_tmdb,
                }
                for t in tasks
            ]
        }


@router.post("/")
async def create_task(data: ScheduledShareTaskCreate, user=Depends(get_current_user)):
    """创建定时分享任务"""
    # 校验 cron 表达式
    from apscheduler.triggers.cron import CronTrigger
    try:
        CronTrigger.from_crontab(data.cron_expression)
    except Exception as e:
        return {"state": False, "message": f"Cron 表达式格式错误: {e}"}

    # Reconcile share_mode and clear_files:
    share_mode = data.share_mode
    clear_files = data.clear_files
    if share_mode == "move" and not clear_files:
        share_mode = "copy"
    elif share_mode == "direct":
        clear_files = False
    elif share_mode == "copy":
        clear_files = False
    elif share_mode == "move":
        clear_files = True

    async with async_session() as session:
        task = ScheduledShareTask(
            name=data.name,
            account_id=data.account_id,
            dir_path=data.dir_path,
            cron_expression=data.cron_expression,
            clear_files=clear_files,
            share_mode=share_mode,
            cleanup_temp_dir=data.cleanup_temp_dir,
            min_size=data.min_size,
            min_size_unit=data.min_size_unit,
            target_channels=data.target_channels,
            enabled=data.enabled,
            status="waiting" if data.enabled else "disabled",
            sensitive_replace_enabled=data.sensitive_replace_enabled,
            sensitive_replace_pinyin=data.sensitive_replace_pinyin,
            sensitive_replace_tmdb=data.sensitive_replace_tmdb
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        # 同步调度器中的任务
        from app.services.scheduler import cleanup_scheduler
        cleanup_scheduler.sync_scheduled_share_job(task)

        return {"state": True, "message": "创建定时任务成功", "data": {"id": task.id}}


@router.put("/{task_id}")
async def update_task(task_id: int, data: ScheduledShareTaskUpdate, user=Depends(get_current_user)):
    """更新定时分享任务"""
    if data.cron_expression is not None:
        from apscheduler.triggers.cron import CronTrigger
        try:
            CronTrigger.from_crontab(data.cron_expression)
        except Exception as e:
            return {"state": False, "message": f"Cron 表达式格式错误: {e}"}

    async with async_session() as session:
        task = await session.get(ScheduledShareTask, task_id)
        if not task:
            return {"state": False, "message": "任务不存在"}

        # 更新字段
        update_dict = data.model_dump(exclude_unset=True)
        # Reconcile share_mode and clear_files if either is updated
        if "share_mode" in update_dict or "clear_files" in update_dict:
            curr_share_mode = update_dict.get("share_mode", task.share_mode or ("move" if task.clear_files else "copy"))
            curr_clear_files = update_dict.get("clear_files", task.clear_files)
            
            if curr_share_mode == "move" and not curr_clear_files:
                curr_share_mode = "copy"
            elif curr_share_mode == "direct":
                curr_clear_files = False
            elif curr_share_mode == "copy":
                curr_clear_files = False
            elif curr_share_mode == "move":
                curr_clear_files = True
                
            update_dict["share_mode"] = curr_share_mode
            update_dict["clear_files"] = curr_clear_files

        for field, val in update_dict.items():
            setattr(task, field, val)

        if data.enabled is not None:
            task.status = "waiting" if task.enabled else "disabled"

        await session.commit()
        await session.refresh(task)

        # 同步调度器中的任务
        from app.services.scheduler import cleanup_scheduler
        cleanup_scheduler.sync_scheduled_share_job(task)

        return {"state": True, "message": "更新定时任务成功"}


@router.delete("/{task_id}")
async def delete_task(task_id: int, user=Depends(get_current_user)):
    """删除定时分享任务"""
    async with async_session() as session:
        task = await session.get(ScheduledShareTask, task_id)
        if not task:
            return {"state": False, "message": "任务不存在"}

        # 禁用并从调度器中移除
        task.enabled = False
        from app.services.scheduler import cleanup_scheduler
        cleanup_scheduler.sync_scheduled_share_job(task)

        await session.delete(task)
        await session.commit()

        return {"state": True, "message": "删除定时任务成功"}


@router.post("/{task_id}/toggle")
async def toggle_task(task_id: int, user=Depends(get_current_user)):
    """启用/禁用定时任务"""
    async with async_session() as session:
        task = await session.get(ScheduledShareTask, task_id)
        if not task:
            return {"state": False, "message": "任务不存在"}

        task.enabled = not task.enabled
        task.status = "waiting" if task.enabled else "disabled"

        await session.commit()
        await session.refresh(task)

        # 同步调度器中的任务
        from app.services.scheduler import cleanup_scheduler
        cleanup_scheduler.sync_scheduled_share_job(task)

        return {"state": True, "message": f"任务已{'启用' if task.enabled else '禁用'}", "data": {
            "id": task.id,
            "enabled": task.enabled,
            "status": task.status
        }}


@router.post("/{task_id}/trigger")
async def trigger_task(task_id: int, user=Depends(get_current_user)):
    """手动触发定时分享任务（后台异步执行）"""
    async with async_session() as session:
        task = await session.get(ScheduledShareTask, task_id)
        if not task:
            return {"state": False, "message": "任务不存在"}

        if not task.enabled:
            return {"state": False, "message": "任务未启用，无法触发"}

        # 异步启动任务
        from app.services.scheduler import _run_scheduled_share_task
        asyncio.create_task(_run_scheduled_share_task(task_id))

        return {"state": True, "message": "任务已手动触发，后台异步执行中"}

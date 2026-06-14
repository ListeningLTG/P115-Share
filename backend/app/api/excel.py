import json
from typing import List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from sqlalchemy import select, desc
from app.core.database import async_session
from app.models.schema import ExcelTask, ExcelTaskItem
from app.services.excel_batch import excel_batch_service
from app.services.p115 import p115_service
from app.api.auth import get_current_user

router = APIRouter(prefix="/excel", tags=["excel"])

class StartTaskRequest(BaseModel):
    skip_count: Optional[int] = 0
    stop_row: Optional[int] = 0
    interval_min: Optional[int] = 5
    interval_max: Optional[int] = 10
    target_channels: Optional[List[str]] = None
    white_list_keywords: Optional[str] = None
    black_list_keywords: Optional[str] = None
    skip_large_package: Optional[bool] = False
    strategy: Optional[str] = "transfer"
    force: Optional[bool] = False
    target_account_id: Optional[int] = None
    target_dir: Optional[str] = None

@router.post("/parse")
async def parse_excel(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    content = await file.read()
    try:
        result = await excel_batch_service.parse_file(content, file.filename)
        return {"status": "success", "data": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/tasks")
async def create_task(
    filename: str = Form(...),
    mapping: str = Form(...), # JSON string
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    content = await file.read()
    try:
        mapping_dict = json.loads(mapping)
        task_id = await excel_batch_service.create_task(filename, mapping_dict, content)
        return {"status": "success", "task_id": task_id}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/tasks")
async def list_tasks(current_user: dict = Depends(get_current_user)):
    from sqlalchemy import func
    async with async_session() as session:
        # 使用子查询统计每条任务的跳过数量
        stmt = select(
            ExcelTask,
            select(func.count(ExcelTaskItem.id)).where(
                ExcelTaskItem.task_id == ExcelTask.id,
                ExcelTaskItem.status == "跳过"
            ).scalar_subquery().label("skipped_count")
        ).order_by(desc(ExcelTask.created_at))
        
        result = await session.execute(stmt)
        tasks_with_counts = result.all()
        
        # 将结果转换为字典以包含额外的字段
        response_data = []
        for row in tasks_with_counts:
            task = row[0]
            skipped_count = row[1]
            task_dict = {c.name: getattr(task, c.name) for c in task.__table__.columns}
            task_dict["skipped_count"] = skipped_count
            response_data.append(task_dict)
            
        return {"status": "success", "data": response_data}

@router.get("/tasks/{task_id}")
async def get_task_detail(task_id: int, current_user: dict = Depends(get_current_user)):
    from sqlalchemy import func
    async with async_session() as session:
        stmt = select(
            ExcelTask,
            select(func.count(ExcelTaskItem.id)).where(
                ExcelTaskItem.task_id == ExcelTask.id,
                ExcelTaskItem.status == "跳过"
            ).scalar_subquery().label("skipped_count")
        ).where(ExcelTask.id == task_id)
        
        result = await session.execute(stmt)
        row = result.first()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
            
        task = row[0]
        skipped_count = row[1]
        task_dict = {c.name: getattr(task, c.name) for c in task.__table__.columns}
        task_dict["skipped_count"] = skipped_count
        
        return {"status": "success", "data": task_dict}

@router.get("/tasks/{task_id}/items")
async def get_task_items(
    task_id: int, 
    page: int = 1, 
    page_size: int = 50,
    status: str = None,
    current_user: dict = Depends(get_current_user)
):
    async with async_session() as session:
        query = select(ExcelTaskItem).where(ExcelTaskItem.task_id == task_id)
        if status:
            query = query.where(ExcelTaskItem.status == status)
        
        query = query.order_by(ExcelTaskItem.row_index)
        
        # Count total
        from sqlalchemy import func
        count_query = select(func.count()).select_from(query.subquery())
        total = await session.scalar(count_query)
        
        # Pagination
        query = query.offset((page - 1) * page_size).limit(page_size)
        result = await session.execute(query)
        items = result.scalars().all()
        
        # Add message_time from metadata to items
        data_with_time = []
        for item in items:
            item_dict = {c.name: getattr(item, c.name) for c in item.__table__.columns}
            if item.item_metadata and 'msg_date' in item.item_metadata:
                item_dict['message_time'] = item.item_metadata['msg_date']
            else:
                item_dict['message_time'] = ""
            data_with_time.append(item_dict)

        return {
            "status": "success", 
            "data": data_with_time,
            "total": total,
            "page": page,
            "page_size": page_size
        }

@router.post("/tasks/{task_id}/start")
async def start_task(task_id: int, req: StartTaskRequest, current_user: dict = Depends(get_current_user)):
    if p115_service.is_restricted and not req.force:
        raise HTTPException(status_code=400, detail="115 账号当前处于受限状态，请等候恢复或点击强制继续触发探路检测。")
    
    if req.force:
        await p115_service.clear_restriction()

    await excel_batch_service.start_task(
        task_id,
        req.skip_count,
        req.stop_row,
        req.interval_min,
        req.interval_max,
        req.target_channels,
        req.white_list_keywords,
        req.black_list_keywords,
        req.skip_large_package,
        req.strategy,
        req.target_account_id,
        req.target_dir
    )
    return {"status": "success"}

@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: int, current_user: dict = Depends(get_current_user)):
    await excel_batch_service.pause_task(task_id)
    return {"status": "success"}

@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: int, current_user: dict = Depends(get_current_user)):
    await excel_batch_service.cancel_task(task_id)
    return {"status": "success"}

@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int, current_user: dict = Depends(get_current_user)):
    await excel_batch_service.delete_task(task_id)
    return {"status": "success"}

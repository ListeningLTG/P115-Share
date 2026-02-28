import json
from typing import List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from sqlalchemy import select, desc
from app.core.database import async_session
from app.models.schema import ExcelTask, ExcelTaskItem
from app.services.excel_batch import excel_batch_service
from app.api.auth import get_current_user

router = APIRouter(prefix="/excel", tags=["excel"])

class StartTaskRequest(BaseModel):
    skip_count: Optional[int] = 0
    interval_min: Optional[int] = 5
    interval_max: Optional[int] = 10
    target_channels: Optional[List[str]] = None
    white_list_keywords: Optional[str] = None
    black_list_keywords: Optional[str] = None
    skip_large_package: Optional[bool] = False

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
    async with async_session() as session:
        result = await session.execute(select(ExcelTask).order_by(desc(ExcelTask.created_at)))
        tasks = result.scalars().all()
        return {"status": "success", "data": tasks}

@router.get("/tasks/{task_id}")
async def get_task_detail(task_id: int, current_user: dict = Depends(get_current_user)):
    async with async_session() as session:
        result = await session.execute(select(ExcelTask).where(ExcelTask.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"status": "success", "data": task}

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
        
        return {
            "status": "success", 
            "data": items,
            "total": total,
            "page": page,
            "page_size": page_size
        }

@router.post("/tasks/{task_id}/start")
async def start_task(task_id: int, req: StartTaskRequest, current_user: dict = Depends(get_current_user)):
    await excel_batch_service.start_task(
        task_id, 
        req.skip_count, 
        req.interval_min, 
        req.interval_max, 
        req.target_channels,
        req.white_list_keywords,
        req.black_list_keywords,
        req.skip_large_package
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

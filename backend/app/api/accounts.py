"""
115网盘账号管理 API
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from app.api.auth import get_current_user
from app.services.account_manager import account_manager
from loguru import logger

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    name: str = "新账号"
    cookie: str = ""
    save_dir: str = "115-Share"
    recycle_password: str = ""
    priority: int = 10
    enabled: bool = True
    save_file_limit: int = 500
    share_file_limit: int = 10000


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    cookie: Optional[str] = None
    save_dir: Optional[str] = None
    recycle_password: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    save_file_limit: Optional[int] = None
    share_file_limit: Optional[int] = None


class LoadBalanceUpdate(BaseModel):
    enabled: bool


@router.get("/")
async def list_accounts(user=Depends(get_current_user)):
    """获取所有账号列表"""
    accounts = await account_manager.get_accounts_list()
    return {
        "state": True,
        "accounts": accounts,
        "load_balance_enabled": account_manager.load_balance_enabled,
    }


@router.post("/")
async def create_account(data: AccountCreate, user=Depends(get_current_user)):
    """创建新账号"""
    result = await account_manager.create_account(data.model_dump())
    return {"state": True, "message": "账号创建成功", "data": result}


@router.put("/{account_id}")
async def update_account(account_id: int, data: AccountUpdate, user=Depends(get_current_user)):
    """更新账号信息"""
    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        return {"state": True, "message": "无变更"}
    ok = await account_manager.update_account(account_id, update_data)
    if ok:
        return {"state": True, "message": "账号更新成功"}
    return {"state": False, "message": "账号不存在"}


@router.delete("/{account_id}")
async def delete_account(account_id: int, user=Depends(get_current_user)):
    """删除账号（级联删除分析结果）"""
    ok = await account_manager.delete_account(account_id)
    if ok:
        return {"state": True, "message": "账号已删除，关联数据已清理"}
    return {"state": False, "message": "账号不存在"}


@router.post("/{account_id}/test")
async def test_account(account_id: int, user=Depends(get_current_user)):
    """测试账号连接"""
    result = await account_manager.test_account_connection(account_id)
    return {"state": result["success"], "message": result["message"]}


@router.post("/load-balance")
async def set_load_balance(data: LoadBalanceUpdate, user=Depends(get_current_user)):
    """设置负载均衡开关"""
    await account_manager.set_load_balance(data.enabled)
    status = "已开启" if data.enabled else "已关闭"
    logger.info(f"⚖️ 负载均衡 {status}")
    return {"state": True, "message": f"负载均衡{status}"}

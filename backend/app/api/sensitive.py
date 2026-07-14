from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app.api.auth import get_current_user
from app.core.database import async_session
from app.models.schema import TMDBAliasCache

router = APIRouter(prefix="/sensitive", tags=["sensitive"])


class TMDBAliasCacheUpsert(BaseModel):
	tmdb_id: int
	media_type: Optional[str] = "unknown"
	chinese_title: Optional[str] = None
	original_title: Optional[str] = None
	alias: Optional[str] = None
	source: Optional[str] = "manual"
	status: Optional[str] = "success"
	note: Optional[str] = None


class TMDBAliasCacheUpdate(BaseModel):
	media_type: Optional[str] = None
	chinese_title: Optional[str] = None
	original_title: Optional[str] = None
	alias: Optional[str] = None
	source: Optional[str] = None
	status: Optional[str] = None
	note: Optional[str] = None


class TMDBAliasCacheBatchDelete(BaseModel):
	ids: list[int]


@router.get("/tmdb-alias-cache")
async def list_tmdb_alias_cache(
	page: int = 1,
	page_size: int = 20,
	search: str = "",
	status: str = "",
	user=Depends(get_current_user),
):
	if page < 1:
		page = 1
	page_size = max(1, min(page_size, 200))

	async with async_session() as session:
		where_clause = []
		if search:
			if search.isdigit():
				where_clause.append(
					or_(
						TMDBAliasCache.tmdb_id == int(search),
						TMDBAliasCache.chinese_title.contains(search),
						TMDBAliasCache.original_title.contains(search),
						TMDBAliasCache.alias.contains(search),
					)
				)
			else:
				where_clause.append(
					or_(
						TMDBAliasCache.chinese_title.contains(search),
						TMDBAliasCache.original_title.contains(search),
						TMDBAliasCache.alias.contains(search),
						TMDBAliasCache.media_type.contains(search),
					)
				)
		if status:
			where_clause.append(TMDBAliasCache.status == status)

		count_stmt = select(func.count()).select_from(TMDBAliasCache)
		list_stmt = select(TMDBAliasCache)
		for cond in where_clause:
			count_stmt = count_stmt.where(cond)
			list_stmt = list_stmt.where(cond)

		total = (await session.execute(count_stmt)).scalar() or 0
		rows = (
			(
				await session.execute(
					list_stmt.order_by(TMDBAliasCache.updated_at.desc())
					.offset((page - 1) * page_size)
					.limit(page_size)
				)
			)
			.scalars()
			.all()
		)

		return {
			"state": True,
			"total": total,
			"page": page,
			"page_size": page_size,
			"items": [
				{
					"id": r.id,
					"tmdb_id": r.tmdb_id,
					"media_type": r.media_type,
					"chinese_title": r.chinese_title,
					"original_title": r.original_title,
					"alias": r.alias,
					"source": r.source,
					"status": r.status,
					"note": r.note,
					"updated_at": r.updated_at.isoformat() if r.updated_at else None,
					"created_at": r.created_at.isoformat() if r.created_at else None,
				}
				for r in rows
			],
		}


@router.post("/tmdb-alias-cache")
async def upsert_tmdb_alias_cache(payload: TMDBAliasCacheUpsert, user=Depends(get_current_user)):
	async with async_session() as session:
		existing = (
			(
				await session.execute(
					select(TMDBAliasCache).where(TMDBAliasCache.tmdb_id == payload.tmdb_id)
				)
			)
			.scalars()
			.first()
		)
		if existing:
			existing.media_type = payload.media_type or existing.media_type
			existing.chinese_title = payload.chinese_title
			existing.original_title = payload.original_title
			existing.alias = payload.alias
			existing.source = payload.source or existing.source
			existing.status = payload.status or existing.status
			existing.note = payload.note
			existing.updated_at = datetime.utcnow()
		else:
			existing = TMDBAliasCache(
				tmdb_id=payload.tmdb_id,
				media_type=payload.media_type or "unknown",
				chinese_title=payload.chinese_title,
				original_title=payload.original_title,
				alias=payload.alias,
				source=payload.source or "manual",
				status=payload.status or "success",
				note=payload.note,
			)
			session.add(existing)

		await session.commit()
		await session.refresh(existing)
		return {"state": True, "message": "保存成功", "id": existing.id}


@router.put("/tmdb-alias-cache/{cache_id}")
async def update_tmdb_alias_cache(cache_id: int, payload: TMDBAliasCacheUpdate, user=Depends(get_current_user)):
	async with async_session() as session:
		row = await session.get(TMDBAliasCache, cache_id)
		if not row:
			raise HTTPException(status_code=404, detail="记录不存在")

		data = payload.model_dump(exclude_unset=True)
		for key, value in data.items():
			setattr(row, key, value)
		row.updated_at = datetime.utcnow()

		await session.commit()
		return {"state": True, "message": "更新成功"}


@router.delete("/tmdb-alias-cache/{cache_id}")
async def delete_tmdb_alias_cache(cache_id: int, user=Depends(get_current_user)):
	async with async_session() as session:
		row = await session.get(TMDBAliasCache, cache_id)
		if not row:
			raise HTTPException(status_code=404, detail="记录不存在")

		await session.delete(row)
		await session.commit()
		return {"state": True, "message": "删除成功"}


@router.post("/tmdb-alias-cache/batch-delete")
async def batch_delete_tmdb_alias_cache(payload: TMDBAliasCacheBatchDelete, user=Depends(get_current_user)):
	ids = [i for i in payload.ids if isinstance(i, int)]
	if not ids:
		raise HTTPException(status_code=400, detail="请提供要删除的记录 ID")

	async with async_session() as session:
		rows = (
			(
				await session.execute(
					select(TMDBAliasCache).where(TMDBAliasCache.id.in_(ids))
				)
			)
			.scalars()
			.all()
		)
		if not rows:
			return {"state": True, "message": "没有匹配到可删除记录", "deleted": 0}

		for row in rows:
			await session.delete(row)

		await session.commit()
		return {"state": True, "message": "批量删除成功", "deleted": len(rows)}

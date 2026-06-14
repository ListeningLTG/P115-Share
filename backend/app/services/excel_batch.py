import asyncio
import random
import io
import pandas as pd
from datetime import datetime
from loguru import logger
from sqlalchemy import select, update, delete, func
from app.core.database import async_session
from app.models.schema import ExcelTask, ExcelTaskItem
from app.services.p115 import p115_service
from app.services.tg_bot import tg_service
from app.core.config import settings

def _get_svc():
    """获取当前最优 P115Service，降级到全局单例"""
    try:
        from app.services.account_manager import account_manager
        svc = account_manager.get_primary_service()
        if svc:
            return svc, account_manager
    except Exception:
        pass
    return p115_service, None

# 审核中条目重试配置
AUDIT_MAX_RETRIES = 3         # 最大重试轮次
AUDIT_RETRY_INTERVAL = 300    # 每轮重试前等待秒数（5分钟）

class ExcelBatchService:
    def __init__(self):
        self.worker_task = None
        self.active_task_id = None
        self.active_task_strategy = None  # 当前活跃任务的策略: 'transfer' | 'push'
        self._lock = asyncio.Lock()
        self._audit_retry_rounds: dict[int, int] = {}  # task_id -> 已重试轮次

    def _read_csv(self, content: bytes):
        """Try reading CSV with multiple encodings"""
        for encoding in ['utf-8', 'utf-8-sig', 'gbk', 'gb18030']:
            try:
                return pd.read_csv(io.BytesIO(content), encoding=encoding)
            except UnicodeDecodeError:
                continue
            except Exception as e:
                raise e
        raise Exception("无法识别CSV文件编码，请确保文件是 UTF-8 或 GBK 格式")

    async def parse_file(self, content: bytes, filename: str):
        """Parse Excel/CSV/JSON file and return headers and sample data"""
        try:
            if filename.endswith('.json'):
                data = self._parse_telegram_json(content)
                df = pd.DataFrame(data)
            elif filename.endswith('.csv'):
                df = self._read_csv(content)
            else:
                df = pd.read_excel(io.BytesIO(content))
            
            headers = df.columns.tolist()
            # Convert NaN to None for JSON serialization
            df_cleaned = df.where(pd.notnull(df), None)
            preview_data = df_cleaned.head(5).to_dict(orient='records')
            
            return {
                "headers": headers,
                "preview": preview_data,
                "total_rows": len(df)
            }
        except Exception as e:
            logger.error(f"解析文件失败 {filename}: {e}")
            raise Exception(f"解析文件失败: {str(e)}")

    builder_functions = {
        'bold': lambda t: t,
        'italic': lambda t: t,
        'underline': lambda t: t,
        'strikethrough': lambda t: t,
        'code': lambda t: t,
        'pre': lambda t: t,
        'text_link': lambda t: t,
        'mention': lambda t: t,
        'hashtag': lambda t: t,
        'cashtag': lambda t: t,
        'bot_command': lambda t: t,
        'email': lambda t: t,
        'phone_number': lambda t: t,
        'blockquote': lambda t: t,
        'spoiler': lambda t: t,
    }

    def _parse_telegram_json(self, content: bytes):
        """Parse Telegram export JSON and extract links, titles, and original message format"""
        import json
        import re
        
        try:
            data = json.loads(content)
            messages = data.get('messages', [])
            extracted_data = []
            
            # Regex for 115 links: 115.com/s/ or 115cdn.com/s/
            link_pattern = re.compile(r'https?://(?:115\.com|115cdn\.com)/s/([a-z0-9]+)(?:\?password=([a-z0-9]+))?')
            
            for msg in messages:
                text_entities = msg.get('text_entities', [])
                if not text_entities:
                    continue
                    
                # 1. Reconstruct full_text and entities for the message
                full_text = ""
                entities = []
                
                # We need to track the current offset in UTF-16 code units
                def get_u16_len(s):
                    return len(s.encode('utf-16-le')) // 2

                current_offset = 0
                links_info = [] # [(start_u16, end_u16, url, password)]

                for entity in text_entities:
                    entity_text = entity.get('text', '')
                    entity_type = entity.get('type')
                    
                    if not entity_text:
                        continue
                        
                    length = get_u16_len(entity_text)
                    
                    # Mapping Telegram types to Aiogram types
                    tg_to_aio = {
                        'bold': 'bold',
                        'italic': 'italic',
                        'underline': 'underline',
                        'strikethrough': 'strikethrough',
                        'code': 'code',
                        'pre': 'pre',
                        'text_link': 'text_link',
                        'mention': 'mention',
                        'hashtag': 'hashtag',
                        'cashtag': 'cashtag',
                        'bot_command': 'bot_command',
                        'email': 'email',
                        'phone_number': 'phone_number',
                        'blockquote': 'blockquote',
                        'spoiler': 'spoiler',
                        'link': 'url',
                    }
                    
                    if entity_type in tg_to_aio:
                        ent_data = {
                            "type": tg_to_aio[entity_type],
                            "offset": current_offset,
                            "length": length
                        }
                        if entity_type == 'text_link':
                            ent_data["url"] = entity.get('href')
                            # Check URL for 115 link
                            match = link_pattern.search(ent_data["url"])
                            if match:
                                links_info.append((current_offset, current_offset + length, ent_data["url"], match.group(2)))
                        elif entity_type == 'link':
                            # Check plain text URL for 115 link
                            match = link_pattern.search(entity_text)
                            if match:
                                links_info.append((current_offset, current_offset + length, entity_text, match.group(2)))
                        
                        entities.append(ent_data)
                    
                    full_text += entity_text
                    current_offset += length

                if not links_info:
                    continue

                # Extract message date and format it as [MM-DD HH:MM]
                date_suffix = ""
                msg_date = msg.get('date')
                if msg_date:
                    try:
                        dt = datetime.fromisoformat(msg_date)
                        date_suffix = f" [{dt.strftime('%m-%d %H:%M')}]"
                    except Exception:
                        pass
                
                # 2. Smart Segmentation logic
                text_utf16_len = get_u16_len(full_text)
                last_boundary = 0
                segments = []
                
                for idx, pos in enumerate(links_info):
                    start_u16, end_u16, url, password = pos
                    seg_end = end_u16
                    
                    if idx < len(links_info) - 1:
                        next_start_u16 = links_info[idx + 1][0]
                        try:
                            # Convert U16 offsets to string characters for string searching
                            between_start_char = len(full_text.encode('utf-16-le')[:end_u16*2].decode('utf-16-le', errors='ignore'))
                            between_end_char = len(full_text.encode('utf-16-le')[:next_start_u16*2].decode('utf-16-le', errors='ignore'))
                            between_text = full_text[between_start_char:between_end_char]
                            
                            double_newline_pos = between_text.find('\n\n')
                            if double_newline_pos != -1:
                                split_char = between_start_char + double_newline_pos + 2
                                seg_end = get_u16_len(full_text[:split_char])
                            else:
                                if len(between_text.strip()) > 10:
                                    seg_end = next_start_u16
                        except Exception:
                            seg_end = next_start_u16
                    else:
                        seg_end = text_utf16_len
                        
                    # Slice text and entities
                    try:
                        u16_text = full_text.encode('utf-16-le')
                        slice_u16 = u16_text[last_boundary*2:seg_end*2]
                        seg_text = slice_u16.decode('utf-16-le', errors='ignore')
                        
                        seg_entities = []
                        for e in entities:
                            offset = e["offset"]
                            length = e["length"]
                            if offset >= last_boundary and (offset + length) <= seg_end:
                                e_copy = e.copy()
                                e_copy["offset"] = offset - last_boundary
                                seg_entities.append(e_copy)
                            elif offset < seg_end and (offset + length) > last_boundary:
                                o_start = max(offset, last_boundary)
                                o_end = min(offset + length, seg_end)
                                e_copy = e.copy()
                                e_copy["offset"] = o_start - last_boundary
                                e_copy["length"] = o_end - o_start
                                seg_entities.append(e_copy)
                                
                        segments.append({
                            "text": seg_text,
                            "entities": seg_entities,
                            "url": url,
                            "password": password
                        })
                    except Exception as sl_e:
                        # Fallback if slice fails
                        logger.error(f"Slice message failed: {sl_e}")
                        segments.append({
                            "text": full_text,
                            "entities": entities,
                            "url": url,
                            "password": password
                        })

                    last_boundary = seg_end
                    
                # 3. Process each segment and extract title
                for seg in segments:
                    seg_text = seg["text"]
                    seg_entities = seg["entities"]
                    url = seg["url"]
                    password = seg["password"]
                    title = None
                    if seg_text:
                        first_line = seg_text.split('\n')[0].strip()
                        if first_line:
                            if any(first_line.startswith(prefix) for prefix in ['📺', '🎬', '🎥', '🎞️', '📁', '【', '[']) or \
                               any(keyword in first_line for keyword in ['电视剧', '电影', '剧集', '名称', '资源']):
                                clean_title = re.sub(r'^[🎬🎥🎞️📀📁📺\s]*(剧集|电影|名称|内容|资源)?[:：]?\s*', '', first_line)
                                if clean_title:
                                    title = clean_title.strip()
                    
                    if not title:
                        try:
                            for e in seg_entities:
                                if e.get('type') == 'bold':
                                    e_offset = e.get('offset')
                                    e_length = e.get('length')
                                    extracted = seg_text.encode('utf-16-le')[e_offset*2:(e_offset+e_length)*2].decode('utf-16-le', errors='ignore').strip()
                                    if len(extracted) > 3 and extracted not in ["名称", "剧集", "电影", "资源", "标 签", "标签", "分 类", "分类", "体 积", "体积", "链接"]:
                                        title = extracted
                                        break
                        except Exception:
                            pass
                            
                    if not title and seg_text:
                        first_line = seg_text.split('\n')[0].strip()
                        if first_line and len(first_line) > 1 and "http" not in first_line:
                            if "链接" not in first_line and "网盘" not in first_line:
                                title = first_line
                            elif len(first_line) > 10:
                                title = first_line

                    if title:
                        title = re.sub(r'^[🎬🎥🎞️📀📁📺\s]*', '', title).strip()
                        
                    current_title = (title or f"Message_{msg.get('id')}") + date_suffix

                    extracted_data.append({
                        "链接": url,
                        "标题": current_title,
                        "消息时间": date_suffix.strip(' []') if date_suffix else "",
                        "提取码": password or "",
                        "item_metadata": {
                            "full_text": seg_text,
                            "entities": seg_entities,
                            "msg_date": date_suffix.strip(' []') if date_suffix else ""
                        }
                    })
            
            if not extracted_data:
                raise Exception("未在 JSON 文件中找到有效的 115 分享链接")
            
            return extracted_data
        except Exception as e:
            logger.exception(f"解析 Telegram JSON 失败")
            raise Exception(f"解析 Telegram JSON 失败: {str(e)}")

    async def create_task(self, filename: str, mapping: dict, content: bytes):
        """Create task and items based on mapping"""
        try:
            if filename.endswith('.json'):
                data = self._parse_telegram_json(content)
                df = pd.DataFrame(data)
            elif filename.endswith('.csv'):
                df = self._read_csv(content)
            else:
                df = pd.read_excel(io.BytesIO(content))
            
            df = df.where(pd.notnull(df), None)
            
            link_col = mapping.get('link')
            title_col = mapping.get('title')
            code_col = mapping.get('code')
            
            if not link_col:
                raise Exception("未指定链接列")

            async with async_session() as session:
                task = ExcelTask(
                    name=filename,
                    status="wait",
                    total_count=len(df)
                )
                session.add(task)
                await session.flush()
                
                # Add items
                for idx, row in df.iterrows():
                    item = ExcelTaskItem(
                        task_id=task.id,
                        row_index=int(idx) + 1,
                        original_url=str(row[link_col]) if row[link_col] else "",
                        title=str(row[title_col]) if title_col and row[title_col] else None,
                        extraction_code=str(row[code_col]) if code_col and row[code_col] else None,
                        item_metadata=row.get('item_metadata') if 'item_metadata' in row else None,
                        status="待处理"
                    )
                    session.add(item)
                
                await session.commit()
                return task.id
        except Exception as e:
            logger.error(f"创建任务失败: {e}")
            raise e

    async def start_worker(self):
        if self.worker_task and not self.worker_task.done():
            return
        self.worker_task = asyncio.create_task(self._worker())
        logger.info("Excel 批量转存服务工作线程启动")

    async def _worker(self):
        while True:
            try:
                item_id = None
                is_processed = False
                # Check for tasks that are "running"
                async with async_session() as session:
                    result = await session.execute(
                        select(ExcelTask).where(ExcelTask.status == "running").limit(1)
                    )
                    task = result.scalar_one_or_none()
                    
                    if not task:
                        # If no running task, check for "queued" tasks
                        result = await session.execute(
                            select(ExcelTask).where(ExcelTask.status == "queued").order_by(ExcelTask.created_at).limit(1)
                        )
                        task = result.scalar_one_or_none()
                        if task:
                            # Start the queued task
                            task.status = "running"
                            await session.commit()
                            logger.info(f"队列任务 {task.id} ({task.name}) 开始运行")
                
                    if not task:
                        # If no running taskFound, exit worker
                        logger.info("Excel 批量转存服务工作线程退出（无运行中的任务）")
                        self.worker_task = None
                        break
                    
                    self.active_task_id = task.id
                    self.active_task_strategy = task.strategy
                    interval_min = task.interval_min
                    interval_max = task.interval_max
                    
                    try:
                        # Get one pending item
                        async with async_session() as session:
                            result = await session.execute(
                                select(ExcelTaskItem).where(
                                    ExcelTaskItem.task_id == task.id,
                                    ExcelTaskItem.status == "待处理"
                                ).order_by(ExcelTaskItem.row_index).limit(1)
                            )
                            item = result.scalar_one_or_none()
                            
                            if item:
                                item.status = "处理中"
                                item_id = item.id
                                # Update current_row in ExcelTask and set is_waiting to False
                                await session.execute(
                                    update(ExcelTask).where(ExcelTask.id == task.id).values(
                                        current_row=item.row_index,
                                        is_waiting=False
                                    )
                                )
                                await session.commit()
                            else:
                                # 主流程条目全部处理完 — 检查是否有「待审核」条目需要重试
                                auditing_count = await session.scalar(
                                    select(func.count(ExcelTaskItem.id)).where(
                                        ExcelTaskItem.task_id == task.id,
                                        ExcelTaskItem.status == "待审核"
                                    )
                                )

                                if auditing_count > 0:
                                    retry_round = self._audit_retry_rounds.get(task.id, 0) + 1
                                    if retry_round > AUDIT_MAX_RETRIES:
                                        logger.info(f"⏭️ 任务 {task.id} 中 {auditing_count} 个审核中条目已达最大重试轮次 ({AUDIT_MAX_RETRIES})，标记为跳过")
                                        await session.execute(
                                            update(ExcelTaskItem).where(
                                                ExcelTaskItem.task_id == task.id,
                                                ExcelTaskItem.status == "待审核"
                                            ).values(status="跳过", error_msg=f"审核中超时，已达最大重试次数({AUDIT_MAX_RETRIES}轮)")
                                        )
                                        await session.execute(
                                            update(ExcelTask).where(ExcelTask.id == task.id).values(
                                                status="completed", current_row=0, is_waiting=False
                                            )
                                        )
                                        await session.commit()
                                        self._audit_retry_rounds.pop(task.id, None)
                                        await self._update_task_counts(task.id)
                                        self.active_task_id = None
                                        continue
                                    else:
                                        self._audit_retry_rounds[task.id] = retry_round
                                        logger.info(f"🔄 任务 {task.id} 主流程完成，第 {retry_round}/{AUDIT_MAX_RETRIES} 轮重试 {auditing_count} 个审核中条目，等待 {AUDIT_RETRY_INTERVAL}s...")
                                        await session.execute(
                                            update(ExcelTaskItem).where(
                                                ExcelTaskItem.task_id == task.id,
                                                ExcelTaskItem.status == "待审核"
                                            ).values(status="待处理", error_msg=f"审核中，第 {retry_round} 轮重试")
                                        )
                                        await session.execute(
                                            update(ExcelTask).where(ExcelTask.id == task.id).values(
                                                current_row=0, is_waiting=True
                                            )
                                        )
                                        await session.commit()
                                        self.active_task_id = None
                                        await asyncio.sleep(AUDIT_RETRY_INTERVAL)
                                        continue
                                else:
                                    # 真正完成
                                    await session.execute(
                                        update(ExcelTask).where(ExcelTask.id == task.id).values(
                                            status="completed",
                                            current_row=0,
                                            is_waiting=False
                                        )
                                    )
                                    await session.commit()
                                    self._audit_retry_rounds.pop(task.id, None)
                                    self.active_task_id = None
                                    continue

                        # Process the item
                        if task.strategy == "direct_save" and task.target_account_id:
                            from app.services.account_manager import account_manager
                            svc = account_manager.get_service(task.target_account_id)
                            acct_mgr = account_manager
                            if not svc:
                                svc, acct_mgr = _get_svc()
                        else:
                            svc, acct_mgr = _get_svc()
                        
                        if task.strategy != "push" and svc.is_restricted:
                            # 尝试切换到未被风控的账号
                            new_svc, new_acct_mgr = _get_svc()
                            if new_svc is not svc and not new_svc.is_restricted:
                                logger.info(f"🔄 账号风控，批量任务切换账号: [{getattr(svc.account, 'id', '?')}] → [{getattr(new_svc.account, 'id', '?')}]")
                                svc, acct_mgr = new_svc, new_acct_mgr
                            else:
                                logger.info(f"⏳ P115 服务当前处于受限状态，批量任务 {task.id} 自动暂停...")
                                async with async_session() as session:
                                    await session.execute(
                                        update(ExcelTask).where(ExcelTask.id == task.id).values(status="paused")
                                    )
                                    if item_id:
                                        await session.execute(
                                            update(ExcelTaskItem).where(ExcelTaskItem.id == item_id).values(status="待处理")
                                        )
                                    await session.commit()

                                if tg_service:
                                    await tg_service.send_admin_msg(f"⏸️ 检测到所有账号受限，批量转存任务 '{task.name}' 已自动暂停。")

                                self.active_task_id = None
                                break # 停止当前任务的 worker

                        task_config = {
                            "target_channels": task.target_channels,
                            "white_list_keywords": task.white_list_keywords,
                            "black_list_keywords": task.black_list_keywords,
                            "skip_large_package": task.skip_large_package,
                            "strategy": task.strategy,
                            "target_account_id": task.target_account_id,
                            "target_dir": task.target_dir
                        }

                        is_processed = await self._process_item(item_id, task_config, svc=svc, acct_mgr=acct_mgr)
                        
                        if getattr(is_processed, "__eq__", None) and is_processed == "RESTRICTED":
                            # 尝试切换到未风控账号重试一次
                            retry_svc, retry_acct_mgr = _get_svc()
                            if retry_svc is not svc and not retry_svc.is_restricted:
                                logger.info(f"🔄 处理中途风控，切换账号重试: [{getattr(svc.account, 'id', '?')}] → [{getattr(retry_svc.account, 'id', '?')}]")
                                is_processed = await self._process_item(item_id, task_config, svc=retry_svc, acct_mgr=retry_acct_mgr)

                        if getattr(is_processed, "__eq__", None) and is_processed == "RESTRICTED":
                            logger.info(f"⏳ 任务 {task.id} 当前项遇到受限且无可用账号，立即暂停后续处理...")
                            async with async_session() as session:
                                await session.execute(
                                    update(ExcelTask).where(ExcelTask.id == task.id).values(status="paused")
                                )
                                await session.commit()
                            self.active_task_id = None
                            break
                        
                    finally:
                        # Find next row and set is_waiting to True before sleep
                        if item_id:
                            async with async_session() as session:
                                # Look ahead for next pending item
                                next_result = await session.execute(
                                    select(ExcelTaskItem.row_index).where(
                                        ExcelTaskItem.task_id == task.id,
                                        ExcelTaskItem.status == "待处理"
                                    ).order_by(ExcelTaskItem.row_index).limit(1)
                                )
                                next_row = next_result.scalar_one_or_none()
                                
                                if next_row:
                                    await session.execute(
                                        update(ExcelTask).where(ExcelTask.id == task.id).values(
                                            current_row=next_row,
                                            is_waiting=True
                                        )
                                    )
                                else:
                                    await session.execute(
                                        update(ExcelTask).where(ExcelTask.id == task.id).values(
                                            current_row=0,
                                            is_waiting=False
                                        )
                                    )
                                await session.commit()
                        self.active_task_id = None
                
                # Rate limiting (Random interval) with capacity check
                if is_processed:
                    interval = random.randint(interval_min, interval_max)

                    # 先执行容量检查（使用与当前任务相同的账号）
                    try:
                        start_check = datetime.now()
                        cap_svc, _ = _get_svc()
                        await cap_svc.check_capacity_and_cleanup(mode="batch")
                        elapsed = (datetime.now() - start_check).total_seconds()
                        logger.debug(f"容量检查完成，耗时 {elapsed:.2f}s")
                    except Exception as ce:
                        logger.error(f"批量任务容量检查失败: {ce}")
                        elapsed = 0

                    # 计算剩余等待时间并执行
                    remaining_sleep = max(0, interval - elapsed)
                    if remaining_sleep > 0:
                        await asyncio.sleep(remaining_sleep)
                else:
                    # 跳过的项目不等待，直接处理下一个
                    pass
                
            except Exception as e:
                logger.error(f"Excel 工作线程出错: {e}")
                await asyncio.sleep(5)

    async def _process_item(self, item_id: int, task_config: dict = None, svc=None, acct_mgr=None):
        async with async_session() as session:
            result = await session.execute(
                select(ExcelTaskItem).where(ExcelTaskItem.id == item_id)
            )
            try:
                item = result.scalar_one()
                # 如果传了配置字典则直接取，否则回退一次库查询（保险策略）
                if task_config:
                    target_channels = task_config.get("target_channels")
                    white_list = task_config.get("white_list_keywords")
                    black_list = task_config.get("black_list_keywords")
                    skip_large_package = task_config.get("skip_large_package")
                    strategy = task_config.get("strategy", "transfer")
                    target_account_id = task_config.get("target_account_id")
                    target_dir = task_config.get("target_dir")
                else:
                    task_result = await session.execute(
                        select(ExcelTask).where(ExcelTask.id == item.task_id)
                    )
                    t_row = task_result.scalar_one()
                    target_channels = t_row.target_channels
                    white_list = t_row.white_list_keywords
                    black_list = t_row.black_list_keywords
                    skip_large_package = t_row.skip_large_package
                    strategy = t_row.strategy
                    target_account_id = t_row.target_account_id
                    target_dir = t_row.target_dir
            except Exception:
                logger.error(f"Item {item_id} not found or task deleted")
                return

            # 若未从外部传入，则在此处重新选择账号
            if svc is None:
                if strategy == "direct_save" and target_account_id:
                    from app.services.account_manager import account_manager
                    svc = account_manager.get_service(target_account_id)
                    acct_mgr = account_manager
                if svc is None:
                    svc, acct_mgr = _get_svc()

            task_id = item.task_id
            
            # --- Keyword Filtering Logic ---
            search_text = f"{item.title or ''} {item.original_url or ''}"
            if item.item_metadata and isinstance(item.item_metadata, dict):
                search_text += f" {item.item_metadata.get('full_text', '')}"
            
            search_text = search_text.lower()
            
            # 1. Check Blacklist (Blacklist Wins)
            if black_list:
                black_keywords = [k.strip().lower() for k in black_list.split(',') if k.strip()]
                for kw in black_keywords:
                    if kw in search_text:
                        logger.info(f"Item {item.id} skipped (Blacklist match: {kw})")
                        item.status = "跳过"
                        item.error_msg = f"命中黑名单关键词: {kw}"
                        await session.commit()
                        await self._update_task_counts(task_id)
                        return False
            
            # 2. Check Whitelist
            if white_list:
                white_keywords = [k.strip().lower() for k in white_list.split(',') if k.strip()]
                if white_keywords:
                    found_white = False
                    for kw in white_keywords:
                        if kw in search_text:
                            found_white = True
                            break
                    
                    if not found_white:
                        logger.info(f"Item {item.id} skipped (Whitelist no match)")
                        item.status = "跳过"
                        item.error_msg = "未命中白名单关键词"
                        await session.commit()
                        await self._update_task_counts(task_id)
                        return False
            # --- End Filtering Logic ---
            
            original_url = item.original_url
            if not original_url:
                item.status = "失败"
                item.error_msg = "链接为空"
                await session.commit()
                await self._update_task_counts(task_id)
                return True

            # Prepare metadata
            if item.item_metadata:
                metadata = item.item_metadata.copy()
                metadata["share_url"] = original_url
            else:
                metadata = {
                    "description": item.title or "Excel Batch Import",
                    "full_text": f"云盘分享\n资源名称：{item.title or '未知'}\n分享链接：{{{{share_link}}}}",
                    "share_url": original_url
                }

            # --- Strategy: Push ---
            if strategy == "push":
                if tg_service:
                    await tg_service.broadcast_to_channels({original_url: original_url}, metadata, channel_ids=target_channels)
                
                item.status = "成功"
                item.new_share_url = original_url
                item.error_msg = "直接推送完成"
                await session.commit()
                await self._update_task_counts(task_id)
                return True

            # --- Strategy: Direct Save ---
            if strategy == "direct_save":
                try:
                    # Combine password if present for saving
                    url_to_save = original_url
                    if item.extraction_code and "?password=" not in url_to_save:
                        url_to_save = f"{url_to_save}?password={item.extraction_code}"

                    save_res = await svc.save_share_link(
                        url_to_save,
                        metadata=metadata,
                        target_dir=target_dir or "115-Save",
                        skip_large_package=True,
                        is_batch=True,
                        create_task_subdir=False
                    )
                    
                    if save_res:
                        if save_res.get("status") == "success":
                            item.status = "成功"
                            item.new_share_url = None
                            item.error_msg = f"已直接保存到 {target_dir or '115-Save'}"
                            if acct_mgr and svc.account:
                                asyncio.create_task(acct_mgr.update_last_used(svc.account.id))
                        elif save_res.get("status") == "pending":
                            reason = save_res.get("reason")
                            if reason == "snapshotting":
                                item.status = "待审核"
                                item.error_msg = "快照生成中，等待本批次完成后自动重试"
                            elif reason == "restricted":
                                item.status = "待处理"
                                item.error_msg = "检测到115账号限制接收，等待恢复"
                                await session.commit()
                                await self._update_task_counts(task_id)
                                return "RESTRICTED"
                            else:
                                item.status = "待审核"
                                item.error_msg = "审核中，等待本批次完成后自动重试"
                        elif save_res.get("status") == "skipped":
                            item.status = "跳过"
                            item.error_msg = save_res.get("message", "跳过处理")
                        else:
                            item.status = "失败"
                            item.error_msg = save_res.get("message", "保存失败")
                    else:
                        item.status = "失败"
                        item.error_msg = "保存服务无响应"
                except Exception as e:
                    logger.exception(f"处理项目失败: {item_id}")
                    item.status = "失败"
                    item.error_msg = str(e)
                
                await session.commit()
                await self._update_task_counts(task_id)
                return True

            # --- Strategy: Transfer (Original Logic) ---
            # 1. Check history first
            history_url = await svc.get_history_link(original_url)
            if history_url:
                item.status = "成功"
                item.error_msg = None
                import json
                item.new_share_url = json.dumps(history_url) if isinstance(history_url, list) else history_url
                await session.commit()
                await self._update_task_counts(task_id)
                if tg_service:
                    await tg_service.broadcast_to_channels({original_url: history_url}, metadata, channel_ids=target_channels)
                return True

            try:
                
                # Combine password if present for saving
                url_to_save = original_url
                if item.extraction_code and "?password=" not in url_to_save:
                    url_to_save = f"{url_to_save}?password={item.extraction_code}"

                save_res = await svc.save_and_share(
                    url_to_save,
                    metadata=metadata,
                    skip_large_package=True,
                    is_batch=True
                )
                
                if save_res:
                    if save_res.get("status") == "success":
                        share_link = save_res.get("share_link")
                        recursive_links = save_res.get("recursive_links", [])
                        
                        # 合并主链接和分卷链接
                        all_links = recursive_links + ([share_link] if share_link else [])
                        
                        if all_links:
                            import json
                            # 如果只有一个链接存字符串，多个存 JSON
                            link_to_store = json.dumps(all_links) if len(all_links) > 1 else all_links[0]

                            await svc.save_history_link(original_url, all_links)
                            if acct_mgr and svc.account:
                                asyncio.create_task(acct_mgr.update_last_used(svc.account.id))
                            item.new_share_url = link_to_store
                            item.status = "成功"
                            item.error_msg = None
                            
                            # Broadcast to channels
                            if tg_service:
                                if item.item_metadata:
                                    await tg_service.broadcast_to_channels({original_url: all_links}, metadata, channel_ids=target_channels)
                                else:
                                    await tg_service.broadcast_to_channels({original_url: all_links}, {"full_text": f"资源名称：{item.title or '未知'}\n分享链接：{{{{share_link}}}}"}, channel_ids=target_channels)
                        else:
                            item.status = "失败"
                            item.error_msg = "转存成功但生成分享链接返回为空"
                    elif save_res.get("status") == "pending":
                        reason = save_res.get("reason")
                        if reason == "snapshotting":
                            item.status = "待审核"
                            item.error_msg = "快照生成中，等待本批次完成后自动重试"
                        elif reason == "restricted":
                            item.status = "待处理"
                            item.error_msg = "检测到115账号限制接收，等待恢复"
                            await session.commit()
                            await self._update_task_counts(task_id)
                            return "RESTRICTED"
                        else:
                            item.status = "待审核"
                            item.error_msg = "审核中，等待本批次完成后自动重试"
                    elif save_res.get("status") == "skipped":
                        item.status = "跳过"
                        item.error_msg = save_res.get("message", "跳过处理")
                    else:
                        item.status = "失败"
                        item.error_msg = save_res.get("message", "转存失败")
                else:
                    item.status = "失败"
                    item.error_msg = "转存服务无响应"
            except Exception as e:
                logger.exception(f"处理项目失败: {item_id}")
                item.status = "失败"
                item.error_msg = str(e)
            
            await session.commit()
            await self._update_task_counts(task_id)
            return True

    async def _update_task_counts(self, task_id: int):
        async with async_session() as session:
            # Get success count
            success_count = await session.scalar(
                select(func.count(ExcelTaskItem.id)).where(
                    ExcelTaskItem.task_id == task_id, 
                    ExcelTaskItem.status == "成功"
                )
            )
            # Get fail count
            fail_count = await session.scalar(
                select(func.count(ExcelTaskItem.id)).where(
                    ExcelTaskItem.task_id == task_id, 
                    ExcelTaskItem.status == "失败"
                )
            )
            
            await session.execute(
                update(ExcelTask).where(ExcelTask.id == task_id).values(
                    success_count=success_count,
                    fail_count=fail_count
                )
            )
            await session.commit()

    async def start_task(self, task_id: int, skip_count: int = 0, stop_row: int = 0, interval_min: int = 5, interval_max: int = 10, target_channels: list = None, white_list_keywords: str = None, black_list_keywords: str = None, skip_large_package: bool = False, strategy: str = "transfer", target_account_id: int = None, target_dir: str = None):
        async with async_session() as session:
            # Get currrent status
            result = await session.execute(select(ExcelTask).where(ExcelTask.id == task_id))
            task = result.scalar_one()
            
            # Check if another task is already running
            result = await session.execute(
                select(ExcelTask).where(ExcelTask.status == "running", ExcelTask.id != task_id)
            )
            other_running = result.scalar_one_or_none()
            
            new_status = "queued" if other_running else "running"
            
            # If resume from paused, dont reset skip/pending
            # BUT if skip_count or stop_row changed, treat as fresh start
            is_resume = task.status == "paused" and task.skip_count == skip_count and task.stop_row == stop_row
            
            # Update intervals and status
            task.interval_min = interval_min
            task.interval_max = interval_max
            task.status = new_status
            if target_channels is not None:
                task.target_channels = target_channels
            
            # Save keywords
            if white_list_keywords is not None:
                task.white_list_keywords = white_list_keywords
            if black_list_keywords is not None:
                task.black_list_keywords = black_list_keywords
            
            task.skip_large_package = skip_large_package
            task.strategy = strategy
            task.target_account_id = target_account_id
            task.target_dir = target_dir
            
            if not is_resume:
                self._audit_retry_rounds.pop(task_id, None)
                task.skip_count = skip_count
                task.stop_row = stop_row
                task.current_row = 0
                # Mark first skip_count items as "跳过"
                await session.execute(
                    update(ExcelTaskItem).where(
                        ExcelTaskItem.task_id == task_id,
                        ExcelTaskItem.row_index <= skip_count
                    ).values(status="跳过", error_msg=None, new_share_url=None)
                )
                if stop_row > 0:
                    # Mark items within [skip_count+1, stop_row] as "待处理"
                    await session.execute(
                        update(ExcelTaskItem).where(
                            ExcelTaskItem.task_id == task_id,
                            ExcelTaskItem.row_index > skip_count,
                            ExcelTaskItem.row_index <= stop_row
                        ).values(status="待处理", error_msg=None, new_share_url=None)
                    )
                    # Mark items after stop_row as "跳过"
                    await session.execute(
                        update(ExcelTaskItem).where(
                            ExcelTaskItem.task_id == task_id,
                            ExcelTaskItem.row_index > stop_row
                        ).values(status="跳过", error_msg=None, new_share_url=None)
                    )
                else:
                    # Mark remaining items as "待处理"
                    await session.execute(
                        update(ExcelTaskItem).where(
                            ExcelTaskItem.task_id == task_id,
                            ExcelTaskItem.row_index > skip_count
                        ).values(status="待处理", error_msg=None, new_share_url=None)
                    )
            
            await session.commit()
            
            if new_status == "running":
                logger.info(f"任务 {task_id} 开始运行")
            else:
                logger.info(f"任务 {task_id} 已进入队列排队")

        await self._update_task_counts(task_id)
        if new_status == "running":
            await self.start_worker()

    async def shutdown(self):
        """Handle graceful shutdown: pause running tasks, reset queued tasks"""
        logger.info("Excel 批量转存服务正在关闭，正在保存任务状态...")
        async with async_session() as session:
            # Reset running, pausing, cancelling, and queued tasks to paused
            await session.execute(
                update(ExcelTask).where(
                    ExcelTask.status.in_(["running", "pausing", "cancelling", "queued"])
                ).values(status="paused", is_waiting=False)
            )
            await session.commit()
        
        # Wait for current processing item if any
        wait_start = datetime.now()
        while self.active_task_id is not None:
            await asyncio.sleep(0.1)
            if (datetime.now() - wait_start).total_seconds() > 30:
                logger.warning("Excel shutdown wait timeout")
                break
        
        logger.info("Excel 批量转存服务已关闭")


    async def pause_task(self, task_id: int):
        async with async_session() as session:
            # Set to transitional status first
            await session.execute(
                update(ExcelTask).where(ExcelTask.id == task_id).values(status="pausing")
            )
            await session.commit()
        
        # Safety wait: wait until the current item processing finishes
        wait_start = datetime.now()
        while self.active_task_id == task_id:
            await asyncio.sleep(0.1)
            if (datetime.now() - wait_start).total_seconds() > 60:
                logger.warning(f"Pause task {task_id} safety wait timeout")
                break
        
        # Set to final status
        async with async_session() as session:
            await session.execute(
                update(ExcelTask).where(ExcelTask.id == task_id).values(status="paused")
            )
            await session.commit()
        logger.info(f"Task {task_id} paused safely")

    async def cancel_task(self, task_id: int):
        async with async_session() as session:
            # Set to transitional status first
            await session.execute(
                update(ExcelTask).where(ExcelTask.id == task_id).values(status="cancelling")
            )
            await session.commit()
            
        # Safety wait: same as pause
        wait_start = datetime.now()
        while self.active_task_id == task_id:
            await asyncio.sleep(0.1)
            if (datetime.now() - wait_start).total_seconds() > 60:
                break
        
        # Set to final status
        async with async_session() as session:
            await session.execute(
                update(ExcelTask).where(ExcelTask.id == task_id).values(status="cancelled")
            )
            await session.commit()
        logger.info(f"Task {task_id} cancelled safely")

    async def recover_tasks(self):
        """Recover tasks from non-graceful shutdown"""
        logger.info("Excel 批量转存服务正在进行故障恢复...")
        async with async_session() as session:
            # 1. Reset tasks that were stuck in active or transitional states
            await session.execute(
                update(ExcelTask).where(
                    ExcelTask.status.in_(["running", "pausing", "cancelling", "queued"])
                ).values(status="paused", is_waiting=False)
            )
            # 2. Reset items that were stuck in "处理中"
            await session.execute(
                update(ExcelTaskItem).where(ExcelTaskItem.status == "处理中").values(status="待处理")
            )
            await session.commit()
        logger.info("Excel 故障恢复完成")

    async def delete_task(self, task_id: int):
        async with async_session() as session:
            await session.execute(delete(ExcelTaskItem).where(ExcelTaskItem.task_id == task_id))
            await session.execute(delete(ExcelTask).where(ExcelTask.id == task_id))
            await session.commit()

excel_batch_service = ExcelBatchService()

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiohttp_socks import ProxyConnector
from app.core.config import settings
from app.services.p115 import p115_service
from loguru import logger
import asyncio
import re

def _get_svc():
    """获取当前最优 P115Service（负载均衡调度），降级到全局单例"""
    try:
        from app.services.account_manager import account_manager
        svc = account_manager.get_primary_service()
        if svc:
            return svc, account_manager
    except Exception:
        pass
    return p115_service, None

class TGService:
    def __init__(self):
        self.bot = None
        self.dp = None
        self.polling_task = None
        self.is_connected = False
        self._lock = asyncio.Lock()
        self._current_polling_id = 0
        self._verify_tasks = []
        if settings.TG_BOT_TOKEN:
            self.init_bot(settings.TG_BOT_TOKEN)

    def init_bot(self, token: str):
        """Synchronous initialization for startup or immediate use. 
        Note: For clean restarts, use restart_polling instead."""
        try:
            # Configure proxy if set
            session = None
            if settings.PROXY_ENABLED and settings.PROXY_HOST and settings.PROXY_PORT:
                proxy_type = settings.PROXY_TYPE.lower()
                auth = f"{settings.PROXY_USER}:{settings.PROXY_PASS}@" if settings.PROXY_USER and settings.PROXY_PASS else ""
                proxy_url = f"{proxy_type}://{auth}{settings.PROXY_HOST}:{settings.PROXY_PORT}"
                session = AiohttpSession(proxy=proxy_url)
                logger.info(f"Telegram Bot using {settings.PROXY_TYPE} proxy: {settings.PROXY_HOST}:{settings.PROXY_PORT}")
            else:
                # Use default session
                session = AiohttpSession()

            # Add retry middleware to session with improved backoff
            async def retry_middleware(make_request, bot, method):
                max_retries = 5  # 增加到 5 次
                base_delay = 2.0
                for attempt in range(max_retries):
                    try:
                        return await make_request(bot, method)
                    except (TelegramNetworkError, asyncio.TimeoutError) as e:
                        if attempt == max_retries - 1:
                            raise
                        # 指数退避：2, 4, 8, 16 秒
                        wait_time = base_delay * (2 ** attempt)
                        logger.warning(f"Bot network error: {e}. Retrying ({attempt + 1}/{max_retries}) in {wait_time:.1f}s...")
                        await asyncio.sleep(wait_time)
                    except Exception as e:
                        # 非网络错误直接抛出，不重试
                        logger.error(f"Bot API non-network error: {e}")
                        raise
            
            session.middleware(retry_middleware)
                
            self.bot = Bot(token=token, session=session)
            self.dp = Dispatcher()
            self._register_handlers()
            logger.info("Telegram Bot initialized successfully")
            
            # Verify connection asynchronously and track the task
            v_task = asyncio.create_task(self.verify_connection())
            self._verify_tasks.append(v_task)
            # Cleanup finished verify tasks
            v_task.add_done_callback(lambda t: self._verify_tasks.remove(t) if t in self._verify_tasks else None)
        except Exception as e:
            import traceback
            logger.error(f"Failed to initialize Telegram Bot: {e}")
            logger.error(traceback.format_exc())
            self.bot = None
            self.is_connected = False

    async def _cleanup_bot(self, bot_instance=None):
        """Thoroughly clean up specified or current bot instance and its session"""
        target_bot = bot_instance or self.bot
        prefix = f"[Cleanup-Internal]" if bot_instance else f"[Cleanup-Main-ID:{self._current_polling_id}]"
        
        if target_bot:
            try:
                # Log with safe ID access (bot.id is an int)
                bot_id_str = str(getattr(target_bot, 'id', 'unknown'))
                logger.debug(f"{prefix} 🧹 正在清理 Bot 实例 (ID: {bot_id_str[:5]}...)")
                
                # 0. Cancel all pending verify tasks
                num_v = len(self._verify_tasks)
                for vt in self._verify_tasks[:]:
                    if not vt.done():
                        vt.cancel()
                self._verify_tasks.clear()
                if num_v > 0:
                    logger.debug(f"{prefix} 已取消 {num_v} 个验证任务")
                
                # 1. Webhook Cleanup (best effort, may fail if proxy is broken)
                try:
                    logger.debug(f"{prefix} 正在尝试删除 Webhook...")
                    await asyncio.wait_for(target_bot.delete_webhook(drop_pending_updates=True), timeout=3.0)
                    logger.debug(f"{prefix} ✅ Webhook 已删除")
                except asyncio.TimeoutError:
                    logger.debug(f"{prefix} Webhook 删除超时 (代理可能已失效)，跳过")
                except Exception as ex:
                    logger.debug(f"{prefix} Webhook 删除失败 (非致命): {ex}")

                # 2. 直接关闭 HTTP 会话 (强制断开所有 TCP 连接)
                if hasattr(target_bot, 'session') and target_bot.session:
                    try:
                        logger.debug(f"{prefix} 正在强制关闭 HTTP 会话...")
                        await target_bot.session.close()
                        logger.debug(f"{prefix} ✅ HTTP 会话已关闭，所有 TCP 连接已断开")
                    except Exception as ex:
                        logger.debug(f"{prefix} HTTP 会话关闭出错: {ex}")
            except Exception as e:
                logger.error(f"{prefix} ❌ 清理过程中发生严重错误: {e}")
            finally:
                if not bot_instance:
                    self.bot = None
                    self.dp = None
                    self.is_connected = False
                    logger.debug(f"{prefix} 状态变量已重置为 None")

    def _get_allowed_chats(self):
        if not settings.TG_ALLOW_CHATS:
            return []
        return [c.strip() for c in settings.TG_ALLOW_CHATS.split(",") if c.strip()]

    def _register_handlers(self):
        self.dp.message(Command("start"))(self.handle_start)
        self.dp.message(Command("help"))(self.handle_help)
        self.dp.message(Command("id"))(self.handle_id)
        self.dp.message()(self.handle_message)

    async def handle_start(self, message: types.Message):
        allowed = self._get_allowed_chats()
        if allowed and str(message.chat.id) not in allowed:
            logger.warning(f"Unauthorized chat access attempt for /start: {message.chat.id}")
            return
        help_text = (
            "👋 欢迎使用 P115-Share 机器人！\n\n"
            "直接发送 115 分享链接（支持 115.com, 115cdn.com, anxia.com），我将自动为你保存并创建长期分享。\n\n"
            "💡 可用命令：\n"
            "/start - 显示欢迎信息\n"
            "/help - 查看详细使用说明\n"
            "/id - 获取当前聊天的 ID (用于设置白名单)"
        )
        await message.answer(help_text)

    async def handle_help(self, message: types.Message):
        allowed = self._get_allowed_chats()
        if allowed and str(message.chat.id) not in allowed:
            logger.warning(f"Unauthorized chat access attempt for /help: {message.chat.id}")
            return
        await self.handle_start(message)

    async def handle_id(self, message: types.Message):
        allowed = self._get_allowed_chats()
        if allowed and str(message.chat.id) not in allowed:
            logger.warning(f"Unauthorized chat access attempt for /id: {message.chat.id}")
            return
        await message.answer(f"当前聊天 ID: `{message.chat.id}`", parse_mode="Markdown")

    async def handle_message(self, message: types.Message):
        # Whitelist check
        allowed = self._get_allowed_chats()
        if allowed and str(message.chat.id) not in allowed:
            logger.warning(f"Unauthorized chat access attempt: {message.chat.id}")
            return

        # Get message content - text from message or caption from photo message
        full_text = message.caption or message.text or ""
        photo = message.photo[-1] if message.photo else None  # Get highest resolution photo
        entities = message.caption_entities or message.entities or []
        
        # Debug logging
        logger.debug(f"📨 收到消息 - 文本长度: {len(full_text)}, 图片: {bool(photo)}, 实体数量: {len(entities)}")
        
        # Extract all URLs from entities (hyperlinks)
        entity_urls = []
        for entity in entities:
            if entity.type == "text_link" and hasattr(entity, 'url'):
                entity_urls.append(entity.url)
            elif entity.type == "url":
                start = entity.offset
                end = entity.offset + entity.length
                url = full_text[start:end]
                entity_urls.append(url)
        
        # 115 Link Detection (Regex)
        link_pattern = r'https?://(?:115\.com|115cdn\.com|anxia\.com)/s/[a-zA-Z0-9]+(?:[\?#][^ \s\n\r"\'<>]+)?'
        
        # Extract links from text and entity URLs
        text_links = re.findall(link_pattern, full_text)
        all_potential_links = text_links + [url for url in entity_urls if re.match(link_pattern, url)]
        
        # Deduplicate while preserving order
        share_urls = []
        seen = set()
        for url in all_potential_links:
            if url not in seen:
                share_urls.append(url)
                seen.add(url)
        
        if not share_urls:
            logger.debug(f"❌ 未检测到 115 链接 - 文本: '{full_text[:100]}...', 实体URLs: {entity_urls}")
            if not full_text.startswith("/"):
                await message.answer("⚠️ 请发送有效的 115 分享链接。\n支持域名: 115.com, 115cdn.com, anxia.com")
            return

        total_links = len(share_urls)
        logger.info(f"🎯 发现 {total_links} 个 115 链接，开始批量处理...")

        # 通过负载均衡选择本次处理使用的账号（用列表包装，闭包内可重新赋值）
        _svc_box, _acct_mgr_box = [None], [None]
        _svc_box[0], _acct_mgr_box[0] = _get_svc()
        if _acct_mgr_box[0] and _svc_box[0].account:
            asyncio.create_task(_acct_mgr_box[0].update_last_used(_svc_box[0].account.id))

        # 通知 P115 服务有来自 TG 的实时活动，以便批量任务进行让步
        _svc_box[0].update_tg_activity()

        status_msg = await message.answer(f"⌛️ 正在处理 {total_links} 个链接，请稍候...")

        # Prepare metadata entities common logic
        ser_entities = []
        if entities:
            for e in entities:
                try:
                    ser_entities.append(e.model_dump())
                except AttributeError:
                    ser_entities.append(dict(e))

        processed_links = {} # {original_url: share_link}

        async def process_single_link(share_url, index, segment_info=None):
            nonlocal _svc_box, _acct_mgr_box
            # 每次处理前检查当前 svc 是否被风控，被风控则立即切换账号
            if _svc_box[0].is_restricted:
                new_svc, new_acct_mgr = _get_svc()
                if new_svc is not _svc_box[0]:
                    logger.info(f"🔄 账号风控，批次内切换账号: [{getattr(_svc_box[0].account, 'id', '?')}] → [{getattr(new_svc.account, 'id', '?')}]")
                    _svc_box[0] = new_svc
                    _acct_mgr_box[0] = new_acct_mgr
                    if new_acct_mgr and new_svc.account:
                        asyncio.create_task(new_acct_mgr.update_last_used(new_svc.account.id))
            svc = _svc_box[0]
            acct_mgr = _acct_mgr_box[0]
            try:
                # 0. Check history first
                history_share_link = await svc.get_history_link(share_url)

                if history_share_link:
                    logger.info(f"✨ [{index}/{total_links}] 发现历史记录: {share_url}")
                    processed_links[share_url] = history_share_link
                    await message.reply(f"✅ 处理成功！\n长期分享链接：\n{history_share_link}")
                    await message.reply(f"🔔 链接保存成功！\n原链接: {share_url}\n新分享: {history_share_link}")
                    return True, history_share_link

                # 1. Check restriction & queue status
                q_size = svc.queue_size
                is_restricted = svc.is_restricted

                # 探测恢复逻辑：如果处于限制中，仅检查链接合法性，但不直接清除全局标志
                probing = False
                if is_restricted:
                    logger.info(f"🕵️ 限制期间嗅探探测: {share_url}")
                    status = await svc.get_share_status(share_url)
                    if status and not status.get("is_prohibited"):
                        logger.info("📡 嗅探发现链接有效，由于当前处于受限状态，将该链接作为’探路探测’执行...")
                        probing = True
                    else:
                        await message.reply(f"⏳ 115 账号当前处于接收限制中，系统将在解封后自动处理该链接。")
                        return # 链接无效或确认为持续受限（API不通）

                if not probing:
                    if q_size > 0 or svc.is_busy:
                        position = q_size + (1 if svc.is_busy else 0)
                        await message.reply(f"⏳ 系统繁忙，您的请求已加入队列（当前排在第 {position} 位），请稍候...")

                # 2. Save link with metadata
                # Use segmented metadata if available
                metadata = {
                    "description": full_text.strip(),
                    "full_text": segment_info["text"] if segment_info else full_text,
                    "photo_id": segment_info["photo_id"] if segment_info else (photo.file_id if photo else None),
                    "share_url": share_url,
                    "entities": segment_info["entities"] if segment_info else ser_entities
                }
                save_res = await svc.save_and_share(
                    share_url,
                    metadata=metadata,
                    skip_large_package=True,
                    db_id=None # 初次入库时不带ID
                )

                if save_res:
                    if save_res.get("status") == "success":
                        share_link = save_res.get("share_link")
                        if share_link:
                            await svc.save_history_link(share_url, share_link)
                            processed_links[share_url] = share_link
                            
                            # 处理递归保存中间产生的链接
                            recursive_links = save_res.get("recursive_links", [])
                            if recursive_links:
                                links_text = "\n".join([f"分卷 {idx}: {link}" for idx, link in enumerate(recursive_links, 1)])
                                await message.reply(f"📦 递归保存中产生的中间链接：\n{links_text}")

                            # Send detailed success messages to sender
                            await message.reply(f"✅ 处理成功！\n长期分享链接：\n{share_link}")
                            await message.reply(f"🔔 链接保存成功！\n原链接: {share_url}\n新分享: {share_link}")
                            return True, share_link
                    elif save_res.get("status") == "skipped":
                        msg_text = "⚠️ 此分享链接为大包，已跳过处理"
                        await message.reply(msg_text)
                        return "skipped", None
                    elif save_res.get("status") == "pending":
                        # Handle different pending reasons
                        reason = save_res.get("reason", "auditing")
                        if reason == "restricted":
                            logger.info(f"🚫 账号受限排队中: {share_url}")
                        else:
                            logger.info(f"🔍 分享链接正在审核中: {share_url}")
                        
                        asyncio.create_task(self.poll_pending_link(message, save_res))
                        return save_res, None
                    elif save_res.get("status") == "error":
                        error_type = save_res.get("error_type")
                        error_msg = save_res.get("message") or "未知错误"
                        logger.warning(f"⚠️ 处理链接失败 ({error_type}): {error_msg}")
                        return save_res, None
                
                # Generic failure without specific error info
                return {"error_type": "unknown", "message": "处理过程中发生未知错误"}, None
            except Exception as e:
                logger.error(f"❌ 处理链接出错 {share_url}: {e}")
                return {"error_type": "exception", "message": str(e)}, None

        # Prepare segments for broadcasting
        # We find the positions of all share URLs in the original text (UTF-16)
        text_utf16_len = self._get_utf16_len(full_text)
        link_positions = [] # [(start_u16, end_u16, url)]
        
        # Search for each URL's position to define segment boundaries
        for url in share_urls:
            start_char = full_text.find(url)
            if start_char != -1:
                start_u16 = self._get_utf16_len(full_text[:start_char])
                end_char = start_char + len(url)
                end_u16 = start_u16 + self._get_utf16_len(url)
                link_positions.append((start_u16, end_u16, url))
        
        # Sort by start position
        link_positions.sort()
        
        # Smart segmentation: Find appropriate boundaries that work for both scenarios:
        # - Title before link: "title\nlink\n\ntitle2\nlink2"
        # - Title after link: "link\ntitle\n\nlink2\ntitle2"
        # Strategy: Segment from last boundary to current link, then extend to a natural break point.
        
        last_boundary = 0
        segments = [] # List of (segmented_text, segmented_entities, target_url)
        for idx, pos in enumerate(link_positions):
            start_u16, end_u16, url = pos
            
            # Default: end at current link's end (works for title-before-link)
            seg_end = end_u16
            
            # For non-last links, try to find a better boundary
            if idx < len(link_positions) - 1:
                next_start_u16 = link_positions[idx + 1][0]
                # Get text between current link end and next link start
                between_start_char = len(full_text.encode('utf-16-le')[:end_u16*2].decode('utf-16-le', errors='ignore'))
                between_end_char = len(full_text.encode('utf-16-le')[:next_start_u16*2].decode('utf-16-le', errors='ignore'))
                between_text = full_text[between_start_char:between_end_char]
                
                # Look for double newline as a natural separator
                double_newline_pos = between_text.find('\n\n')
                if double_newline_pos != -1:
                    # Found a paragraph break, split there
                    split_char = between_start_char + double_newline_pos + 2  # +2 to include the \n\n
                    seg_end = self._get_utf16_len(full_text[:split_char])
                else:
                    # No clear separator; use a heuristic
                    # If there's significant content after the link, include some of it
                    if len(between_text.strip()) > 10:
                        # Likely title-after-link scenario, extend to next link start
                        seg_end = next_start_u16
                    # Otherwise keep seg_end = end_u16 (title-before-link)
            else:
                # Last link: extend to end of text
                seg_end = text_utf16_len
            
            slice_text, slice_entities = self._slice_message(full_text, ser_entities, last_boundary, seg_end)
            segments.append({
                "text": slice_text,
                "entities": slice_entities,
                "url": url,
                "photo_id": photo.file_id if photo else None
            })
            last_boundary = seg_end

        # Process links sequentially
        success_count = 0
        pending_count = 0
        failed_count = 0
        skip_count = 0
        failed_details = []  # Store failed link details: [(url, error_msg)]
        
        last_res = None
        for i, url in enumerate(share_urls, 1):
            if total_links > 1:
                await status_msg.edit_text(f"⏳ 正在处理第 {i}/{total_links} 个链接...")
            
            # Find the segment for this specific URL
            target_segment = next((s for s in segments if s["url"] == url), None)
            
            res, share_link = await process_single_link(url, i, target_segment)
            last_res = res

            if res is True:
                success_count += 1
                # Broadcast this segment IMMEDIATELY
                if target_segment:
                    await self.broadcast_to_channels(
                        {url: share_link}, 
                        {
                            "full_text": target_segment["text"],
                            "entities": target_segment["entities"],
                            "photo_id": target_segment["photo_id"]
                        }
                    )
                else:
                    # URL not found in visible text (e.g. text_link entity),
                    # broadcast with the full original message metadata
                    await self.broadcast_to_channels(
                        {url: share_link},
                        {
                            "full_text": full_text,
                            "entities": ser_entities,
                            "photo_id": photo.file_id if photo else None
                        }
                    )
            elif res == "pending":
                pending_count += 1
            elif res == "skipped":
                skip_count += 1
            else:
                failed_count += 1
                # Record failure details
                error_msg = "未知错误"
                if isinstance(res, dict):
                    err_type = res.get("error_type")
                    if err_type == "expired":
                        error_msg = "链接已过期"
                    elif err_type == "violated":
                        error_msg = "包含违规内容"
                    elif res.get("message"):
                        error_msg = res.get("message")
                failed_details.append((url, error_msg))
        
        if total_links == 1:
            if success_count == 1:
                # For single successful link, delete the processing status message to reduce clutter
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            elif isinstance(last_res, dict) and last_res.get("status") == "pending":
                # For single pending link, use a more friendly message based on reason
                reason = last_res.get("reason", "auditing")
                if reason == "restricted":
                    await status_msg.edit_text("⏳ 115 账号当前处于接收限制中，系统将在解封后自动处理")
                elif reason == "snapshotting":
                    await status_msg.edit_text("🔍 分享链接正在生成快照，系统将自动重试处理")
                else:
                    await status_msg.edit_text("🔍 分享链接正在审核中，将在审核通过后，进行保存分享处理")
            elif skip_count == 1:
                # For single skipped link, delete the processing status message
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            else:
                # For single failed link
                error_text = "❌ 处理完成，但链接处理失败。"
                if isinstance(last_res, dict):
                    err_type = last_res.get("error_type")
                    if err_type == "expired":
                        error_text = "⚠️ 分享链接已过期"
                    elif err_type == "violated":
                        error_text = f"🚫 {last_res.get('message') or '分享链接包含违规内容'}"
                    elif last_res.get("message"):
                        error_text = f"❌ {last_res.get('message')}"
                
                if error_text.startswith("❌ 处理完成"):
                    await status_msg.edit_text(f"{error_text}\n\n成功: 0\n❌ 失败: 1")
                else:
                    await status_msg.edit_text(error_text)
        else:
            # Final notification for batch
            result_text = f"✅ 批量处理完成！\n\n成功: {success_count}\n"
            if pending_count:
                result_text += f"⏳ 审核/快照中 (转换后自动发布): {pending_count}\n"
            if skip_count:
                result_text += f"⏭️ 已跳过 (大包限制): {skip_count}\n"
            if failed_count:
                result_text += f"❌ 失败: {failed_count}\n"
                # Add detailed failure information
                if failed_details:
                    result_text += "\n📋 失败详情：\n"
                    for idx, (failed_url, error_msg) in enumerate(failed_details, 1):
                        # Shorten URL to make it more readable
                        short_url = failed_url if len(failed_url) <= 50 else failed_url[:47] + "..."
                        result_text += f"{idx}. {error_msg}\n   {short_url}\n"
            
            await status_msg.edit_text(result_text)

        # Broadcast removed from here because it's done segment-wise in the loop

        # Notify admin if configured
        if settings.TG_USER_ID and str(message.chat.id) != str(settings.TG_USER_ID):
            try:
                admin_msg = f"📢 用户 {message.chat.id} 提交了 {total_links} 个链接\n\n"
                admin_msg += f"成功: {success_count}\n"
                if pending_count:
                    admin_msg += f"⏳ 审核中: {pending_count}\n"
                if failed_count:
                    admin_msg += f"❌ 失败: {failed_count}\n"
                    if failed_details:
                        admin_msg += "\n失败详情：\n"
                        for idx, (failed_url, error_msg) in enumerate(failed_details[:3], 1):  # Show max 3 to admin
                            short_url = failed_url if len(failed_url) <= 40 else failed_url[:37] + "..."
                            admin_msg += f"{idx}. {error_msg}: {short_url}\n"
                        if len(failed_details) > 3:
                            admin_msg += f"... 还有 {len(failed_details) - 3} 个失败链接"
                
                await self.send_admin_msg(admin_msg)
            except Exception as e:
                logger.error(f"Failed to notify admin: {e}")

    async def send_admin_msg(self, text: str):
        """Send a message to the admin user"""
        if not self.bot or not settings.TG_USER_ID:
            return
        try:
            await self.bot.send_message(settings.TG_USER_ID, text)
        except Exception as e:
            logger.error(f"Failed to send admin message: {e}")


    async def poll_pending_link(self, message: types.Message, pending_info: dict, silent: bool = False):
        """Poll the status of a pending link and process it when ready"""
        share_url = pending_info["share_url"]
        metadata = pending_info.get("metadata", {})
        reason = pending_info.get("reason", "auditing")

        # 正在生成快照或受限的链接轮询频率较低
        if reason == "snapshotting":
            interval = 1800
        elif reason == "restricted":
            interval = 3600
            if not silent:
                await message.reply(f"⚠️ 触发 115 账号限制（接收/分享），该链接已进入排队，将每小时尝试一次。或者您可以发来一个新的有效链接，系统将通过该链接嗅探限制是否解除。\n链接: {share_url}")
        else:
            interval = 300

        max_attempts = 36

        logger.info(f"⏳ 开始为链接启动轮询任务 (原因: {reason}, 间隔: {interval}s): {share_url}")

        for attempt in range(1, max_attempts + 1):
            # 动态计算本次循环的等待时间 (针对审核中链接增加 1min, 2min 前置尝试)
            if reason == "auditing":
                if attempt == 1:
                    current_interval = 60
                elif attempt == 2:
                    current_interval = 120
                else:
                    current_interval = 300
            else:
                current_interval = interval

            # 每次轮询重新选择最优账号
            svc, acct_mgr = _get_svc()

            if reason == "restricted":
                # 分布休眠以便能实时响应全局限制的消除
                slept = 0
                while slept < current_interval:
                    if not svc.is_restricted:
                        logger.info(f"🔓 检测到全局限制已解除，立即触发队列重试: {share_url}")
                        break
                    await asyncio.sleep(5)
                    slept += 5
            else:
                await asyncio.sleep(current_interval)

            logger.info(f"🔄 正在进行第 {attempt}/{max_attempts} 次审核状态检查: {share_url}")
            status_info = await svc.get_share_status(share_url)

            if status_info is None:
                logger.warning(f"⚠️ 无法获取检查状态，将在下次重试: {share_url}")
                continue

            if status_info["is_prohibited"]:
                logger.warning(f"⚠️ 轮询检测到链接包含违规内容标志: {share_url}")
                # 不再直接终止，允许在后续 is_auditing 为 false 时尝试转存


            if status_info["is_expired"]:
                logger.warning(f"⏰ 轮询检测到链接已过期: {share_url}")
                await message.reply(f"⏰ 链接已失效：在审核期间该分享已过期。\n链接: {share_url}")
                await self._delete_pending_task(pending_info.get("db_id"))
                return

            if not status_info["is_pending"]:  # Not pending anymore (Audit passed and Snapshot ready)
                # 如果之前处于受限状态，尝试转存前先清理限制标志（如果还没过时间，但外部可能解除了）
                if reason == "restricted":
                    svc.clear_restriction()

                logger.info(f"🎉 链接可以开始处理 (status: {status_info['share_state']}): {share_url}")
                if acct_mgr and svc.account:
                    asyncio.create_task(acct_mgr.update_last_used(svc.account.id))
                save_res = await svc.save_and_share(share_url, metadata=metadata, db_id=pending_info.get("db_id"))
                
                if save_res and save_res.get("status") == "success":
                    logger.info(f"✅ 审核通过后转存成功: {share_url}")
                    share_link = save_res.get("share_link")
                    
                    if share_link:
                        await svc.save_history_link(share_url, share_link)
                        # Broadcast single successful link from poll
                        await self.broadcast_to_channels({share_url: share_link}, metadata)
                        
                        # Use the title if available or a generic success msg
                        success_text = f"✅ 处理完成！\n原链接: {share_url}\n新分享: {share_link}"
                        
                        # 管理员通知不受 silent 影响（除非 chat ID 本身就是管理员）
                        is_admin_chat = settings.TG_USER_ID and str(message.chat.id) == str(settings.TG_USER_ID)
                        
                        if not silent:
                            await message.reply(success_text)
                        elif not is_admin_chat:
                            # 如果静默模式且当前不是管理员私聊，则单独发给管理员一份
                            try:
                                await self.send_admin_msg(f"🔔 [后台任务] {success_text}")
                            except Exception:
                                pass
                        
                        # 原本就有的广播给管理员的逻辑（针对非管理员用户的请求）
                        if not is_admin_chat:
                            try:
                                await self.bot.send_message(settings.TG_USER_ID, f"🔔 [后台处理] {success_text}")
                            except Exception:
                                pass
                    await self._delete_pending_task(pending_info.get("db_id"))
                    return 
                elif save_res and save_res.get("status") == "pending":
                    new_reason = save_res.get("reason", reason)
                    logger.warning(f"⚠️ 尝试转存时再次返回排队状态(原因: {new_reason})，维持轮询: {share_url}")
                    reason = new_reason
                    continue
                else:
                    logger.error(f"❌ 审核通过后转存仍然失败: {share_url}")
                    error_msg = "自动转存失败，请手动尝试"
                    if isinstance(save_res, dict):
                        if save_res.get("error_type") == "violated":
                            error_msg = save_res.get("message") or "链接包含违规内容，无法转存分享"
                        elif save_res.get("message"):
                            error_msg = save_res.get("message")
                    
                    if not silent:
                        await message.reply(f"❌ 链接审核已通过，但{error_msg}: {share_url}")
                    await self._delete_pending_task(pending_info.get("db_id"))
                    return
        
        logger.warning(f"⏰ 链接审核轮询超时 (3小时): {share_url}")
        await message.reply(f"⏰ 链接审核轮询超时 (已持续 3 小时)，请稍后手动检查: {share_url}")
        await self._delete_pending_task(pending_info.get("db_id"))

    def _slice_message(self, text: str, entities: list, start_u16: int, end_u16: int) -> tuple[str, list]:
        """Slice message and entities to a specific UTF-16 range"""
        # Encode to UTF-16-LE to work with offsets
        u16_text = text.encode('utf-16-le')
        # Each code unit is 2 bytes
        slice_u16 = u16_text[start_u16*2:end_u16*2]
        new_text = slice_u16.decode('utf-16-le')
        
        new_entities = []
        if entities:
            for e in entities:
                is_dict = isinstance(e, dict)
                offset = e.get("offset") if is_dict else e.offset
                length = e.get("length") if is_dict else e.length
                
                # Check if entity is within the slice
                if offset >= start_u16 and (offset + length) <= end_u16:
                    # Fully contained
                    new_offset = offset - start_u16
                    if is_dict:
                        e_copy = e.copy()
                        e_copy["offset"] = new_offset
                        new_entities.append(e_copy)
                    else:
                        e_copy = e.model_dump() if hasattr(e, "model_dump") else dict(e)
                        e_copy["offset"] = new_offset
                        new_entities.append(e_copy)
                elif offset < end_u16 and (offset + length) > start_u16:
                    # Partially contained - slice it
                    o_start = max(offset, start_u16)
                    o_end = min(offset + length, end_u16)
                    new_offset = o_start - start_u16
                    new_length = o_end - o_start
                    if is_dict:
                        e_copy = e.copy()
                        e_copy["offset"] = new_offset
                        e_copy["length"] = new_length
                        new_entities.append(e_copy)
                    else:
                        e_copy = e.model_dump() if hasattr(e, "model_dump") else dict(e)
                        e_copy["offset"] = new_offset
                        e_copy["length"] = new_length
                        new_entities.append(e_copy)
                        
        return new_text, new_entities

    def _get_utf16_len(self, text: str) -> int:
        """Calculate length in UTF-16 code units"""
        return len(text.encode('utf-16-le')) // 2

    async def _post_to_single_channel_batch(self, channel_config: dict, share_links_map: dict, metadata: dict):
        """Post to a single channel with multiple link replacements"""
        channel_id = channel_config.get("id")
        is_concise = channel_config.get("concise", False)
        
        if not channel_id:
            return
            
        full_text = metadata.get("full_text", "")
        photo_id = metadata.get("photo_id")
        entities_raw = metadata.get("entities", [])
        
        from aiogram.types import MessageEntity
        entities = []
        for e in entities_raw:
            if isinstance(e, dict):
                try: entities.append(MessageEntity(**e))
                except Exception: pass
            else:
                entities.append(e)

        try:
            if is_concise:
                for original_url, share_link in share_links_map.items():
                    if isinstance(share_link, list) and len(share_link) > 1:
                        links_text = "\n".join([f"分卷 {i+1}：{lnk}" for i, lnk in enumerate(share_link)])
                        await self.bot.send_message(channel_id, f"✅ 处理成功！\n{links_text}")
                    else:
                        actual_link = share_link[0] if isinstance(share_link, list) and share_link else share_link
                        await self.bot.send_message(channel_id, f"✅ 处理成功！\n链接：{actual_link}")
                return

            # Batch replacement logic
            new_text = full_text
            new_entities = entities
            
            # 1. Replace all URLs (in text and entities)
            for old_url, new_url_val in share_links_map.items():
                if not old_url: continue
                
                # Format list to multi-part links if needed
                if isinstance(new_url_val, list):
                    if len(new_url_val) > 1:
                        new_url = "\n" + "\n".join([f"分卷 {i+1}：{lnk}" for i, lnk in enumerate(new_url_val)])
                    elif new_url_val:
                        new_url = new_url_val[0]
                    else:
                        new_url = ""
                else:
                    new_url = new_url_val

                new_text, new_entities = self._replace_text_and_adjust_entities(
                    new_text, new_entities, old_url, new_url
                )
            
            # 2. Update all access codes
            new_text, new_entities = self._update_access_codes(new_text, new_entities, share_links_map)

            if photo_id:
                max_len_utf16 = 1024
                current_len_utf16 = self._get_utf16_len(new_text)
                if current_len_utf16 > max_len_utf16:
                    new_text_encoded = new_text.encode('utf-16-le')
                    new_text = new_text_encoded[:max_len_utf16 * 2].decode('utf-16-le', errors='ignore')
                    if new_entities:
                        final_len_utf16 = self._get_utf16_len(new_text)
                        valid_entities = []
                        for e in new_entities:
                            if e.offset < final_len_utf16:
                                if e.offset + e.length > final_len_utf16:
                                    e.length = final_len_utf16 - e.offset
                                valid_entities.append(e)
                        new_entities = valid_entities

                await self.bot.send_photo(
                    channel_id, 
                    photo=photo_id, 
                    caption=new_text,
                    caption_entities=new_entities
                )
            else:
                await self.bot.send_message(
                    channel_id, 
                    text=new_text,
                    entities=new_entities,
                    disable_web_page_preview=False
                )
            logger.info(f"已将推送发送至频道: {channel_id}")
        except Exception as e:
            logger.error(f"Failed to post to channel {channel_id}: {e}")

    def _update_access_codes(self, text: str, entities: list, share_links_map: dict) -> tuple[str, list]:
        """Update access codes in text to match new links for multiple pairs"""
        from urllib.parse import urlparse, parse_qs
        import re
        
        current_text = text
        current_entities = list(entities)
        
        # Sort original URLs by their appearance in text
        sorted_originals = sorted(share_links_map.keys(), key=lambda url: text.find(url) if url in text else 999999)

        for old_url in sorted_originals:
            share_link_val = share_links_map[old_url]
            # If it's a list (multi-part share), use the first link to parse the password
            # Since all parts usually share the same password setting
            if isinstance(share_link_val, list):
                if not share_link_val:
                    continue
                share_link = share_link_val[0]
            else:
                share_link = share_link_val

            parsed = urlparse(share_link)
            params = parse_qs(parsed.query)
            new_pwd = params.get("password", [""])[0]
            
            if not new_pwd:
                continue

            pwd_patterns = [
                r'((?:访问码|提取码|密码)(?:：|:|%EF%BC%9A|%3A)\s*)([a-zA-Z0-9]{4})',
                r'((?:%E8%AE%BF%E9%97%AE%E7%A0%81|%E6%8F%90%E5%8F%96%E7%A0%81|%E5%AF%86%E7%A0%81)(?:%EF%BC%9A|%3A)(?:%20)*)([a-zA-Z0-9]{4})'
            ]
            
            search_start = 0
            if old_url in current_text:
                search_start = current_text.find(old_url)
            elif share_link in current_text:
                search_start = current_text.find(share_link)
            
            best_match = None
            best_start = 999999
            
            for pattern in pwd_patterns:
                for match in re.finditer(pattern, current_text[search_start:], flags=re.IGNORECASE):
                    start = search_start + match.start()
                    if start < best_start:
                        best_start = start
                        best_match = match
                
            if best_match:
                prefix, old_code = best_match.groups()
                if old_code != new_pwd:
                    old_str = f"{prefix}{old_code}"
                    new_str = f"{prefix}{new_pwd}"
                    current_text, current_entities = self._replace_text_and_adjust_entities(
                        current_text, current_entities, old_str, new_str
                    )
                    
        return current_text, current_entities

    def _replace_text_and_adjust_entities(self, text: str, entities: list, old_str: str, new_str: str):
        """Helper to replace text and shift entity offsets/lengths accordingly"""
        has_text_match = old_str in text
        
        if not has_text_match:
            # Check if any text_link URL matches
            new_entities = []
            changed = False
            for entity in entities:
                if hasattr(entity, 'url') and entity.url == old_str:
                    entity.url = new_str
                    changed = True
                new_entities.append(entity)
            return text, new_entities

        start_pos_char = text.find(old_str)
        end_pos_char = start_pos_char + len(old_str)
        
        start_pos_u16 = self._get_utf16_len(text[:start_pos_char])
        old_len_u16 = self._get_utf16_len(old_str)
        end_pos_u16 = start_pos_u16 + old_len_u16
        new_len_u16 = self._get_utf16_len(new_str)
        diff_u16 = new_len_u16 - old_len_u16
        
        new_text = text[:start_pos_char] + new_str + text[end_pos_char:]

        new_entities = []
        if entities:
            from aiogram.types import MessageEntity
            for entity in entities:
                is_dict = isinstance(entity, dict)
                e_offset = entity.get("offset") if is_dict else entity.offset
                e_length = entity.get("length") if is_dict else entity.length
                e_url = (entity.get("url") if is_dict else getattr(entity, "url", None))
                e_type = entity.get("type") if is_dict else entity.type
                
                if e_offset >= end_pos_u16:
                    e_offset += diff_u16
                elif e_offset <= start_pos_u16 and (e_offset + e_length) >= end_pos_u16:
                    e_length += diff_u16
                elif e_offset == start_pos_u16 and e_length == old_len_u16:
                    e_length = new_len_u16
                
                if e_url == old_str:
                    e_url = new_str

                new_entities.append(MessageEntity(
                    type=e_type,
                    offset=e_offset,
                    length=e_length,
                    url=e_url,
                    user=entity.get("user") if is_dict else getattr(entity, "user", None),
                    language=entity.get("language") if is_dict else getattr(entity, "language", None),
                    custom_emoji_id=entity.get("custom_emoji_id") if is_dict else getattr(entity, "custom_emoji_id", None)
                ))
        return new_text, new_entities

    async def get_chat_info(self, chat_id: str):
        """Fetch chat info (title, type) from Telegram"""
        if not self.bot:
            return None
        try:
            chat = await self.bot.get_chat(chat_id)
            return {"id": str(chat.id), "title": chat.title, "type": chat.type}
        except Exception as e:
            logger.error(f"Failed to get chat info for {chat_id}: {e}")
            return None

    async def broadcast_to_channels(self, share_links_map: dict, metadata: dict, channel_ids: list = None):
        """Broadcast processed link(s) to all configured and enabled channels
        :param channel_ids: Optional list of channel IDs to filter the broadcast. If None, send to all enabled.
        """
        import json
        channels = []
        try:
            channels = json.loads(settings.TG_CHANNELS)
        except Exception:
            pass
            
        legacy_id = settings.TG_CHANNEL_ID
        if legacy_id and not any(c.get("id") == str(legacy_id) for c in channels):
            channels.append({"id": str(legacy_id), "enabled": True, "concise": False})
            
        enabled_channels = [c for c in channels if c.get("enabled")]
        
        # Filter by specific channel_ids if requested (e.g. for batch tasks)
        if channel_ids is not None:
            target_ids = set(str(cid) for cid in channel_ids)
            enabled_channels = [c for c in enabled_channels if str(c.get("id")) in target_ids]
        else:
            # 普通消息推送：仅发送至开启了“自动转发”的频道
            # 注意：默认为 True 以保证向后兼容性 (已有频道默认开启)
            enabled_channels = [c for c in enabled_channels if c.get("auto_forward", True)]
        
        if not enabled_channels:
            logger.debug(f"没有符合条件的目标频道 (channel_ids={channel_ids})，跳过广播")
            return
            
        for chan in enabled_channels:
            is_concise = chan.get("concise", False)
            if is_concise:
                # Concise mode: Every success link gets a separate simple message
                for original_url, share_link in share_links_map.items():
                    temp_meta = metadata.copy()
                    temp_meta["share_url"] = original_url
                    await self._post_to_single_channel(chan, share_link, temp_meta)
            else:
                # Normal mode: Single broadcast with all links replaced
                await self._post_to_single_channel_batch(chan, share_links_map, metadata)

    async def _post_to_single_channel(self, channel_config: dict, share_link: str, metadata: dict):
        """Legacy helper for single link post (still used by poll_pending or concise)"""
        await self._post_to_single_channel_batch(channel_config, {metadata.get("share_url", ""): share_link}, metadata)

    async def _delete_pending_task(self, db_id: int):
        if db_id:
            from app.core.database import async_session
            from app.models.schema import PendingLink
            from sqlalchemy import delete
            async with async_session() as session:
                await session.execute(delete(PendingLink).where(PendingLink.id == db_id))
                await session.commit()

    async def recover_pending_tasks(self):
        from app.core.database import async_session
        from app.models.schema import PendingLink
        from sqlalchemy import select
        async with async_session() as session:
            result = await session.execute(select(PendingLink).where(PendingLink.status.in_(["auditing", "snapshotting", "restricted"])))
            tasks = result.scalars().all()
            if tasks:
                # 统计受限链接并发送汇总消息
                restricted_tasks = [t for t in tasks if t.status == "restricted"]
                if restricted_tasks:
                    svc, _ = _get_svc()
                    if svc.is_restricted:
                        summary_msg = f"目前 115 云盘账号已被限制接收分享文件，共有 {len(restricted_tasks)} 个链接已进入排队队列，将在检测到解除限制后，开始处理队列中的任务。"
                        await self.send_admin_msg(summary_msg)
                
                for task in tasks:
                    pending_info = {
                        "share_url": task.share_url, 
                        "metadata": task.metadata_json, 
                        "db_id": task.id,
                        "reason": task.status
                    }
                    asyncio.create_task(self._recovered_poll(pending_info))

    async def _recovered_poll(self, pending_info: dict):
        class MockMessage:
            def __init__(self, bot, user_id):
                self.bot = bot
                self.chat = type('obj', (object,), {'id': user_id})
            async def reply(self, text):
                try: await self.bot.send_message(self.chat.id, text)
                except Exception: pass
        user_id = settings.TG_USER_ID or "0"
        mock_msg = MockMessage(self.bot, user_id)
        await self.poll_pending_link(mock_msg, pending_info, silent=True)

    async def verify_connection(self) -> bool:
        if not self.bot:
            self.is_connected = False
            return False
        try:
            # 添加超时控制，避免长时间阻塞
            me = await asyncio.wait_for(self.bot.get_me(), timeout=10.0)
            if me:
                self.is_connected = True
                logger.info(f"✅ Telegram Bot 连接验证成功: @{me.username}")
                return True
        except asyncio.TimeoutError:
            logger.warning("⏱️ Telegram Bot 连接验证超时")
            self.is_connected = False
            return False
        except Exception as e:
            logger.warning(f"❌ Telegram Bot 连接验证失败: {e}")
            self.is_connected = False
            return False
        self.is_connected = False
        return False

    async def start_polling(self):
        if not self.dp or not self.bot:
            return

        self._current_polling_id += 1
        polling_id = self._current_polling_id

        logger.info(f"🚀 Telegram Bot 启动轮询 (Polling ID: {polling_id})")

        retry_count = 0
        max_retry_delay = 300  # 最大重试间隔 5 分钟

        while polling_id == self._current_polling_id:
            try:
                # 验证连接
                connection_ok = await self.verify_connection()

                if connection_ok:
                    retry_count = 0  # 重置重试计数
                    await self.dp.start_polling(self.bot, skip_updates=True, handle_signals=False)
                    # 正常退出
                    if polling_id != self._current_polling_id:
                        break
                else:
                    # 连接失败，使用指数退避
                    retry_count += 1
                    delay = min(10 * (2 ** min(retry_count - 1, 5)), max_retry_delay)
                    logger.warning(f"Bot 未连接，将在 {delay} 秒后重试 (第 {retry_count} 次)...")
                    await asyncio.sleep(delay)
                    continue

            except Exception as e:
                self.is_connected = False
                retry_count += 1
                delay = min(10 * (2 ** min(retry_count - 1, 5)), max_retry_delay)
                logger.error(f"Polling error: {e}，将在 {delay} 秒后重试 (第 {retry_count} 次)...")

            # 异常退出，等待后重试
            if polling_id == self._current_polling_id:
                if retry_count == 0:
                    retry_count = 1
                delay = min(10 * (2 ** min(retry_count - 1, 5)), max_retry_delay)
                await asyncio.sleep(delay)

    async def stop_polling(self):
        if self.dp:
            try: await self.dp.stop_polling()
            except Exception: pass
        if self.polling_task and not self.polling_task.done():
            try: await asyncio.wait_for(asyncio.shield(self.polling_task), timeout=3.0)
            except asyncio.TimeoutError:
                self.polling_task.cancel()
                try: await self.polling_task
                except: pass
            self.polling_task = None

    async def restart_polling(self):
        async with self._lock:
            await self.stop_polling()
            await self._cleanup_bot()
            await asyncio.sleep(5)
            if not settings.TG_BOT_TOKEN: return
            self.init_bot(settings.TG_BOT_TOKEN)
            if not self.bot: return
            try: await self.bot.delete_webhook(drop_pending_updates=True)
            except: pass
            await asyncio.sleep(2)
            self.polling_task = asyncio.create_task(self.start_polling())

    async def test_send_to_user(self):
        if not self.bot or not settings.TG_USER_ID: return False, "未配置"
        try:
            await self.bot.send_message(settings.TG_USER_ID, "🔔 测试成功")
            self.is_connected = True
            return True, "成功"
        except Exception as e: 
            self.is_connected = False
            return False, str(e)

    async def test_send_to_channel(self, channel_id: str = None):
        target_id = channel_id or settings.TG_CHANNEL_ID
        if not self.bot or not target_id: return False, "未配置"
        try:
            await self.bot.send_message(target_id, "📢 测试成功")
            self.is_connected = True
            return True, "成功"
        except Exception as e: 
            self.is_connected = False
            return False, str(e)

tg_service = TGService()

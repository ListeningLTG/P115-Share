import asyncio
import logging
import os
import sys
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.core.config import settings
from app.api.config import router as config_router
from app.api.auth import router as auth_router
from app.api.excel import router as excel_router
from app.api.share import router as share_router
from app.api.accounts import router as accounts_router
from app.api.sensitive import router as sensitive_router
from app.api.scheduled_share import router as scheduled_share_router
from app.services.tg_bot import tg_service
from app.version import VERSION

# 启动时清除进程级代理环境变量，防止 docker-compose 注入的 HTTP_PROXY 等
# 被 httpcore/aiohttp 自动读取，导致 P115Client 意外走代理
for _proxy_key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_key, None)

# Setup Loguru to capture standard logging
class InterceptHandler(logging.Handler):
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

# Determine log level
log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
logging.basicConfig(handlers=[InterceptHandler()], level=log_level, force=True)

# WebSocket Log Broadcaster
class LogBroadcast:
    def __init__(self, max_history=100):
        self.active_connections: list[WebSocket] = []
        self.history = deque(maxlen=max_history)
        self.loop = None

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        # Send history first
        for msg in self.history:
            try:
                await websocket.send_text(msg)
            except Exception:
                pass
        self.active_connections.append(websocket)
        if not self.loop:
            self.loop = asyncio.get_running_loop()

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    def broadcast(self, message: str):
        self.history.append(message)
        if not self.loop or not self.active_connections:
            return
        
        # Ensure we schedule the send task in the right loop
        for connection in list(self.active_connections):
            asyncio.run_coroutine_threadsafe(self._send_safe(connection, message), self.loop)

    async def _send_safe(self, websocket: WebSocket, message: str):
        try:
            await websocket.send_text(message)
        except Exception:
            self.disconnect(websocket)

log_broadcast = LogBroadcast()

# Intercept Loguru logs to send to WebSocket
def websocket_sink(message):
    log_broadcast.broadcast(str(message))

# Create logs directory
LOG_DIR = "data/logs"
os.makedirs(LOG_DIR, exist_ok=True)

logger.add(websocket_sink, format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{line} | {message}", level=settings.LOG_LEVEL, enqueue=True)
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | <cyan>{name}:{line}</cyan> | {message}", level=settings.LOG_LEVEL, enqueue=True)
logger.add(
    os.path.join(LOG_DIR, "p115share_{time:YYYY-MM-DD}.log"),
    rotation="00:00",
    retention="7 days",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{line} | {message}",
    encoding="utf-8",
    level=settings.LOG_LEVEL,
    enqueue=True
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info(f"P115-Share API {VERSION} starting up...")
    
    # Init DB and migrate settings
    await settings.init_db()

    # 初始化多账号管理器（包含旧版配置迁移）
    from app.services.account_manager import account_manager
    await account_manager.initialize()

    # Re-initialize services with loaded settings
    from app.services.tg_bot import tg_service

    # Start telegram bot
    if settings.TG_BOT_TOKEN:
        if not tg_service.bot:
            tg_service.init_bot(settings.TG_BOT_TOKEN)
        tg_service.polling_task = asyncio.create_task(tg_service.start_polling())
        # Recover pending tasks from DB
        await tg_service.recover_pending_tasks()
    
    # Start cleanup scheduler  
    from app.services.scheduler import cleanup_scheduler
    cleanup_scheduler.start()
    await cleanup_scheduler.sync_all_scheduled_share_jobs()
    
    # Excel Batch recovery and logic
    from app.services.excel_batch import excel_batch_service
    await excel_batch_service.recover_tasks()
    
    yield
    
    # Shutdown
    from app.services.excel_batch import excel_batch_service
    await excel_batch_service.shutdown()
    cleanup_scheduler.shutdown()
    logger.info("P115-Share API shutting down...")

app = FastAPI(
    title="P115-Share API",
    version=VERSION,
    lifespan=lifespan
)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routers BEFORE catch-all route
app.include_router(auth_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(excel_router, prefix="/api")
app.include_router(share_router, prefix="/api")
app.include_router(accounts_router, prefix="/api")
app.include_router(sensitive_router, prefix="/api")
app.include_router(scheduled_share_router, prefix="/api")

# Mount static files separately (highest priority for /static)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    # API and WebSocket routes should be handled by their routers
    if full_path.startswith("api/") or full_path.startswith("ws/"):
        return {"detail": "Not Found"}
    
    # Path to static folder
    static_dir = "static"
    
    # Try to find the actual file (strip 'static/' prefix if present in catch-all)
    lookup_path = full_path
    if lookup_path.startswith("static/"):
        lookup_path = lookup_path[7:]
        
    file_path = os.path.join(static_dir, lookup_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    
    # Default to index.html for SPA support if file not found
    index_path = os.path.join(static_dir, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    
    return {"detail": "Frontend not found"}

@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await log_broadcast.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        log_broadcast.disconnect(websocket)

@app.get("/")
async def root():
    return {"status": "ok", "version": VERSION, "message": "P115-Share API is running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=settings.WEB_PORT)

# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。
from contextlib import asynccontextmanager
import asyncio
import logging
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from core.runtime_settings import validate_startup, env_flag, sanitize_no_proxy

load_dotenv()
# 必须在任何 httpx/OpenAI 客户端构造之前执行：某些环境的 NO_PROXY 含 "[::1]"，
# 会让 httpx 抛 InvalidURL 并在 /research/stream 上表现为 500（详见该函数注释）。
_sanitized = sanitize_no_proxy()
SETTINGS = validate_startup()  # before importing/initializing clients or writing database state
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
if _sanitized:
    logger.info("no_proxy entries normalized (bracketed IPv6): %s", ",".join(_sanitized))
from core.database import engine, Base
import models
from router.auth_router import router as auth_router
from router.session_router import router as session_router
from router.knowledge_router import router as knowledge_router
from router.attachment_router import router as attachment_router
from router.memory_router import router as memory_router
from router.research_router import router as research_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # create_all handles empty development DBs, NOT upgrades; apply SQL migration first.
    await asyncio.to_thread(Base.metadata.create_all, bind=engine)
    scheduler = None
    try:
        if SETTINGS["legacy_enabled"] and env_flag("ENABLE_NEWS_SCHEDULER", False):
            from service.scheduler_service import init_scheduler_and_check_data, get_scheduler_service
            await init_scheduler_and_check_data()
            scheduler = get_scheduler_service()
        else:
            logger.info("legacy_news_scheduler=disabled; no initial collection")
        yield
    finally:
        if scheduler is not None:
            scheduler.stop()

app = FastAPI(title="LitTrace / DeepResearch API", version="2.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=SETTINGS["cors_origins"], allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"])
for router in (auth_router, session_router, knowledge_router, attachment_router, memory_router, research_router):
    app.include_router(router)
if SETTINGS["text2sql_enabled"]:
    from router.database_router import router as database_router
    app.include_router(database_router)
if SETTINGS["legacy_enabled"]:
    from router.news_router import router as news_router
    from router.document_router import router as document_router
    from router.search_router import router as search_router
    from router.chat_router import router as chat_router
    for router in (news_router, document_router, search_router, chat_router):
        app.include_router(router)

@app.get("/hello")
async def hello_world():
    return {"status": "success", "legacy_enabled": SETTINGS["legacy_enabled"],
            "text2sql_enabled": SETTINGS["text2sql_enabled"]}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

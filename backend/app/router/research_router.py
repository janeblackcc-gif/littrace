# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""Authenticated V2 entry points. V1/debug execution is deliberately not exposed."""
import asyncio
import uuid
from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from core.database import get_db, engine
from core.runtime_settings import env_flag
from models.user import User
from models.chat import ChatSession
from models.research import ResearchCheckpoint
from router.auth_router import get_current_user_required
from service.deep_research_v2.service import DeepResearchV2Service
from service.kb_binding import resolve_kb_binding
from service.research_integrity import ContractError, validate_resume
from service.checkpoint_guard import CheckpointGuard, RunBusy
from service.research_control import request_cancel, is_research_cancelled, clear_cancel_flag
from service.direction_contracts import DirectionRequest, load_preset

router = APIRouter(prefix="/research", tags=["research"])

class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=8000)
    session_id: Optional[str] = Field(default=None, max_length=64)
    max_iterations: int = Field(default=3, ge=0, le=5)
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None  # compatibility: resolves to an owned, unambiguous KB once
    search_web: Optional[bool] = None
    search_local: Optional[bool] = None
    search_modes: Optional[list[Literal["web", "local"]]] = None
    version: Literal["v1", "v2"] = "v2"
    direction: Optional[DirectionRequest] = None

    def get_search_web(self):
        return "web" in self.search_modes if self.search_modes is not None else self.search_web if self.search_web is not None else True

    def get_search_local(self):
        return "local" in self.search_modes if self.search_modes is not None else self.search_local if self.search_local is not None else False


def owned_checkpoint(db, session_id, user):
    rows = db.query(ResearchCheckpoint).filter(ResearchCheckpoint.session_id == session_id).limit(2).all()
    if not rows or str(rows[0].user_id) != str(user.id):
        raise HTTPException(404, "检查点不存在或无权访问")
    if len(rows) != 1:
        raise HTTPException(409, "旧会话存在多个检查点，需显式处理")
    return rows[0]


def check_chat_owner(db, sid, user):
    try:
        key = uuid.UUID(sid)
    except ValueError:
        return  # standalone research IDs are still bound to the authenticated checkpoint owner
    chat = db.query(ChatSession).filter(ChatSession.id == key).first()
    if chat is not None and str(chat.user_id) != str(user.id):
        raise HTTPException(404, "会话不存在或无权访问")


def research_response(service, **kwargs):
    return StreamingResponse(service.research(**kwargs), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/directions")
def get_directions(current_user: User = Depends(get_current_user_required)):
    return {"directions": [load_preset(i).model_dump() for i in ("agent_memory", "rag_context", "llm_evaluation")]}


@router.post("/stream")
async def stream_research(request: ResearchRequest, current_user: User = Depends(get_current_user_required),
                          db: Session = Depends(get_db)):
    if request.version != "v2":
        raise HTTPException(410, "旧版研究执行器已关闭")
    if not request.get_search_web() and not request.get_search_local():
        raise HTTPException(422, "至少选择一种搜索模式")
    if request.get_search_web() and not env_flag("ENABLE_WEB_SEARCH", True):
        raise HTTPException(503, "网络搜索未启用")
    # 文献产品**缺省即要求**显式 direction：缺 direction 时拒绝请求，
    # 不再允许落到行业规划路径。开关只在显式配置为 false 时才关闭校验，
    # 这是给隔离实验留的逃生口，不是正常运行配置；.env 里显式写 ENABLE_DIRECTION_PROFILES=true。
    if env_flag("ENABLE_DIRECTION_PROFILES", True) and request.direction is None:
        raise HTTPException(422, "请选择研究方向或显式选择自由输入")
    if request.direction and request.direction.choice.mode == "preset":
        try:
            load_preset(request.direction.choice.preset_id)
        except ContractError as exc:
            raise HTTPException(422, str(exc)) from exc
    sid = request.session_id or str(uuid.uuid4())
    check_chat_owner(db, sid, current_user)
    if db.query(ResearchCheckpoint.id).filter(ResearchCheckpoint.session_id == sid).first():
        raise HTTPException(409, "会话已有检查点，请恢复或新建会话")
    binding = resolve_kb_binding(db, str(current_user.id), kb_id=request.kb_id,
                                 kb_name=request.kb_name, required=request.get_search_local())
    uid = str(current_user.id)
    db.rollback()  # release request-side read transaction before a long SSE stream
    return research_response(DeepResearchV2Service(max_iterations=request.max_iterations),
        query=request.query, session_id=sid, user_id=uid, kb_binding=binding,
        search_web=request.get_search_web(), search_local=request.get_search_local(),
        direction_request=request.direction.model_dump() if request.direction else None)


@router.get("/stream")
async def stream_research_get(query: str, session_id: Optional[str] = None,
    max_iterations: int = Query(3, ge=0, le=5), kb_id: Optional[str] = None,
    kb_name: Optional[str] = None, search_web: bool = True, search_local: bool = False,
    version: Literal["v1", "v2"] = "v2", direction_id: Optional[str] = None,
    start_year: int = 2022, end_year: int = 2025,
    current_user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    direction = DirectionRequest(choice={"mode": "preset", "preset_id": direction_id},
        start_year=start_year, end_year=end_year) if direction_id else None
    return await stream_research(ResearchRequest(query=query, session_id=session_id,
        max_iterations=max_iterations, kb_id=kb_id, kb_name=kb_name, search_web=search_web,
        search_local=search_local, version=version, direction=direction), current_user, db)


@router.post("/cancel/{session_id}")
async def cancel_research(session_id: str, current_user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    owned_checkpoint(db, session_id, current_user)
    db.rollback()
    try:
        await asyncio.to_thread(request_cancel, session_id)
    except Exception as exc:
        raise HTTPException(503, "Redis取消请求未写入，不能报告已取消") from exc
    return {"success": True, "message": "Cancellation requested; not yet confirmed by executor"}


@router.get("/checkpoint/{session_id}")
def get_checkpoint(session_id: str, current_user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    row = owned_checkpoint(db, session_id, current_user)
    return {"success": True, "checkpoint": row.to_dict()}


@router.get("/checkpoint/{session_id}/full")
def get_full_checkpoint(session_id: str, current_user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    row = owned_checkpoint(db, session_id, current_user)
    return {"success": True, "checkpoint": row.to_dict(include_state=True)}


@router.get("/checkpoints")
def list_checkpoints(status: Optional[str] = None, limit: int = Query(20, ge=1, le=100),
                      current_user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    query = db.query(ResearchCheckpoint).filter(ResearchCheckpoint.user_id == current_user.id)
    if status:
        query = query.filter(ResearchCheckpoint.status == status)
    rows = query.order_by(ResearchCheckpoint.updated_at.desc()).limit(limit).all()
    return {"success": True, "checkpoints": [r.to_dict() for r in rows], "total": len(rows)}


@router.delete("/checkpoint/{session_id}")
def delete_checkpoint(session_id: str, current_user: User = Depends(get_current_user_required)):
    try:
        with CheckpointGuard(engine, session_id, str(current_user.id)) as guard:
            success = guard.delete()
        return {"success": success}
    except RunBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ContractError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/resume/{session_id}")
async def resume_research(session_id: str, current_user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    row = owned_checkpoint(db, session_id, current_user)
    state = row.state_json
    uid = str(current_user.id)
    try:
        validate_resume(state, session_id, uid)
    except ContractError as exc:
        raise HTTPException(409, str(exc)) from exc
    if row.status == "completed":
        raise HTTPException(409, "已结束任务不可续跑；需新建任务")
    if state.get("search_local"):
        live = resolve_kb_binding(db, uid, kb_id=state["kb_binding"]["kb_id"], required=True)
        if live["collection_name"] != state["kb_binding"]["collection_name"]:
            raise HTTPException(409, "知识库绑定变化，不能无提示恢复")
    db.rollback()
    return research_response(DeepResearchV2Service(max_iterations=state["max_iterations"]),
        query=state["query"], session_id=session_id, resume=True, user_id=uid)

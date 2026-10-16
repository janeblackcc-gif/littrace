"""Server-authorized KB identity. A display name is never a vector namespace."""
from uuid import UUID
from fastapi import HTTPException
from models.knowledge import KnowledgeBase, Document
from core.database import SessionLocal
from .research_integrity import validate_kb_binding


def resolve_kb_binding(db, user_id: str, kb_id=None, kb_name=None, required=False):
    if not required:
        return None
    if not kb_id and not kb_name:
        raise HTTPException(422, "请选择知识库；local 模式不自动选择默认库")
    query = db.query(KnowledgeBase).filter(KnowledgeBase.user_id == UUID(str(user_id)))
    if kb_id:
        try:
            query = query.filter(KnowledgeBase.id == UUID(str(kb_id)))
        except ValueError as exc:
            raise HTTPException(422, "无效 kb_id") from exc
    else:
        query = query.filter(KnowledgeBase.name == kb_name)
    rows = query.limit(2).all()
    if len(rows) != 1:
        raise HTTPException(404 if not rows else 409, "知识库不存在、无权访问或名称不唯一")
    kb = rows[0]
    if kb.status != "ready":
        raise HTTPException(409, "知识库正在删除，不可检索")
    if not kb.vector_collection:
        raise HTTPException(409, "旧知识库需显式重建索引，不会静默回退旧集合")
    return validate_kb_binding({"schema_version": 2, "kb_id": str(kb.id), "user_id": str(kb.user_id),
                               "collection_name": kb.vector_collection, "display_name": kb.name}, str(user_id))


def live_document_ids(binding: dict, user_id: str) -> list[str]:
    validate_kb_binding(binding, user_id)
    with SessionLocal() as db:
        live = resolve_kb_binding(db, user_id, kb_id=binding["kb_id"], required=True)
        if live["collection_name"] != binding["collection_name"]:
            raise HTTPException(409, "知识库绑定已改变，请新建研究任务")
        rows = db.query(Document.id).filter(Document.knowledge_base_id == UUID(binding["kb_id"]),
                                            Document.user_id == UUID(user_id), Document.status == "completed").all()
        return [str(row[0]) for row in rows]

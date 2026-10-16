# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""知识库管理路由"""
import asyncio
import uuid
from pathlib import Path
from service.research_integrity import collection_for_kb
from service.kb_binding import resolve_kb_binding
from core.runtime_settings import env_flag
import os
import shutil
from typing import List
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, BackgroundTasks
from sqlalchemy.orm import Session

from core.database import get_db
from models.knowledge import KnowledgeBase, Document
from models.user import User
from router.auth_router import get_current_user_required
from schemas.knowledge import (
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeBaseResponse,
    KnowledgeBaseWithDocuments,
    DocumentResponse,
    DocumentUploadResponse,
)

router = APIRouter(prefix="/knowledge-bases", tags=["知识库管理"])

# 文件上传目录
UPLOAD_DIR = "/tmp/knowledge_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 支持的文件类型
ALLOWED_EXTENSIONS = {
    # 文档类型
    '.pdf', '.docx', '.doc', '.txt', '.md', '.html', '.xlsx', '.xls', '.pptx', '.ppt',
    # 图片类型
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp',
    # 代码/数据类型
    '.py', '.js', '.ts', '.json', '.yaml', '.yml', '.xml', '.csv',
}


def get_file_extension(filename: str) -> str:
    """获取文件扩展名"""
    return os.path.splitext(filename)[1].lower()


def kb_to_response(kb: KnowledgeBase) -> KnowledgeBaseResponse:
    """将知识库模型转换为响应"""
    return KnowledgeBaseResponse(
        id=str(kb.id),
        name=kb.name,
        description=kb.description,
        document_count=kb.document_count or 0,
        status=kb.status, requires_reindex=kb.vector_collection is None,
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


def doc_to_response(doc: Document) -> DocumentResponse:
    """将文档模型转换为响应"""
    return DocumentResponse(
        id=str(doc.id),
        knowledge_base_id=str(doc.knowledge_base_id),
        filename=doc.filename,
        file_type=doc.file_type,
        file_size=doc.file_size,
        status=doc.status,
        chunk_count=doc.chunk_count or 0,
        error_message=doc.error_message,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


async def process_document(document_id: str, file_path: str, kb_name: str, db_session_factory):
    # kb_name is retained only for old call compatibility; it is NOT used as identity.
    def work():
        from service.docmind_service import process_document_with_docmind
        with db_session_factory() as db:
            doc = db.query(Document).filter(Document.id == UUID(document_id)).first()
            if not doc or doc.status != "pending":
                return
            kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == doc.knowledge_base_id).with_for_update().one()
            if kb.status != "ready" or not kb.vector_collection:
                doc.status, doc.error_message = "failed", "KB unavailable/reindex required"
                db.commit()
                return
            doc.status = "processing"
            collection, filename = kb.vector_collection, doc.filename
            db.commit()
            try:
                result = process_document_with_docmind(file_path=file_path, file_name=filename,
                    index_name=collection, document_id=document_id)
                doc.status = "completed" if result["success"] else "failed"
                doc.chunk_count = result.get("document_count", 0)
                doc.error_message = None if result["success"] else result.get("message", "parse failed")
            except Exception as exc:
                doc.status, doc.error_message = "failed", type(exc).__name__
            db.commit()
        # Retain the uploaded original for reindexing; explicit deletion removes it.
    await asyncio.to_thread(work)


@router.get("", response_model=List[KnowledgeBaseResponse])
async def get_knowledge_bases(
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """获取用户的知识库列表"""
    kbs = db.query(KnowledgeBase).filter(
        KnowledgeBase.user_id == current_user.id
    ).order_by(KnowledgeBase.updated_at.desc()).all()

    return [kb_to_response(kb) for kb in kbs]


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    kb_data: KnowledgeBaseCreate,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """创建知识库"""
    # 检查是否已存在同名知识库
    existing = db.query(KnowledgeBase).filter(
        KnowledgeBase.user_id == current_user.id,
        KnowledgeBase.name == kb_data.name
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="已存在同名知识库"
        )

    kb = KnowledgeBase(
        user_id=current_user.id,
        name=kb_data.name,
        description=kb_data.description,
    )
    db.add(kb)
    db.flush()
    kb.vector_collection = collection_for_kb(str(kb.id))
    db.commit()
    db.refresh(kb)

    return kb_to_response(kb)


@router.get("/{kb_id}", response_model=KnowledgeBaseWithDocuments)
async def get_knowledge_base(
    kb_id: str,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """获取知识库详情（包含文档列表）"""
    try:
        kb_uuid = UUID(kb_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的知识库ID格式"
        )

    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()

    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在"
        )

    documents = db.query(Document).filter(
        Document.knowledge_base_id == kb.id
    ).order_by(Document.created_at.desc()).all()

    return KnowledgeBaseWithDocuments(
        id=str(kb.id),
        name=kb.name,
        description=kb.description,
        document_count=kb.document_count or 0,
        created_at=kb.created_at,
        updated_at=kb.updated_at,
        documents=[doc_to_response(doc) for doc in documents],
    )


@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: str,
    kb_data: KnowledgeBaseUpdate,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """更新知识库"""
    try:
        kb_uuid = UUID(kb_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的知识库ID格式"
        )

    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()

    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在"
        )

    if kb_data.name is not None:
        # 检查是否与其他知识库重名
        existing = db.query(KnowledgeBase).filter(
            KnowledgeBase.user_id == current_user.id,
            KnowledgeBase.name == kb_data.name,
            KnowledgeBase.id != kb_uuid
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="已存在同名知识库"
            )
        kb.name = kb_data.name

    if kb_data.description is not None:
        kb.description = kb_data.description

    db.commit()
    db.refresh(kb)

    return kb_to_response(kb)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(kb_id: str,
                               current_user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    from service.milvus_service import get_milvus_service
    try:
        kb_uuid = UUID(kb_id)
    except ValueError as exc:
        raise HTTPException(422, "无效ID") from exc
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid,
          KnowledgeBase.user_id == current_user.id).with_for_update().first()
    if not kb:
        return None
    if not kb.vector_collection:
        raise HTTPException(409, "旧库需先按迁移清单处理，不能猜测旧集合身份")
    docs = db.query(Document).filter(Document.knowledge_base_id == kb_uuid).all()
    if any(d.status in {"pending", "processing"} for d in docs):
        raise HTTPException(409, "库内仍有写入任务，请结束处理后删除")
    kb.status = "deleting"
    collection, paths = kb.vector_collection, [d.file_path for d in docs if d.file_path]
    db.commit()
    try:
        ok = await asyncio.to_thread(lambda: get_milvus_service().delete_collection(collection))
    except Exception as exc:
        raise HTTPException(503, "向量服务不可用；保留 deleting 状态等待重试") from exc
    if not ok:
        raise HTTPException(503, "集合删除未确认；知识库保持 deleting，可重试")
    for path in paths:
        Path(path).unlink(missing_ok=True)
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid).with_for_update().first()
    if kb:
        db.delete(kb)
        db.commit()
    return None


@router.post("/{kb_id}/documents", response_model=DocumentUploadResponse)
async def upload_document(kb_id: str, background_tasks: BackgroundTasks, file: UploadFile = File(...),
                          current_user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    if not env_flag("ENABLE_DOCMIND", False):
        raise HTTPException(503, "文档解析未启用；请配置 DocMind 后重启")
    try:
        kb_uuid = UUID(kb_id)
    except ValueError as exc:
        raise HTTPException(422, "无效的知识库ID") from exc
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid,
          KnowledgeBase.user_id == current_user.id).with_for_update().first()
    if not kb:
        raise HTTPException(404, "知识库不存在")
    if kb.status != "ready" or not kb.vector_collection:
        raise HTTPException(409, "知识库不可用或需要重建索引")
    filename = Path(file.filename or "upload").name
    ext = get_file_extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "不支持的文件类型")
    doc_id = uuid.uuid4()
    file_path = str(Path(UPLOAD_DIR) / (doc_id.hex + ext))
    try:
        with open(file_path, "xb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        doc = Document(id=doc_id, knowledge_base_id=kb_uuid, user_id=current_user.id,
            filename=filename, file_type=ext.lstrip('.'), file_size=os.path.getsize(file_path),
            file_path=file_path, status="pending")
        db.add(doc)
        kb.document_count = (kb.document_count or 0) + 1
        db.commit()
    except Exception:
        db.rollback()
        Path(file_path).unlink(missing_ok=True)
        raise
    from core.database import SessionLocal
    background_tasks.add_task(process_document, str(doc_id), file_path, kb.name, SessionLocal)
    return DocumentUploadResponse(id=str(doc_id), filename=filename, process_status="pending",
                                   message="文档已上传，正在处理中")


@router.get("/{kb_id}/documents", response_model=List[DocumentResponse])
async def get_documents(
    kb_id: str,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """获取知识库的文档列表"""
    try:
        kb_uuid = UUID(kb_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的知识库ID格式"
        )

    # 验证知识库存在
    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()

    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在"
        )

    documents = db.query(Document).filter(
        Document.knowledge_base_id == kb_uuid
    ).order_by(Document.created_at.desc()).all()

    return [doc_to_response(doc) for doc in documents]


@router.get("/{kb_id}/documents/{doc_id}/chunks")
async def get_document_chunks(
    kb_id: str,
    doc_id: str,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """获取文档的所有切片"""
    from service.milvus_service import get_milvus_service

    try:
        kb_uuid = UUID(kb_id)
        doc_uuid = UUID(doc_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的ID格式"
        )

    # 验证知识库存在
    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()

    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在"
        )

    # 获取文档
    doc = db.query(Document).filter(
        Document.id == doc_uuid,
        Document.knowledge_base_id == kb_uuid
    ).first()

    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档不存在"
        )

    if doc.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="文档尚未处理完成"
        )

    # 从 Milvus 获取切片
    binding = resolve_kb_binding(db, str(current_user.id), kb_id=kb_id, required=True)
    collection_name = binding["collection_name"]
    print(f"[get_document_chunks] 查询切片: collection={collection_name}, filename={doc.filename}")

    try:
        milvus = get_milvus_service()
        chunks = milvus.get_chunks_by_doc_id(collection_name, str(doc.id))
        print(f"[get_document_chunks] 找到 {len(chunks)} 个切片")
    except Exception as e:
        print(f"[get_document_chunks] Milvus 查询失败: {e}")
        raise HTTPException(503, "切片读取失败，不能把服务错误当作空结果") from e

    return {
        "document_id": str(doc.id),
        "filename": doc.filename,
        "chunk_count": len(chunks),
        "chunks": [
            {
                "index": chunk.get("chunk_index", i),
                "content": chunk.get("content", ""),
            }
            for i, chunk in enumerate(chunks)
        ]
    }


@router.delete("/{kb_id}/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(kb_id: str, doc_id: str,
                          current_user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    from service.milvus_service import get_milvus_service
    try:
        kb_uuid, doc_uuid = UUID(kb_id), UUID(doc_id)
    except ValueError as exc:
        raise HTTPException(422, "无效ID") from exc
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid,
         KnowledgeBase.user_id == current_user.id).with_for_update().first()
    if not kb:
        raise HTTPException(404, "知识库不存在")
    if not kb.vector_collection:
        raise HTTPException(409, "旧库需先按迁移清单处理，不能猜测旧向量身份")
    doc = db.query(Document).filter(Document.id == doc_uuid, Document.knowledge_base_id == kb_uuid).first()
    if not doc:
        return None  # idempotent retry after a successful delete
    if doc.status in {"pending", "processing"}:
        raise HTTPException(409, "文档仍在写入，请结束处理后删除")
    doc.status = "deleting"  # durable tombstone; retrieval filters completed documents only
    collection, file_path = kb.vector_collection, doc.file_path
    db.commit()
    try:
        ok = await asyncio.to_thread(lambda: get_milvus_service().delete_by_doc_id(collection, str(doc_uuid)))
    except Exception as exc:
        raise HTTPException(503, "向量服务不可用；保留 deleting 状态等待重试") from exc
    if not ok:
        raise HTTPException(503, "向量删除未确认；文档保持 deleting，可重试")
    if file_path:
        Path(file_path).unlink(missing_ok=True)
    # Serialize counters and prevent races with KB deletion after the external call.
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid).with_for_update().first()
    doc = db.query(Document).filter(Document.id == doc_uuid).first()
    if doc:
        if kb:
            kb.document_count = max((kb.document_count or 0) - 1, 0)
        db.delete(doc)
        db.commit()
    return None

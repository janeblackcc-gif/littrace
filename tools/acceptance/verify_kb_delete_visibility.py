#!/usr/bin/env python3
"""D-05 最小真实集成：自建 KB 文档的权限边界与删除后不可检索。"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "backend" / "app"))

from dotenv import load_dotenv
load_dotenv(BASE / ".env")

from core.runtime_settings import sanitize_no_proxy
sanitize_no_proxy()

API = os.getenv("LITTRACE_BASE", "http://127.0.0.1:8000")
MARKER = "D05_DELETE_VISIBILITY_" + uuid.uuid4().hex
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register(client: httpx.Client, label: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "username": f"d05_{label}_{suffix}",
        "email": f"d05_{label}_{suffix}@example.com",
        "password": "D05-Kb-Delete-Passw0rd!",
    }
    response = client.post("/auth/register", json=payload)
    response.raise_for_status()
    body = response.json()
    return body["access_token"], str(body["user"]["id"])


def wait_completed(client: httpx.Client, token: str, kb_id: str,
                   doc_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        response = client.get(f"/knowledge-bases/{kb_id}/documents", headers=auth(token))
        response.raise_for_status()
        for doc in response.json():
            if str(doc.get("id")) == doc_id:
                last = doc
                if doc.get("status") in {"completed", "failed"}:
                    return doc
        time.sleep(1)
    return last


async def local_search(kb_id: str, user_id: str, query: str) -> list[dict]:
    from config.llm_config import get_config
    from core.database import SessionLocal
    from service.deep_research_v2.agents.scout import DeepScout
    from service.kb_binding import resolve_kb_binding

    with SessionLocal() as db:
        binding = resolve_kb_binding(db, user_id, kb_id=kb_id, required=True)
    cfg = get_config()
    agent = DeepScout(llm_api_key=cfg.api_key, llm_base_url=cfg.base_url,
                      search_api_key=cfg.search_api_key)
    if not agent.milvus_service:
        raise RuntimeError("DeepScout 未初始化 Milvus，无法验证生产本地检索")
    return await agent._execute_local_search(query, {"kb_binding": binding, "user_id": user_id})


def main() -> int:
    from core.database import SessionLocal
    from models import Document, KnowledgeBase
    from service.milvus_service import get_milvus_service
    from uuid import UUID

    client = httpx.Client(base_url=API, timeout=60.0)
    hello = client.get("/hello")
    check("后端真实服务可达", hello.status_code == 200, f"status={hello.status_code}")

    owner_token, owner_id = register(client, "owner")
    other_token, _ = register(client, "other")
    check("注册本轮两个隔离用户", bool(owner_token and other_token))

    kb_response = client.post("/knowledge-bases", headers=auth(owner_token), json={
        "name": f"D05_DELETE_{uuid.uuid4().hex[:8]}",
        "description": "D-05 删除可见性专项，仅含本轮测试数据",
    })
    check("Owner 创建本轮测试 KB", kb_response.status_code == 201,
          f"status={kb_response.status_code}")
    if kb_response.status_code != 201:
        return finish()
    kb_id = str(kb_response.json()["id"])

    owner_kbs = client.get("/knowledge-bases", headers=auth(owner_token)).json()
    other_kbs = client.get("/knowledge-bases", headers=auth(other_token)).json()
    check("Owner 的 KB 选择列表包含测试 KB", any(str(kb["id"]) == kb_id for kb in owner_kbs))
    check("其他用户的 KB 选择列表不含测试 KB", all(str(kb["id"]) != kb_id for kb in other_kbs))

    content = (
        "LitTrace 删除可见性专项测试。" * 30
        + "\n唯一检索标记：" + MARKER
        + "\n该文档只属于本轮自动创建的测试知识库。"
    )
    upload = client.post(f"/knowledge-bases/{kb_id}/documents", headers=auth(owner_token),
                         files={"file": ("d05_delete_test.txt", content.encode("utf-8"), "text/plain")})
    check("Owner 上传本轮测试文档", upload.status_code in {200, 201},
          f"status={upload.status_code}")
    if upload.status_code not in {200, 201}:
        return finish()
    doc_id = str(upload.json()["id"])
    completed = wait_completed(client, owner_token, kb_id, doc_id)
    check("测试文档真实解析完成", completed.get("status") == "completed",
          f"status={completed.get('status')} error={completed.get('error_message')}")
    if completed.get("status") != "completed":
        return finish()

    with SessionLocal() as db:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == UUID(kb_id)).one()
        collection = kb.vector_collection
        pg_before = db.query(Document).filter(Document.id == UUID(doc_id)).count()
    milvus = get_milvus_service()
    rows_before = milvus.get_chunks_by_doc_id(collection, doc_id)
    check("删除前 PG 文档存在", pg_before == 1, f"count={pg_before}")
    check("删除前 Milvus 目标 doc_id 有真实向量", len(rows_before) > 0,
          f"rows={len(rows_before)}")

    search_before = asyncio.run(local_search(kb_id, owner_id, MARKER))
    check("删除前生产本地检索可见目标文档",
          any(result.get("work_id") == doc_id for result in search_before),
          f"work_ids={[result.get('work_id') for result in search_before]}")

    forbidden = client.delete(f"/knowledge-bases/{kb_id}/documents/{doc_id}",
                              headers=auth(other_token))
    check("其他用户删除 Owner 文档被 404 拒绝", forbidden.status_code == 404,
          f"status={forbidden.status_code}")
    owner_delete = client.delete(f"/knowledge-bases/{kb_id}/documents/{doc_id}",
                                 headers=auth(owner_token))
    check("Owner 删除自己的测试文档返回 204", owner_delete.status_code == 204,
          f"status={owner_delete.status_code}")

    rows_after = milvus.get_chunks_by_doc_id(collection, doc_id)
    with SessionLocal() as db:
        pg_after = db.query(Document).filter(Document.id == UUID(doc_id)).count()
    listed_after = client.get(f"/knowledge-bases/{kb_id}/documents",
                              headers=auth(owner_token)).json()
    search_after = asyncio.run(local_search(kb_id, owner_id, MARKER))
    check("删除后 Milvus 按目标 doc_id 强一致查询为空", rows_after == [],
          f"rows={len(rows_after)}")
    check("删除后 PG 文档行不存在", pg_after == 0, f"count={pg_after}")
    check("删除后 Owner 文档列表不再出现目标", all(str(doc.get("id")) != doc_id for doc in listed_after))
    check("删除后生产本地检索不再返回目标文档",
          all(result.get("work_id") != doc_id for result in search_after),
          f"work_ids={[result.get('work_id') for result in search_after]}")
    retry = client.delete(f"/knowledge-bases/{kb_id}/documents/{doc_id}",
                          headers=auth(owner_token))
    check("重复删除自己的已删测试文档保持幂等 204", retry.status_code == 204,
          f"status={retry.status_code}")

    print("  [EVIDENCE] " + __import__("json").dumps({
        "kb_id": kb_id,
        "doc_id": doc_id,
        "collection": collection,
        "marker": MARKER,
        "rows_before": len(rows_before),
        "rows_after": len(rows_after),
        "search_before_work_ids": [result.get("work_id") for result in search_before],
        "search_after_work_ids": [result.get("work_id") for result in search_after],
        "pg_after": pg_after,
    }, ensure_ascii=False, sort_keys=True))
    return finish()


def finish() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nD-05 KB 权限与删除可见性: {passed}/{total} PASS")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

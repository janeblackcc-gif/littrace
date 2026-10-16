# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""A1-I 真实上传链路验收。

此前的 A1-I 17/17 是用**手工插入 synthetic 向量**完成的：当时
`ENABLE_DOCMIND=false`，`POST /knowledge-bases/{kb}/documents` 直接 503。
本脚本走**真实链路**：

    HTTP 上传 -> BackgroundTasks -> DocMind 真解析 -> 真 embedding -> Milvus 真插入

然后验证身份的每一环仍然成立：
  1. 集合名 = `kb_` + UUID 去连字符（与展示名无关）
  2. Milvus `doc_id` == PostgreSQL 文档 UUID（不是文件名哈希）
  3. chunk 主键 == sha256(f"{doc_id}:{index}:{chunk}")（内容指纹）
  4. 同名不同内容的文档分库后**不串库**（ALPHA_ONLY / BETA_ONLY 互不出现）
  5. 跨用户隔离与 401/404/422/409 语义仍成立

用法（后端需已用 ENABLE_DOCMIND=true 启动在 8000）：
    python tools/acceptance/a1i_real_upload.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid

import httpx

BASE = os.getenv("LITTRACE_BASE", "http://127.0.0.1:8000")
ALPHA = "ALPHA_ONLY_MARKER_7F3A9C"
BETA = "BETA_ONLY_MARKER_2D8E41"

# 与 docmind_service.chunk_text(chunk_size=500) 配合：让标记落在靠后的片，
# 而两片共享的公共前缀用于"同 query 两边都能召回"的中性检索测试。
#
# 为什么样本要够大：最初用 226 字符的小样本，全文只切出 1 片，
# 「不串库」就退化成"在同一个向量内部比较"，等于没验证跨片串库。
# 这里把每份文档加到 ~1200 字符（DocMind 抽取会在行间插入空行，实际会更长），
# 保证至少切出 3 片、且两份文档的标记各自落在**不同**的片里。
FILLER = (
    "LitTrace 同名校验样本。\n"
    "本文件用于验证两份文件名相同、内容不同的文档上传到两个独立知识库后，\n"
    "各自的向量集合互不串库，且 Milvus 中的 doc_id 必须等于 PostgreSQL 的文档 UUID。\n\n"
    "第一段填充：向量检索依赖切片粒度，切片过小会让隔离性验证失去意义，\n"
    "因此本文本刻意放大到足以产生多个切片的长度。\n\n"
    "第二段填充：知识库的集合身份由 UUID 派生，与展示名称完全无关，\n"
    "所以两个库即使文档同名，也不会共享任何一条向量记录。\n\n"
    "第三段填充：文档标识必须是数据库里的文档主键，而不是文件名的哈希值，\n"
    "否则同名文件互相覆盖，历史引用会指向错误的文档。\n\n"
    "第四段填充：切片主键采用文档标识、序号与正文内容共同计算的内容指纹，\n"
    "同一份内容重复入库时主键稳定，可以据此判断重复写入。\n\n"
    "第五段填充：删除文档时按文档标识批量删除向量，并在强一致读下复查为空，\n"
    "而不是依赖删除计数或实体总数变化作为判定依据。\n\n"
    "第六段填充：检索时还会再次校验文档仍在库内且归属正确，\n"
    "以覆盖「向量检索期间文档被删除」这一竞态窗口。\n\n"
)
DOC_ALPHA = FILLER + "【唯一标记】" + ALPHA + "\n该标记只应出现在第一份文档中。\n"
DOC_BETA = FILLER + "【唯一标记】" + BETA + "\n该标记只应出现在第二份文档中。\n"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def register(client: httpx.Client, tag: str) -> str:
    """注册一个一次性用户并返回 access token。名字带随机后缀避免与历史数据冲突。"""
    payload = {"username": f"a1i_{tag}_{uuid.uuid4().hex[:8]}",
               "email": f"a1i_{tag}_{uuid.uuid4().hex[:8]}@example.com",
               "password": "A1i-Real-Upload-Passw0rd!"}
    r = client.post("/auth/register", json=payload)
    if r.status_code not in (200, 201):
        raise SystemExit(f"注册失败 {r.status_code}: {r.text[:400]}")
    body = r.json()
    token = body.get("access_token") or body.get("token")
    if not token:
        raise SystemExit(f"注册响应无 token: {list(body)}")
    return token


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def create_kb(client: httpx.Client, token: str, name: str) -> dict:
    r = client.post("/knowledge-bases", json={"name": name, "description": "A1-I 真实上传验收"},
                    headers=auth(token))
    if r.status_code != 201:
        raise SystemExit(f"建库失败 {r.status_code}: {r.text[:400]}")
    return r.json()


def upload(client: httpx.Client, token: str, kb_id: str, filename: str, content: str) -> dict:
    r = client.post(f"/knowledge-bases/{kb_id}/documents",
                    files={"file": (filename, content.encode("utf-8"), "text/plain")},
                    headers=auth(token), timeout=60.0)
    return {"status": r.status_code, "body": (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)}


def wait_completed(client: httpx.Client, token: str, kb_id: str, doc_id: str, timeout: float = 360.0) -> dict:
    """轮询文档状态直到 completed / failed。DocMind 解析是异步 BackgroundTask。"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = client.get(f"/knowledge-bases/{kb_id}/documents", headers=auth(token))
        for d in r.json():
            if str(d.get("id")) == str(doc_id):
                last = d
                if d.get("status") in {"completed", "failed"}:
                    return d
        time.sleep(3)
    return last


def main() -> int:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend", "app"))
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

    # 与 app_main.sanitize_no_proxy 同一原因：本机 NO_PROXY 含 "[::1]" 会让 httpx
    # 在构造 Client 时抛 InvalidURL('Invalid port: ":1]"')。验收脚本是独立客户端，
    # 不会走到应用的启动钩子，必须自己规范化。
    from core.runtime_settings import sanitize_no_proxy
    sanitize_no_proxy()

    client = httpx.Client(base_url=BASE, timeout=30.0)
    print(f"后端: {BASE}")

    # ---------- 前置：健康 + DocMind 开关 ----------
    print("\n[0] 前置检查")
    try:
        h = client.get("/hello")
    except httpx.HTTPError as exc:
        print(f"  后端不可达: {exc}")
        return 2
    check("后端可达", h.status_code < 500, f"GET /hello -> {h.status_code}")

    tok_u1 = register(client, "u1")
    tok_u2 = register(client, "u2")
    check("注册两个用户 U1/U2", bool(tok_u1 and tok_u2))

    ka = create_kb(client, tok_u1, f"KA_{uuid.uuid4().hex[:6]}")
    kb = create_kb(client, tok_u1, f"KB_{uuid.uuid4().hex[:6]}")
    check("U1 建两个知识库 KA/KB", bool(ka.get("id") and kb.get("id")))
    # 集合名不由 API 暴露（KnowledgeBaseResponse 无 vector_collection 字段，
    # 只用 requires_reindex 表达其缺失）。按约定 kb_ + UUID 去连字符推导，
    # 并与数据库里的真实值对账，而不是假设。
    coll_a = "kb_" + str(ka["id"]).replace("-", "")
    coll_b = "kb_" + str(kb["id"]).replace("-", "")
    print(f"      KA={ka['id']} 推导 collection={coll_a}")
    print(f"      KB={kb['id']} 推导 collection={coll_b}")

    # ---------- 1. 集合命名与数据库对账 ----------
    print("\n[1] 集合命名与展示名无关")
    from core.database import SessionLocal
    from models import KnowledgeBase as KBModel
    from uuid import UUID as _UUID
    with SessionLocal() as db:
        row_a = db.query(KBModel).filter(KBModel.id == _UUID(ka["id"])).first()
        row_b = db.query(KBModel).filter(KBModel.id == _UUID(kb["id"])).first()
        coll_a_db, coll_b_db = row_a.vector_collection, row_b.vector_collection
    check("DB 中 KA.vector_collection == kb_+UUID-hex", coll_a_db == coll_a, f"DB={coll_a_db}")
    check("DB 中 KB.vector_collection == kb_+UUID-hex", coll_b_db == coll_b, f"DB={coll_b_db}")
    check("集合名与展示名无关（不含库名）",
          row_a.name not in coll_a and row_b.name not in coll_b,
          f"库名 {row_a.name} / {row_b.name}")
    check("两库集合名不同", coll_a != coll_b)
    check("API 未泄露内部集合名", "vector_collection" not in ka, f"响应键={sorted(ka)}")

    # ---------- 2. 真实上传（同名不同内容）----------
    print("\n[2] 真实上传（同一文件名，内容不同）")
    up_a = upload(client, tok_u1, ka["id"], "same_name_sample.txt", DOC_ALPHA)
    check("KA 上传返回 200/201", up_a["status"] in (200, 201), f"status={up_a['status']} body={str(up_a['body'])[:160]}")
    up_b = upload(client, tok_u1, kb["id"], "same_name_sample.txt", DOC_BETA)
    check("KB 上传返回 200/201", up_b["status"] in (200, 201), f"status={up_b['status']} body={str(up_b['body'])[:160]}")

    if up_a["status"] not in (200, 201) or up_b["status"] not in (200, 201):
        print("\n上传未受理，后续身份验证无法进行。")
        return finish()

    doc_a, doc_b = up_a["body"]["id"], up_b["body"]["id"]
    check("两个 doc id 不同（同名文件不覆盖）", doc_a != doc_b, f"{doc_a} / {doc_b}")
    check("上传即返回 process_status=pending",
          up_a["body"].get("process_status") == "pending" and up_b["body"].get("process_status") == "pending")

    # ---------- 3. 等待 DocMind 真实解析完成 ----------
    print("\n[3] 等待 DocMind 真实解析（异步 BackgroundTask）")
    t0 = time.time()
    da = wait_completed(client, tok_u1, ka["id"], doc_a)
    db_ = wait_completed(client, tok_u1, kb["id"], doc_b)
    elapsed = time.time() - t0
    ok_a = check("KA 文档 status=completed", da.get("status") == "completed",
                 f"status={da.get('status')} err={da.get('error_message')} 耗时={elapsed:.0f}s")
    ok_b = check("KB 文档 status=completed", db_.get("status") == "completed",
                 f"status={db_.get('status')} err={db_.get('error_message')}")
    if not (ok_a and ok_b):
        print("\nDocMind 解析未成功；这是真实链路的关键环节，不继续。")
        return finish()

    check("KA chunk_count >= 2（样本足够大，隔离验证才有意义）", (da.get("chunk_count") or 0) >= 2,
          f"chunk_count={da.get('chunk_count')}")
    check("KB chunk_count >= 2", (db_.get("chunk_count") or 0) >= 2, f"chunk_count={db_.get('chunk_count')}")

    # ---------- 4. Milvus 层身份与内容 ----------
    print("\n[4] Milvus 记录身份（直连 Milvus 而非只看 API）")
    from service.milvus_service import get_milvus_service
    from models import Document as DocModel
    from uuid import UUID

    milvus = get_milvus_service()
    with SessionLocal() as db:
        pg_a = db.query(DocModel).filter(DocModel.id == UUID(doc_a)).first()
        pg_b = db.query(DocModel).filter(DocModel.id == UUID(doc_b)).first()
        pg_rows = {"a": (str(pg_a.id), pg_a.knowledge_base_id, pg_a.filename),
                   "b": (str(pg_b.id), pg_b.knowledge_base_id, pg_b.filename)}

    check("PG 中两条文档都在且 filename 相同",
          pg_rows["a"][2] == pg_rows["b"][2] == "same_name_sample.txt")
    check("PG 中两条文档属于不同 KB",
          str(pg_rows["a"][1]) != str(pg_rows["b"][1]))

    rows_a = milvus.get_chunks_by_doc_id(coll_a, doc_a)
    rows_b = milvus.get_chunks_by_doc_id(coll_b, doc_b)
    check("KA 集合内 doc_id==PG UUID 的行非空", len(rows_a) > 0, f"{len(rows_a)} 行")
    check("KB 集合内 doc_id==PG UUID 的行非空", len(rows_b) > 0, f"{len(rows_b)} 行")
    check("Milvus 行数 == API chunk_count",
          len(rows_a) == (da.get("chunk_count") or 0) and len(rows_b) == (db_.get("chunk_count") or 0),
          f"Milvus {len(rows_a)}/{len(rows_b)} vs API {da.get('chunk_count')}/{db_.get('chunk_count')}")

    check("KA 全部行 doc_id == PG 文档 UUID（非文件名哈希）",
          all(str(r.get("doc_id")) == doc_a for r in rows_a))
    check("KB 全部行 doc_id == PG 文档 UUID",
          all(str(r.get("doc_id")) == doc_b for r in rows_b))
    # 文件名哈希的典型形态：32/64 位无连字符十六进制。显式排除。
    check("doc_id 不是无连字符文件名哈希",
          all(len(str(r.get("doc_id"))) == 36 and "-" in str(r.get("doc_id")) for r in rows_a + rows_b))

    # chunk 主键 = sha256(f"{doc_id}:{index}:{chunk}")
    bad_chunk = []
    for r in rows_a + rows_b:
        expect = hashlib.sha256(f"{r['doc_id']}:{r['chunk_index']}:{r['content']}".encode()).hexdigest()
        if r.get("id") != expect:
            bad_chunk.append((r.get("id"), expect))
    check("全部 chunk 主键 == sha256(doc_id:index:content)", not bad_chunk,
          f"不符 {len(bad_chunk)} 条" if bad_chunk else "内容指纹成立")
    check("chunk 主键全局唯一", len({r["id"] for r in rows_a + rows_b}) == len(rows_a) + len(rows_b))
    check("chunk_index 连续覆盖 0..n-1",
          sorted(r["chunk_index"] for r in rows_a) == list(range(len(rows_a)))
          and sorted(r["chunk_index"] for r in rows_b) == list(range(len(rows_b))))

    # ---------- 5. 不串库（核心）----------
    print("\n[5] 同名文档不串库")
    text_a = "\n".join(r["content"] for r in rows_a)
    text_b = "\n".join(r["content"] for r in rows_b)
    check("KA 内容含 ALPHA 标记", ALPHA in text_a)
    check("KA 内容不含 BETA 标记", BETA not in text_a)
    check("KB 内容含 BETA 标记", BETA in text_b)
    check("KB 内容不含 ALPHA 标记", ALPHA not in text_b)
    # 强版：两份文档的标记必须落在**不同的片**，且没有任何一片同时含两个标记。
    # 注意：两库是各自独立的集合，chunk_index 天然会从 0 重复，不能跨库比较片号；
    # 跨库隔离只能通过「同一片内容里是否同时出现两个标记」以及主键/条件查询来判定。
    cross = [r["chunk_index"] for r in rows_a if ALPHA in r["content"] and BETA in r["content"]] \
        + [r["chunk_index"] for r in rows_b if ALPHA in r["content"] and BETA in r["content"]]
    check("没有任何切片同时含 ALPHA 与 BETA", not cross, f"同时含两者的片={cross}")
    marker_chunks_a = [r["chunk_index"] for r in rows_a if ALPHA in r["content"]]
    marker_chunks_b = [r["chunk_index"] for r in rows_b if BETA in r["content"]]
    check("每库恰有一片承载自己的标记（标记未被复制到多片）",
          len(marker_chunks_a) == 1 and len(marker_chunks_b) == 1,
          f"ALPHA@KA{marker_chunks_a} BETA@KB{marker_chunks_b}")
    check("标记落在非首片（证明多片切分确实发生）",
          all(i > 0 for i in marker_chunks_a + marker_chunks_b))
    check("KA 的每一片都只属于 KA（含自己的标记或公共前缀，绝不含对方标记）",
          all(BETA not in r["content"] for r in rows_a))
    check("KB 的每一片都只属于 KB",
          all(ALPHA not in r["content"] for r in rows_b))
    check("KA 与 KB 的 chunk 主键无交集", not ({r["id"] for r in rows_a} & {r["id"] for r in rows_b}))
    check("两库 kb_id 字段分别指向各自集合",
          all(r.get("kb_id") == coll_a for r in rows_a)
          and all(r.get("kb_id") == coll_b for r in rows_b))

    # 直接在另一集合里按标记条件查询（绕过检索层，直接问 Milvus）：应为空
    from pymilvus import Collection, utility
    if utility.has_collection(coll_a):
        in_ka_beta = Collection(coll_a).query(
            expr=f'content like "%{BETA}%"', output_fields=["id", "doc_id"],
            limit=10, consistency_level="Strong", timeout=15)
    else:
        in_ka_beta = None
    check("KA 集合按 BETA 条件查询为空（跨库不可见）", in_ka_beta == [],
          f"命中 {len(in_ka_beta)} 条" if in_ka_beta else "0 命中")

    # ---------- 6. 授权边界（真实上传后仍成立）----------
    print("\n[6] 授权边界")
    r = client.get(f"/knowledge-bases/{ka['id']}/documents", headers=auth(tok_u2))
    check("U2 读 U1 的库文档 -> 404", r.status_code == 404, f"status={r.status_code}")
    r = client.post(f"/knowledge-bases/{ka['id']}/documents",
                    files={"file": ("x.txt", b"x", "text/plain")}, headers=auth(tok_u2))
    check("U2 往 U1 的库上传 -> 404", r.status_code == 404, f"status={r.status_code}")
    r = client.post("/research/stream", json={"query": "x", "search_web": False, "search_local": True,
                    "search_modes": ["local"], "version": "v2"}, headers=auth(tok_u1))
    check("local 搜索缺 kb_id/kb_name -> 422", r.status_code == 422, f"status={r.status_code}")
    r = client.post("/research/stream", json={"query": "x", "search_web": False, "search_local": True,
                    "search_modes": ["local"], "kb_id": "not-a-uuid", "version": "v2"}, headers=auth(tok_u1))
    check("local 搜索 kb_id 非法 -> 422", r.status_code == 422, f"status={r.status_code}")
    r = client.post("/research/stream", json={"query": "x", "search_web": False, "search_local": True,
                    "search_modes": ["local"], "kb_id": ka["id"], "version": "v2"}, headers=auth(tok_u2))
    check("U2 用 kb_id 指向 U1 的库 -> 404", r.status_code == 404, f"status={r.status_code}")
    r = client.get("/knowledge-bases", headers=auth("bogus-token"))
    check("伪造 token -> 401", r.status_code == 401, f"status={r.status_code}")

    # ---------- 7. 真实检索可见性（走 scout 本地检索真实路径）----------
    # 说明：这里直接调用 ScoutAgent._execute_local_search，而不是起整轮 /research/stream。
    # 理由是整轮会连带 6 个 Agent 的 LLM 调用（约 13 万 token / 20 分钟），
    # 而本项要验证的只是"授权绑定 -> 查询向量化 -> Milvus 过滤 -> 文档仍在库内"
    # 这条链路本身。它不是 mock：用的就是生产代码里的同一个方法与同一个 binding。
    print("\n[7] 真实检索可见性（scout 本地检索真实路径）")
    import asyncio
    from service.kb_binding import resolve_kb_binding
    from service.deep_research_v2.agents.scout import DeepScout

    async def local_search(kb_id: str, uid: str, query: str):
        with SessionLocal() as db:
            binding = resolve_kb_binding(db, uid, kb_id=kb_id, required=True)
        from config.llm_config import get_config
        cfg = get_config()
        agent = DeepScout(llm_api_key=cfg.api_key, llm_base_url=cfg.base_url,
                          search_api_key=cfg.search_api_key)
        if not agent.milvus_service:
            raise SystemExit("ScoutAgent 未能初始化 Milvus 服务，本地检索不可用")
        state = {"kb_binding": binding, "user_id": uid}
        return await agent._execute_local_search(query, state)

    # 中性 query：命中两份文档共享的公共前缀，两边都应召回，从而能检查"另一边在不在"。
    neutral = "向量集合互不串库 doc_id 文档 UUID 切片"
    with SessionLocal() as db:
        uid1 = str(db.query(KBModel).filter(KBModel.id == _UUID(ka["id"])).first().user_id)
    res_a = asyncio.run(local_search(ka["id"], uid1, neutral))
    res_b = asyncio.run(local_search(kb["id"], uid1, neutral))
    blob_a = "\n".join(r.get("content", "") for r in res_a)
    blob_b = "\n".join(r.get("content", "") for r in res_b)
    check("KA 本地检索有召回", len(res_a) > 0, f"{len(res_a)} 条")
    check("KB 本地检索有召回", len(res_b) > 0, f"{len(res_b)} 条")
    check("KA 检索结果含 ALPHA 且不含 BETA", ALPHA in blob_a and BETA not in blob_a)
    check("KB 检索结果含 BETA 且不含 ALPHA", BETA in blob_b and ALPHA not in blob_b)
    check("KA 检索结果的 work_id 均指向 KA 的文档",
          all(r.get("work_id") == doc_a for r in res_a), f"work_ids={[r.get('work_id') for r in res_a]}")
    check("KB 检索结果的 work_id 均指向 KB 的文档",
          all(r.get("work_id") == doc_b for r in res_b))
    check("本地检索 url 的 chunk_id 与 Milvus 主键一致",
          all(r["url"].rsplit("#", 1)[-1] in {x["id"] for x in rows_a} for r in res_a))

    print("\n" + "=" * 72)
    print(f"取证信息: KA={ka['id']} doc={doc_a} chunks={len(rows_a)} collection={coll_a}")
    print(f"          KB={kb['id']} doc={doc_b} chunks={len(rows_b)} collection={coll_b}")
    print(f"          检索召回 KA={len(res_a)} 条 / KB={len(res_b)} 条")
    print("=" * 72)
    return finish()


def finish() -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\nA1-I(真实上传链路): {passed}/{total} PASS")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} — {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

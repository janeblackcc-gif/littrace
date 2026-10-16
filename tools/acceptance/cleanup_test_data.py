"""清理验收测试产生的数据（用户 / 知识库 / 文档 / Milvus 集合 / 检查点）。

安全约束（重要）：
- 只删**名字带验收前缀**的对象（a1i_ / uii_ / probe_ 用户；KA_ / KB_ / UII_ 知识库）。
- 只删这些知识库对应的 `kb_<uuid-hex>` Milvus 集合，**绝不触碰**其他集合
  （如他项目的 hybrid_demo）。共享 Milvus 实例上不做任何全局操作。
- 默认 DRY RUN，只打印将删除什么；加 --apply 才真正执行。

用法：
    python tools/acceptance/cleanup_test_data.py
    python tools/acceptance/cleanup_test_data.py --apply
"""
from __future__ import annotations

import os
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(BASE, "backend", "app"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(BASE, ".env"))

from core.database import SessionLocal  # noqa: E402
from models import User, KnowledgeBase, Document, ResearchCheckpoint  # noqa: E402
from service.milvus_service import get_milvus_service  # noqa: E402
from pymilvus import utility  # noqa: E402

# 验收与测量脚本建的都是一次性用户。前缀**穷举自全仓所有创建用户的脚本**：
#   a1i_real_upload.py            -> a1i_
#   ui_i_browser.mjs              -> uii_
#   c1i_check_credentials.py      -> probe_（旧命名，保留）
#   diagnose_run_and_evidence.py  -> measure_（前缀保留：文件改名但用户前缀不变，
#                                    否则改名前的残留用户会被漏删；见 §PR1 改名说明）
#   ui_j_real_vertical_run.mjs    -> uib_（B 真实纵向联调）
#   verify_jwt_rotation.py        -> jwt_
#   verify_a5i_after_keepalive.py -> a5i_
#   verify_direction_lineage.py   -> dl_
#   （diag 为早期诊断脚本用过的前缀）
# 曾多次「发现一个补一个」而漏删，故新增脚本时**必须**同步这里，
# 并由下面的 unmatched 检查兜底报警，而不是静默漏掉。
USER_PREFIXES = ("a1i_", "uii_", "uib_", "probe_", "measure_", "jwt_", "a5i_", "dl_", "diag")
KB_PREFIXES = ("KA_", "KB_", "UII_")


def main() -> int:
    apply = "--apply" in sys.argv
    mode = "APPLY" if apply else "DRY RUN"
    print(f"模式: {mode}\n")

    milvus = get_milvus_service()
    all_collections = set(utility.list_collections())

    with SessionLocal() as db:
        users = [u for u in db.query(User).all() if u.username.startswith(USER_PREFIXES)]
        # 兜底报警：库里有用户却不匹配任何已知前缀时，明确报出来而不是静默漏掉。
        # （历史上多次因新增脚本而漏前缀，导致测试用户残留。）
        all_users = db.query(User).all()
        unmatched = [u.username for u in all_users if not u.username.startswith(USER_PREFIXES)]
        if unmatched:
            print(f"⚠️ 有 {len(unmatched)} 个用户不匹配任何已知测试前缀，本次**不会**删除：")
            for name in unmatched[:20]:
                print(f"     {name}")
            print("   若它们确实来自某个验收脚本，请把该脚本的用户名前缀加进 USER_PREFIXES。\n")
        kbs = [k for k in db.query(KnowledgeBase).all() if k.name.startswith(KB_PREFIXES)]
        user_ids = {u.id for u in users}
        kb_ids = {k.id for k in kbs}
        docs = [d for d in db.query(Document).all() if d.knowledge_base_id in kb_ids or d.user_id in user_ids]
        cps = [c for c in db.query(ResearchCheckpoint).all() if c.user_id in user_ids]

        print(f"将删除: 用户 {len(users)} / 知识库 {len(kbs)} / 文档 {len(docs)} / 检查点 {len(cps)}")
        for k in kbs:
            print(f"  KB {k.name}  collection={k.vector_collection}")
        print(f"\n将删除的 Milvus 集合 (仅 kb_<UUID-hex> 且属于上述知识库):")
        target_collections = []
        for k in kbs:
            if k.vector_collection and k.vector_collection in all_collections:
                target_collections.append(k.vector_collection)
                print(f"  {k.vector_collection}")
        untouched = sorted(all_collections - set(target_collections))
        print(f"\n【不会触碰】的其他集合: {untouched}")

        if not apply:
            print("\nDRY RUN 结束。加 --apply 真正执行。")
            return 0

        # 1) Milvus 集合（逐个删，每个都确认不再存在）
        for name in target_collections:
            ok = milvus.delete_collection(name)
            print(f"  drop {name}: {'OK' if ok and name not in utility.list_collections() else 'FAILED'}")
        for k in kbs:
            for d in [d for d in docs if d.knowledge_base_id == k.id and d.file_path]:
                try:
                    os.unlink(d.file_path)
                except OSError:
                    pass
        # 2) 数据库行（先子后父）
        for c in cps:
            db.delete(c)
        for d in docs:
            db.delete(d)
        db.flush()
        for k in kbs:
            db.delete(k)
        db.flush()
        for u in users:
            db.delete(u)
        db.commit()

    with SessionLocal() as db:
        left_u = len([u for u in db.query(User).all() if u.username.startswith(USER_PREFIXES)])
        left_k = len([k for k in db.query(KnowledgeBase).all() if k.name.startswith(KB_PREFIXES)])
    left_c = len([c for c in utility.list_collections() if c in set(target_collections)])
    print(f"\n清理后残留: 测试用户 {left_u} / 测试知识库 {left_k} / 目标集合 {left_c}")
    print(f"Milvus 现有集合: {sorted(utility.list_collections())}")
    return 0 if (left_u == 0 and left_k == 0 and left_c == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())

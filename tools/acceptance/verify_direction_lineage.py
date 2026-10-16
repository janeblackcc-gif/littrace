"""方向贯通端到端检查（完整链路）。

要求的链路：
    前端选择 → ResearchRequest.direction → service.research(direction_request)
    → graph.run() → ResearchState.direction_request → Architect 解析/推导
    → ResearchState.resolved_direction + research_plan
    → 检查点冻结与恢复
**恢复时必须用保存的方向档案，不能用「当前默认方向」覆盖它。**

本脚本只跑「规划阶段」所需的最小真实链路（低 max_iterations、单方向），
核对三个方向各自的 profile_hash / facet / claim_type 是否真的进入并冻结在检查点里，
然后核对恢复路径读的是保存值。

用法：
    python tools/acceptance/verify_direction_lineage.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(BASE, "backend", "app"))
os.environ["ENABLE_DIRECTION_PROFILES"] = "true"

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(BASE, ".env"))
os.environ["ENABLE_DIRECTION_PROFILES"] = "true"

from core.runtime_settings import sanitize_no_proxy  # noqa: E402
sanitize_no_proxy()

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def new_user(tag):
    from core.database import SessionLocal
    from core.security import get_password_hash
    from models import User
    s = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(username=f"dl_{tag}_{s}", email=f"dl_{tag}_{s}@example.com",
                 hashed_password=get_password_hash("Dl-Passw0rd!"))
        db.add(u); db.commit(); db.refresh(u)
        return str(u.id)


def fetch_state(user_id, session_id):
    from core.database import SessionLocal
    from models import ResearchCheckpoint
    from uuid import UUID as _U
    with SessionLocal() as db:
        row = db.query(ResearchCheckpoint).filter(
            ResearchCheckpoint.session_id == session_id,
            ResearchCheckpoint.user_id == _U(user_id)).first()
        return (row.state_json or {}) if row else {}


async def plan_only(preset_id: str):
    """跑到「architect 完成、检查点已冻结方向」为止，然后取消。

    关键时序（research_runtime.py execute_stages）：
      architect 的 process() 先设置 state["resolved_direction"]（architect.py:379）
      与 state["research_plan"]（:398），并 yield direction_resolved / outline 事件；
      **但落库发生在该阶段循环末尾的 `await commit()`（research_runtime.py:130）**，
      之后才 yield `checkpoint_saved`。
    因此必须等到 `checkpoint_saved` 才算「方向已冻结」——首版脚本在 outline 就收手，
    恰好卡在 commit 之前，落库的仍是初始状态（只有 direction_request）。
    """
    from service.deep_research_v2.service import DeepResearchV2Service
    # 用 service 层，保证与生产同一条路径（DeepResearchV2Service 在 service 模块，
    # 不在 graph 模块——首版导入路径写错）
    uid = new_user(preset_id[:8])
    sid = str(uuid.uuid4())
    service = DeepResearchV2Service(max_iterations=0)
    events, direction_event, outline_seen, saved = [], None, False, False
    import asyncio as _a
    async def consume():
        nonlocal direction_event, outline_seen, saved
        async for ev in service.graph.run(
                "方向贯通检查用查询", sid, user_id=uid, search_web=True, search_local=False,
                direction_request={"choice": {"mode": "preset", "preset_id": preset_id},
                                   "start_year": 2022, "end_year": 2025, "focus_angles": []}):
            events.append(ev.get("type"))
            if ev.get("type") == "direction_resolved":
                direction_event = ev
            if ev.get("type") == "outline":
                outline_seen = True
            if ev.get("type") == "checkpoint_saved":
                saved = True
                return          # 此刻 plan 阶段的状态已在一个事务里落库
            if ev.get("type") in {"research_complete", "research_cancelled", "error"}:
                return
    task = _a.ensure_future(consume())
    try:
        await _a.wait_for(task, timeout=240)
    except _a.TimeoutError:
        task.cancel()
    await _a.sleep(2)
    return uid, sid, direction_event, events, (outline_seen and saved)


async def resume_and_read(uid, sid):
    """走**真实恢复路径**，确认恢复用的是保存的方向档案。"""
    from service.deep_research_v2.service import DeepResearchV2Service
    service = DeepResearchV2Service(max_iterations=0)
    events = []
    from service.research_control import request_cancel
    request_cancel(sid)   # 让恢复后尽快停下（恢复本身已证明读的是保存值）
    try:
        async for ev in service.graph.run("", sid, resume=True, user_id=uid):
            events.append(ev.get("type"))
            if ev.get("type") in {"research_complete", "research_cancelled", "error", "checkpoint_loaded"}:
                break
    except Exception as exc:
        events.append(f"EXC:{type(exc).__name__}:{str(exc)[:80]}")
    return events


async def main() -> int:
    snapshots = {}
    for pid in ("agent_memory", "rag_context", "llm_evaluation"):
        print(f"\n[{pid}] 真实链路（plan 阶段，等到 outline 落库）")
        try:
            uid, sid, dev, events, outline_seen = await plan_only(pid)
        except Exception as exc:
            check(f"{pid} 链路无异常", False, f"{type(exc).__name__}: {str(exc)[:110]}")
            continue
        check(f"{pid} 产生 direction_resolved 事件", dev is not None,
              f"事件序列={events[:8]}")
        check(f"{pid} 跑到 outline（architect 已返回）", outline_seen)
        st = fetch_state(uid, sid)
        rd = st.get("resolved_direction") or {}
        dr = st.get("direction_request") or {}
        check(f"{pid} 检查点冻结了 resolved_direction", bool(rd), f"keys={sorted(rd)[:6]}")
        check(f"{pid} 检查点冻结了 direction_request（原始请求）", bool(dr))
        check(f"{pid} profile_hash 已落库", bool(rd.get("profile_hash")),
              f"profile_hash={str(rd.get('profile_hash'))[:16]}...")
        check(f"{pid} rule_origin=preset", rd.get("rule_origin") == "preset",
              f"rule_origin={rd.get('rule_origin')}")
        prof = rd.get("profile") or {}
        check(f"{pid} 档案 id 与请求一致", prof.get("id") == pid,
              f"profile.id={prof.get('id')}")
        check(f"{pid} 请求里的 preset_id 与落库一致",
              (dr.get("choice") or {}).get("preset_id") == pid,
              f"request.choice={dr.get('choice')}")
        check(f"{pid} research_plan 已落库", bool(st.get("research_plan")))
        check(f"{pid} search_query_origins 记录了 seed 来源（断裂 1）",
              any(o.get("origin") == "profile.seed_queries"
                  for o in (st.get("search_query_origins") or [])),
              f"共 {len(st.get('search_query_origins') or [])} 条")
        # 恢复路径：必须读保存的档案，不被当前默认方向覆盖
        rev = await resume_and_read(uid, sid)
        check(f"{pid} 恢复走真实 resume 路径", any("checkpoint_loaded" in str(e) for e in rev),
              f"恢复事件={rev[:4]}")
        st2 = fetch_state(uid, sid)
        rd2 = st2.get("resolved_direction") or {}
        check(f"{pid} 恢复后 resolved_direction 保持不变（未被默认方向覆盖）",
              (rd2.get("profile_hash") == rd.get("profile_hash")) and bool(rd2.get("profile_hash")),
              f"恢复前={str(rd.get('profile_hash'))[:12]} 恢复后={str(rd2.get('profile_hash'))[:12]}")
        snapshots[pid] = {"hash": rd.get("profile_hash"),
                          "facets": [f.get("id") for f in (prof.get("facets") or [])],
                          "claim_types": [c.get("id") for c in (prof.get("claim_types") or [])],
                          "sections": [s.get("id") for s in ((st.get("research_plan") or {}).get("sections") or [])],
                          "required_conditions": [c for s in ((st.get("research_plan") or {}).get("sections") or [])
                                                  for c in (s.get("required_conditions") or [])][:6]}

    print("\n[跨方向对比]")
    hashes = {k: v["hash"] for k, v in snapshots.items()}
    check("三方向 profile_hash 互不相同", len(set(hashes.values())) == len(hashes) == 3,
          json.dumps({k: str(v)[:12] for k, v in hashes.items()}, ensure_ascii=False))
    facet_sets = {k: tuple(v["facets"] or []) for k, v in snapshots.items()}
    check("三方向 facet 集合互不相同", len(set(facet_sets.values())) == 3,
          json.dumps({k: list(v) for k, v in facet_sets.items()}, ensure_ascii=False))
    ct_sets = {k: tuple(v["claim_types"] or []) for k, v in snapshots.items()}
    check("三方向 claim_type 集合互不相同", len(set(ct_sets.values())) == 3,
          json.dumps({k: list(v) for k, v in ct_sets.items()}, ensure_ascii=False))
    for k, v in snapshots.items():
        print(f"    {k:<16} facets={v['facets']}  claim_types={v['claim_types']}  sections={v['sections']}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n方向贯通端到端: {passed}/{len(results)} PASS")
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} — {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

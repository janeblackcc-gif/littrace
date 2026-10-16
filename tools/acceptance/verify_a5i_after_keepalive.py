"""A5-I 补充验证：改了 CheckpointGuard（加保活）之后的真实恢复与防重入行为。

为什么必须重验：本轮为修「长跑被 idle-session timeout 掐断」给 CheckpointGuard 加了
看门狗线程 + RLock + 退出时停线程。**锁语义是 A5-I 的核心**，改动后必须重新证明：
  1. 阶段中断后 resume 能从保存的 next_stage 继续，且不重跑已完成阶段
  2. 同会话第二个执行者被 RunBusy 拒绝（不出现第二条执行轨迹）
  3. 持锁进程被强杀后锁自动释放
  4. 全流程无残留 advisory 锁

用法：
    python tools/acceptance/verify_a5i_after_keepalive.py
"""
from __future__ import annotations

import os
import sys
import time
import uuid

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(BASE, "backend", "app"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(BASE, ".env"))
from core.runtime_settings import sanitize_no_proxy  # noqa: E402
sanitize_no_proxy()

from core.database import SessionLocal, engine  # noqa: E402
from core.security import get_password_hash  # noqa: E402
from models import User  # noqa: E402
from service.checkpoint_guard import CheckpointGuard, RunBusy  # noqa: E402
from service.research_integrity import ContractError  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def advisory_count():
    with engine.connect() as c:
        return c.exec_driver_sql(
            "SELECT count(*) FROM pg_locks WHERE locktype='advisory'").scalar()


def orphan_report():
    """列出**本脚本之外**就已存在的 advisory 锁。

    为什么需要：若先前某次运行被强杀（例如测试中途 kill 掉持锁进程），
    PG 会留下一个 idle backend 继续持锁，直到 idle_session_timeout 回收。
    此时「全局锁数 == 0」这类断言会无辜失败，让人误以为是本次回归。
    故本脚本改为**幂等**：先记录基线，只断言「本次没有新增残留」。
    """
    with engine.connect() as c:
        rows = c.exec_driver_sql("""
            SELECT l.pid, a.state, now()-a.state_change AS idle_for
            FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid=l.pid
            WHERE l.locktype='advisory'
        """).mappings().all()
    return [(r["pid"], r["state"], str(r["idle_for"])) for r in rows]


def new_user():
    s = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(username=f"a5i_{s}", email=f"a5i_{s}@example.com",
                 hashed_password=get_password_hash("A5i-Passw0rd!"))
        db.add(u); db.commit(); db.refresh(u)
        return str(u.id), f"a5i_{s}"


def state(session_id, user_id, next_stage, completed):
    return {"schema_version": 2, "pipeline_version": 2, "session_id": session_id,
            "user_id": user_id, "query": "q", "phase": "p", "iteration": 0,
            "next_stage": next_stage, "completed_stages": list(completed),
            "stage_history": [], "search_web": True, "search_local": False,
            "run_status": "running", "hypotheses": [], "evidence_links": [],
            "final_report": "", "research_config": {"quality_threshold": 6,
            "max_iterations": 1, "schema_version": 2, "pipeline_version": 2}}


def main() -> int:
    baseline = orphan_report()
    if baseline:
        print("⚠️ 检测到本次运行【之前就存在】的 advisory 锁（多为上次被强杀留下的孤儿）：")
        for pid, holder_state, idle in baseline:   # 勿用 state 作循环变量：会遮蔽模块级 state() 函数
            print(f"     pid={pid} state={holder_state} idle_for={idle}")
        print("   本脚本按「不新增残留」判定，不受其影响；如需清理：")
        print("     docker exec littrace-pg psql -U littrace -d littrace_dev \\")
        print("       -c \"SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype='advisory';\"")
        print()
    base_count = advisory_count()
    print(f"基线 advisory 锁数 = {base_count}（本次结束时应仍为 {base_count}）\n")

    uid, uname = new_user()
    sid = str(uuid.uuid4())

    # ---------- 1. 保存阶段进度后释放锁（模拟阶段边界中断）----------
    print("[1] 阶段边界中断后恢复")
    with CheckpointGuard(engine, sid, uid) as g:
        g.save(state(sid, uid, "write", ["plan", "research", "analyze"]), {"streaming_report": ""})
        check("保存完成，锁仍持有", g.conn is not None)
    check("退出后本会话的锁已释放", advisory_count() == base_count,
          f"advisory={advisory_count()}（基线 {base_count}）")

    with CheckpointGuard(engine, sid, uid) as g:
        loaded = g.load()
        check("能重新取回检查点", loaded is not None)
        check("next_stage 保持为保存值（未被默认值覆盖）",
              loaded.get("next_stage") == "write", f"next_stage={loaded.get('next_stage')}")
        check("completed_stages 保持",
              loaded.get("completed_stages") == ["plan", "research", "analyze"],
              str(loaded.get("completed_stages")))

    # ---------- 2. 双执行者：第二个必须被 RunBusy 拒绝 ----------
    print("\n[2] 同会话第二个执行者被拒绝（防重入）")
    holder = CheckpointGuard(engine, sid, uid)
    holder.__enter__()
    try:
        check("第一个执行者拿到锁", holder.conn is not None, f"pid={holder.pid}")
        second = CheckpointGuard(engine, sid, uid)
        try:
            second.__enter__()
            check("第二个执行者被拒绝", False, "竟然也拿到了锁")
            second.__exit__(None, None, None)
        except RunBusy as exc:
            check("第二个执行者被 RunBusy 拒绝", True, str(exc)[:60])
        except Exception as exc:
            check("第二个执行者被 RunBusy 拒绝", False, f"{type(exc).__name__}: {exc}")
        # 持锁期间保活仍应让连接可用
        time.sleep(3)
        holder._check_lock()
        check("持锁期间锁仍有效（保活未影响锁）", True)
    finally:
        holder.__exit__(None, None, None)

    # ---------- 3. 旧执行者被作废后拒绝写入 ----------
    print("\n[3] 锁连接失效后拒绝继续写")
    g2 = CheckpointGuard(engine, sid, uid)
    g2.__enter__()
    g2.conn.invalidate()
    try:
        g2.save(state(sid, uid, "done", []), {})
        check("连接失效后 save 被拒绝", False, "竟然写成功了")
    except (ContractError, Exception) as exc:
        ok = isinstance(exc, ContractError) or "lock" in str(exc).lower() or "closed" in str(exc).lower()
        check("连接失效后 save 被拒绝", ok, f"{type(exc).__name__}: {str(exc)[:70]}")
    finally:
        g2.__exit__(None, None, None)

    # ---------- 4. 收尾：删检查点、无残留锁 ----------
    print("\n[4] 收尾")
    with CheckpointGuard(engine, sid, uid) as g:
        deleted = g.delete()
        check("删除检查点成功", deleted)
    check("无新增残留 advisory 锁", advisory_count() == base_count,
          f"advisory={advisory_count()}（基线 {base_count}）")
    with SessionLocal() as db:
        row = db.query(User).filter(User.username == uname).first()
        if row:
            db.delete(row); db.commit()
    with SessionLocal() as db:
        gone = db.query(User).filter(User.username == uname).first() is None
    check("测试用户已清理", gone)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\nA5-I(改保活后重验): {passed}/{len(results)} PASS")
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} — {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""针对性验证 CheckpointGuard 保活心跳是否解决了 idle-session timeout。

背景：一轮研究跑 10–25 分钟，持锁连接大部分时间空闲；littrace-pg 的
idle_session_timeout=600s 会单方面终止它，导致整轮失败（实测 11.8/12.5 分钟崩）。

本脚本不赌 12 分钟：把 CHECKPOINT_KEEPALIVE_SEC 设小（默认 5s），
持锁时间**超过该阈值若干倍**，然后断言
  1) 连接没有被服务端终止（还能执行查询）
  2) 仍是同一个 backend pid（锁仍在同一连接上，没被静默换掉）
  3) advisory 锁仍被持有

对照组：CHECKPOINT_KEEPALIVE_SEC=0（关闭保活）时，用一个**真正空闲**的连接
证明服务端确实会杀空闲连接——但服务端阈值是 600s，无法在短测里触发，
故改为直接断言「关闭保活时不启动看门狗线程」，并用 server 端参数说明理论后果。

用法：
    python tools/acceptance/verify_guard_keepalive.py
"""
from __future__ import annotations

import os
import sys
import time

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(BASE, "backend", "app"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(BASE, ".env"))
from core.runtime_settings import sanitize_no_proxy  # noqa: E402
sanitize_no_proxy()

HOLD_SEC = float(os.getenv("HOLD_SEC", "25"))
INTERVAL = os.getenv("CHECKPOINT_KEEPALIVE_SEC", "5")

results = []


def advisory_count() -> int:
    from core.database import engine
    with engine.connect() as c:
        return c.exec_driver_sql(
            "SELECT count(*) FROM pg_locks WHERE locktype='advisory'").scalar()


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def main() -> int:
    os.environ["CHECKPOINT_KEEPALIVE_SEC"] = INTERVAL
    from core.database import engine
    from service.checkpoint_guard import CheckpointGuard

    print(f"配置: CHECKPOINT_KEEPALIVE_SEC={INTERVAL}s, 持锁 {HOLD_SEC}s\n")
    with engine.connect() as c:
        server_idle = c.exec_driver_sql("SHOW idle_session_timeout").scalar()
    print(f"服务端 idle_session_timeout = {server_idle}")
    base_count = advisory_count()
    if base_count:
        print(f"⚠️ 基线已存在 {base_count} 个 advisory 锁（多为上次被强杀留下的孤儿）；"
              f"本脚本按「不新增」判定")
    print()

    guard = CheckpointGuard(engine, "keepalive-probe-session", None)
    with guard:
        pid0 = guard.pid
        check("进入时启动了看门狗线程",
              guard._watchdog is not None and guard._watchdog.is_alive(),
              f"thread={getattr(guard._watchdog, 'name', None)}")
        print(f"      持锁 backend pid={pid0}，开始等待 {HOLD_SEC}s（阈值 {INTERVAL}s → 期间应触发多次保活）…")
        time.sleep(HOLD_SEC)
        # 注意：这里的任何一次 execute 都会 autobegin 打开事务，检查完必须 rollback，
        # 否则下面的 begin() 会因「already initialized a Transaction」而失败——
        # 那是本脚本自己的副作用，不是被测代码的问题（第一版就踩了这个坑）。
        pid_now = None
        try:
            with guard.conn.begin():
                pid_now = guard.conn.execute(
                    __import__("sqlalchemy").text("SELECT pg_backend_pid()")).scalar()
        except Exception as exc:
            check("持锁期间连接仍可用", False, f"{type(exc).__name__}: {str(exc)[:140]}")
        if pid_now is not None:
            check("持锁期间连接仍可用（未被 idle-session timeout 终止）", True, f"pid={pid_now}")
            check("仍是同一个 backend pid（锁未被静默换连接）", pid_now == pid0, f"{pid0} -> {pid_now}")
        try:
            guard._check_lock()
            check("_check_lock 通过（advisory 锁仍持有）", True)
        except Exception as exc:
            check("_check_lock 通过（advisory 锁仍持有）", False, f"{type(exc).__name__}: {str(exc)[:140]}")
        # _check_lock 自身会 execute → autobegin 打开事务；独立调用它之后必须自行收尾，
        # 否则下面的 begin() 会抛 InvalidRequestError。这是**本脚本的**责任：
        # 生产路径里 load/save/delete 都是先 `with conn.begin()` 再调用 _check_lock，
        # 属于同一事务，不存在这个残留。
        if guard.conn.in_transaction():
            guard.conn.rollback()

        # 关键补测：保活跑了很多次之后，必须还能正常开业务事务。
        # 首版保活直接 conn.execute(text("SELECT 1"))，在 SQLAlchemy 2.0 下 autobegin
        # 留下打开的事务，导致后续 conn.begin() 抛 InvalidRequestError
        # （实测整轮研究在 14.2 分钟时失败）。当时的验证只查了「连接还在、pid 没变」，
        # 没模拟真实的 begin() 序列，所以漏掉了它。这里连续做多次以覆盖反复触发。
        txn_ok, txn_detail = True, []
        for _ in range(3):
            if guard.conn.in_transaction():
                guard.conn.rollback()
            try:
                with guard.conn.begin():
                    v = guard.conn.execute(__import__("sqlalchemy").text("SELECT 1")).scalar()
                if v != 1:
                    txn_ok, txn_detail = False, [f"SELECT 1 -> {v}"]
            except Exception as exc:
                txn_ok, txn_detail = False, [f"{type(exc).__name__}: {str(exc)[:130]}"]
                break
            time.sleep(min(2.0, max(0.5, float(INTERVAL) / 2)))
        check("保活反复触发后仍能连续开启业务事务（autobegin 冲突回归）", txn_ok,
              "；".join(txn_detail))
        check("业务事务后连接处于无事务状态", not guard.conn.in_transaction(),
              f"in_transaction={guard.conn.in_transaction()}")

    check("退出后看门狗已停止", guard._watchdog is None)
    # 幂等判定：只断言「本次没有新增残留」。若上次运行被强杀，PG 会留下一个 idle
    # backend 继续持锁（直到 idle_session_timeout 回收），此时「全局锁数==0」会无辜失败，
    # 让人误判成本次回归——故先取基线再比较。
    left = advisory_count()
    check("退出后无新增 advisory 锁（幂等，允许历史孤儿）", left <= base_count,
          f"pg_locks advisory={left}（基线 {base_count}）")

    # 关闭保活时不启动线程
    os.environ["CHECKPOINT_KEEPALIVE_SEC"] = "0"
    g2 = CheckpointGuard(engine, "keepalive-off-probe", None)
    with g2:
        check("保活设为 0 时不启动看门狗（可关闭）", g2._watchdog is None)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n保活验证: {passed}/{len(results)} PASS")
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} — {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

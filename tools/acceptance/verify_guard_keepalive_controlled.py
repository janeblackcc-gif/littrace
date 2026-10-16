#!/usr/bin/env python3
"""PR4 心跳验收：用**受控 timeout 的测试连接**证明保活真的在起作用。

代码评审的要求（PR4）：用受控 timeout 的测试连接验证 heartbeat，
**而不是**「服务端 idle 600 秒、测试只等 25 秒」——后者根本没进入被测区间，等于没测。

本脚本做三件事，全部**只操作我们自己的会话**（`SET` 会话级参数），
不使用 `ALTER SYSTEM` / 不改共享 PostgreSQL 全局配置：

  A. 反证：普通连接设 `idle_session_timeout='3s'` 后空置 ~7s → 连接**必须被服务端终止**
     （证明这个短超时真的生效，测试确实落在被测区间内）。
  B. 正例：`CheckpointGuard` 持锁 + 保活心跳（`CHECKPOINT_KEEPALIVE_SEC=1`），
     同样把会话超时调到 3s 后空置 ~8s（> 2× 超时）→ 连接**必须存活**、锁仍由同一 backend 持有。
  C. 断言全程没有出现未处理异常，并报告实际时间线。

用法：
    python tools/acceptance/verify_guard_keepalive_controlled.py
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "backend" / "app"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(BASE / ".env")

from core.runtime_settings import sanitize_no_proxy  # noqa: E402
sanitize_no_proxy()

IDLE_TIMEOUT = os.getenv("CONTROLLED_IDLE_TIMEOUT", "3s")
IDLE_SECONDS = int(os.getenv("CONTROLLED_IDLE_SECONDS", "7"))
HOLD_SECONDS = float(os.getenv("CONTROLLED_HOLD_SECONDS", "8"))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def session_timeout_verdict(exc: Exception) -> bool:
    """判断异常是否**明确**是「会话被 idle_session_timeout 终止」。

    旧实现在消息里找 "server closed the connection" /
    "administrator command" / "connection has been closed"，于是**任何**普通断连
    都会被算成「超时控制生效」。PostgreSQL 建议优先判 SQLSTATE，而不是错误文字：
      - idle_session_timeout 终止会话的 SQLSTATE 是 **57P05**；
      - 只有在驱动没保留 SQLSTATE 时，才退回到 PostgreSQL 的**精确措辞**
        "idle-session timeout"（仍然不接受泛化的断连描述）。
    """
    idle_sqlstate = "57P05"
    codes = set()
    orig = getattr(exc, "orig", None)
    for obj in (exc, orig):
        if obj is None:
            continue
        for attr in ("pgcode", "sqlstate"):
            code = getattr(obj, attr, None)
            if code:
                codes.add(str(code))
    if idle_sqlstate in codes:
        return True
    diag = getattr(orig, "diag", None)
    for attr in ("message_primary", "message_detail", "message_hint"):
        text = getattr(diag, attr, None) if diag is not None else None
        if text and "idle-session timeout" in str(text).lower():
            return True
    return "idle-session timeout" in str(exc).lower()


def part_a_negative_control(engine) -> None:
    """没有保活的连接在短超时下必须被服务端**回收**。

    不再靠错误文字判断原因——用两条**服务端可观察**的事实：
      ① 该会话的 `idle_session_timeout` 确实被设成短值（`SHOW` 读回）；
      ② 空置超过该值后，该 backend **从 pg_stat_activity 消失**（由另一条连接观察）。
    消息分类（`session_timeout_verdict`）仅作附带信息，不作为唯一依据。
    """
    from sqlalchemy import text
    conn = engine.connect()
    observer = engine.connect()
    try:
        conn.execute(text(f"SET idle_session_timeout = '{IDLE_TIMEOUT}'"))
        # 必须 commit：PostgreSQL 的普通 SET 是**事务性**的，rollback 会把它撤销，
        # 于是「超时」根本没生效（第一版就是这样自欺的，被本反证用例当场抓到）。
        conn.commit()
        pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        effective = conn.execute(text("SHOW idle_session_timeout")).scalar_one()
        conn.commit()
        check("A 前提：会话级 idle_session_timeout 已生效（SHOW 读回）", str(effective) == IDLE_TIMEOUT,
              f"SHOW idle_session_timeout = {effective}")
        print(f"[A] backend pid={pid}，idle_session_timeout={effective}，空置 {IDLE_SECONDS}s …")
        time.sleep(IDLE_SECONDS)
        alive = observer.execute(text("SELECT count(*) FROM pg_stat_activity WHERE pid = :p"),
                                 {"p": pid}).scalar_one()
        observer.rollback()
        query_failed, classified = False, None
        try:
            conn.execute(text("SELECT 1")).scalar_one()
            conn.rollback()
        except Exception as exc:  # noqa: BLE001
            query_failed = True
            classified = session_timeout_verdict(exc)
        check("A 反证：无保活的连接在短超时后被服务端回收（backend 已消失且查询失败）",
              int(alive) == 0 and query_failed,
              f"pg_stat_activity 中该 pid 计数={alive}，查询失败={query_failed}，"
              f"消息分类(仅参考)={classified}")
    finally:
        for c in (conn, observer):
            try:
                c.invalidate()
                c.close()
            except Exception:  # noqa: BLE001
                pass


def part_b_keepalive(engine) -> None:
    """有保活心跳的连接必须扛过同一短超时，且锁仍由同一 backend 持有。

    不再用字面量 True 表示「线程无异常」，
    改为**真的收集线程异常**（`threading.excepthook`）+ 观察保活线程状态。
    """
    from sqlalchemy import text
    from service.checkpoint_guard import CheckpointGuard
    import threading
    thread_errors: list[str] = []
    previous_hook = threading.excepthook

    def hook(args):  # pragma: no cover - 只在真的出错时触发
        thread_errors.append(f"{args.exc_type.__name__}: {args.exc_value}")

    threading.excepthook = hook
    try:
        os.environ["CHECKPOINT_KEEPALIVE_SEC"] = "1"
        guard = CheckpointGuard(engine, f"keepalive-probe-{uuid.uuid4().hex[:8]}", str(uuid.uuid4()))
        guard.__enter__()
        try:
            pid_before = guard.conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            guard.conn.execute(text(f"SET idle_session_timeout = '{IDLE_TIMEOUT}'"))
            guard.conn.commit()   # 同 A：SET 是事务性的，rollback 会撤销它
            print(f"[B] 保活间隔=1s，会话 idle_session_timeout={IDLE_TIMEOUT}，持续 {HOLD_SECONDS}s …")
            started = time.monotonic()
            watchdog_alive_during = []
            while time.monotonic() - started < HOLD_SECONDS:
                time.sleep(0.25)
                watchdog_alive_during.append(bool(getattr(guard._watchdog, "is_alive", lambda: False)()))
            elapsed = time.monotonic() - started
            try:
                guard._check_lock()                       # 失效必须显式抛错
                pid_after = guard.conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
                guard.conn.rollback()
                check("B 正例：保活心跳让连接扛过短超时（>2× 超时）", True,
                      f"存活 {elapsed:.1f}s，backend pid 未变：{pid_before == pid_after}")
                check("B 正例：锁仍由同一 backend 持有", pid_before == pid_after,
                      f"{pid_before} -> {pid_after}")
                check("B 正例：保活线程在整个持锁期间存活、且未记录未处理线程异常",
                      all(watchdog_alive_during) and not thread_errors,
                      f"存活采样={watchdog_alive_during}，线程异常={thread_errors}")
            except Exception as exc:  # noqa: BLE001
                check("B 正例：保活心跳让连接扛过短超时（>2× 超时）", False,
                      f"{type(exc).__name__}: {str(exc)[:140]}")
        finally:
            guard.__exit__(None, None, None)
        check("B 收尾：退出后保活线程已停止",
              not bool(getattr(guard._watchdog, "is_alive", lambda: False)()),
              f"watchdog.is_alive()={getattr(guard._watchdog, 'is_alive', lambda: None)()}")
    finally:
        threading.excepthook = previous_hook


def main() -> int:
    from core.database import engine
    assert HOLD_SECONDS > 2 * float(IDLE_TIMEOUT.rstrip("s")), "测试必须落在被测区间内（等待 > 2× 超时）"
    print(f"受控超时测试：会话级 idle_session_timeout={IDLE_TIMEOUT}（不使用 ALTER SYSTEM，不改全局配置）")
    part_a_negative_control(engine)
    part_b_keepalive(engine)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n心跳验收（受控超时）: {passed}/{len(RESULTS)} PASS")
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

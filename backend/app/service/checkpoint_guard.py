"""PostgreSQL session-level lock with a pinned connection.

All writes made by this executor use the SAME connection which owns the lock.
No long-running database transaction is held while Agents call external services.

为什么需要保活心跳（keepalive）：
    本对象为了持有 session 级 advisory 锁，会**长期借用一个连接**，并用
    ``conn.pid`` 自证身份（见 ``_check_lock``）——所以不能中途换连接。
    但一轮研究要跑 10–25 分钟，期间大部分时间在等 LLM，这条连接从服务端看
    **处于空闲状态**。而 littrace-pg 为修「孤儿 backend 持锁」设了
    ``idle_session_timeout=600000``（600s），服务端会**单方面终止**空闲超时的连接：

        FATAL: terminating connection due to idle-session timeout

    表现为下一处 DB 操作抛
    ``psycopg2.OperationalError: server closed the connection unexpectedly``，
    整轮研究失败。**实测踩到过两次**（thinking 轮分别跑到 11.8 / 12.5 分钟时崩）。

    注意 ``pool_pre_ping`` 与 ``pool_recycle`` **都解决不了**这个问题：
    前者只在**取用连接时**探测一次，后者只在**归还连接池时**判定——
    两者都管不到「已借出、正被持有、但一段时间没发查询」的连接。
    唯一有效的办法是不让这条连接被判定为空闲，故起一个看门狗线程周期性地
    发一条极轻量查询重置服务端计时器。

    阈值取服务端空闲超时的 1/5（600s → 120s），留足余量；
    可用 ``CHECKPOINT_KEEPALIVE_SEC`` 覆盖，设为 0 关闭。
"""
from __future__ import annotations
import hashlib
import json
import os
import threading
from uuid import uuid4
from sqlalchemy import text
from .research_integrity import ContractError, clean_snapshot


class RunBusy(ContractError):
    pass


def _keepalive_interval() -> float:
    raw = os.getenv("CHECKPOINT_KEEPALIVE_SEC", "")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 120.0


class CheckpointGuard:
    def __init__(self, engine, session_id: str, user_id: str | None):
        if not session_id or len(session_id) > 64:
            raise ContractError("invalid session_id")
        self.engine, self.session_id, self.user_id = engine, session_id, user_id
        self.key = int.from_bytes(hashlib.sha256(("research:" + session_id).encode()).digest()[:8], "big", signed=True)
        self.conn = None
        self.pid = None
        # db_call 通过 asyncio.to_thread 执行本对象的各方法，连接因此是**跨线程**使用的。
        # 保活线程与业务线程都会碰这条连接，必须串行化，否则是数据竞争。
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    def _keepalive_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                with self._lock:
                    if self.conn is None or self.conn.closed or self.conn.invalidated:
                        return
                    # 如果此刻正有业务事务在跑，就不要插手——保活只为重置服务端的
                    # idle 计时器，绝不能干扰业务事务的边界。
                    if self.conn.in_transaction():
                        continue
                    # SQLAlchemy 2.0 对 execute 会 **autobegin**，执行后留下一个打开的事务，
                    # 之后业务侧再调 conn.begin() 会抛
                    # InvalidRequestError("already initialized a SQLAlchemy Transaction()
                    # object via begin() or autobegin")——实测整轮研究因此在 14 分钟时失败。
                    # 因此这里显式 rollback() 收尾，把连接还原成「无事务」状态。
                    #
                    # 注意：不要用 execution_options(isolation_level="AUTOCOMMIT")——
                    # 它返回的是**新的** Connection 对象，不会原地生效（实测 isolation_level
                    # 仍为 READ COMMITTED，事务照样残留），故改用 rollback 这条确定路径。
                    self.conn.execute(text("SELECT 1"))
                    self.conn.rollback()
            except Exception:
                # 连接真的坏了就退出；由业务侧 _check_lock 给出明确错误，不在此吞掉真相。
                return

    def __enter__(self):
        self.conn = self.engine.connect()
        try:
            locked = self.conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": self.key}).scalar_one()
            if not locked:
                raise RunBusy("research session is already running")
            self.pid = self.conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            self.conn.commit()
        except BaseException:
            self.conn.invalidate()  # a partially acquired session lock must never enter the pool
            self.conn.close()
            self.conn = None
            raise
        interval = _keepalive_interval()
        if interval > 0:
            self._watchdog = threading.Thread(target=self._keepalive_loop, args=(interval,),
                                              name=f"cp-keepalive-{self.session_id[:8]}", daemon=True)
            self._watchdog.start()
        return self

    def _check_lock(self):
        """确认锁仍由**同一个** backend 持有；失效必须显式失败，绝不静默继续写。

        关于事务：本方法自身会 execute，因此在**独立调用**时会 autobegin 打开一个事务；
        而 load/save/delete 都是先 ``with self.conn.begin()`` 再调用它，属于同一事务。
        故本方法**刻意不做 rollback**——在调用方事务内部 rollback 会把调用方的事务一起
        结束掉（load 紧接着还要读 checkpoint 数据）。独立调用者需自行收尾事务，
        这一点已写进 verify_guard_keepalive.py 的注释。
        """
        if self.conn is None or self.conn.closed or self.conn.invalidated:
            raise ContractError("checkpoint lock connection lost")
        unsigned = self.key & ((1 << 64) - 1)
        row = self.conn.execute(text("""
            SELECT pg_backend_pid() AS pid, EXISTS (
              SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()
                AND classid=:hi AND objid=:lo AND objsubid=1 AND granted
            ) AS held
        """), {"hi": unsigned >> 32, "lo": unsigned & 0xffffffff}).mappings().one()
        if row["pid"] != self.pid or not row["held"]:
            raise ContractError("checkpoint lock lost; refusing stale writer")

    def _rows(self):
        return self.conn.execute(text("""
            SELECT id, user_id, state_json FROM research_checkpoints
            WHERE session_id=:sid ORDER BY updated_at DESC LIMIT 2 FOR UPDATE
        """), {"sid": self.session_id}).mappings().all()

    def _owner(self, row):
        if str(row["user_id"]) != str(self.user_id):
            raise ContractError("checkpoint owner mismatch")

    def load(self) -> dict | None:
        with self._lock, self.conn.begin():
            self._check_lock()
            rows = self._rows()
            if len(rows) > 1:
                raise ContractError("duplicate legacy checkpoints; explicit cleanup required")
            if not rows:
                return None
            self._owner(rows[0])
            return clean_snapshot(rows[0]["state_json"])

    def save(self, state: dict, ui_state: dict) -> None:
        if state.get("session_id") != self.session_id or str(state.get("user_id")) != str(self.user_id):
            raise ContractError("state/lock identity mismatch")
        state, ui_state = clean_snapshot(state), clean_snapshot(ui_state)
        payload = {"sid": self.session_id, "uid": self.user_id, "query": state["query"],
                   "phase": state["phase"], "iteration": state["iteration"],
                   "state": json.dumps(state, ensure_ascii=False, allow_nan=False),
                   "ui": json.dumps(ui_state, ensure_ascii=False, allow_nan=False),
                   "report": state.get("final_report", ""), "status": state.get("run_status", "running"),
                   "error": state.get("errors", [])[-1] if state.get("run_status") == "failed" and state.get("errors") else None}
        with self._lock, self.conn.begin():
            self._check_lock()
            rows = self._rows()
            if len(rows) > 1:
                raise ContractError("duplicate checkpoints")
            if rows:
                self._owner(rows[0])
                payload["id"] = str(rows[0]["id"])
                self.conn.execute(text("""
                    UPDATE research_checkpoints SET phase=:phase, iteration=:iteration,
                      state_json=CAST(:state AS jsonb), ui_state_json=CAST(:ui AS jsonb),
                      final_report=:report, status=:status, error_message=:error, updated_at=now()
                    WHERE id=CAST(:id AS uuid)
                """), payload)
            else:
                payload["id"] = str(uuid4())
                self.conn.execute(text("""
                    INSERT INTO research_checkpoints
                      (id,session_id,user_id,query,phase,iteration,state_json,ui_state_json,
                       final_report,status,error_message,created_at,updated_at)
                    VALUES (CAST(:id AS uuid),:sid,CAST(:uid AS uuid),:query,:phase,:iteration,
                      CAST(:state AS jsonb),CAST(:ui AS jsonb),:report,:status,:error,now(),now())
                """), payload)

    def delete(self) -> bool:
        with self._lock, self.conn.begin():
            self._check_lock()
            rows = self._rows()
            if not rows:
                return False
            for row in rows:
                self._owner(row)
            result = self.conn.execute(text("DELETE FROM research_checkpoints WHERE session_id=:sid"), {"sid": self.session_id})
            return result.rowcount > 0

    def __exit__(self, exc_type, exc, tb):
        # 先停看门狗，再动连接：否则保活线程可能在关闭过程中并发使用连接。
        self._stop.set()
        wd, self._watchdog = self._watchdog, None
        if wd is not None:
            wd.join(timeout=5)
        if self.conn is None:
            return
        with self._lock:
            try:
                if self.conn.in_transaction():
                    self.conn.rollback()
                if not self.conn.invalidated:
                    self.conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.key})
                    self.conn.commit()
            except Exception:
                self.conn.invalidate()  # never return a possibly locked physical connection to the pool
            finally:
                self.conn.close()
                self.conn = None

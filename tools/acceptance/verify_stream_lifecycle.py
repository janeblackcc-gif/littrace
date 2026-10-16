#!/usr/bin/env python3
"""PR4 流生命周期验收（离线，可失败）。

反例 R11 只覆盖一种形态（在**另一个 task** 里 aclose）。本脚本把代码评审要求的场景都跑一遍，
并且**在出现 "Task exception was never retrieved" 时判失败**——不是只看脚本返回值：

  1. 正常跑完（终态 research_complete）
  2. 消费到一半 `break`（不显式关闭）
  3. 同一 task 内 `aclose()`
  4. **另一个 task** 内 `aclose()`（R11 的原始形态）
  5. 消费到一半被 `cancel()`
  6. Agent 中途报错

每个场景都检查四件事：
  - 没有任何未被取回的 task 异常（自定义 loop exception handler 记录）；
  - 所有 Agent task 都已结束（fake agent 自报 active 集合为空）；
  - 调用方 Context 里的 `RUN_STATE.get()` 回到默认（无 token 泄漏）；
  - `state` 里的 `_message_queue` 已清理。

用法：
    python tools/acceptance/verify_stream_lifecycle.py
"""
from __future__ import annotations

import asyncio
import gc
import importlib.util
import json
import sys
import types
from contextlib import aclosing
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
APP = BASE / "backend" / "app"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def build_modules():
    """按文件路径加载（与复核探针一致的加载方式），并注册成一个包以支持相对导入。"""
    package = types.ModuleType("_lifecycle_pkg")
    package.__path__ = []
    sys.modules[package.__name__] = package
    integrity = load(package.__name__ + ".research_integrity", APP / "service" / "research_integrity.py")
    sources = load(package.__name__ + ".research_sources", APP / "service" / "research_sources.py")
    runtime = load(package.__name__ + ".research_runtime", APP / "service" / "research_runtime.py")
    return integrity, sources, runtime


class FakeAgents:
    """最小 Agent 替身：按阶段产出运行时需要的最小状态，并自报 task 生命周期。

    用 `started`/`release` 两个 Event 做**屏障**，
    让「取消 / 跨 task 关闭」发生在 Agent **确实正在运行**时，而不是在一个固定 sleep 之后。
    """

    def __init__(self, integrity, fail_at: str | None = None, block: bool = True):
        self.R = integrity
        self.active: set[str] = set()
        self.started: list[str] = []
        self.fail_at = fail_at
        self.block = block
        self.started_event = asyncio.Event()
        self.release_event = asyncio.Event()

    def _agent(self, name: str):
        outer = self

        class Agent:
            def __init__(self):
                self.name = name

            async def process(self, state):
                outer.active.add(name)
                outer.started.append(name)
                queue = state.get("_message_queue")
                if queue is not None:
                    queue.put_nowait({"type": "thought", "agent": name, "content": f"{name} working"})
                outer.started_event.set()
                try:
                    if outer.block:
                        # 阻塞直到测试显式释放；这样「此时 Agent 在运行」是可断言的事实
                        await outer.release_event.wait()
                    if outer.fail_at == name:
                        raise RuntimeError(f"injected failure in {name}")
                    if name == "architect":
                        state["outline"] = [{"id": "sec1", "title": "Research", "status": "pending"}]
                    elif name == "writer":
                        state["final_report"] = (state.get("final_report") or "") + " FULL-REPORT-END"
                    elif name == "critic":
                        state.update(review_complete=True, review_verdict="pass", quality_score=9,
                                     unresolved_issues=0, review_route="revise",
                                     reviewed_report_hash=outer.R.report_hash(state["final_report"]))
                        for h in state.get("hypotheses", []):
                            if h.get("id") == "H-synthetic":
                                h["status"] = "refuted_in_scope"
                    await asyncio.sleep(0)
                    return state
                finally:
                    outer.active.discard(name)

        return Agent()

    def build(self) -> dict:
        return {n: self._agent(n) for n in
                ("architect", "scout", "data_analyst", "wizard", "writer", "critic")}


def base_state(integrity, next_stage: str = "write") -> dict:
    report = ""
    return {
        "schema_version": integrity.STATE_VERSION, "pipeline_version": integrity.PIPELINE_VERSION,
        "evidence_semantics_version": integrity.EVIDENCE_SEMANTICS_VERSION,
        "session_id": "s", "user_id": "u", "next_stage": next_stage,
        "completed_stages": ["plan"] if next_stage in {"research", "analyze"} else ["plan", "research", "analyze"],
        "stage_history": [],
        "research_config": {"quality_threshold": 6}, "run_status": "running",
        "quality_status": "not_reviewed", "report_hash": "", "reviewed_report_hash": "",
        "review_complete": False, "review_verdict": "not_reviewed", "review_route": "revise",
        "review_coverage": {}, "review_history": [], "evidence_links": [], "evidence_audit": [],
        "llm_calls": [], "usage": {}, "direction_request": None, "resolved_direction": None,
        "research_plan": None, "run_models": {}, "run_flags": {}, "run_config_hash": "",
        "claim_assessments": [], "query": "q", "phase": "writing", "iteration": 0, "max_iterations": 0,
        "search_web": True, "search_local": False, "outline": [{"id": "sec1", "title": "Research",
                                                                "status": "researched"}],
        "mind_map": {}, "key_entities": [], "research_questions": [],
        "hypotheses": [{"id": "H-synthetic", "status": "unverified", "synthetic": True}],
        "knowledge_graph": {"nodes": [], "edges": []}, "facts": [], "data_points": [], "raw_sources": [],
        "charts": [], "code_executions": [], "insights": [], "draft_sections": {},
        "final_report": report, "references": [], "critic_feedback": [], "unresolved_issues": 0,
        "quality_score": 0.0, "pending_search_queries": [], "logs": [], "errors": [], "messages": [],
    }


async def scenario(runtime, integrity, kind: str) -> dict:
    """返回该场景的观察结果；所有清理都在本函数内完成。

    名字与覆盖必须一致——
      - `break_then_release`：消费一帧后 break，**不显式 aclose**，用 `shutdown_asyncgens` 走隐式终结；
      - `aclose_same_task` / `aclose_other_task`：在 **Agent 正在运行**（Event 屏障已置位）时关闭；
      - `cancel_while_agent_running`：同样在屏障置位后 cancel，并断言当时确有 Agent 在运行。
    """
    loop = asyncio.get_running_loop()
    unhandled: list[str] = []

    def handler(loop_, context):
        message = str(context.get("message") or "")
        if "never retrieved" in message or context.get("exception") is not None:
            unhandled.append(message + " | " + repr(context.get("exception")))

    previous = loop.get_exception_handler()
    loop.set_exception_handler(handler)
    agents = FakeAgents(integrity, fail_at="scout" if kind == "agent_error" else None,
                        block=kind not in {"normal_end", "agent_error"})
    state = base_state(integrity, next_stage="research" if kind == "agent_error" else "write")
    events: list[dict] = []
    raised: str | None = None
    saved: list[dict] = []
    agent_running_at_action: bool | None = None

    async def save(snapshot, ui):
        saved.append(snapshot)

    async def cancelled():
        return False

    try:
        if kind == "normal_end":
            agents.block = False
            stream = runtime.execute_stages(state, agents.build(), save, cancelled)
            async with aclosing(stream) as closing:
                async for event in closing:
                    events.append(event)
        elif kind == "break_then_release":
            stream = runtime.execute_stages(state, agents.build(), save, cancelled)
            async for event in stream:
                events.append(event)
                break                      # 故意不调用 aclose：交给 async generator 的隐式终结
            agents.release_event.set()
            del stream
            gc.collect()
            await asyncio.sleep(0)
            await loop.shutdown_asyncgens()
        elif kind in {"aclose_same_task", "aclose_other_task"}:
            stream = runtime.execute_stages(state, agents.build(), save, cancelled)
            # 拉到 Agent **确实开始运行**为止（首个 phase 事件之后 Agent 才会被创建），
            # 否则「关闭时是否有 Agent 在跑」根本不可断言。
            while not agents.started and len(events) < 10:
                try:
                    events.append(await asyncio.wait_for(anext(stream), timeout=5))
                except StopAsyncIteration:
                    break
            agent_running_at_action = bool(agents.active)
            if kind == "aclose_same_task":
                await stream.aclose()
            else:
                await asyncio.create_task(stream.aclose())   # R11 的原始形态
        elif kind == "cancel_while_agent_running":
            async def consume():
                stream = runtime.execute_stages(state, agents.build(), save, cancelled)
                async for event in stream:
                    events.append(event)
            task = asyncio.create_task(consume())
            await asyncio.wait_for(agents.started_event.wait(), timeout=5)
            agent_running_at_action = bool(agents.active)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        elif kind == "agent_error":
            stream = runtime.execute_stages(state, agents.build(), save, cancelled)
            async with aclosing(stream) as closing:
                async for event in closing:
                    events.append(event)
                    if event.get("type") in {"error", "research_cancelled"}:
                        break
        else:
            raise AssertionError(f"unknown scenario {kind}")
    except Exception as exc:  # noqa: BLE001  —— 场景本身允许抛错，如实记录
        raised = f"{type(exc).__name__}: {exc}"
    finally:
        agents.release_event.set()
        await asyncio.sleep(0)
        gc.collect()                       # 触发未被取回异常的 "never retrieved" 上报
        await asyncio.sleep(0)
        loop.set_exception_handler(previous)

    return {"kind": kind, "events": [e.get("type") for e in events], "raised": raised,
            "unhandled": unhandled, "active_agents": sorted(agents.active),
            "agent_running_at_action": agent_running_at_action,
            "run_state_leaked": integrity.RUN_STATE.get() is not None,
            "queue_cleaned": "_message_queue" not in state,
            "run_status": state.get("run_status"), "next_stage": state.get("next_stage"),
            "started": agents.started, "saved": len(saved),
            # 执行身份取证事件必须能按 (run_id, event_seq) 拼接，
            # 终态必须显式标记，且同一执行只能有一个终态。
            "raw_events": events,
            "run_history": state.get("run_history"),
            "last_terminal_event": state.get("last_terminal_event")}


async def run_scenario(runtime, integrity, kind: str, timeout: float = 20.0) -> dict:
    """带超时地跑一个场景：卡住必须表现为**失败**，不能把整个套件挂死。"""
    try:
        return await asyncio.wait_for(scenario(runtime, integrity, kind), timeout=timeout)
    except asyncio.TimeoutError:
        return {"kind": kind, "events": [], "raised": f"TimeoutError: scenario exceeded {timeout}s",
                "unhandled": [], "active_agents": [], "agent_running_at_action": None,
                "run_state_leaked": False, "queue_cleaned": False, "run_status": None,
                "next_stage": None, "started": [], "saved": 0}


async def main() -> int:
    integrity, _sources, runtime = build_modules()
    scenarios = ["normal_end", "break_then_release", "aclose_same_task", "aclose_other_task",
                 "cancel_while_agent_running", "agent_error"]
    print("场景 | 事件 | 未取回异常 | 残留 Agent | 动作时 Agent 在运行 | RUN_STATE 泄漏 | 队列已清理")
    for kind in scenarios:
        result = await run_scenario(runtime, integrity, kind)
        print(f"{kind:28} | {','.join(result['events'])[:40]:40} | {result['unhandled']} | "
              f"{result['active_agents']} | {result['agent_running_at_action']} | "
              f"{result['run_state_leaked']} | {result['queue_cleaned']}", flush=True)
        check(f"{kind}：未超时（场景在 {20.0}s 内结束）", not str(result.get("raised") or "").startswith("TimeoutError"),
              str(result.get("raised")))
        check(f"{kind}：无未取回的 task 异常", not result["unhandled"], json.dumps(result["unhandled"]))
        check(f"{kind}：所有 Agent task 已结束", not result["active_agents"], str(result["active_agents"]))
        check(f"{kind}：RUN_STATE 未泄漏到调用方 Context", result["run_state_leaked"] is False)
        check(f"{kind}：_message_queue 已清理", result["queue_cleaned"] is True)
    # 场景特定断言
    normal = await run_scenario(runtime, integrity, "normal_end")
    check("正常结束：终态为 research_complete 且 run_status=completed",
          normal["events"][-1:] == ["research_complete"] and normal["run_status"] == "completed",
          json.dumps(normal["events"]) + f" run_status={normal['run_status']}")
    for kind in ("aclose_same_task", "aclose_other_task", "cancel_while_agent_running"):
        outcome = await run_scenario(runtime, integrity, kind)
        check(f"{kind}：动作发生时确有 Agent 正在运行（屏障保证）",
              outcome["agent_running_at_action"] is True, str(outcome["agent_running_at_action"]))
    other = await run_scenario(runtime, integrity, "aclose_other_task")
    check("跨 task 关闭：不抛异常（R11 的原始形态）", other["raised"] is None, str(other["raised"]))
    err = await run_scenario(runtime, integrity, "agent_error")
    check("Agent 报错：产出 error 事件且 run_status=failed，不静默继续",
          "error" in err["events"] and err["run_status"] == "failed",
          json.dumps(err["events"]) + f" run_status={err['run_status']}")
    cancel = await run_scenario(runtime, integrity, "cancel_while_agent_running")
    check("取消：不遗留未取回异常，也不留下运行中的 Agent",
          not cancel["unhandled"] and not cancel["active_agents"], json.dumps(cancel["unhandled"]))

    # ---------- 执行身份与终态----------
    # 真实联调曾出现「同一 session 既有 error 又有 completed」的两种观测；要让「是不是同一次执行」
    # 直接可判，事件必须带 run_id + 单调 event_seq + terminal 标记，且每次执行只允许一个终态。
    for kind in ("normal_end", "agent_error", "cancel_while_agent_running"):
        res = await run_scenario(runtime, integrity, kind)
        raw = res["raw_events"]
        ids = {e.get("run_id") for e in raw}
        seqs = [e.get("event_seq") for e in raw]
        terminals = [e for e in raw if e.get("terminal")]
        check(f"{kind}：事件都带同一个 run_id 且 event_seq 严格递增",
              bool(raw) and None not in ids and len(ids) == 1
              and seqs == list(range(1, len(seqs) + 1)),
              json.dumps({"run_ids": sorted(map(str, ids)), "seqs": seqs[:8]}, ensure_ascii=False))
        check(f"{kind}：恰好一个终态事件，且与 run_history 的终态一致",
              len(terminals) <= 1
              and (res["run_history"] or [{}])[-1].get("terminal_event")
              == (res["last_terminal_event"] or {}).get("type")
              and (not terminals or terminals[0].get("type") == (res["last_terminal_event"] or {}).get("type")),
              json.dumps({"terminals": [t.get("type") for t in terminals],
                          "history": res["run_history"],
                          "last_terminal": res["last_terminal_event"]}, ensure_ascii=False)[:300])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n流生命周期验收: {passed}/{len(RESULTS)} PASS")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

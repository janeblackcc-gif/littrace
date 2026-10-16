#!/usr/bin/env python3
"""D-02 真实 HTTP SSE 客户端断连：Agent 屏障后关闭流并核对清理。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "backend" / "app"))
from dotenv import load_dotenv
load_dotenv(BASE / ".env")
from core.runtime_settings import sanitize_no_proxy
sanitize_no_proxy()
from core.database import engine

API = os.getenv("LITTRACE_BASE", "http://127.0.0.1:8000")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def lock_count(session_id: str) -> int:
    key = int.from_bytes(hashlib.sha256(("research:" + session_id).encode()).digest()[:8],
                         "big", signed=True)
    unsigned = key & ((1 << 64) - 1)
    with engine.connect() as connection:
        return connection.execute(text("""
            SELECT count(*) FROM pg_locks
            WHERE locktype='advisory' AND classid=:hi AND objid=:lo
              AND objsubid=1 AND granted
        """), {"hi": unsigned >> 32, "lo": unsigned & 0xffffffff}).scalar_one()


async def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    session_id = str(uuid.uuid4())
    async with httpx.AsyncClient(base_url=API, timeout=60.0) as client:
        registered = await client.post("/auth/register", json={
            "username": f"d02_disconnect_{suffix}",
            "email": f"d02_disconnect_{suffix}@example.com",
            "password": "D02-Disconnect-Passw0rd!",
        })
        check("断连探针用户注册成功", registered.status_code in {200, 201},
              f"status={registered.status_code}")
        if registered.status_code not in {200, 201}:
            return finish()
        token = registered.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        body = {
            "query": "Agent 记忆粒度的条件性效果",
            "session_id": session_id,
            "search_modes": ["web"],
            "version": "v2",
            "max_iterations": 0,
            "direction": {
                "choice": {"mode": "preset", "preset_id": "agent_memory"},
                "start_year": 2022,
                "end_year": 2025,
            },
        }
        events: list[dict] = []
        run_id = None
        async with client.stream("POST", "/research/stream", json=body,
                                 headers=headers, timeout=None) as response:
            check("真实 SSE 响应建立", response.status_code == 200,
                  f"status={response.status_code}")
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                events.append(event)
                if event.get("type") == "direction_resolved":
                    run_id = event.get("run_id")
                    break
            check("断连前已到达 Architect.direction_resolved 屏障", bool(run_id),
                  f"run_id={run_id}")
            check("断连前没有完成终态",
                  not any(event.get("type") == "research_complete" for event in events))
            # 离开 stream 上下文即关闭真实 HTTP 响应，不调用 cancel API。

        deadline = time.monotonic() + 30
        checkpoint = None
        while time.monotonic() < deadline:
            observed = await client.get(f"/research/checkpoint/{session_id}/full", headers=headers)
            if observed.status_code == 200:
                checkpoint = observed.json()["checkpoint"]
                terminal = checkpoint.get("state_json", {}).get("last_terminal_event", {})
                if checkpoint.get("status") == "paused" and terminal.get("type") == "research_cancelled":
                    break
            await asyncio.sleep(0.25)

    terminal = (checkpoint or {}).get("state_json", {}).get("last_terminal_event", {})
    check("真实客户端断连后 PG checkpoint=paused", (checkpoint or {}).get("status") == "paused",
          f"status={(checkpoint or {}).get('status')}")
    check("真实客户端断连记录 research_cancelled 且 run_id 一致",
          terminal.get("type") == "research_cancelled" and terminal.get("run_id") == run_id,
          json.dumps(terminal, ensure_ascii=False))
    locks = lock_count(session_id)
    check("真实客户端断连后 advisory lock 已释放", locks == 0, f"granted={locks}")
    print("  [EVIDENCE] " + json.dumps({
        "session_id": session_id,
        "run_id": run_id,
        "events_before_disconnect": events,
        "db_status": (checkpoint or {}).get("status"),
        "last_terminal_event": terminal,
        "granted_advisory_locks": locks,
        "cancel_api_called": False,
    }, ensure_ascii=False, sort_keys=True))
    return finish()


def finish() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nD-02 真实 SSE 断连: {passed}/{total} PASS")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

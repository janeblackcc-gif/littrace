#!/usr/bin/env python3
"""把一次浏览器运行捕获里的**传输标记**从业务事件数组中拆出（可复现的证据重序列化）。

为什么需要：早期版本的 `ui_j_real_vertical_run.mjs` 把 `data: [DONE]` 这类**非 JSON 传输标记**
也放进了 `events` 数组（记为 `{type: "unparsable", raw: ...}`）。这会让「事件序号 1..N 连续」
这类断言面对一个没有 `event_seq` 的条目。脚本已修，本工具用于把**已捕获的旧文件**按同一规则
重新序列化：业务事件保持**逐字节不变**（仍带完整载荷），传输标记移到 `transport_trailers`。

不改动任何业务事件的字段，也不删除任何字节：`transport_trailers` 里保留原始行。
用法：
    python tools/acceptance/normalize_run_capture.py --in <capture.json> [--out <capture.json>]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", default=None)
    args = ap.parse_args()
    src = Path(args.src)
    dst = Path(args.dst) if args.dst else src
    payload = json.loads(src.read_text(encoding="utf-8"))
    events = payload.get("events") or []
    business = [e for e in events if isinstance(e, dict) and "event_seq" in e]
    extras = [e for e in events if not (isinstance(e, dict) and "event_seq" in e)]
    trailers = list(payload.get("transport_trailers") or [])
    for item in extras:
        raw = item.get("raw") if isinstance(item, dict) else None
        if raw:
            trailers.append(raw)
    payload["events"] = business
    payload["transport_trailers"] = trailers
    payload["data_event_count"] = len(business)
    payload["unparsable_count"] = len(payload["transport_trailers"])
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"file": str(dst), "business_events": len(business),
                      "transport_trailers": payload["transport_trailers"],
                      "seq_continuous": [e.get("event_seq") for e in business] == list(range(1, len(business) + 1)),
                      "single_run": len({e.get("run_id") for e in business}) == 1,
                      "terminals": sum(1 for e in business if e.get("terminal"))},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

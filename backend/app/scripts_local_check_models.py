"""校验 2026-09-20 迁移后的三个模型是否真的可用、且与本项目的调用方式兼容。

为什么单独写：模型 ID 从官方文档查证过，但
  1) OpenAI 兼容端点是否接受 `enable_thinking: false`（非流式）；
  2) 是否接受 `response_format={"type":"json_object"}`（本项目 Agent 强依赖）；
  3) embedding 端点是否接受 `dimensions=1024` 且真的返回 1024 维；
这三点只有真调一次才能确认。**本脚本不依赖 core.database，不会要求 POSTGRES_PASSWORD。**

用法（backend/app 目录下，需已设置 DASHSCOPE_API_KEY）：
    py -3.12 scripts_local_check_models.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openai import OpenAI  # noqa: E402

from core.runtime_settings import sanitize_no_proxy  # noqa: E402

# 必须早于任何 httpx 客户端构造：某些环境的 NO_PROXY 含 "[::1]"，
# httpx 会把它当 URL 解析并抛 InvalidURL（app_main.py 在启动时做同样的事）。
sanitize_no_proxy()

API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
CHAT_MODELS = ["deepseek-v4.1-flash", "qwen3.7-flash"]
EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3.7-text-embedding-flash")
EMBED_DIMS = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def main() -> int:
    if not API_KEY:
        print("缺少 DASHSCOPE_API_KEY，无法校验。")
        return 2
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    print(f"base_url = {BASE_URL}\n")

    for model in CHAT_MODELS:
        # 1) 最简调用：模型是否存在、Key 是否与地域匹配
        try:
            r = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "回复一个字：好"}], max_tokens=16)
            record(f"{model} 基础调用", True, f"usage={r.usage}")
        except Exception as exc:  # noqa: BLE001
            record(f"{model} 基础调用", False, f"{type(exc).__name__}: {exc}")
            continue

        # 2) JSON 模式（本项目 Agent 强依赖）
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": '只输出 JSON：{"ok": true}'}],
                response_format={"type": "json_object"}, max_tokens=64)
            record(f"{model} response_format=json_object", True, repr(r.choices[0].message.content)[:60])
        except Exception as exc:  # noqa: BLE001
            record(f"{model} response_format=json_object", False, f"{type(exc).__name__}: {exc}")

        # 3) 开启思考模式（本项目默认开启）：确认参数被接受，并量化思考 token 成本
        body: dict = {"enable_thinking": True}
        if model.startswith("deepseek-v4"):
            body["reasoning_effort"] = os.getenv("LLM_REASONING_EFFORT", "high")
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "简要推理后回复一个字：好"}],
                max_tokens=512, extra_body=body)
            details = getattr(r.usage, "completion_tokens_details", None)
            reasoning = getattr(details, "reasoning_tokens", None) if details else None
            record(f"{model} enable_thinking=true", reasoning is not None and reasoning > 0,
                   f"reasoning_tokens={reasoning}, completion_tokens={r.usage.completion_tokens}")
        except Exception as exc:  # noqa: BLE001
            record(f"{model} enable_thinking=true", False, f"{type(exc).__name__}: {exc}")

        # 4) 关闭思考模式（降级路径，确认兜底开关可用）
        try:
            r = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "回复一个字：好"}],
                max_tokens=16, extra_body={"enable_thinking": False})
            details = getattr(r.usage, "completion_tokens_details", None)
            reasoning = getattr(details, "reasoning_tokens", None) if details else None
            record(f"{model} enable_thinking=false", True, f"reasoning_tokens={reasoning}")
        except Exception as exc:  # noqa: BLE001
            record(f"{model} enable_thinking=false", False, f"{type(exc).__name__}: {exc}")

    # 5) Embedding：维度必须与 Milvus 集合一致
    try:
        r = client.embeddings.create(model=EMBED_MODEL, input="文献调研助手向量维度校验",
                                     dimensions=EMBED_DIMS, encoding_format="float")
        dim = len(r.data[0].embedding)
        record(f"{EMBED_MODEL} dimensions={EMBED_DIMS}", dim == EMBED_DIMS, f"实际维度={dim}")
    except Exception as exc:  # noqa: BLE001
        record(f"{EMBED_MODEL} dimensions={EMBED_DIMS}", False, f"{type(exc).__name__}: {exc}")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS")
    if failed:
        print("失败项：" + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

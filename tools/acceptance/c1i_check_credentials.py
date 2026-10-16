"""C1-I 验收辅助：逐个凭证做最小无害调用，判断「可用 / 已被吊销」。

用途：轮换前跑一次（旧凭证应显示 可用），轮换后再跑一次
（新凭证 可用、旧凭证 已失效），即得到 C1-I 要求的
「新凭证最小无害请求成功 + 旧凭证明确鉴权失败」。

为什么不把它写成「一键轮换」：平台侧的控制台操作属账号持有人，
代码无法代做；且本脚本刻意不读取、不打印任何明文。

判定原则（重要）：
- 只有明确的**鉴权类**失败（401/403/InvalidApiKey/AccessDenied…）才算「已失效」。
- 网络失败、DNS 失败、超时、余额不足**一律不算**吊销证明，会单独标注为「无法判定」。
  这条来自真实服务验收文档的要求：「不能拿 DNS/网络失败当吊销证明」。

用法：
    python tools/acceptance/c1i_check_credentials.py            # 读 .env 当前值
    # 只验某一个凭证，并临时覆盖它的值（用于验证「旧值已失效」）：
    python tools/acceptance/c1i_check_credentials.py --only dashscope --set DASHSCOPE_API_KEY=<旧值>
    python tools/acceptance/c1i_check_credentials.py --only bocha     --set BOCHA_API_KEY=<旧值>
    python tools/acceptance/c1i_check_credentials.py --only docmind \
        --set DOCMIND_ACCESS_KEY_ID=<旧AK> --set DOCMIND_ACCESS_KEY_SECRET=<旧SK>

--set 只在进程内生效，不落盘；输出始终只显示前后各 4 位，不打印明文。
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(BASE, "backend", "app"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(BASE, ".env"))

# 鉴权类失败的判定词（只有命中这些才算「已失效」）
AUTH_FAIL_WORDS = ("invalidapikey", "invalid api key", "invalid_api_key", "unauthorized",
                   "accessdenied", "access denied", "forbidden", "401", "403",
                   "invalidaccesskeyid", "signaturedoesnotmatch", "apikeynotfound",
                   "authentication", "permission")
AUTH_OK_WORDS = ("choices", "data", "usage", "id", "code", "request_id")


def verdict_from_error(exc: Exception) -> tuple[str, str]:
    """把异常归到 已失效 / 无法判定，并说明理由。"""
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        low = (body or "").lower()
        if exc.code in (401, 403) or any(w in low for w in AUTH_FAIL_WORDS):
            return "已失效（鉴权失败）", f"HTTP {exc.code}: {body[:160]}"
        return "无法判定", f"HTTP {exc.code}（非鉴权类）: {body[:160]}"
    low = str(exc).lower()
    if any(w in low for w in AUTH_FAIL_WORDS):
        return "已失效（鉴权失败）", str(exc)[:160]
    return "无法判定（网络/其他，不能作为吊销证明）", f"{type(exc).__name__}: {str(exc)[:140]}"


def http_json(url: str, payload: dict | None, headers: dict, method: str = "POST") -> str:
    import json
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def mask(value: str) -> str:
    """只显示足以区分凭证的前后各 4 位，避免把明文写进任何输出。"""
    if not value:
        return "(空)"
    return f"{value[:4]}…{value[-4:]} (len={len(value)})"


def check_dashscope() -> None:
    key = os.getenv("DASHSCOPE_API_KEY", "")
    print(f"\n[1] DASHSCOPE_API_KEY  {mask(key)}")
    try:
        out = http_json(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            {"model": os.getenv("DASHSCOPE_MODEL", "qwen3.7-flash"),
             "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4},
            {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        print(f"    结论: 可用 — 响应含 {[w for w in AUTH_OK_WORDS if w in out][:3]}")
    except Exception as exc:
        v, why = verdict_from_error(exc)
        print(f"    结论: {v} — {why}")


def check_embedding() -> None:
    """embedding 与 LLM 共用同一个 Key，但走不同端点，必须单独验。

    注意：本机实测百炼 embedding 端点偶发返回
    `{"code":"InternalError","message":"Receive batching backend response failed!"}`
    —— 那是**服务端内部错误，不是鉴权失败**，所以这里允许重试几次，
    避免把一次服务端抖动误判成「凭证有问题」。
    """
    key = os.getenv("DASHSCOPE_API_KEY", "")
    model = os.getenv("EMBEDDING_MODEL", "qwen3.7-text-embedding-flash")
    print(f"\n[2] embedding（同 Key、不同端点）model={model}")
    last = None
    for attempt in range(1, 4):
        try:
            out = http_json(
                "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
                # input 用字符串（非批量），与项目 embedding_service 的单条路径一致
                {"model": model, "input": "ping",
                 "dimensions": int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))},
                {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            print(f"    结论: 可用 — 响应含 {[w for w in AUTH_OK_WORDS if w in out][:3]}（第 {attempt} 次尝试）")
            return
        except Exception as exc:
            last = exc
            v, why = verdict_from_error(exc)
            if v.startswith("已失效"):
                print(f"    结论: {v} — {why}")
                return
            if "InternalError" in why or "batching backend" in why:
                print(f"    （第 {attempt} 次：服务端 InternalError，非鉴权问题，重试）")
                continue
            print(f"    结论: {v} — {why}")
            return
    v, why = verdict_from_error(last) if last else ("无法判定", "")
    print(f"    结论: {v}（重试 3 次后仍为服务端错误，与凭证无关） — {why}")


def check_bocha() -> None:
    key = os.getenv("BOCHA_API_KEY", "")
    print(f"\n[3] BOCHA_API_KEY  {mask(key)}")
    try:
        out = http_json("https://api.bochaai.com/v1/web-search",
                        {"query": "ping", "count": 1},
                        {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        print(f"    结论: 可用 — 响应含 {[w for w in AUTH_OK_WORDS if w in out][:3]}")
    except Exception as exc:
        v, why = verdict_from_error(exc)
        print(f"    结论: {v} — {why}")


def check_docmind() -> None:
    """DocMind 走 RAM AccessKey（AK/SK）签名，与 DashScope Key 完全无关。"""
    ak = os.getenv("DOCMIND_ACCESS_KEY_ID", "")
    sk = os.getenv("DOCMIND_ACCESS_KEY_SECRET", "")
    print(f"\n[4] DOCMIND AccessKey  ID={mask(ak)} Secret={mask(sk)}")
    if not ak or not sk:
        print("    结论: 未配置（跳过）")
        return
    try:
        from alibabacloud_docmind_api20220711.client import Client
        from alibabacloud_docmind_api20220711 import models as m
        from alibabacloud_tea_openapi import models as open_api_models
        from alibabacloud_tea_util import models as util_models
        cfg = open_api_models.Config(access_key_id=ak, access_key_secret=sk)
        cfg.endpoint = "docmind-api.cn-hangzhou.aliyuncs.com"
        client = Client(cfg)
        # 最小无害调用：查询一个不存在的任务 ID，只验证签名与鉴权，不产生解析费用。
        resp = client.query_doc_parser_status_with_options(
            m.QueryDocParserStatusRequest(id="c1i-nonexistent-probe"), util_models.RuntimeOptions())
        code = getattr(getattr(resp, "body", None), "code", None)
        print(f"    结论: 可用（签名通过，业务返回 code={code}）")
    except Exception as exc:
        raw = str(exc)
        low = raw.lower()
        # 阿里云 SDK 把鉴权失败包在异常消息里，需按关键词判定
        if any(w in low for w in ("invalidaccesskeyid", "signaturedoesnotmatch", "accessdenied",
                                  "forbidden", "unauthorized", "401", "403", "denied")):
            print(f"    结论: 已失效（鉴权失败） — {raw[:170]}")
        elif "notexist" in low or "invalidparameter" in low or "invalid id" in low:
            print(f"    结论: 可用（签名通过；仅业务参数不合法） — {raw[:170]}")
        else:
            print(f"    结论: 无法判定（非鉴权类，不能作为吊销证明） — {raw[:170]}")


def check_db() -> None:
    print("\n[5] PostgreSQL 口令（本机 littrace-pg）")
    try:
        from core.database import engine
        with engine.connect() as c:
            print(f"    结论: 可用 — current_database()={c.exec_driver_sql('select current_database()').scalar()}")
    except Exception as exc:
        print(f"    结论: 不可用 — {type(exc).__name__}: {str(exc)[:150]}")


def check_env_residue() -> None:
    """机器上是否还有独立于 .env 的凭证副本（轮换时最容易漏掉这类）。

    刻意只打印「是否存在 + 值长度 + 前后 4 位」，不打印明文。
    """
    print("\n[6] 机器级环境变量残留副本（独立于 .env，轮换时易漏）")
    for name in ("DASHSCOPE_API_KEY", "BOCHA_API_KEY", "DOCMIND_ACCESS_KEY_ID",
                 "DOCMIND_ACCESS_KEY_SECRET", "SERPER_API_KEY", "API_KEY", "JWT_SECRET_KEY"):
        user = machine = None
        if os.name == "nt":
            try:
                import winreg  # noqa: PLC0415
                for root, store in ((winreg.HKEY_CURRENT_USER, "user"),
                                    (winreg.HKEY_LOCAL_MACHINE, "machine")):
                    try:
                        with winreg.OpenKey(root, "Environment") as k:
                            val, _ = winreg.QueryValueEx(k, name)
                            if store == "user":
                                user = val
                            else:
                                machine = val
                    except OSError:
                        pass
            except Exception:
                pass
        marks = []
        if user:
            marks.append(f"User: {mask(user)}")
        if machine:
            marks.append(f"Machine: {mask(machine)}")
        print(f"    {name:<28} {'  '.join(marks) if marks else '（无）'}")


def check_pool_recycle() -> None:
    """连接池回收周期必须**小于**服务端 idle_session_timeout。

    为什么单列一项：littrace-pg 为修 A5-I 的「孤儿 backend 持锁」设了
    idle_session_timeout=600000（600s）。若客户端连接池的 pool_recycle 不比它小，
    服务端会单方面关掉连接，而 pool_pre_ping 只在取用时探测，长跑的研究流程
    （连接在跑 LLM 时长时间空闲）会中途拿到死连接并失败。
    实测踩到过：thinking 轮跑到 11.8 分钟时
    `psycopg2.OperationalError: server closed the connection unexpectedly`。
    这是一条不变式，值得长期锁住。
    """
    print("\n[7] 连接池回收周期 vs 服务端空闲超时")
    try:
        from core.database import engine
        recycle = getattr(engine.pool, "_recycle", None)
        pre_ping = getattr(engine.pool, "_pre_ping", None)
        server_ms = int(os.getenv("POSTGRES_IDLE_TIMEOUT_MS", "600000"))
        print(f"    pool_recycle={recycle}s  pool_pre_ping={pre_ping}  服务端 idle={server_ms/1000:.0f}s")
        if not recycle:
            print("    结论: 风险 — pool_recycle 未设置，服务端空闲超时会单方面关连接")
        elif recycle >= server_ms / 1000:
            print(f"    结论: 风险 — pool_recycle({recycle}s) >= 服务端超时({server_ms/1000:.0f}s)")
        else:
            print("    结论: 正常（客户端先淘汰连接）")
    except Exception as exc:
        print(f"    结论: 无法判定 — {type(exc).__name__}: {str(exc)[:120]}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="C1-I 凭证核查（最小无害调用）")
    p.add_argument("--only",
                   choices=["dashscope", "embedding", "bocha", "docmind", "db", "residue", "pool"],
                   help="只检查其中一项（默认全部）")
    p.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                   help="临时覆盖某个凭证（仅进程内，不落盘，不打印）")
    p.add_argument("--set-file", metavar="PATH",
                   help="从 JSON 文件读 {NAME: VALUE} 并临时覆盖（避免命令行参数里出现明文）")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.set_file:
        import json
        with open(args.set_file, encoding="utf-8") as f:
            overrides = json.load(f)
        for name, value in overrides.items():
            os.environ[name] = value
        print(f"已从文件覆盖 {len(overrides)} 个凭证（值不回显）")
    for item in args.set:
        if "=" not in item:
            raise SystemExit(f"--set 需要 NAME=VALUE 形式: {item[:16]}...")
        name, value = item.split("=", 1)
        os.environ[name.strip()] = value

    print("=" * 72)
    print("C1-I 凭证核查（最小无害调用；不打印任何明文，只显示前后各 4 位）")
    if args.set:
        print(f"已临时覆盖: {', '.join(i.split('=', 1)[0] for i in args.set)}")
    print("=" * 72)
    table = {"dashscope": check_dashscope, "embedding": check_embedding, "bocha": check_bocha,
             "docmind": check_docmind, "db": check_db, "residue": check_env_residue,
             "pool": check_pool_recycle}
    for key, fn in table.items():
        if args.only in (None, key):
            fn()
    print("\n" + "=" * 72)
    print("判定说明：只有明确的鉴权类失败才算「已失效」；")
    print("网络/DNS/超时/余额不足一律标为「无法判定」，不能当作吊销证明。")
    print("=" * 72)

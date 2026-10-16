"""复现运行配置 fail-closed 的六种情形。

只导入 core.runtime_settings（它不 import core.database，因此不建数据库连接），
用显式 env 字典驱动 validate_startup，避免污染当前进程环境。

用法（cwd = backend/app）：
    py -3.12 scripts_local_check_runtime_settings.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.runtime_settings import validate_startup, sanitize_no_proxy  # noqa: E402

VALID = {
    "POSTGRES_PASSWORD": "local-dev-pg-Passw0rd-not-a-placeholder",
    "JWT_SECRET_KEY": "a" * 64,
    "DASHSCOPE_API_KEY": "sk-local-dev-dashscope-0000000000",
    "BOCHA_API_KEY": "local-dev-bocha-0000000000",
    "CORS_ORIGINS": '["http://localhost:5173"]',
}

CASES = [
    ("全空", {}, False),
    ("占位符 changeme", {**VALID, "POSTGRES_PASSWORD": "changeme"}, False),
    ("JWT 短于 32", {**VALID, "JWT_SECRET_KEY": "short"}, False),
    ("缺 BOCHA 但 ENABLE_WEB_SEARCH=true", {k: v for k, v in VALID.items() if k != "BOCHA_API_KEY"}, False),
    ("CORS 通配符", {**VALID, "CORS_ORIGINS": '["*"]'}, False),
    ("全部合法", VALID, True),
    ("REDACTED 也算占位符", {**VALID, "POSTGRES_PASSWORD": "REDACTED"}, False),
    ("非法布尔值", {**VALID, "ENABLE_WEB_SEARCH": "yes"}, False),
    ("显式关闭 web search 后 BOCHA 可不填",
     {k: v for k, v in VALID.items() if k != "BOCHA_API_KEY"} | {"ENABLE_WEB_SEARCH": "false"}, True),
    ("legacy 开启则 API_KEY 必需",
     {**VALID, "ENABLE_LEGACY_FEATURES": "true"}, False),
]


# --- sanitize_no_proxy 回归 ---
# 为什么单独立一组：本机 NO_PROXY 含 "[::1]"，httpx 会把它当 URL 解析并抛
# InvalidURL('Invalid port: ":1]"')。这个坑已在三处独立客户端上重复踩到
# （应用启动钩子、a1i_real_upload.py、diagnose_run_and_evidence.py），
# 而应用的 app_main 会调用 sanitize_no_proxy，独立脚本不会——所以必须有回归用例，
# 保证该函数本身的行为被锁住。
NO_PROXY_CASES = [
    # (名称, 输入, 期望被改动的变量名, 期望结果)
    ("方括号 IPv6 被去掉", {"NO_PROXY": "[::1],127.0.0.1"}, ["NO_PROXY"], "::1,127.0.0.1"),
    ("小写变量名同样处理", {"no_proxy": "[::1]"}, ["no_proxy"], "::1"),
    ("无方括号时保持不变", {"NO_PROXY": "localhost,127.0.0.1"}, [], "localhost,127.0.0.1"),
    ("空条目被丢弃", {"NO_PROXY": "[::1],,  ,localhost"}, ["NO_PROXY"], "::1,localhost"),
    ("未设置时不报错", {}, [], None),
    ("混合方括号与非方括号", {"NO_PROXY": "a.com,[::1],b.com,[2001:db8::1]"},
     ["NO_PROXY"], "a.com,::1,b.com,2001:db8::1"),
]


def run_no_proxy_cases() -> int:
    print("\n--- sanitize_no_proxy 回归（NO_PROXY 含 [::1] 会让 httpx 抛 InvalidURL）---")
    passed = 0
    for name, env, expect_changed, expect_value in NO_PROXY_CASES:
        working = dict(env)
        changed = sanitize_no_proxy(working)
        key = "NO_PROXY" if "NO_PROXY" in env else ("no_proxy" if "no_proxy" in env else None)
        actual = working.get(key) if key else None
        good = (changed == expect_changed) and (actual == expect_value)
        passed += good
        print(f"[{'PASS' if good else 'FAIL'}] {name}")
        print(f"       改动={changed} 结果={actual!r}")
    return passed

if __name__ == "__main__":
    passed = 0
    for name, env, expect_ok in CASES:
        try:
            result = validate_startup(env)
            ok, detail = True, str(result)
        except RuntimeError as exc:
            ok, detail = False, str(exc)
        good = ok == expect_ok
        passed += good
        print(f"[{'PASS' if good else 'FAIL'}] {name}")
        print(f"       期望 {'放行' if expect_ok else '拒绝'} -> 实际 {'放行' if ok else '拒绝'}: {detail}")

    passed += run_no_proxy_cases()
    total = len(CASES) + len(NO_PROXY_CASES)
    print(f"\n{passed}/{total} PASS")
    sys.exit(0 if passed == total else 1)

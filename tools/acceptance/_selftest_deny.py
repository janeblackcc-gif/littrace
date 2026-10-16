"""自测报告包 DENY 正则：确保不放行密钥文件，也不误拦 .env.example 样板。"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("mrb", os.path.join(HERE, "make_report_bundle.py"))
mrb = importlib.util.module_from_spec(spec)
# 阻止其 __main__ 逻辑执行：只用模块级常量
spec.loader.exec_module(mrb)
DENY = mrb.DENY

CASES = [
    (".env", True), (".env.bak", True), (".rotate-old-secrets.json", True),
    (".rotate-old-appkeys.json", True), ("node_modules/playwright/index.js", True),
    ("__pycache__/x.pyc", True), ("_apply_test/foo.py", True),
    ("backend/.venv/Lib/x.py", True), ("frontend/dist/index.js", True),
    # 以下必须**放行**（样板无真实值；报告与脚本要打包）
    (".env.example", False), ("backend/.env.example", False), ("deploy/.env.example", False),
    ("06_关键实现与配置/deploy/.env.example", False),
    ("07_对齐与差异说明/环境事实.md", False),
    ("01_报告/进度报告.md", False),
]

if __name__ == "__main__":
    bad = 0
    for path, expect in CASES:
        got = bool(DENY.search(path))
        ok = got == expect
        bad += 0 if ok else 1
        print(f"  [{'OK ' if ok else 'BAD'}] {path:<52} deny={got!s:<5} 期望={expect}")
    print(f"\nDENY 正则自测: {len(CASES) - bad}/{len(CASES)} PASS")
    sys.exit(1 if bad else 0)

"""为 littrace_dev 建立基础表结构（等价于应用启动时的 Base.metadata.create_all）。

用途：本地集成验收需要空库建表。app_main.py 的 lifespan 也会调 create_all，
这里单独跑一次便于在启动应用前先完成 deploy/001_kb_identity.sql 的迁移步骤。

用法（在 backend/app 目录下，且已设置 POSTGRES_PASSWORD 等环境变量）：
    py -3.12 scripts_local_create_all.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.database import engine, Base  # noqa: E402
import models  # noqa: F401,E402  —— 必须导入以注册全部表

if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    names = sorted(Base.metadata.tables)
    print(f"create_all 完成，共 {len(names)} 张表：")
    for n in names:
        print("  ", n)

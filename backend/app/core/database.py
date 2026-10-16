# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""数据库连接和会话管理"""
from core.runtime_settings import required_secret
from sqlalchemy.engine import URL
import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# 从环境变量获取数据库配置
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = required_secret("POSTGRES_PASSWORD")
POSTGRES_DB = os.getenv("POSTGRES_DB", "industry_assistant")

DATABASE_URL = URL.create("postgresql+psycopg2", username=POSTGRES_USER, password=POSTGRES_PASSWORD, host=POSTGRES_HOST, port=int(POSTGRES_PORT), database=POSTGRES_DB)

# pool_pre_ping 只在**取用连接时**探测一次，但服务端空闲超时会**单方面关掉**连接：
# littrace-pg 为修 A5-I 的「孤儿 backend 持锁」问题设了 idle_session_timeout=600000（10 分钟），
# 于是一轮研究（10–25 分钟，期间连接在跑 LLM 时长时间空闲）会中途拿到已被服务端关闭的
# 连接，表现为 `psycopg2.OperationalError: server closed the connection unexpectedly`
# （实测：thinking 轮跑到 11.8 分钟时失败）。
# pool_recycle 必须**小于服务端空闲超时**，让客户端先一步淘汰连接，两侧才不会打架。
_DEFAULT_RECYCLE = max(30, min(600, int(os.getenv("POSTGRES_IDLE_TIMEOUT_MS", "600000")) // 1000 // 2))
engine = create_engine(DATABASE_URL, pool_pre_ping=True,
                       pool_recycle=int(os.getenv("POSTGRES_POOL_RECYCLE", str(_DEFAULT_RECYCLE))))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """获取数据库会话的依赖函数"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

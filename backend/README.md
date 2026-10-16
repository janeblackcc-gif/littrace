# LitTrace 后端

FastAPI 服务，入口为 `app/app_main.py`。环境变量、依赖服务和启动命令见 [本地运行指南](../docs/QUICKSTART.md)，测试入口见 [验证说明](../docs/VERIFICATION.md)。

| 目录 | 内容 |
| --- | --- |
| `app/router` | HTTP / SSE 接口，`research_router.py` 是研究入口 |
| `app/service/deep_research_v2` | Agent 编排（`graph.py`）、共享状态（`state.py`）和各 Agent 实现 |
| `app/service` | 阶段执行器、证据约束、来源处理、检查点并发控制等 |
| `app/config` | 模型配置（`llm_config.py`）和研究方向档案（`direction_profiles/`） |
| `app/core` | 数据库、Redis、认证与运行配置 |

`docker-compose-base.yml` 可在本地启动 Redis 与 Milvus（含 etcd、MinIO），PostgreSQL 和 Redis 也可以用仓库根目录的 `docker-compose.littrace.yml` 启动。

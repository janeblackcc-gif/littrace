# LitTrace 本地运行指南

以下以全新克隆和 PowerShell 7 为例，所有命令从仓库根目录 `littrace` 执行。前后端分别在独立终端启动。

## 运行依赖

| 依赖 | 用途 |
| --- | --- |
| Python 3.12 | 后端与验证脚本 |
| Node.js 20+、npm | 前端安装与构建 |
| Docker 与 Compose 插件 | PostgreSQL 15、Redis 7 |
| 可访问的 Milvus 2.5.x | 本地文献库的向量存储 |
| 模型服务与 Bocha 凭据 | LLM / Embedding 调用与联网检索 |

`docker-compose.littrace.yml` 只启动 PostgreSQL 与 Redis。Milvus 需单独部署或连接已有实例，并通过 `MILVUS_HOST`、`MILVUS_PORT` 配置。现有向量客户端约束为 `pymilvus>=2.5,<3`，见 [后端依赖](../backend/requirements.txt)。

## 1. 配置环境变量

在全新克隆中执行；已有配置时直接编辑对应文件。

```powershell
$ErrorActionPreference = 'Stop'
Copy-Item -LiteralPath backend/.env.example -Destination backend/.env
Copy-Item -LiteralPath frontend/.env.example -Destination frontend/.env
```

编辑 `backend/.env`：

| 配置 | 填写说明 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 有效的模型服务 API Key |
| `BOCHA_API_KEY` | 开启联网检索时必填 |
| `POSTGRES_PASSWORD` | 自行设置；Compose 与后端读取同一文件 |
| `JWT_SECRET_KEY` | 自行生成至少 32 字符的随机密钥 |
| `MILVUS_HOST` / `MILVUS_PORT` | 实际向量数据库地址 |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` / `MILVUS_VECTOR_DIM` | 模型和向量集合维度须一致 |
| `CORS_ORIGINS` | 保留示例中的 `localhost:5173` 与 `127.0.0.1:5173` 来源 |

保留文献调研默认开关：`ENABLE_DIRECTION_PROFILES=true`、`ENABLE_WEB_SEARCH=true`、`ENABLE_CODE_EXECUTION=false`。DocMind 为可选解析服务，启用后再填写对应凭据。

各 Agent 的模型名称在 [llm_config.py](../backend/app/config/llm_config.py) 中配置，运行前确认账号有对应模型的调用权限。Agent 主流程的自定义兼容端点由 `LLM_BASE_URL` 配置；其他服务使用的 `DASHSCOPE_BASE_URL` 见环境变量示例，两者按实际服务地址填写。

前端 `frontend/.env` 使用示例中的 `http://localhost:8000/` 后端地址。`VITE_*` 会进入浏览器构建产物，只填写前端所需的公开配置。

## 2. 启动 PostgreSQL 与 Redis

```powershell
$ErrorActionPreference = 'Stop'
docker compose --env-file backend/.env -f docker-compose.littrace.yml up -d
if ($LASTEXITCODE -ne 0) { throw '基础服务启动失败' }
docker compose --env-file backend/.env -f docker-compose.littrace.yml ps
if ($LASTEXITCODE -ne 0) { throw '服务状态读取失败' }
```

默认 PostgreSQL 地址为 `127.0.0.1:5433`，数据库／用户为 `littrace_dev` / `littrace`；Redis 地址为 `127.0.0.1:6379`。端口已被占用时，需同时调整 Compose 映射和后端环境配置。

## 3. 启动后端

```powershell
$ErrorActionPreference = 'Stop'
python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw '虚拟环境创建失败' }
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败' }
Push-Location backend
try {
    ..\.venv\Scripts\python.exe app/app_main.py
    if ($LASTEXITCODE -ne 0) { throw '后端启动或运行失败' }
} finally {
    Pop-Location
}
```

后端从 `backend/.env` 读取配置。全新开发数据库在应用启动时创建表；已有数据库升级前应备份并核对 [数据库变更脚本](../deploy/001_kb_identity.sql)，表结构创建不会替代已有表的升级。

启动后访问 `http://localhost:8000/docs` 查看接口文档，`http://localhost:8000/hello` 可以快速确认 API 已经启动。

## 4. 启动前端

另开终端，从仓库根目录执行：

```powershell
$ErrorActionPreference = 'Stop'
Push-Location frontend
try {
    npm install --legacy-peer-deps
    if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }
    npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
    if ($LASTEXITCODE -ne 0) { throw '前端启动或运行失败' }
} finally {
    Pop-Location
}
```

访问 `http://localhost:5173/login`，注册／登录后进入研究工作台。启动命令显式使用 `5173`，以匹配环境变量示例的 CORS 白名单；项目 Vite 配置的默认端口为 `5183`，直接运行 `npm run dev` 时须自行对齐前端端口与 CORS。

## 5. 发起一次研究

1. 选择研究方向并填写年份范围，也可显式选择自定义方向。
2. 输入具体研究问题，选择网络检索或本地检索。
3. 使用本地检索时，先创建当前用户的知识库、上传材料并等待处理完成，再选择该知识库。
4. 提交后查看研究计划、检索材料、阶段进度和报告。
5. 按需取消任务或从检查点继续；恢复时保持模型、关键开关与知识库绑定一致。
6. 阅读报告的来源和待复核提示，回查关键结论所引用的原文。

模型和搜索调用会使用所配置服务的额度。研究接口的请求 Schema、认证方式及参数说明以运行中的 `/docs` 为准。

## 验证与排障

以下离线脚本通过统一入口禁止网络和数据库访问，使用测试数据核对确定性规则，可在后端依赖安装完成后运行：

```powershell
$ErrorActionPreference = 'Stop'
.\.venv\Scripts\python.exe tools/acceptance/run_offline_acceptance.py tools/acceptance/verify_integrity_semantics.py
if ($LASTEXITCODE -ne 0) { throw '状态与证据约束验证失败' }
.\.venv\Scripts\python.exe tools/acceptance/run_offline_acceptance.py tools/acceptance/verify_direction_enforcement.py
if ($LASTEXITCODE -ne 0) { throw '研究方向约束验证失败' }
```

完整的离线回归列表，以及连接真实服务的验收脚本，见 [验证说明](VERIFICATION.md)，工具入口位于 [tools/acceptance](../tools/acceptance)。

| 现象 | 检查方向 |
| --- | --- |
| 启动时报缺少密钥或配置 | 确认从 `backend` 目录启动，且 `backend/.env` 已填写有效配置 |
| 前端请求被 CORS 拦截 | 核对浏览器实际来源与 `CORS_ORIGINS` 的主机、协议和端口 |
| 未选择方向时请求被拒绝 | 在界面选择预设方向或显式选择自定义方向 |
| 本地检索提示知识库不可用 | 检查 Milvus 连通性、库归属、集合绑定和文档处理状态 |
| 恢复时返回 409 | 核对任务终态、运行配置、状态版本、知识库变化及同会话并发执行 |
| SSE 已结束但报告仍需复核 | 分别查看执行状态和内容质量状态，按报告提示核对证据 |

停止基础服务时使用 `docker compose --env-file backend/.env -f docker-compose.littrace.yml stop`，保留已有数据卷。

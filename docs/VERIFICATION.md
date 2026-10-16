# 验证说明

验证脚本都在 [tools/acceptance](../tools/acceptance)，分为两类：不依赖任何外部服务的离线回归，以及需要真实 PostgreSQL、模型服务或浏览器的验收脚本。

## 离线回归

`run_offline_acceptance.py` 会先注入只用于导入的假配置，再拦截 SQLAlchemy 连接以及 `requests`、`httpx` 的发送方法，保证下列测试完全离线、结果可复现。六个脚本合计 214 项检查，在 [GitHub Actions](../.github/workflows/ci.yml) 上随每次推送运行。

| 脚本 | 覆盖内容 | 断言数 |
| --- | --- | --- |
| `verify_integrity_semantics.py` | 证据入证门、来源版本与日期绑定、条件化论断、质量状态归约 | 104 |
| `verify_direction_enforcement.py` | 研究方向档案校验、未知方向 ID、非法时间窗口拒绝 | 32 |
| `verify_stream_lifecycle.py` | SSE 流的取消、断连、终态标记和执行身份 | 43 |
| `verify_pr5_pipeline.py` | 证据块生产路径、DataAnalyst 实验记录化，无可比数据时不编造图表 | 23 |
| `verify_eval_tool.py` | 人工评测工具链的严格检查与基线样本 | 8 |
| `verify_label_updates.py` | 人工标签导入先校验后写入，并保留修订记录 | 4 个用例 |

本地运行（PowerShell）：

```powershell
$ErrorActionPreference = 'Stop'
foreach ($suite in 'verify_integrity_semantics', 'verify_direction_enforcement', 'verify_stream_lifecycle',
                   'verify_pr5_pipeline', 'verify_eval_tool', 'verify_label_updates') {
    python tools/acceptance/run_offline_acceptance.py "tools/acceptance/$suite.py"
    if ($LASTEXITCODE -ne 0) { throw "$suite 失败" }
}
```

## 真实服务验收

这些脚本需要按 [本地运行指南](QUICKSTART.md) 准备好对应服务，部分会调用模型或搜索接口并产生费用。

| 脚本 | 验证内容 | 依赖 |
| --- | --- | --- |
| `verify_pg_lifecycle_and_failures.py` | 阶段恢复、同会话并发拒绝、三种故障注入 | PostgreSQL |
| `verify_guard_keepalive.py`、`verify_guard_keepalive_controlled.py`、`verify_a5i_after_keepalive.py` | 检查点锁连接保活，空闲超时后的恢复与防重入 | PostgreSQL |
| `verify_real_stream_disconnect.py` | 真实 HTTP SSE 客户端断连后的清理 | 后端服务 |
| `verify_kb_delete_visibility.py` | 知识库文档的权限边界，删除后不可检索 | 后端、PostgreSQL、Milvus |
| `verify_jwt_rotation.py` | JWT 密钥轮换后旧 token 失效 | 后端服务 |
| `verify_direction_lineage.py` | 研究方向从请求到检查点冻结与恢复的完整链路 | PostgreSQL 与模型服务 |
| `verify_custom_derivation.py`、`verify_judge_protocol_positive.py`、`verify_adopted_edge_contract.py` | 自定义方向推导、Judge 标注协议检测、证据边合同（需传入导出的运行包） | 运行导出包 |
| `ui_i_browser.mjs`、`ui_j_real_vertical_run.mjs`、`verify_ui_cancel.mjs` | 浏览器端真实交互、纵向联调和取消 | Playwright、前后端服务 |

运行产物可以用 `export_run_bundle.py` 从检查点导出为 [docs/examples](examples) 中的结构，再用 `diagnose_run_and_evidence.py` 检查证据结构。

# 开发与文档维护

状态：`Implemented`（工作入口）。核对日期：2026-11-11。

从产品根目录按 [AGENTS.md](../../AGENTS.md) 进入。先读[架构总览](../architecture/overview.md)和相关模块，
再查实际代码与测试。当前代码决定现有行为；新的需求和提案另行记录，不混入使用说明。

## 本地环境

```powershell
uv sync --locked
uv run jdagent --version
uv run jdagent --provider fake --output json "离线测试"
```

开发依赖由 [pyproject.toml](../../pyproject.toml) 的 dev 组管理，解释器版本见 [.python-version](../../.python-version)。
RAG 可选依赖用 `uv sync --locked --extra rag` 安装；基础环境和可选服务的验证分别记录。
既有 `.venv` 已完整同步时，可用 `.\.venv\Scripts\python.exe -m pytest` 运行测试，无需联网同步。

## 根据变更选择检查

| 变更 | 优先检查 |
| --- | --- |
| README、导航和规则 | 相对链接／锚点、示例参数，`tests/test_agent_entrypoints.py` |
| 配置或 CLI | `test_configuration.py`、`test_cli_entrypoint.py`、`test_interactive_cli.py`及相关 v05/v06/v07 用例 |
| 模型或 Runner | `test_agent_loop.py`、`test_langchain_model.py`、`test_v06_composition.py`、`test_v06_graph_runner.py` |
| 文件与命令 | `test_v04_files.py`、`test_v04_commands.py`、`test_v04_contracts.py`；使用临时工作区 |
| 上下文／记忆／恢复 | 对应 `test_v05_*.py`，同时检查两种 Runner 的 `test_v06_*.py` |
| MCP | 对应 `test_v07_*.py`；目录、发送／保存、取消、配额和重启边界按变更选择 |
| 知识库 | 摄取、来源 revision、检索、引用与 `test_v03_eval.py` |

代码变更的常规静态检查和完整离线入口：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q -m "not integration"
```

仅文档变更使用相称检查，不为重复描述写新测试。历史完整套件有 RAG Recall 失败，见
[验证说明](../modules/verification.md)；若本次遇到同项失败，仍应报告本次结果，不能静默排除。
真实模型、Embedding、Milvus 与规模实验有独立开关和环境要求；先读取相应测试与历史实验范围，
按当前用户授权执行。离线测试通过不能替代 Live 或人工学习出口。

## 更新哪一份文档

| 内容变化 | 维护位置 |
| --- | --- |
| 用户可用能力或启动方式 | README 和对应 guides |
| 当前调用链、状态归属或限制 | architecture／modules，附代码与测试入口 |
| 配置字段或默认值 | reference/configuration.md 和必要的使用示例 |
| 新实验的实际结果 | 带日期与 revision 的验证记录，再更新 verification／current 摘要 |
| 历史设计取舍 | decisions 或 archive；保留当时审批与失败结论 |

架构和模块文档写明核对日期与代码基准。归档时列出迁移原因、原路径、目标和当前替代入口，
修复文件链接及锚点。历史报告中的固定文件指纹只能在原 revision 下核验，不能重算后冒充原评审快照。
根入口保持简短，Map 只做导航，current 只保留可运行内容与未决事项。

# 2026-11-11 文档现状核对

状态：`Implemented`（文档核对记录）。代码基准：`e23604fcc4660fa393d65f07215d6ac220532e2f`，分支 `main`。

本轮按用户要求以代码为唯一产品行为事实来源，归档旧方案并重建当前文档入口。
归档清单与原始指纹见[本次归档](../archive/2026-11-11/README.md)。源代码、依赖和测试未修改。

## 已核对的关键事实

- 配置和组合根：默认 native/native，包版本 0.5.0；两种 Runner、两个 DeepSeek 适配器均已接线。
- 工具组成：本地八工具、四个历史／记忆工具及 MCP 控制和动态工具。
- 持久化：Session schema 4、sessions-v5；Headless JSON v2；图引擎未启用持久 checkpoint。
- 配置范围：项目不能选择 Provider／后端；memory 表整体覆盖；data-dir 不是所有数据根目录开关。
- 知识管理：默认 HashEmbedding／本地索引；普通对话默认不加载该本地索引；connection test 不连接远端。
- MCP：目录与定义缓存分开，发送前预留、原文持久化、顺序 Task 与显式管理；外部来源贯穿压缩和恢复。

## 按代码修正的使用说明

- 知识库教程原先使用中文 connection／kb 名，实际返回 `name must be a stable identifier`。
  已改为 `tutorial-local` 和 `tutorial-docs`，补充名称字符限制，随后完整演练成功。
- 首次对话前发现 MCP 目录也会创建空 Session，不能再写成“只有发消息才建会话”。
- 正常组合根有八个本地工具、四个历史／记忆工具以及 MCP 动态入口，不能把八工具当作全部工具。
- 普通 CLI 的次数配置没有按 TOML 生效：`host.run_cli()` 传 RuntimeOptions，
  `build_runtime()` 将其保存为 runtime_limits，`ConfiguredLoopFactory.prepare_turn()` 优先使用它。
  隔离 Fake 探针配置 `3 / 5 / 1`，普通 CLI 同样的组装方式得到 `16 / 32 / 2`；
  不传 RuntimeOptions 的直接组合根得到 `3 / 5 / 1`。文档记录此限制，本轮没有修改代码。

## 验证范围

在现有 Windows / Python 3.11.15 / 已安装 .venv 上验证，使用隔离的配置、数据和临时目录，
移除真实模型与外部追踪环境设置。只使用 Fake、离线索引和测试替身。

| 检查 | 结果 |
| --- | --- |
| 既有入口、CLI、配置、数据路径、四组合、MCP 配置／CLI 和知识管理测试 | **105 passed in 23.29s** |
| `--version`、主帮助、knowledge 帮助、source add 帮助 | 4 条命令正常退出 |
| Fake native／langgraph 的 JSON 单次执行和原会话恢复 | 4 条命令正常退出，回答和会话 ID 核验通过 |
| 本地连接、建库、Markdown 摄取、状态、来源、绑定与绑定列表 | 7 条命令正常退出，返回 ID 关联核验通过 |
| 使用／MCP／记忆文档中的 TOML | 6 个示例通过 tomllib 解析；相关配置行为由上述既有测试覆盖 |
| 调用次数接线探针 | 确认普通 CLI 与直接组合根的差异，结果如上 |
| 归档与历史保护 | 32 份迁移前指纹与基准提交匹配（考虑 Git 换行转换）；历史评审和学习记录仅改链接 |
| Markdown 文件与导航 | 150 个文件、1425 条本地链接，路径与锚点全部有效；归档正文除提示和链接外保留 |
| 变更范围与格式 | `git diff --check` 通过；src、tests、pyproject.toml、uv.lock、.python-version 无变更 |

测试命令（临时目录可以替换为自己的空目录）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q -x --basetemp=tmp/docs-check tests/test_agent_entrypoints.py tests/test_cli_entrypoint.py tests/test_configuration.py tests/test_data_paths.py tests/test_v06_composition.py tests/test_v07_mcp_configuration.py tests/test_knowledge_cli.py tests/test_v07_mcp_cli.py
```

初次验证发现兼容索引的链接文本不符合入口测试，已修复；默认临时目录产生错误且异步测试卡在
Windows asyncio 的 `_fallback_socketpair` / `accept`，尚未进入产品逻辑。隔离临时目录并允许本机回环后，
上述 105 项测试完成。过程中保留真实的失败与修正，不将受阻尝试算作通过。

本轮未重跑完整产品套件、付费模型／Embedding、Milvus Live、真实远端 MCP 服务或人工学习验收，
也不把当前环境验证作为全新机器安装验证。历史版本通过／失败仍见[验证说明](../modules/verification.md)。
核对脚本、临时配置与数据在完成后清理；正式结论保留在本文。

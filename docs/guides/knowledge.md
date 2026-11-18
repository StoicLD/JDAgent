# 知识库操作教程

状态：`Implemented`（已有管理入口说明），更新：2026-11-11；代码基准：`e23604f`。

知识库保存外部材料，长期记忆保存对话形成的陈述，两者配置和用途不同。先完成
[安装步骤](usage.md#安装与更新)。当前摄取支持 TXT、Markdown、CSV；不要把 PDF/DOCX 当作
已支持的直接输入。

## 当前可用范围

CLI 可以管理连接、知识库、工作区绑定、来源和摄取操作。未配置 endpoint 的连接使用本地
索引，默认 Embedding 为离线 HashEmbedding，用来演练管理和摄取流程。
**正常对话 Runtime 默认不读取这个本地索引**，CLI 也尚未完整暴露真实 Embedding/Profile
与凭据配置。下面的导入演练成功，不等于普通对话已能对材料做真实 RAG 问答。
完整缺口见[知识模块](../modules/knowledge.md#已确认限制和后续工作)。

## 查看帮助

在产品仓库根目录运行：

```powershell
uv run jdagent knowledge --help
uv run jdagent knowledge connection --help
uv run jdagent knowledge source add --help
```

全局 `--workspace` 放在 `knowledge` 前。管理命令读取正常配置，但不创建对话模型；下面的
本地管理与摄取演练不需要 DeepSeek Key。索引类型由连接的 endpoint 决定，不用
`--provider fake` 切换知识索引。

## 创建连接、知识库并导入

以下 PowerShell 步骤会在本机知识 Catalog 中创建持久记录。选择专用的已有工作区和一个
UTF-8 Markdown 文件，替换两个路径后执行：

```powershell
$workspacePath = 'E:\Projects\Example'
$sourcePath = 'E:\Documents\policy.md'
$connection = uv run jdagent --workspace $workspacePath knowledge connection add --name 'tutorial-local' | ConvertFrom-Json
$kb = uv run jdagent --workspace $workspacePath knowledge kb create --connection $connection.connection_id --name 'tutorial-docs' | ConvertFrom-Json
uv run jdagent --workspace $workspacePath knowledge source add --kb $kb.knowledge_base_id --file $sourcePath
uv run jdagent --workspace $workspacePath knowledge kb status --id $kb.knowledge_base_id
uv run jdagent --workspace $workspacePath knowledge source list --kb $kb.knowledge_base_id
uv run jdagent --workspace $workspacePath knowledge binding add --connection $connection.connection_id --kb $kb.knowledge_base_id
uv run jdagent --workspace $workspacePath knowledge binding list
```

逐条检查命令退出码和 JSON，前一步失败时不要继续使用空 ID。保留返回的 connection_id、
knowledge_base_id、source_id、binding_id 和 operation_id；关闭终端后变量不会保留，
可用 `connection list`、`kb list`、`source list --kb …`、`binding list` 重新查询。
连接与知识库名称须为 1–128 个 ASCII 字母、数字、点、下划线或连字符，首字符须为字母或数字；
不能使用中文名称或空格。Binding 只授权当前 workspace 使用知识库，不代表检索服务已配置成功。

在该 workspace 的交互界面查看：

```text
/knowledge status
/knowledge bindings
/knowledge sources
```

`status` 显示绑定数量，不是端到端可用性探针。未配置知识的普通问答可能显示 not_configured，
不影响一般模型与工具功能。

## 更新、停用与诊断

以下为参数模板，将占位 ID 换成前面返回的值。`--yes` 表示明确确认替换或删除，不要直接
复制到不熟悉的数据上。

```powershell
uv run jdagent knowledge source replace --kb '实际KB_ID' --file 'E:\Documents\policy.md' --yes
uv run jdagent knowledge source deactivate --kb '实际KB_ID' --id '实际SOURCE_ID'
uv run jdagent knowledge source reactivate --kb '实际KB_ID' --id '实际SOURCE_ID'
uv run jdagent knowledge operation status --id '实际OPERATION_ID'
uv run jdagent knowledge operation retry --id '实际OPERATION_ID'
uv run jdagent knowledge operation reconcile
```

replace 使用文件路径定位来源；deactivate 排除来源的新检索，reactivate 尝试重新启用。
retry 和 reconcile 用于已有操作恢复，但当前崩溃恢复仍有已知缺口，不能假定重试必然补齐。
删除入口有 `source delete --kb … --id … --yes`、`kb delete --id … --yes`，
解绑为 `binding remove --id … --yes`，删除连接为 `connection remove --id … --yes`。
删除的物理清理与幂等恢复尚未完整实现，不能把返回成功解释为所有派生索引均已物理擦除。

## 真实服务与验证

`connection add` 支持 `--endpoint` 和 `--credential-ref`。`connection test --id …` 只检查登记记录、
凭据环境变量是否存在或凭据文件是否存在，不访问远端，也不验证认证、Milvus 或检索可用性。
真实 Embedding/Profile、凭据传递以及 Milvus 兼容性仍有缺口。安装
`uv sync --locked --extra rag` 只增加依赖，不应据此编写“配置完即可生产使用”的操作承诺。

需要研究已有集成实验时，阅读[人工验证记录](../archive/2026-11-11/development/v0.3-rag-manual-verification.md)
和[验证状态](../modules/verification.md)。这些历史测试记录不是当前普通用户已打通的使用链。
Memory 的 `[memory.embedding]` 配置也不会自动替知识库设置 Embedding Profile。

当前步骤的执行核对见[文档维护记录](../development/documentation-refresh-2026-11-11.md)。
本地管理链的验证不代表 Milvus、真实 Embedding 或普通对话检索通过。

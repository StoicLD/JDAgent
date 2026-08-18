# Python 工程规范

状态：`Approved`

批准日期：2026-08-12

## 目标与来源

本规范为 JDAgent 的 Python 3.11+ Agent Core 建立一致、可读、可测试的基线。它参考成熟 Python Agent 项目的实际配置，但不复制其大型治理文件：

- [OpenAI Agents Python `pyproject.toml`](https://github.com/openai/openai-agents-python/blob/main/pyproject.toml)：Ruff 100 列、严格类型检查和 Pytest。
- [PydanticAI `pyproject.toml`](https://github.com/pydantic/pydantic-ai/blob/main/pyproject.toml)：Ruff、Pyright strict、Mypy strict 和严格测试设置。
- [smolagents `pyproject.toml`](https://github.com/huggingface/smolagents/blob/main/pyproject.toml)：精简的 Ruff、导入排序和 Pytest 基线。

项目采用其中适合新工程的最小组合，不追求规则数量。

## 工具基线

首次代码实现前创建可执行配置：

- Python：最低 3.11；具体补丁版本由 `.python-version` 锁定。
- 环境与依赖：`uv`。
- 格式与静态检查：Ruff，行宽 100，目标版本 `py311`，默认双引号。
- 类型检查：Pyright strict。
- 测试：Pytest；异步测试只使用显式批准的插件。

预期验证顺序：

```text
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

本轮不创建 `pyproject.toml`，上述配置在首次实现计划获批后落地。

## 代码组织

- 使用 `src` layout；包名在工程初始化前单独确认。
- Core、Application、Ports 和 Adapters 保持单向依赖，不通过运行时导入绕过边界。
- 模块按稳定职责命名，避免无边界的 `utils.py`、`helpers.py` 或 `common.py`。
- `__init__.py` 只暴露明确公共 API，不执行 I/O、读取环境或注册全局状态。
- 每个概念只有一个数据定义和一个规范化入口，避免 Provider、CLI 和存储各自定义相似类型。

## 类型与数据

- 所有公共接口、端口、Adapter 边界和模块级函数必须具有完整类型注解。
- 使用 `Protocol` 定义依赖反转端口；不因只有一个实现就跳过边界。
- 内部不可变值优先使用 frozen dataclass；外部 JSON 和配置可在依赖获批后使用 Pydantic 校验。
- `Any` 只允许出现在无法避免的第三方边界，必须立即校验并转换为项目领域类型。
- 不使用无类型 `dict` 在模块间传递重要状态；使用显式 dataclass、TypedDict 或带标识联合。
- `None`、空集合、缺失字段和未知枚举值的语义必须显式，不依赖 truthy/falsy 猜测。

## 异步与资源生命周期

- 网络、文件和长时操作不得阻塞事件循环；同步阻塞依赖必须隔离在 Adapter。
- 创建任务的模块负责取消、等待和清理，不产生无人持有的后台 Task。
- 超时、用户取消和 Provider 失败使用不同错误类别，不笼统吞掉 `CancelledError`。
- Context Manager 明确拥有连接、文件和客户端生命周期；Core 不创建 Provider Client。

## 错误处理与日志

- 使用具体异常和稳定错误码，不以字符串匹配作为跨模块控制流。
- 在发生副作用前完成校验；不为无效输入执行部分操作。
- 捕获异常时保留安全的因果链，同时清除秘密和不可信的大块正文。
- 日志使用结构化字段；消息说明发生了什么、位于哪个阶段、下一步如何定位。
- 不记录 API Key、认证 Header、完整 Prompt、未经筛选的文件内容或用户敏感数据。

## 命名、注释与文档

- 模块、变量、函数、类型、Docstring、代码注释和错误标识使用英文。
- 名称表达领域职责，避免 `manager`、`processor`、`data` 等无边界词，除非职责已经由模块限定。
- 注释解释原因、约束和非显然风险，不逐行翻译代码。
- 公共接口 Docstring 说明契约、失败和生命周期；简单内部实现不强制冗余 Docstring。
- 项目设计文档使用中文，精确 API 标识保持英文并使用反引号。

## 测试规范

- 单元测试默认离线、确定性且不依赖真实时钟、随机数或网络。
- Agent Core 使用 `FakeModelPort`、Fake Clock、临时 workspace 和内存或临时 Session Adapter。
- 每项行为覆盖正常路径、关键失败路径和副作用未发生的断言。
- 异常、取消、超时和恢复测试必须验证资源关闭与事件顺序。
- 真实 DeepSeek 调用属于显式 opt-in 集成测试，不进入默认测试集合，也不记录凭据或响应正文。
- 测试名称描述行为和条件，不复述实现函数名。

## 变更纪律

- 新抽象必须对应已批准需求、稳定边界或已验证风险。
- 当相关修改不断增加特殊条件、重复元数据或跨模块分支时，停止补丁并重新审视边界。
- 完成声明必须包含与风险相称的格式、Lint、类型、测试和运行证据。

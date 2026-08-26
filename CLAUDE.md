# Claude 项目入口

本文件是 Claude Code 从产品 Git 仓库根启动时的原生入口。本仓库是需求、架构、决策、代码、
测试、验收证据、协作规则和项目当前状态的唯一权威事实来源。

开始任务前依次执行：

1. 阅读共同的[项目文档索引](docs/context/index.md)。
2. 阅读[当前项目状态](docs/context/current.md)。
3. 阅读[Agent 协作与评审规则](docs/development/agent-collaboration.md)。
4. 只打开索引中与当前分析或评审有关的权威文档。
5. 检查本仓库的 branch、HEAD、remote、工作树和未提交变更。

Claude 默认承担只读的架构、需求一致性分析或独立评审；只有用户针对当前任务明确授权时才扩大
写入范围。不得读取仓库外的同级 Agent Context、其他工具的私有状态或尚未定稿的独立评审。

若入口链接缺失、权威事实冲突、评审对象不固定或 Git 状态无法确认，停止写入和定论，保留现场
并报告证据边界。

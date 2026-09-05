# 当前项目状态

状态：`Implemented`

版本状态：v0.1 `Implemented`；v0.2 `Implemented`（工程与学习门禁全部关闭）；v0.3
`Implemented（工程复核未通过，学习出口待执行）`。P1 核心代码已合入 `0.3.0`；M0 需求/合同/Eval
规格仍为 `Proposed`（实现者不得自批）；Milvus Live 未跑。2026-09-05 对 `ac17c40` 审查后修复七类
问题，最终离线 `197 passed, 1 failed, 5 skipped`；正确计分后的 Recall 未达门槛，来源恢复、删除和
真实 RAG 路径仍有缺口。详见[本轮审查](../reviews/REV-20260905-001-v0.3-implementation-audit.md)。
`5254905` 之后的上一轮修复已提交为 `ac17c40`；本轮新修复仍在工作树，未 commit。

验收状态：v0.1 工程、真实 DeepSeek 与 M10b 人工学习出口全部通过；v0.2 CLI 工程验收通过，
学习门禁于 2026-08-24 由项目所有者按范围裁定关闭。v0.3 曾通过当时的 P1 离线门禁、clean-env 安装、
DeepSeek 流式/Tool Call 与 Gold Answer/Citation Live（每题 3 次中位数）；本轮发现旧 Eval 忽略来源身份，
历史分数不能证明正确来源或语义支持质量，当前工程验收未通过。对
`40b9aee` 的独立实现评审为 `fail`（0 Blocker，High 未关闭），F01–F06 已在 `2b28228`
处置；人工学习出口未执行。

## 当前阶段

v0.1 通用 Agent Runtime 已按批准计划实现并于 2026-08-20 完成最终验收。离线工程门禁、
真实 DeepSeek 流式回答、Tool Call 闭环和 M10b 人工学习出口均已通过。

v0.2 已按批准设计完成 M1–M9：v0.1 薄 CLI 已提升为可安装、可在任意 workspace 日常使用的
持久交互式 CLI。Cursor 计划审查的 F01–F12 已处置；实现后的 Standards/Spec 双轴审查初轮
finding 已全部修复，复核为 0 Blocker、0 High。项目所有者于 2026-08-24 明确认定 CLI/UI 不属于
当前最急切的 Agent 核心学习范围，因此不要求逐项执行原 M10 练习，并验收 v0.2 学习门禁完成。

v0.3 的 Q1–Q59 高层设计访谈已于 2026-08-29 收敛，形成
[RAG 与来源可追踪知识检索高层设计](../architecture/v0.3-knowledge-retrieval.md)、RAG 限定语境
[词汇表](v0.3-rag-glossary.md)、[ADR-0006](../decisions/ADR-0006-milvus-standalone-knowledge-index.md)
和[实施计划 P1/P2 附页](../plans/v0.3-rag-p1-p2-appendix.md)。项目所有者于 2026-08-30 批准上述
设计、决策、默认项和[分阶段实施计划](../plans/v0.3-rag-implementation.md)，并授权实现者在不改变
核心结果的前提下推进 P1 产品代码。M0–M6 的 P1 实现已合入包版本 `0.3.0`；对 `40b9aee` 的
独立实现评审 High 已处置。M0 详细合同仍保持 `Proposed`，学习检查表须由学习者本人完成。

## 已批准事实

- 当前产品 Git 仓库同时承载产品、工程与项目级协作事实，是唯一权威仓库。
- 用户于 2026-08-26 批准[ADR-0005](../decisions/ADR-0005-agent-context-free-startup.md)：Agent
  启动、恢复和正式协作不再读取独立 Context 仓库；旧仓库只作为未处置历史输入保留，外层本地
  工作区只提供到产品原生入口的薄路由。
- 第一阶段建设领域无关的通用 Agent Runtime；第二阶段通过求职领域包进行特化。
- Agent Core 使用 Python 3.11+，不维护第二套 TypeScript Core。
- Agent Core 依赖模型无关的 `ModelPort`；首个真实 Provider Adapter 对接 DeepSeek。
- 项目权威文档使用中文；未来代码标识符、公共 API、Docstring 和错误类型使用英文。
- [v0.1 Agent Runtime 需求](../requirements/v0.1-agent-runtime.md) 已于 2026-08-09 获得用户批准。
- Agent Runtime 架构、核心契约、ModelPort、Python 工程规范和实施计划已于 2026-08-12
  获得用户批准。
- Python import package 与 distribution 均使用 `jdagent`；最低版本为 Python 3.11，当前
  `.python-version` 固定 3.11.15。
- DeepSeek Adapter 默认模型为 `deepseek-v4-flash`，默认 base URL 为
  `https://api.deepseek.com`；v0.1 显式关闭 thinking，避免丢失未纳入事件模型的 reasoning
  内容。
- 2026-08-19 用户批准开发期 DeepSeek Key 回退：显式 `DEEPSEEK_API_KEY`/配置值优先，
  缺省读取工作区 `tmp/keys/deepseek-api-key.txt`。该文件位于产品仓库外，不得进入 Git。
- 2026-08-20 用户批准 [v0.2 CLI 体验需求](../requirements/v0.2-cli-experience.md)、
  [CLI 应用层架构](../architecture/cli-application.md)、
  [ADR-0004](../decisions/ADR-0004-cli-application-layer.md) 与
  [v0.2 实施计划](../plans/v0.2-cli-implementation.md)。
- 2026-08-30 用户批准 v0.3 高层设计、知识检索语境、ADR-0006、P1/P2 默认项和
  [分阶段实施计划](../plans/v0.3-rag-implementation.md)。批准允许从 M0 开始推进，并给予实现者对
  文件布局、内部算法、局部 Interface 和 Commit 拆分的自主裁量；改变核心结果、不变量、事实
  所有权、公开合同、重大风险或 P1/P2 范围时仍须升级裁定。
- v0.2 以交互式 CLI 日常可用性为唯一核心结果；Claude Code 与
  `claude-code-best/claude-code` 仅是非规范性设计参考，不是兼容目标、代码来源或第二事实源。
- v0.2 默认真实 Provider 为 DeepSeek；Fake 保留为显式测试能力。CLI 使用增强型行式 REPL，
  不建设全屏 TUI。
- 一个显式 workspace 可以拥有多个 Session；项目配置可共享，Session、输入历史和 UI 状态
  存入按 workspace 分区的本机用户数据目录。
- Session 级允许规则是可审计、可撤销的 Session 事实；`/exit` 后恢复同一 Session 时继续有效，
  但不得覆盖用户或项目的 `DENY`，也不得泄漏到其他 Session。

## 已实现范围

- 模型无关 `ModelPort`、脚本化 Fake Model 与 DeepSeek SSE Adapter。
- 基于规范事件投影的薄 ContextBuilder 与小型 Agent Loop。
- Tool Registry、JSON Schema 校验、集中式 workspace 路径解析、`calculator`、
  `read_text_file`、`write_text_file`。
- `ALLOW | ASK | DENY` 权限策略、CLI 审批与副作用前检查。
- InMemory 与 append-only JSONL Session、显式 resume、损坏数据 fail closed。
- 同源 Runtime Event 的 Trace 投影、Usage、超时、取消与调用预算。
- CLI Composition Root；Core 不依赖 Provider SDK、文件系统、环境变量或具体 Session Adapter。
- CLI 支持安全结构化 `--show-trace`，以及模型/工具超时和硬上下文限制配置；Trace 不输出
  消息正文、工具参数或文件内容。
- 分层 Configuration Resolver、DeepSeek 默认 Provider、开发 Key 回退和稳定 Headless
  text/json/退出码合同。
- Prompt Toolkit 行式 REPL、Rich 流式 Markdown 与持续状态 footer、九条内建斜杠命令、历史、
  多行输入和 workspace 级 Session Catalog。
- Session 生命周期事实、名称/短 ID 解析、legacy 非破坏导入、物理尾行修复、逻辑未完成 Turn
  分类与副作用不确定时的恢复快照。
- 可取消 Turn 与可恢复主循环；运行中 `Ctrl+C` 返回 idle，空闲 `Ctrl+C` 返回 130，`/exit`/EOF
  正常保留 Session。
- 可审计/可撤销的 Session 文件或目录允许规则；恢复同一 Session 后仍有效，新 Session 不继承，
  且不能放宽用户或项目 `DENY` 上限。
- v0.3 P1：Workspace Binding 的知识库、Catalog/Store/Index 所有权分离、TXT/MD/CSV 解析与
  Parent-Child Chunking、来源 Saga/Lease、离线 File Index 与可选 Milvus Adapter、Turn
  Knowledge Preparation、Schema v2 Session/Headless、Turn 内 Citation 与最多一次 Repair。
- 普通无 Binding 对话输出 `not_configured`，不因缺少 RAG extra 或 Catalog 文件失败；Catalog
  文件不存在时普通 Turn 不会创建它。

## 验证与评审证据

- 2026-08-12：Ruff format/check、Pyright strict 均通过。
- 2026-08-19：默认测试 `61 passed, 2 skipped`；两项跳过均为显式 opt-in 的 DeepSeek
  live integration tests。启用 live 后完整测试 `63 passed`。
- 真实 DeepSeek 流式文本与 Tool Call 闭环均通过；执行时未设置 `DEEPSEEK_API_KEY`，证明
  默认开发 Key 文件回退有效。
- 双轴审查初轮发现取消传播、Composition Root、Trace 细节、resume、finish reason、空
  JSONL 与秘密忽略规则等阻塞问题；全部完成修复并增加回归测试。
- 2026-08-19 补充审查：Standards 为 `pass_with_findings`、0 个硬违规，仅保留开发路径依赖
  源码布局的非阻塞判断项；Spec 为 `pass`、0 findings。
- 完整记录见 [v0.1 实现审查](../reviews/v0.1-implementation-review.md)。
- 2026-08-20：Cursor 对 v0.2 计划 revision `6f34007a4e7bbf66f39c12237ef30c4f51815e5c`
  完成独立审查；F01–F12 的正式结论和前置门禁见
  [v0.2 CLI 独立审查处置](../reviews/REV-20260820-001-v0.2-cli-plan-disposition.md)。
- 2026-08-20：v0.2 最终离线门禁为 `113 passed, 3 skipped`，Ruff format/check、Pyright strict、
  `git diff --check` 通过；三项显式 opt-in DeepSeek live 测试全部通过。
- 真实 PowerShell PTY 验证了九条命令的主路径、Session 恢复、运行中取消后继续使用和 `/exit`；
  最终 `0.2.0` wheel 在源码树外隔离安装后，console script 与 `python -m jdagent` 的 JSON
  Headless 入口均通过。
- v0.2 双轴实现审查的 Standards 与 Spec 复核均为 0 Blocker、0 High；完整发现、修复与残余
  人工环境风险见 [v0.2 实现审查](../reviews/v0.2-implementation-review.md)。
- 2026-08-30：v0.3 默认离线门禁当时为 `162 passed, 5 skipped`，Ruff format/check、Pyright
  strict、`git diff --check` 通过。五项跳过均为显式 opt-in（DeepSeek 流式/Tool Call/取消、
  Gold Answer/Citation、Milvus Live）。
- 同日启用 `JDAGENT_RUN_DEEPSEEK_INTEGRATION=1` 后，`tests/integration/test_deepseek_live.py`
  3 项通过；Gold Answer/Citation 每题 3 次、发布判定用中位数的 Live 测试通过。未挑选最佳一次。
- 同日 `jdagent-0.3.0` wheel 在源码树外隔离 venv 安装：基础 extra 下 `jdagent --version` 与
  `python -m jdagent --version` 均为 `jdagent 0.3.0`，导入 `jdagent` 不加载 `pymilvus`；
  `.[rag]` extra 可导入 `pymilvus`。
- 同日 Cursor 对 revision `40b9aee520e3a980a1b69e2e1e694eb219d6b7e7` 做只读独立实现评审，
  结论 `fail`、0 Blocker、High 未关闭。处置见
  [v0.3 P1 实现独立评审处置](../reviews/REV-20260830-001-v0.3-p1-rag-disposition.md)；
  代码修复为 `2b282284e075a52d63b43f49e262737a988d3299`。该处置不是对修复 SHA 的第二次独立评审。
- 处置后默认离线门禁 `168 passed, 5 skipped`，Ruff format/check、Pyright strict、
  `git diff --check` 通过。未重跑 DeepSeek Live、Gold Answer Live、Milvus Live 或 clean-env
  wheel。
- 本机 `127.0.0.1:19530` 无 Milvus，未运行 `JDAGENT_RUN_RAG_LIVE=1`。M0 合同仍为 `Proposed`。
- 2026-08-30：Cursor 对当时 `main` HEAD `5254905` 的 v0.3 实现做自审（非独立评审），并在工作树
  中修复仍违反已批准架构的缺口：缺失 Connection 记为 `BINDING_INVALID`、PTK 指纹使用冻结
  Profile、Evidence 注入 KB/来源/Version、HTTP embedding `batch_size`、缺失 Generation 为
  `PROVIDER_UNAVAILABLE`、Delete 先墓碑再 GC 无引用对象、Catalog Citation Resolver、Index
  连接关闭。自审后离线门禁 `173 passed, 5 skipped`，Ruff format/check、Pyright strict、
  `git diff --check` 通过。这些修复之后已提交为 `ac17c40`。未重跑 Live 或 clean-env wheel。
- 2026-09-05：以干净 `ac17c404a7378974209fab4683cee98629e549b3` 为基线，先审查后按用户授权修复。
  旧默认套件 `173 passed, 5 skipped`；本轮新增 25 个场景，最终 `197 passed, 1 failed, 5 skipped`，
  Ruff format/check、Pyright strict 与 `git diff --check` 通过。唯一失败为正确按来源计分后的词项
  Recall@10 `0.8125 < 0.85`；同一正确计分器在原 HEAD 上也为 `0.8125`。Hybrid `0.90625`、PTK
  `0.875` 保持一致，Fake Dense 从 `0.78125` 降至 `0.71875`，不能宣称全部指标无下降。未重跑
  Live/安装；Milvus 默认端口不可达。完整发现、证据与残余风险见[本轮审查](../reviews/REV-20260905-001-v0.3-implementation-audit.md)。

## 后续模块治理

- 用户于 2026-08-24 批准[模块交付与学习工作流](../development/module-delivery-workflow.md)：
  AI 可以承担主要代码实现，学习者必须亲自拥有初始模型、方案选择、Trade-off、故障预测、证据
  判断和人工学习出口。
- 每个实质性模块的设计包必须包含需求与非目标、Interface 与状态所有权、失败模型、最小 Eval、
  实施计划和模块专属学习清单；Eval 从 v0.3 起是横向门禁，不单独占用产品版本，也不得后补。
- 用户批准[v0.2 之后的模块学习路线](../plans/post-v0.2-learning-roadmap.md)中的近期顺序：v0.2
  学习门禁关闭后，依次设计 v0.3 RAG 与来源可追踪的知识检索、v0.4 长期记忆生命周期、v0.5
  Context Orchestration 与 Compaction；v0.3 高层设计与实施计划已获批，P1 工程实现已合入
  `0.3.0`，学习出口仍待学习者本人完成。
- v0.3 的 M0 详细需求、合同、Eval 规格和学习清单仍为 `Proposed`；不得把代码合入理解为这些
  设计包已获项目所有者批准。

## v0.1 最终验收

- 用户已于 2026-08-20 完成 [M10b 学习检查表](../learning/v0.1-learning-checklist.md)：闭卷
  模块关系、Tool Call 数据流和 Trace 故障练习均确认完成。
- v0.1 没有剩余验收项。

## v0.2 最终验收

- v0.2 工程、真实 DeepSeek、PowerShell 主循环、安装验证和双轴独立审查全部通过。
- 用户于 2026-08-24 明确将 CLI/UI 排除出当前最优先的 Agent 核心学习范围，并决定按已完成
  v0.2 学习处理；[原 M10 检查表](../learning/v0.2-learning-checklist.md)保留为可选复习材料，
  未勾选项目不代表待办，也不再阻塞后续版本。
- 该裁定只适用于 v0.2，不修改 v0.3 起的模块交付与学习工作流。

## v0.3 工程状态

- P1 核心代码已有实现，历史 Live/安装证据保留；2026-09-05 工程复核为 `fail`，不是已完成验收。
- `40b9aee` 的独立实现评审 High 已在 `2b28228` 处置；修复 SHA 未再做独立复核。
- 对 `5254905` 的自审修复已在 `ac17c40`；本轮七类问题的修复仍在工作树。完整验证属于自测；
  另有只读 Agent 对本轮改动做回归复核，发现并闭环两个追加问题，不代表全部 v0.3 独立验收通过。
- 未关闭：M0 设计包仍为 `Proposed`；Milvus Standalone Live；修复 SHA 的独立复核（可选）；
  人工学习出口。历史延期项 F09/F11/F13/F16/F17 与 `credential_ref` 解析仍未做；本轮又确认恢复、
  物理删除、历史 Citation、真实配置和 Eval 缺口，具体 ID 以[本轮审查](../reviews/REV-20260905-001-v0.3-implementation-audit.md)为准。
- AI 不得勾选 [v0.3 学习检查表](../learning/v0.3-rag-learning-checklist.md)。

## 下一步

1. 按[改进方案](../plans/v0.3-rag-improvement-proposal.md)补齐持久恢复/删除与真实 RAG 路径；
   该方案为 `Proposed`，不自动改变范围或批准迁移。
2. 关闭正确计分后的 Eval 失败，并在可达的真实 Milvus/Embedding 上验证；保留原门槛。
3. 项目所有者处理 M0 合同审批；对最终固定实现执行相称独立复核，而非仅复核历史 `2b28228`。
4. 学习者本人完成 [v0.3 RAG 学习检查表](../learning/v0.3-rag-learning-checklist.md)；AI 不得
   代填。
5. 在人工环境矩阵中继续观察中文 IME、legacy console 与真实窗口强关；强关不承诺执行 finally，
   恢复仍以已批准的 Session 合同为准。

## 恢复入口

- [项目文档索引](index.md)
- [仓库 Agent 地图](../../AGENTS.md)

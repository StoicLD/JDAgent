# 跨会话检索与长期记忆

状态：`Implemented`（实现说明；语义质量与完整验收另行判断）

历史检索读取同一 workspace 的原始 Session，Memory 保存带来源、作用域和版本的独立陈述。
SQLite 保存权威记忆、意图、作业、提取水位与费用；向量是可重建的派生索引。
当前验证状态见[实现记录](../reviews/REV-20261022-001-v0.5-implementation.md)。

## 检索与来源

`search_history` 定位会话与 Turn，`read_history` 分页读取原文。`search_memory` 返回记忆 ID、
版本、来源、状态与作用域；显式查询可包含停用内容。默认只召回当前项目和用户级有效记忆。
workspace 身份按规范绝对路径生成；移动目录或另一个克隆不会自动继承原项目记忆。

配置真实 Embedding 后，关键词与余弦相似度排名融合，向量按模型档案和内容 hash 版本化。
未配置或服务失败时明确显示 `keyword_only`。语义候选需要达到相似度门槛，无相关项不注入。
离线向量夹具只证明过滤与排序机制，不证明真实语义 Recall 达标。

到期内容为 expired；知识版本、Session 来源或文件 revision 变化后，派生内容变为
needs_verification，不作为有效事实召回。最近一条知识回答后的显式记住会继承其引用依赖，
这是保守处理，可能使不相关的后续记忆也需要核实。

## 用户操作

整句直接输入产生一次性宿主意图，绑定 Session、Turn、用户事件、作用域、目标版本和正文：

| 输入 | 行为 |
| --- | --- |
| `记住：项目使用 pytest` | 新增项目记忆 |
| `记为我的通用偏好：回答使用中文` | 新增用户级记忆 |
| `把本项目记忆 M… 更正为：新内容` | 追加更正版本 |
| `忘记本项目记忆 M…` | 追加 disabled 版本，原文保留 |
| `重新启用本项目记忆 M…` | 校验来源后重新启用 |

用户级更正、停用和启用使用“我的通用偏好”。期限可写在记住/更正正文末尾，例如
`[有效至：2026-12-01T00:00:00+08:00]`。含糊、引用或“分析这段文本”中的命令不产生意图。
模型工具参数必须与宿主记录完全相同；重复消费返回原结果，过时版本不能覆盖更正。
明确意图在 ASK 下无需第二次确认，DENY 和 analyze 仍阻止写入。
停用不删除档案；更正停用内容也不会顺便启用。旧版本查询标为 superseded。

## 自动整理与退出

首个成功 Turn 后持久登记一次作业；启动扫描可补上“首轮完成后、登记前”崩溃造成的缺口。
当前会话和其他活跃来源不参与提取。每来源提取结果立即持久化，包括空结果；合并失败重试
复用已付费的阶段成果。发布前复核来源 hash 与租约 epoch，旧 worker 不能覆盖接管结果。

每作业最多 8 次模型请求、15 分钟；来源按有界批次处理，后续批次单独登记。取消不消耗三次
真实失败的重试额度；失败保留错误和退避状态。自动策略只生成项目候选，不写用户级偏好，
不启用已停用内容，不覆盖明确用户更正。无法排除与显式记忆或停用事实冲突时，保留待核实候选。

前台请求优先取得共享模型槽。Headless 先输出并 flush 回答，再开始新整理，默认最多等待
120 秒；退出清理上限 5 秒。可用 `jdagent maintain --pending --workspace <路径>` 续做待办。
该入口不创建新会话。预算和已发生费用跨重启保留。

## 配置示例

项目 `.jdagent/config.toml`：

```toml
[memory]
auto_consolidation = true
headless_drain_seconds = 120
input_token_limit = 10000000
output_token_limit = 1000000
total_input_token_limit = 30000000
total_output_token_limit = 3000000

# 需要语义检索时填写真实服务，密钥使用 JDAGENT_MEMORY_EMBEDDING_API_KEY。
# [memory.embedding]
# base_url = "https://your-provider.example/v1"
# model = "your-embedding-model"
# dimension = 1024
```

## 入口

- [MemoryService](../../src/jdagent/memory_service.py)、[版本与作业存储](../../src/jdagent/memory_store.py)
- [后台整理](../../src/jdagent/maintenance.py)、[共享费用策略](../../src/jdagent/request_accounting.py)
- [用户意图测试](../../tests/test_v05_memory.py)、[后台运行测试](../../tests/test_v05_memory_runtime.py)

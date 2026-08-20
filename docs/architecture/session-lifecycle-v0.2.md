# v0.2 Session 生命周期事实

状态：`Approved`

批准日期：2026-08-20

## 决策

- `session_started` 是首个事实，payload 固定为可选 `name` 与 `workspace_identity`；新建时默认名为
  `session-<id 前 8 位>`。
- `session_renamed` 只追加新名称，不改写历史；名称为 1–80 个可打印字符，不要求唯一。
- 创建时间来自首事件，更新时间来自末事件，最近状态来自末个 `turn_completed/turn_failed`。
- Catalog 只通过 `SessionDiscoveryPort + SessionPort` 重建；名称、workspace 和状态不以文件名或索引
  为事实源。名称、短 ID 有多个匹配时必须报歧义。
- 默认交互启动不恢复最近 Session；第一条普通 Prompt 创建新 Session。`/new` 只切换到新的待创建
  状态，旧 Session 原样保留。
- 规范化 workspace identity 为 `sha256(Path.resolve(strict=True) → normpath → Windows normcase)`；
  manifest 同时记录规范路径与 identity，碰撞或错配 fail closed。
- v0.1 `.jdagent/sessions` 只做校验后的原子复制；源文件永不删除，目标同名不同内容时报冲突。

## 兼容性

Schema 版本继续为 1；旧 `session_started` 缺少新字段时按 `None` 读取，并投影出默认名称。Runtime
使用的 `SessionPort` 保持 append/read 窄接口，发现能力由独立 `SessionDiscoveryPort` 提供。


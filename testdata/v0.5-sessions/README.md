# v0.5 会话兼容样本

状态：`Implemented`（固定测试数据），日期：2026-11-05。

样本由 v0.5 固定提交 `6996e764537c12f27e85a060011ace32d359a5db` 的事件类型和
`event_to_data` 序列化器生成，不使用开发机用户会话。`manifest.json` 记录原源码及样本的 SHA-256；
`.gitattributes` 保持 JSONL 原始字节，避免 Windows 检出换行改变指纹。

| 样本 | 内容与预期 |
| --- | --- |
| complete | 已完成写入与终态；打开后沿用原历史，不重放工具 |
| safe | 只有用户消息的安全中断；先追加中断关闭记录再续做 |
| unknown_started | 安全旧轮之后，新轮写入开始但没有结果；创建恢复快照、父会话不变 |
| unknown_result | 新轮已有结果，但 effect=unknown；仍不能按已完成或未执行处理 |

unknown 样本的安全旧轮包含保护约束，不确定新轮带有显式 gap 标记。测试同时确认安全历史带入、
缺口不冒充正常对话、unknown 元数据保持，以及新后端续做后可以用 native/native 继续打开。
这些是 schema-v4 最小合同样本，不涵盖所有旧压缩对象、知识依赖或真实业务会话规模。

重建时先将上述提交的 `src/jdagent` 用 `git archive` 导出到一个临时目录，再执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 tests/fixtures/generate_v05_sessions.py <导出目录>/src testdata/v0.5-sessions
```

生成器会检查实际导入的序列化模块位于指定源码目录；传入其他源码不构成同一来源证明。
固定时间和事件 ID 使重建字节稳定。测试入口为 `tests/test_v06_legacy_sessions.py`。

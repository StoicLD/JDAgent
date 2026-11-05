# v0.5 压缩会话与对象

状态：`Implemented`（固定兼容数据），日期：2026-11-05。

数据来自固定 v0.5 提交 `6996e764537c12f27e85a060011ace32d359a5db` 的原生 AgentLoop、
ContextService、对象存储和序列化代码。合成历史包含保护约束、读取工具的原始输出、一次已提交
压缩及后续成功终态；模型为本地固定摘要脚本，没有真实调用或用户数据。

`manifest.json` 指明 checkpoint、普通请求归档、11 份会话／对象的字节指纹和关键源码指纹。
对象目录保留原文 segment、摘要、请求和响应对象；文件名即内容哈希，不改换行。
生成器固定事件时间和标识，已在另一目录重建并逐字节比较通过。

先通过 git archive 导出上述提交的 `src/jdagent`，再执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 tests/fixtures/generate_v05_compacted.py <导出目录>/src <目标目录>
```

测试 `test_v06_legacy_compaction.py` 复制样本后进入四组合，再回退原生后端：完整 checkpoint
继续使用旧摘要；checkpoint 缺失或哈希损坏时沿用原恢复策略，使用持久原文，保护约束仍可见，
不重复压缩或执行历史工具。普通请求归档仍能独立重建。单个小型样本不代表全部历史规模或语义质量。

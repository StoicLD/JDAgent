# v0.5 验证证据

状态：`Implemented`（证据归档）

结论与逐项处置见[实现记录](../../REV-20261022-001-v0.5-implementation.md)。
本目录只保存可复核证据，不作为产品运行、恢复或构建依赖。

- `manifest.json`：基准提交、审查快照、JUnit 计数、规模数据、各次 Live 摘要和文件 SHA-256。
- `a`–`j`（不含未送审的 `d`）：冻结 patch、文件指纹与快照说明。A 的逐文件指纹在归档时补记。
- `v05-*.xml.gz`：压缩保存原始 JUnit 字节，解压后读取；保留失败，不用较新的运行覆盖旧运行。
- `storage-*.json`：1,000、10,000、100,000 条事件的逐条持久化与重开测量。
- `live-run*-attempt*.tar.gz`：固定测试项目、Session、Context 对象与费用账本。
  不含凭据、临时锁、编译缓存或独立响应正文日志。产品原文对象保留在其正式所有者目录中。
- `v05-live-budget.json`：全部真实请求的累计 Usage；诊断请求也计费，不因换轮而清零。
- `v05-live-diagnostic-summary.json`：单次追加诊断的来源序号、Usage 与 hash，
  该响应不是第一轮任何一次原响应。

归档时实际逐一将基准 `1729161b062cdffbdd4b30c6cff0ca4baeaec877` 导出到临时目录，
使用 `git apply` 应用对应解压补丁，再核对冻结文件。A/B/C/E/F/G/H/I/J 分别核对
225/230/233/233/234/235/235/235/235 个文件，全部吻合；文本对比统一 CRLF/LF。
磁盘原文件 hash 仍保留原换行格式，不将两种 hash 混用。

JUnit 的 `tests` 包含失败与跳过，成功数量应减去 `failures`、`errors` 和 `skipped`。
JUnit suite 耗时与 pytest 控制台总耗时的统计边界不同，不能把小幅差值当作结果冲突。

归档不意味着 v0.5 全部验收通过；最新固定修复的真实三次复验仍待新增预算。

# 首轮 Live 原始证据

状态：`Implemented`（实验留档），日期：2026-11-05。

固定 revision：`70264313cdd954e9c61c824b29f0a6f672d2af3f`；实验 `v06-comparison-live-002`。
B1 失败，其余五次未运行。共 8 次请求、21,920 输入／1,351 输出 token，全部已结算。
授权、原因和后续处置见[完整记录](../../../v0.6-live-comparison-20261105.md)。

- [raw-evidence.zip](raw-evidence.zip)：51 份原始文件，包括计划、预算、B1 结果与会话、对象和命令日志。
- [manifest.json](manifest.json)：逐文件与压缩包 SHA-256、费用明细、剩余额度及未运行样本状态。

压缩包已重新读取并逐项比对源文件。没有把 stdout 的成功文本等同于运行时成功，也没有用修复后
离线结果覆盖本次真实失败。OS 锁和字节码缓存不进入证据；原实验仍在本地保留。

# 四组合 CLI 验证

状态：`Implemented`（验证记录），日期：2026-11-05。基准 `b0a198e`。

[JUnit XML](cli-matrix.xml.gz)：16 passed，4.46 秒。命令：
`python -X utf8 -m pytest tests/test_v06_cli_matrix.py --tb=short -q`，另指定 basetemp 和 JUnit 文件。
Ruff 与全量 Pyright strict 通过。

每组均经实际 CLI/Host，测试 JSON v2、退出码、失败不重试、重启回退、交互上下文与压缩命令、
独立维护入口和资源借用。用户输入由 ScriptedTerminal 提供，所有 HTTP 由 MockTransport 响应；
配置、文件和数据库均在临时目录，不读取真实凭据，不执行付费请求。覆盖与限制见
[实施记录](../../../../plans/v0.6-implementation.md)。

# 环境追踪隔离验证

状态：`Implemented`（验证记录），日期：2026-11-05。基准 `bdc8b5a`。

[JUnit XML](trace-isolation.xml.gz)：8 passed，2.52 秒。
命令：`python -X utf8 -m pytest tests/test_v06_trace_isolation.py --tb=short -q`，另指定 basetemp 和 JUnit 文件。
Ruff 与全量 Pyright strict 通过。

在环境变量和外层 tracing_context 均启用的条件下，四组合分别执行读工具后成功回答和 HTTP 503
失败；每组只有两次预期的 MockTransport 请求。LangSmith Client 构造、create_run、update_run 和
batch_ingest_runs 入口设置为拒绝，均没有触发。测试使用合成密钥，追踪端点设为本机地址；没有
访问真实模型或追踪服务。这覆盖当前锁定版本及项目调用入口，不承诺未来新增回调可绕过该边界。

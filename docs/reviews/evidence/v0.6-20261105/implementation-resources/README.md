# 自建客户端关闭验证

状态：`Implemented`（验证记录），日期：2026-11-05。基准 `b131570`。

[JUnit XML](owned-resources.xml.gz)：8 passed，2.52 秒。
命令：`python -X utf8 -m pytest tests/test_v06_owned_resources.py --tb=short -q`，另指定 basetemp 和 JUnit 文件。
Ruff 与全量 Pyright strict 通过。

实际组合根和 Adapter 创建客户端；本地替换 send，不访问网络。四组合分别检查正常关闭和维护
关闭抛错时的收尾顺序、异步／同步池关闭、会话锁释放。借用客户端由先前协议／组合测试覆盖。
本测试不延长既有维护退出等待，不证明永久挂起的第三方清理也会完成。

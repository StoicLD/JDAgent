# v0.6 独立环境安装验证

状态：`Implemented`（验证记录），日期：2026-11-05。固定产品版本见[结果 JSON](result.json)的 revision。

在本机 Windows 新建临时虚拟环境，使用 Python 3.11.15，执行：

```powershell
$env:UV_PROJECT_ENVIRONMENT = 'tmp/v06-implementation/clean-install'
uv sync --locked --no-dev --no-editable --cache-dir tmp/uv-cache
```

实际执行在 finally 中恢复原环境变量；uv 安装 59 个包，锁文件保持不变。最初 `--offline` 因本地缺少
pygments 2.20.0 wheel 失败，允许下载公开锁定依赖后成功。没有上传项目文件或调用模型服务。

验证 jdagent 从新环境 `Lib/site-packages` 导入，pytest 不可导入。用该环境 Python 在隔离配置／数据
目录执行 native、langgraph 两种 Fake CLI，两次均返回 exit 0、schema_version=2、completed。
另外以 build_runtime 构造四种组合，注入 MockTransport；核对真实 Adapter 类型，每组一个 HTTP 请求、
completed 结果，退出后借用客户端仍开放，再由调用方关闭。

结果 JSON 保存依赖版本、锁文件原始 SHA-256、组合结果和环境字节数。71,345,824 bytes 是冒烟运行
之后整份临时虚拟环境的文件长度总和，包含缓存；没有测量旧版本环境或稳定启动延迟，不能据此宣称
体积或性能改善。这是同一 Windows 主机上的独立安装证据，不是全新机器验收，也不替代 Live。

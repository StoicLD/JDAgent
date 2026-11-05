# CLI 切片后的完整离线回归

状态：`Implemented`（验证记录），日期：2026-11-05。
固定 revision：`b1315708b3f6058133ff04e364097cf455a7dd6e`。
锁文件 SHA-256：`e3449e6a5b78857761eee420a96f235c13b571df73f3b31ec21a8bb7567ee55a`。

[JUnit XML](full-cli.xml.gz)：**506 passed、1 failed、3 skipped、11 deselected，72.46 秒**。
唯一失败为 `test_offline_child_and_ptk_eval_meet_gates` 的既有 Recall `0.8125 < 0.85`。
3 项跳过是未 opt-in 的存储规模实验；11 项 integration 被显式排除，没有真实模型调用。

执行命令（另指定临时 basetemp 和 JUnit 路径）：

```powershell
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/ruff.exe check .
.venv/Scripts/pyright.exe --pythonpath .venv/Scripts/python.exe
.venv/Scripts/python.exe -X utf8 -m pytest -m 'not integration' --tb=short -q
```

287 个文件格式通过，Lint 和 Pyright strict 通过。异步测试按已确认的 Windows 沙箱 socketpair
限制在获准宿主执行；网络边界仍为 Fake／MockTransport。后来新增的 8 项资源验证单独保存，
未混入本次完整结果。覆盖分配及尚未完成项见[验收映射](../../../../development/v0.6-acceptance-matrix.md)。

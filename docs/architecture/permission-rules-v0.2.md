# v0.2 Session Permission Rule 合同

状态：`Approved`

批准日期：2026-08-20

## 接口与事实

- `ApprovalRequest` 只提供工具名、脱敏参数摘要、风险、call ID、session ID 与规范化 workspace
  相对目标；写入正文以 `<N chars>` 表达，不进入审批展示或审批事件。
- Terminal 返回拒绝、本次允许、Session 文件允许或 Session 目录允许的 `ApprovalChoice`；
  `ScopedApproval` 才能把选择转换为 `ApprovalOutcome` 与最小 `SessionPermissionRule`。
- Rule 固定包含 UUID、session ID、工具名、`file|directory` 与 POSIX 风格相对目标。
- grant 必须在工具副作用开始前写入 `permission_rule_granted`；撤销追加
  `permission_rule_revoked(rule_id)`。活动规则完全从 grant/revoke 事实投影。

## 匹配与生命周期

- 文件规则只匹配同一规范化文件；目录规则只匹配该目录及后代。每次匹配重新通过
  `WorkspacePathResolver`，不得按字符串前缀判断。
- Rule 只对相同 Session 与工具有效，不进入新 Session、其他 workspace 或用户配置。
- 用户/项目写权限上限为 `ASK | DENY`；项目只能收紧。任何 Rule 都不能覆盖 `DENY`。
- Session Rule 在退出 CLI 后仍是 Session 事实；恢复同一 Session 时继续有效，直到
  `/permissions revoke <rule-id>` 追加撤销事实。该语义是 v0.2 验收合同。


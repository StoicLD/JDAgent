# 任务跟踪约定

状态：`Approved`

批准日期：2026-09-20

任务使用 StoicLD/JDAgent 的 GitHub Issues，通过 gh CLI 操作。
需求、架构、ADR 和验收证据仍以产品仓库中的权威文档为准；
Issue 记录任务、讨论与进度，并链接相关文档。

## 操作约定

- 显式指定仓库：所有命令带 `--repo StoicLD/JDAgent`。
- 读取任务：`gh issue view <number> --comments`。
- 查询任务：`gh issue list`，按需指定状态与标签。
- 创建任务：`gh issue create --title "..." --body-file <path>`。
- 更新正文：`gh issue edit <number> --body-file <path>`。
- 添加评论：`gh issue comment <number> --body-file <path>`。
- 关闭任务：`gh issue close <number>`。
- 多行正文写入 UTF-8 临时文件；完成后清理。
- 技能要求“发布到任务跟踪器”时，创建 GitHub Issue；
  要求“读取相关任务”时，读取 Issue 正文、标签与评论。
- gh 不可用、未登录或权限不足时，报告具体原因，不自动切换跟踪器。

## Pull requests as a triage surface

**PRs as a request surface: no.**

# 从 Teams 的 OneDrive 同步文件夹抓课件

Teams 文件在 OneDrive 客户端完成本地同步后，Moodle-killer 可读取该本地目录，把课程文件增量复制到 Knowledge，并沿用现有的 `[新文件]`、校验和推送流程。无需 Teams 自动化账号。

## 给用户的首次设置指引

Agent 在首次配置 Teams 文件来源时，先把下面的步骤告诉用户，并等用户操作完成：

1. 安装并登录电脑上的 **OneDrive 客户端**，使用能访问该 Teams 团队文件的学校账号。
2. 打开 Teams，进入目标团队/频道的**文件**（有些界面显示为**共享**），找到想复制的文件夹。
3. 点击该文件夹旁的 **⋯（更多）** → **向 OneDrive 添加快捷方式**。
4. 在弹出的窗口里选择 **我的文件**，确认添加。
5. 等 OneDrive 在电脑上显示这个文件夹，打开其中一个文件，确认内容能读取；然后告诉 Agent「已同步」，有本地路径的话一并提供。

**Agent 的停点**：用户没完成以上操作时，不创建 `teams_sources.json`，也不启动正式扫描。用户回复完成后，先在本机找到 OneDrive 下的对应文件夹，确认目录存在且至少一个目标文件可读取，再与用户确认要放入的 Knowledge 课程目录、筛选要复制的子文件夹，接着配置来源并做 `--dry-run`。如果用户已明确完成这些步骤且本机可验证，就直接继续，不重复要求操作。界面名称随 Teams/OneDrive 版本可能略有不同，可按“更多 → 添加 OneDrive 快捷方式 → 我的文件”寻找。

## Agent 配置本地来源

在 `~/.moodle-killer/teams_sources.json` 配置来源。此文件只存在用户电脑，不放进仓库：

```json
{
  "mat201_202609": {
    "name": "MAT201 Mathematical Analysis I",
    "source": "/Users/you/Library/CloudStorage/OneDrive-school/MAT201 Mathematical Analysis I 202609 - General",
    "path": "/Users/you/Documents/Knowledge/MAT201 Mathematical Analysis 1",
    "folders": {
      "01 Class Materials": "Class Materials",
      "02 General": "Course Information",
      "03 Assignments": "Assignments",
      "04 Projects and Presentation": "Projects and Presentation",
      "05 Test": "Test",
      "06 Final Exam": "Final Exam",
      "07 Textbook": "Textbook"
    }
  }
}
```

`folders` 只列要抓的顶层目录；未列入的 `Student Folders` 等目录不会复制。省略 `folders` 时使用上面这套默认映射。已有的 `Textbook` 目录会复用。空目录不会创建；Teams/OneDrive 删除源文件时，不删除 Knowledge 中的副本。

检查与首次落地：

```bash
python3 moodle-killer/scripts/teams_sync.py --dry-run --json
python3 moodle-killer/scripts/teams_sync.py --json
```

后续由 `moodle_prep.py` 定时扫描；Teams 来源可单独运行，也可与 Moodle 文件一起进入输出和推送流程。`mk setup` 的 8 块仅配置 Moodle；仅用 Teams 时无需填写 Moodle 账号或选 Moodle 课程。`mk status` 会列出本地 Teams 来源。一次新增很多文件时，推送按行数限制显示部分条目，并提示剩余数量；完整列表保存在 `out/all_signals.txt`。脚本用 SHA-256 检查变化；只在复制并验证成功后记录状态。目标位置已有不同文件或本地改动时会报错并保留目标文件。OneDrive 文件按需下载时，首次扫描可能需要等待本地客户端取回内容。来源路径不可用或复制失败会作为校验失败显示，不会当作“无新内容”。

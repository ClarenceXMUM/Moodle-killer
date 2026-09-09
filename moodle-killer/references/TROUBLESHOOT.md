# 出问题怎么查

**第一步永远是 `mk doctor`** —— 它逐项检查 11 个地方，坏在哪直接说，还告诉你怎么修。

```bash
mk doctor          # 体检
mk doctor --json   # 结构化结果（给 Agent 解析）
```

---

## 排查树

### ① 收不到任何消息

```
mk doctor
├─ ❌ 账号信息 / 登录测试失败 → 核对学号密码：mk set 密码 xxx；或先手动登录一次学校网站
├─ ❌ 推送通道缺凭据      → 按 doctor 的 → 提示补（如 mk set delivery.ntfy.topic xxx）
├─ ⚠️ 没找到定时任务      → 见下面「定时器」
├─ ⚠️ 已选课程 0 门       → mk add 课程名
└─ ✅ 全绿还是不推        → mk status 看上次运行；再看是不是 output.mode=silent/urgent（没事就是不推）
```

### ② 定时器

`mk doctor` 的「定时任务」只认 **launchd / crontab**。如果定时是靠别的调度器（比如 Hermes 的 cron、系统任务计划），doctor 会显示「没找到定时任务」——**这不一定是坏事**。

先确认「到底谁在负责触发」：

```bash
mk schedule              # 看/改定时方式
mk schedule auto         # 自动选（macOS 用 launchd，Linux 用 cron）
mk schedule off          # 关掉本技能的定时器
```

> ⚠️ **只能有一个调度源**。如果 Hermes cron 已经在跑 `moodle_prep_runner.sh`，就**不要**再 `mk schedule auto`，否则同一天推两次。

### ③ 推送了但内容是空的 / 全是旧的

```bash
mk status                # 看「最近一次」时间和状态
cat ~/.moodle-killer/out/last_run.json
cat ~/.moodle-killer/out/signals.txt
```

- `signals.txt` 为空 → 确实没新内容（看 `output.mode`，silent/urgent 本来就不报）
- 状态文件在 `~/.moodle-killer/state/`，删掉某门课的 `course_<id>.json` 会让它**重新认为全是新的**（谨慎）

### ④ 下载没落地

```
mk doctor
├─ ⚠️ 下载目录不存在   → 第一次运行会自动建；也可以 mkdir -p ~/School
├─ ❌ 下载目录不可写   → mk set 下载目录 ~/School
└─ ✅ 但还是没文件     → mk test 看输出；Moodle 那边可能没有新附件
```

按类型分文件夹是高级项，默认**关**。开了以后目录形如 `~/School/数学分析/Assignment/`。

### ⑤ 校验有 ❌

`verify_report.txt` 里出现 ❌ = 那一步没通过。**不要忽略**：

```bash
cat ~/.moodle-killer/out/verify_report.txt
```

| ❌ 项 | 含义 | 处理 |
|---|---|---|
| 登录 | 没登上 | 账号/密码/网络 |
| 通知抓取 | 页面结构和预期不符 | Moodle 改版了 → 提 issue |
| 下载落地 | 文件没写成功 | 目录权限 / 磁盘满 |
| 课程扫全 | 有课没扫到 | `mk add` 重新同步课程 |

`advanced.verify_strict=true`（默认）时，校验不过**就不推送** —— 这是故意的：宁可静默，不推错的东西。

### ⑥ `mk: command not found`

```bash
ls ~/.local/bin/mk                 # 有没有生成
echo $PATH | tr ':' '\n' | grep .local/bin
source ~/.zshrc                    # 新终端生效
```

没有 shim 就重跑 `./install.sh`。临时用完整路径也行：`python3 ~/Projects/Moodle-killer/moodle-killer/scripts/mk.py status`。

### ⑦ 装到助手后助手看不到这个技能

```bash
mk harnesses        # 看各助手该把技能放哪
ls ~/.hermes/skills/moodle-killer/SKILL.md      # 以 Hermes 为例
```

- 目录不对 → 见 `references/HARNESSES.md` 手动放
- 放对了但没识别 → 重启助手 / 重新加载技能列表
- 助手只认项目目录（Cursor/Windsurf）→ 到项目里跑 `mk install cursor`

---

## 兜底：什么都不确定时

```bash
mk status
mk doctor --json
mk test
```

把这三条的输出贴出来（密码已自动打码），问题基本就定位了。

---

## 开发者注意（改代码时才看）

- **`MOODLE_KILLER_HOME` 会粘住**：为测试导出过它（比如 `export MOODLE_KILLER_HOME=/tmp/mk-test`），之后所有命令都会指向那个目录，忘了 `unset` 就会看到「文件不存在」这类假故障。测试完先 `unset MOODLE_KILLER_HOME` 再跑 `verify --run`。
- **要测新改动用 `mk sandbox`，别直接拿真机测**：沙盒造一台假电脑（独立 HOME / 技能副本 / 数据目录，不装真实定时任务），测完 `mk sandbox rm` 一句删干净。细节 → `references/SANDBOX.md`。
- **改了脚本要同步安装副本**：`bash install.sh`（或 `mk install <助手>`），否则助手读到的还是旧代码；用 `diff -rq <安装位置>/moodle-killer <仓库>/moodle-killer` 自查，应 0 差异。
- **`mk doctor` 报「没找到定时任务」**：如果你用的是 Hermes cron 而不是 launchd/crontab，新版会去 `~/.hermes/cron/jobs.json` 找；找不到才提示 `mk schedule auto`——**装之前先关掉 Hermes 里那条，否则会推两次**。

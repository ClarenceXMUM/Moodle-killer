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

#### ②-1 「今天怎么没自动跑？」——先分清三种情况

按这个顺序查，别猜：

```bash
mk status                       # ① 脚本自己有没有跑过？（看「最近一次」时间）
mk test                         # ② 现在有没有东西在等（试跑，不动状态、不推送）
```

1. **脚本根本没跑**（`out/last_run.json`、`signals.txt` 的时间还停在昨天）
   → 问题在**调度器**，不在这个技能包。去看调度器自己的执行记录：
   - Hermes cron：`hermes cron list`（看 Last run / Next run）、`hermes cron runs <job_id>`
   - 系统定时器：`launchctl list | grep moodle`、`crontab -l`
2. **脚本跑了但报错** → 看 `~/.moodle-killer/logs/moodle.log`（系统定时器的输出）或调度器记录里的 stderr。
3. **脚本跑了、状态是 ok、但还是没消息** → 大概率是 `output.mode` 是 `silent`/`urgent` 且当天没内容；或通道是 Agent 专用通道（`hermes`/`whatsapp`）而当时**没有 Agent 在场**（脚本只交接、不直发）。

#### ②-2 🔴 「立刻跑一次」会吃掉下一次的名额（踩过）

很多调度器的「手动触发一次」是**把下一次排期提前执行**，不是「额外跑一次」。以 Hermes 为例：

```bash
hermes cron run <job_id>        # 语义 = 占用「下一次排期」并立刻跑
```

后果（实测）：任务每天 08:30，你头一晚 23:52 手动跑了一次 → 那一次**用的就是第二天 08:30 的名额** → 第二天 08:30 不会再触发，`next_run` 直接跳到后天。**看起来就像「今天没自动跑」。**

怎么确认是这种情况（而不是真故障）：

```bash
# 执行记录里那一行的 scheduled_instant 是不是「本该在今天跑的那个时刻」，
# 而 claimed_at 却是你手动提前跑的时间 —— 是就说明名额被提前用掉了
hermes cron runs <job_id>
```

两个佐证：① 别的任务当天照常触发（说明调度器没坏）；② 这个技能的日志/产出文件在应该跑的时刻**完全没有写入痕迹**（说明它不是「跑了但失败」，而是压根没被派发）。

**所以：要验证「现在能不能跑」，不要用调度器的手动触发。** 用 `mk fresh` 或 `mk test`：

```bash
mk fresh    # 完整重跑一次（真下载、真写状态），跑完会打印「定时名额没动：下次仍然 …」
mk test     # 试跑（不下载、不推送、不动状态）
```

`mk fresh` 只**读**调度器的排期、绝不改它，所以它不会像 `hermes cron run` 那样吃掉明早的名额。

### ②-3 课上新加了东西，但我没收到提醒（扫描层覆盖到哪里）

先分清是「没扫到」还是「扫到了但没推」：

```bash
mk test        # 试跑：脚本本轮认到的东西全在这儿（不下载不推送）
```

扫描覆盖：课程页上**任何** `/mod/<类型>/` 活动都会进基线——文件（resource/folder）与作业走下载/作业出口，其余类型（网页 page、图书 book、链接 url、测验 quiz、讨论区 forum、标签 label、外部工具 lti…）新出现时报 `[新活动] 课程: 网页 名字`。

四种「看起来漏了」的情况：

1. **升级 / 新接课之后的第一次扫描**：只写基线、不报历史活动（否则会把所有旧网页一次性翻出来）。第二次起才报新的。
2. **这门课没在本技能里登记**（`mk status` 看不到它）：一学期只在 Moodle 上选课不会被盯。本学期的新课现在会自动接上，但**读不到学期分类时不会接**。手动补：`mk add <关键词>`。
3. **老师把活动设成了「隐藏」**：对学生页面不出现，脚本自然看不到（站点行为，不是漏）。
4. **讨论区里的新帖子**不算新活动（基线只记活动本身）。要盯帖子得看 Moodle 通知，走 `check_notifications()` 那条路。

### ②-4 课程文件夹的名字和配置对不上（课件可能被劈成两半）

**症状**：`mk doctor` 报「下载目录对不上磁盘」，或你手动改过 Knowledge 里的课程文件夹名。

**为什么会出事**：配置里写的是「这门课的课件放哪」。文件夹被改名/搬走后，扫描会**按配置新建一个空目录**，于是同一门课的课件从此分两处。**这个命令不只比配置字符串，会同时看磁盘**：

```bash
mk folder            # 看清单：＝已符合 ｜ →要动 ｜ ·只写配置；错配会写明「配置路径已失效，磁盘上是「X」」
mk folder --apply    # 对齐：先备份配置，再逐条改名/修好配置；冲突或目标已存在一律跳过（绝不合并）
```

五种状态的含义：

| 状态 | 含义 | `--apply` 会做什么 |
|---|---|---|
| 已经是这个名字 | 配置与磁盘一致 | 不动 |
| 重命名 | 配置指向的目录在磁盘上，但名字不符模板 | 改名成模板名 + 更新配置 |
| 重命名（配置路径已失效，磁盘上是「X」） | 配置指向的目录**不在**了，但同目录下有像这门课的 X | 把 X 改名成模板名 + 更新配置 |
| 修好配置 | 磁盘上已经是模板名，只是配置还指着旧名 | 只把配置指过去（不动物理目录） |
| 只改大小写 | 同一个目录，只差大小写（macOS 文件系统大小写不敏感） | 走两步改名改成目标大小写 |
| ❌ 歧义：磁盘上有多个像这门课的目录 | 分不清是哪一个（比如同时有「MAT203 Statistics A」「…B」） | **一个都不动**，报出来让你自己确认 |
| 只写配置 | 这门课还没有目录 | 记下新路径，下次下载按新名字建 |

### ②-5 文件名/文件夹名里的课程代号不对（或干脆没有）

**症状**：下载下来的文件叫 `AAI-Course-Information.pdf`、文件夹叫「Abstract Algebra I」而你想要 `MAT211 Abstract Algebra I`。

**根因**：Moodle 里没有官方课程编号（实测 XMUM 四门课的 `idnumber` 全空），课名里也可能没写代号 —— 那种情况下工具不敢瞎猜，就留空/退化。

**怎么办**：

```bash
mk code                # 看每门课的代号现状 + 当场深查（Moodle 官方编号 → 课名 → 课程资料的 Course Code → Teams 交叉验证）
mk code --apply        # 把查到的写进配置
mk code 抽象代数 MAT211     # 实在查不到就人工指定（最高可信，不再被自动改）
mk folder --apply      # 让文件夹跟着新代号改名
```

「查到」的定义很严：课程资料里**明写 `Course Code: MATxxx`** 才算（实测 `Abstract Algebra I` 的 `MAT211` 只在 syllabus 里）；文件名里扫到的代号只认「像本课大纲」的那几份，否则会把共享教材上**别的课**的代号认成自己的。Moodle 短名（`AAI`）永远不算代号。

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

### ⑧ 新课的名字是「XXX 2...」（结尾三个点）

Moodle 的课程页会把长课名截成 `…` 结尾，**接口给的是全名**。`discover_courses()` 先去页面抓、再问接口，
所以同一个课程 id 会见到两个名字；`remember()` 必须在「后来这个名字更长、且自己不是截断版」时覆盖前面的，
否则 `mk add` 会把 `MAT301 and MAT418 Partial Differential Equations 2...` 这种半截名字写进 `courses.json`，
默认下载路径 `~/School/<半截名>/` 也跟着建错（表现为「文件不知道下到哪去了」）。

自查与修：

```bash
cat ~/.moodle-killer/courses.json       # 看 name / path 对不对
mk rm <关键词>                           # 删掉那条
mk add <关键词>                          # 项目已修好版可直接重加（旧版要先更新脚本）
```

修完记得把 `path` 指回真正的课程文件夹（`mk add` 只给默认值，改路径用编辑 `courses.json`：
没有 `mk set` 项，改完 `mk doctor` 里「下载目录 3 个目录都在」应为 ✅）。

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
- **要测新改动，先 `mk test`（试跑，产出落 `out/.dryrun/`，正式状态一个字不动）；要跑真的用 `mk fresh`**。动代码前先把 `~/.moodle-killer/`（config + courses + state）复制到 `backups/<日期>/`，跑完 `diff` 一比就知道有没有误伤——比再造一台「假电脑」便宜，也不用维护第二套环境。
- **改了脚本要同步安装副本**：`bash install.sh`（或 `mk install <助手>`），否则助手读到的还是旧代码；用 `diff -rq <安装位置>/moodle-killer <仓库>/moodle-killer` 自查，应 0 差异。
- **`install.json` 的 `skill_dir` 必须是仓库路径**，它决定「谁是源头」。若它指向某个安装副本（`~/.agents/skills/...`），
  之后 `mk install` 就是**副本 → 副本**互相同步，仓库里改的 `references/`、`SKILL.md` 永远传不出去（`scripts/` 看不出问题，容易误判成已同步）。
  自查：`python3 -c "import json;print(json.load(open('$HOME/.moodle-killer/install.json'))['skill_dir'])"`。
  改了 references 却看不到生效，先查这里。
- **`mk doctor` 报「没找到定时任务」**：如果你用的是 Hermes cron 而不是 launchd/crontab，新版会去 `~/.hermes/cron/jobs.json` 找；找不到才提示 `mk schedule auto`——**装之前先关掉 Hermes 里那条，否则会推两次**。

---
name: moodle-killer
description: Use when 配置/使用/排障学业通知（Moodle 推送、Teams 文件经 OneDrive 同步到 Knowledge）. 引导式配置 + 一句话改配置 + 5 种推送风格 + 每步校验.
---

# Moodle-killer

把「盯 Moodle 课程动态」这件事交给机器：定时抓 → 关键词打桶（零 LLM）→ 拿不准的交给 Agent 判断 → 每步校验 → 推一条 ≤5 行的高密度摘要。

**脚本先扛，Agent 兜底。** 能用规则判断的绝不花 token；每一步都验证，宁可报错也不推赌运气的结果。

---

## 一、装完就能用：四条命令

```bash
mk                  # 现在什么情况（配置/课程/最近一次运行/下次推送）
mk setup            # 一步步配好：问一块答一块，随时可停，下次接着问
mk test             # 试跑一次（不推送、不改状态），当场看到结果
mk fresh            # 立刻完整重扫一次（真下载、真推进状态），不占定时名额
mk new              # 新学期切换：摘上学期的课、接本学期的课（默认只预览）
```

想改哪项，一句话：

```bash
mk set 时间 07:00           # 改推送时间
mk output silent            # 换推送风格
mk naming                   # 看下载下来的文件会叫什么名字（拿你自己的课演示）
mk naming custom            # 换**文件**命名规则；自定义模板：mk set 命名模板 "{code}-{name}"
mk folder                   # 看**课程文件夹**命名，并列出你每门课会叫什么
mk folder --demo            # 并排看几种组合分别得到什么文件夹名
mk folder --apply           # 按当前模板把现有课程文件夹真的改名（先备份配置）
mk code                     # 看/查课程代号（Course Code）；--apply 写入，或 mk code 课名 代号 指定
mk add 数学分析             # 手动加课（本学期新课通常会自动接上，见下）
mk find 概率                # 忘了文件夹在哪，让它帮你找
mk doctor                   # 出问题先跑这个
mk channel test             # 实际发送一条测试消息，检查当前通道
```

Teams 团队文件可先由 OneDrive 客户端同步到本地，再由本技能增量复制进 Knowledge，并接入同一套新文件提醒。**首次配置 Teams 来源时，Agent 必须先按 `references/TEAMS-SYNC.md` 的「给用户的首次设置指引」指导用户添加 OneDrive 快捷方式；等用户完成、Agent 验证本地文件可读后，才配置来源和试跑。**用户已完成并可验证时直接继续。

`mk` 是唯一入口。**记不住命令没关系**，在聊天里直接说也行：

> 「配置 moodle」「moodle 状态」「moodle 改推送时间 07:00」「moodle 把成绩那条推给我」「moodle 文件命名带上课号」「moodle 现在重扫一遍」「moodle 文件夹改成课程代号+名称」

---

## 二、在 Agent 聊天界面里配置（不用敲命令）

用户说「配置 moodle」时，不要凭想象问，**按下面这套走**：

先问一句是否也要同步 Teams 课件；若要，先按 `references/TEAMS-SYNC.md` 指导添加 OneDrive 快捷方式，等用户完成后再配置 Teams 来源。`mk setup` 的 8 块只配置 Moodle；仅用 Teams 的用户直接走 Teams 指引，无需填写 Moodle 账号或选 Moodle 课程。

1. 跑 `mk setup --list` → 拿到分块问题清单（JSON，每块有 `title` / `why` / `questions`）
2. **一次只问一块**，把 `why` 用人话讲给用户听（用户要知道为什么要问这个）
3. 用户答完，跑 `mk setup --answers '{"delivery.schedule":"08:30"}'` 写入
4. 回到第 1 步问下一块，直到 8 块走完

八块顺序（每块都可跳过，跳过不写值）：

| 块 | 问什么 | 为什么要问 |
|---|---|---|
| 1 账号 | Moodle 网址 / 学号 / 密码 | 登录抓通知；密码只存本机 |
| 2 课程 | 盯哪几门课 | 只扫你选的课，省时间少打扰 |
| 3 下载 | 下载到哪、文件怎么命名、要不要按类型分文件夹 | 课件/作业自动落地，省得天天点 |
| 4 时间 | 每天几点推（可多个）、周末推不推 | 挑你习惯看手机的时间 |
| 5 通道 | 推到哪个 App（本地/Telegram/WhatsApp…） | 决定消息落到哪个 App |
| 6 风格 | 5 种推送风格选一个 | 决定一天收几条、没事时安不安静 |
| 7 定时 | 按平台给（launchd / 任务计划程序 / cron / 手动） | 配好还要挂定时器才会自己跑 |
| 8 验收 | 当场试跑 | 装完立刻看到结果，不留到明天才发现坏的 |

**续答与重来**：进度存在 `~/.moodle-killer/.setup_progress.json`，中断后 `mk setup` 接着问；要重来加 `--fresh`。

---

## 三、5 种推送风格（`mk output <风格>`）

先看效果：`mk output --demo`（并排打印 5 种风格的实际样子，不联网）。

| 风格 | 没事的时候 | 有更新的时候 | 适合 |
|---|---|---|---|
| `heartbeat` 默认 | 报一句 `[Moodle] 已扫5课 无新内容 · 下次 09/10 08:30` | 更新 + 收尾行 | 想知道「它到底跑没跑」 |
| `silent` | **完全不说话** | 只报更新 | 嫌吵，只想知道有事 |
| `digest` | `[Moodle 日报 09/09] 扫了 5 门课，0 条更新` | 日报头 + 条目 | 每天固定看一条汇总 |
| `urgent` | **完全不说话** | 只报 24h 内截止 + 新成绩 | 只要紧急的，别的都别烦我 |
| `full` | 没内容就空 | 全报（不受 5 行限制） | 想一眼看全部 |

详细示例与取舍 → `references/OUTPUT-MODES.md`

---

## 四、给 Agent 的硬规矩

**1. 改配置前先找对文件，别猜。**

| 用户想改的东西 | 改哪 |
|---|---|
| 推送时间 / 通道 / 风格 / 账号 | `~/.moodle-killer/config.yaml`（用 `mk set` 改，别手改） |
| 什么值得推 / 什么丢弃 / 课程专属规则 | `~/.moodle-killer/user_requirements.md` |
| 盯哪几门课 / 下载目录 | `~/.moodle-killer/courses.json`（用 `mk add` / `mk rm`；每门课的 `code` 是命名用的课程代号，可手改） |
| 下载下来的文件叫什么名字 | `config.yaml` 的 `download.naming` + `download.name_template`（`mk naming` 看效果，`mk naming --demo` 并排对比） |
| 关键词规则 | 脚本里的 `RULES`（改前先读 `references/CONFIG.md`） |

完整对照表 + 每项怎么改 → `references/AGENT-PLAYBOOK.md`

**2. 只做判断，别重写流程。** 兜底队列里拿不准的，逐条判断「明天早上看到还有用吗」——有用才推。

**3. 输出守契约：** ≤5 行（`full` 除外）、每行 `[类型] 课程: 内容`、零寒暄零空行。

**4. 不确定就不推。** 宁可漏提，不推模糊噪音。

**5. 校验有 ❌ 必须透出。** 静默 ≠ 故障，脚本没产出才允许 `[SILENT]`。

**6. 测试绝不许碰正常扫描和发送。** 三条边界，违反任何一条都算把用户的正常提醒搞坏：

| 你想做的事 | 用哪个 | 为什么 |
|---|---|---|
| 看「现在有什么在等」 | `mk test` | 试跑：不下载、不推、**不写正式状态**；产出落在 `out/.dryrun/`，`out/` 里上一次真跑的记录一个字都不会动 |
| 真的扫一遍并下载 | `mk fresh` | 完整重跑一次：真登录、真下载、真勾 Done、真写状态，**但只读调度器排期、绝不改它**，所以下一次定时名额原封不动 |
| 「让它现在就自动跑一次」 | ❌ **不要用调度器的手动触发** | 多数调度器（含 Hermes 的 `hermes cron run`）是**占用下一次排期并立刻跑**，会把明天的定时名额提前用掉——第二天就「没自动跑」了 |

**关键副作用提醒**：真跑会推进状态（把新文件记成「已看过」）。所以**手动真跑之后，必须把这一次的信号推给用户**，否则第二天定时只剩「无新内容」，用户会以为漏了。试跑没有这个问题。

**7. 下载成功就把那条活动勾成 Done（保持课程进度）。** 默认开着（`completion.mark_done`），只在**真跑**时执行：

- 只勾**真的下载成功**的那几条活动，且必须出现在课程页的活动列表里；**只勾 resource / folder** 两类
- **永不取消**：代码层面禁止 `completed=false`（`set_activity_completion` 直接抛错）
- 已完成 → 跳过（不发请求）；页面上没有那个开关 → 跳过 + 提示，**不算失败**
- 该课扫描状态文件缺失 → 拒绝写（那是「全部文件都算新」的批量误勾场景）
- 单轮应勾数 > `completion.max_marks`（默认 15）→ **一条都不勾**，先报出来让用户确认
- 串行 + 1~3 秒抖动；每条写后**回读**，回读明确为「未完成」→ 立即熔断本轮剩余
- 成功判据五条件齐全：HTTP 200 · JSON 可解析 · 无 error/exception · `data.status` 为真 · warnings 为空

结果写在 `out/verify_report.txt`（含 `完成度: 课程 x/y → x/y（% → %）` 与逐条 cmid 名称），快照追加到 `state/completion_snapshots.jsonl`（唯一的人工回滚依据）。关掉：`mk set completion.mark_done false`。

**8. 课程会自己接上，但**只认 Moodle 自己的学期分类**。**

- **判据唯一**：`core_course_get_enrolled_courses_by_timeline_classification` 的 `inprogress` = 本学期。**`/my/` 页面给的是「最近访问」，混着去年的课**（实测 10 门里 6 门是旧课）——绝不能拿页面列表当「在上的课」。
- **自动接课**：每次真跑时，本学期里还没登记的课会被接上（目录优先复用已有的同名/同代号文件夹），并立刻拉一次基线；新课首轮**只报一行汇总**（`[已接上] … 已拉了 N 个文件 → 路径`），不逐条报、也不勾 Done（首轮安全闸门），从下一次扫描起正常。
- **学期切换用 `mk new`**：`mk new` 只预览（列出将摘掉/将接上/下学期暂不动），`mk new --yes` 才动手——先备份 `courses.json`，再摘上学期、接本学期。**摘课只取消盯课**：已下载的文件与 `state` 原样留着。
- **接口读不到就不动手**：拿不到 `inprogress` 时 `mk new` 直接拒绝（exit 2），自动接课也静默跳过。绝不因为接口抽风就把课全摘了。
- **`mk rm` 会记「不再自动接上」名单**：否则自动接课第二天就把它接回来了——「移除」必须是算数的。想恢复盯课用 `mk add`；只是想安静不推就用 `mk mute`（不摘课）。

**11. 课程代号必须攒够 —— 这是 setup 阶段 Agent 的责任。**

- **短名不是代号**：`AAI`/`Stat`/`PDEs` 是老师起的缩写。拿它当代号，文件夹名与下载文件前缀就跟真编码（`MAT211`）打架 —— 这是踩过的坑。
- **查证顺序**（`mk code` 自动按这个顺序找）：`manual`（`mk code 课名 代号` 人工指定，最高，永不自动覆盖）→ `idnumber`（Moodle 官方编号字段，实测 XMUM 为空）→ 课名里的真代号 → **课程资料的「Course Code」字段**（老师的 syllabus，实测 `Abstract Algebra I` 的 `MAT211` 只有这里才有）→ Teams 来源目录名交叉验证（**必须先确认是同一门课**，否则会把别的课的代号认成自己的）。
- **查证结果写进 `courses.json`**：`code` + `code_source`（来源可追溯）。没确认就留空，**不许瞎填**。
- **自动跑**：每次真跑会对「还没代号」的课查一遍 —— 老师把 syllabus 传上来后，下一次扫描就自动确认（这就是「直到确认为止」）。有确认/变更时写进 `verify_report.txt` 与 stdout。
- **`mk doctor` 会报「课程代号 N 门还没确认」**；setup 阶段就应该跑 `mk code --apply` 把它清零，否则执行阶段文件名/文件夹名只能退化成课名。

**9. 扫描覆盖所有活动类型，不只文件。** 课程页上每种 `/mod/<类型>/` 都会被记进基线；**新出现的活动**（网页 / 图书 / 链接 / 测验 / 讨论区…）也会报一行 `[新活动] 课程: 网页 名字`。文件与作业走各自原有的出口，不会重复报。升级后第一次扫描只写基线、不报历史活动。

- `mk fresh` = 早上那一趟的完整重跑（真跑，不是 `mk test` 的试跑）；它跑完会打印一行「定时名额没动：下次仍然 …」，那就是不占明早名额的凭据。
- **跑完必须把这一轮的信号交给用户**（该推的推、该答的答）：真跑已经把新内容记成「已看过」，明天 08:30 照跑但可能只剩一句心跳——不交出去，用户就会以为漏了。
- 有 `[兜底]` 时按早上同一套规矩：读 `out/unclassified_moodle.json`，逐条判断「明天早上看到还有用吗」，再拼成 ≤5 行输出。

**10. 课程文件夹命名只有一个来源：模板 `download.folder_template`，别手改路径。**

- 字段（`mk folder` 会列全）：`{code}` 课程代号 —— **绝不用 Moodle 短名**（`AAI`/`Stat`/`PDEs` 是缩写，会跟真编码打架）。取法：① 课名里挂着多个代号时（`MPU1022/MPU3322 …` 按批次/专业分），以 `courses.json` 指定那个为准；② 否则课名里的真代号（`MAT203`）；③ 都没有就留空，**靠 `mk code` 去查证**（见规则 11）。要短名请显式写 `{shortname}`· `{name}` 课程名（已去掉代号/学期号/老师）· `{teacher}` · `{semester}`（`2026/09` → `2026-09`）· `{shortname}` · `{fullname}`。默认 `{code} {name}` → `MAT203 Statistics`。
- 四个入口：`mk folder`（看，含每门课的实际结果）｜ `mk folder "模板"`（改）｜ `mk folder --demo`（并排对比）｜ `mk folder --apply`（**按模板真改现有文件夹**，先备份 `courses.json`/`teams_sources.json`）。
- **改模板不会自动改已有文件夹**（只影响以后新接的课）；要改现有的必须 `mk folder --apply`。反过来 `--apply` 只动文件夹名与配置路径，**不动文件内容**。
- 冲突（两门课算成同名）或目标目录已存在 → **跳过并报 ❌，绝不合并**。要区分课程就在模板里加 `{semester}` 或 `{teacher}`。
- **`--apply` 会对照磁盘，不只看配置**：配置指向 A、磁盘上是 B（手动改过名、被别的工具搬过）时必须能认出来 —— 只比配置字符串就会判定「已符合」，而下一次扫描会**另建一个空目录**，把同一门课的课件劈成两半。七种状态：已符合 / 重命名 / **只改大小写**（macOS 大小写不敏感，走两步改名）/ **修好配置**（磁盘上已是模板名，只把配置指过去）/ 只写配置 / ❌ **歧义**（磁盘上多个像它的目录 → 一个都不动）/ ❌ 冲突或目标已存在（绝不合并）。
- 路径细节：配置里写 `~/…` 会先展开再判断；**源目录带尾斜杠时 `os.rename` 会跟随软链**（把软链指向的真实目录搬走、留下死链接）—— 所以移动前源和目标的尾斜杠都要去掉，软链只搬链接本身。
- 发现「配置指向的目录不在磁盘上」别自己 `mkdir` —— 先 `mk folder` 看它认不认得出磁盘上那个疑似目录，再 `mk folder --apply` 对齐（`mk doctor` 也会报这条）。
- 这是**文件夹**命名；单个**文件**的命名是另一套（`mk naming` + `download.name_template`）。两套别混。

---

## 五、出问题怎么办

```bash
mk doctor        # 体检：登录/课程/定时/权限/依赖，逐项说哪坏了 + 怎么修
mk status        # 看最近一次运行是成功还是失败
mk test          # 试跑，不推送
```

- 登录失败 → `mk doctor` 会告诉你账号还是网络的问题
- 收不到推送 → 先 `mk channel` 确认通道，再 `mk doctor` 查定时器
- 不想被打扰 → `mk pause`（脚本照跑，只是不推）；`mk resume` 恢复
- 详细排查树 → `references/TROUBLESHOOT.md`

---

## 六、文件地图

```
moodle-killer/                  ← 技能包（只读，升级覆盖不会动你的数据）
├── SKILL.md                    ← 本文件
├── scripts/
│   ├── mk.py                   ← 唯一命令入口
│   ├── moodle_prep.py          ← 主流程：抓取 + 打桶 + 校验 + 输出
│   ├── config_store.py         ← 配置/数据目录的唯一真相源
│   ├── onboarding.py           ← 引导式配置（8 块问询）
│   ├── moodle_client.py        ← Moodle HTTP 客户端（登录/通知/扫课/下载）
│   ├── teams_sync.py           ← 本地 OneDrive/Teams 文件增量扫描与复制
│   ├── sender.py               ← 推送通道（local/hermes/telegram/ntfy/webhook/whatsapp/none）
│   ├── verify.py               ← 四步校验 + 自检
│   ├── platform_support.py     ← 平台差异（Mac/Windows）唯一分支点
│   ├── pathfinder.py           ← 帮你找文件夹（mk find）
│   └── setup_moodle.py         ← 老入口（已并入 mk，保留兼容）
├── references/                 ← 给 Agent 和用户看的细则
│   ├── AGENT-PLAYBOOK.md       ← 用户想改 X，改哪个文件、怎么改
│   ├── CONFIG.md               ← 全部配置项 + 默认值 + 高级设置
│   ├── OUTPUT-MODES.md         ← 5 种风格的实际输出长什么样
│   ├── TROUBLESHOOT.md         ← 排查树
│   ├── TEAMS-SYNC.md           ← Teams 快捷方式与本地来源配置
│   └── HARNESSES.md            ← 装到哪 3 个位置、没读到怎么办
├── templates/                  ← 配置模板（复制用）

~/.moodle-killer/               ← 你的数据（不在技能包里，升级不丢）
├── config.yaml                 ← 凭据 + 偏好（权限 600）
├── courses.json                ← 盯的课
├── user_requirements.md        ← 你的个性化规则
├── out/                        ← signals.txt / unclassified_moodle.json / verify_report.txt
├── state/                      ← 每门课的「已见过」记录
└── logs/
```

---

## 七、安全与边界

- 密码只写进 `~/.moodle-killer/config.yaml`（600 权限），**不进 git、不上传**
- 脚本只用 Python 标准库 + `requests`/`PyYAML`，零 LLM 调用
- 下载只在用户指定的目录内，不碰别处
- 定时器装了以后，同一时间**只能有一个调度源**（别同时挂 Hermes cron 和 launchd，会双推）

# 配置全表

配置文件：`~/.moodle-killer/config.yaml`（权限 600，不进 git）
改法：`mk set <键或别名> <值>`；不带值就显示当前值。手改也行，但**别改坏缩进**。

---

## 账号

| 键 | 默认 | 说明 |
|---|---|---|
| `moodle.url` | `https://l.xmu.edu.my` | 学校 Moodle 地址 |
| `moodle.user` | 空 | 学号 / 用户名 |
| `moodle.password` | 空 | 密码（只存本机，权限 600） |

## 推送时间

| 键 | 默认 | 说明 |
|---|---|---|
| `delivery.schedule` | `08:30` | 一个或多个时间点，逗号隔开：`08:30,20:00` |
| `delivery.weekend` | `true` | 周末是否推送 |
| `delivery.timezone` | `Asia/Kuala_Lumpur` | 时区（影响倒计时和「下次推送」） |

## 推送通道

| 键 | 默认 | 说明 |
|---|---|---|
| `delivery.channel` | `auto` | `auto` / `local` / `hermes` / `telegram` / `ntfy` / `webhook` / `whatsapp` / `none` |
| `delivery.telegram.bot_token` | 空 | @BotFather 建 bot 拿 |
| `delivery.telegram.chat_id` | 空 | 给 bot 发条消息后用 `getUpdates` 拿 |
| `delivery.ntfy.server` | `https://ntfy.sh` | 可自建 |
| `delivery.ntfy.topic` | 空 | 自己起个别人猜不到的名字 |
| `delivery.webhook.url` | 空 | POST `{"text": "..."}`，钉钉/飞书/企业微信都能接 |
| `delivery.whatsapp.to` | 空 | 国际格式，如 `60123456789`（走 Hermes 网关） |
| `delivery.email.to` | 空 | 备用 |

> **默认 `auto`：跟着你的平台走。** 装了 Hermes 就走 Hermes，配了 Telegram 就走 Telegram，都没有就用本机通知（macOS 通知中心 / Windows 通知）。`mk status` 会告诉你它最后选了哪条路、为什么。
>
> 通道只是「消息从哪出去」。同一个通道配多份凭据不会自动切换，`delivery.channel` 指哪个就用哪个。

## 下载

| 键 | 默认 | 说明 |
|---|---|---|
| `code` / `code_source`（每门课） | 空 / 空 | 课程代号与它的来源：`manual`（人工指定）· `idnumber`（Moodle 官方编号）· `name`（课名里）· `material`（课程资料的 Course Code 字段）· `teams`（Teams 目录名交叉验证）。用 `mk code` 看/查，`mk code --apply` 写入；**短名（AAI）不算代号** |
| `download.folder_template` | `{code} {name}` | **课程文件夹**怎么命名。字段：`{code}` `{name}` `{teacher}` `{semester}` `{shortname}` `{fullname}`（见下表）。用 `mk folder` 看/改，`mk folder --apply` 把现有文件夹改成这个规则 |
| `download.root` | `~/School` | 下载根目录。**不知道填哪就 `mk find`** —— 它扫一遍你电脑，列编号让你挑 |
| `download.per_course` | `true` | 每门课一个子文件夹 |
| `download.by_type` | `false` | **高级**：课内再按文件类型分一层 |
| `download.naming` | `default` | 落地文件怎么命名：`default` / `plain` / `original` / `custom`（见下） |
| `download.name_template` | `{code}-{name}` | 只有 `naming=custom` 时生效；可用字段见下 |
| `download.type_map` | 见下 | **高级**：类型关键词表 |

### 文件命名（`download.naming`）

想看实际效果不用装东西：`mk naming` 拿你**自己的课**演示，`mk naming --demo` 四种并排对照。

| 值 | 落地成什么样 | 什么时候用 |
|---|---|---|
| `default`（默认） | `MAT203-Animals.txt` —— 课程代号 + 原文件名 | 一眼知道是哪个课的，课号又短又不重名 |
| `plain` | `Animals.txt` —— 只用原文件名 | 文件夹已经按课分好，不想重复课号 |
| `original` | `744833-Animals-c96707aa4535.txt` —— Moodle 原名 | 需要绝对唯一的机器名（同步/脚本加工） |
| `custom` | 按 `download.name_template` 渲染 | 想带日期、类型、资料夹名等 |

课程代号从课名里抠（`MAT203 Statistics 2026/09` → `MAT203`）；抠不到就用 Moodle 短名（`Abstract Algebra I` → `AAI`）。
想指定成别的，在 `courses.json` 那门课里加 `"code": "你想用的代号"` 即可（手填的优先）。

`custom` 模板可用的字段：

| 字段 | 含义 | 例子 |
|---|---|---|
| `{code}` | 课程代号 | `MAT203` |
| `{course}` | 课程名 | `Statistics` |
| `{name}` | 原文件名（不含后缀） | `Animals` |
| `{ext}` | 后缀（含点） | `.pdf` |
| `{type}` | 类型桶（`download.by_type` 用的那个） | `Slides` |
| `{folder}` | Moodle 资料夹名 | `Lecture-Notes-2026-09` |
| `{date}` | 下载日期 | `20260929` |
| `{hash}` | 唯一短摘要（只有资料夹里的文件有） | `a50271397f6b` |
| `{cmid}` | Moodle 活动 id | `744363` |

```bash
mk set 命名 custom
mk set 命名模板 "{code}-{date}-{name}"     # → MAT203-20260929-Animals.txt
mk set 命名模板 "{code}_{type}_{name}"      # → MAT203_Slides_Lecture-1.pdf
```

规则保证两件事：**同名不同内容不互相覆盖**（自动排 `-2`、`-3`），**重跑同一个文件不产生副本**（内容一样就复用原名）。
模板写错、课程代号缺了、渲染为空 —— 一律退回原文件名，绝不因为命名规则丢文件。


`type_map` 默认：

```yaml
Assignment: [assignment, homework, 作业, tutorial]
Slides:     [slide, lecture, chapter, 笔记]
Textbook:   [textbook, book, reading, 参考]
Other:      []      # 兜底
```

最终目录形如：

```
~/School/
├── 数学分析/
│   ├── Assignment/  ...
│   ├── Slides/      ...
│   └── Other/       ...
└── 数据结构/        ...
```

**不按类型分时**（默认）：`~/School/数学分析/xxx.pdf`

## 输出

| 键 | 默认 | 说明 |
|---|---|---|
| `output.mode` | `heartbeat` | `heartbeat` / `silent` / `digest` / `urgent` / `full` |
| `output.max_lines` | `5` | 单次最多几行（`full` 不受限） |
| `output.lang` | `zh` | 输出语言 |

## 高级（`mk set --advanced` 才看得到）

| 键 | 默认 | 说明 |
|---|---|---|
| `advanced.verify_strict` | `true` | 校验不过就不推（关掉=带病推送，不建议） |
| `advanced.keep_days` | `30` | 状态文件保留天数，0 = 永久 |
| `advanced.paused` | `false` | 暂停推送（脚本照跑，不推） |
| `completion.mark_done` | `true` | 真跑时把下载成功的活动勾成 Moodle 的 Done（保持课程进度条）。永不取消，只勾 |
| `completion.max_marks` | `15` | 单轮最多勾几条；超过就**一条都不勾**（防状态重算导致的批量误勾），报出来让你确认 |

---

## 别名：说人话也能改

`mk set` 认这些说法，中英文都行：

| 说法 | 等于 |
|---|---|
| `时间` / `推送时间` / `time` / `几点` | `delivery.schedule` |
| `风格` / `输出` / `output` / `模式` | `output.mode` |
| `通道` / `推送` / `推送到哪` / `channel` | `delivery.channel` |
| `周末` / `weekend` | `delivery.weekend` |
| `下载目录` / `目录` / `folder` / `root` | `download.root` |
| `按类型` / `分类` / `bytype` | `download.by_type` |
| `命名` / `自动命名` / `文件名` | `download.naming` |
| `命名模板` / `模板` / `template` | `download.name_template` |
| `站点` / `网站` / `url` | `moodle.url` |
| `账号` / `学号` / `user` | `moodle.user` |
| `密码` / `password` | `moodle.password` |
| `行数` / `maxlines` | `output.max_lines` |

---

## 环境变量

| 变量 | 作用 |
|---|---|
| `MOODLE_KILLER_HOME` | 覆盖数据目录（默认 `~/.moodle-killer`）。多账号/测试用 |

## 目录一览

```
~/.moodle-killer/
├── config.yaml            # 本文件对应的配置
├── courses.json           # 盯的课
├── user_requirements.md   # 你的个性化规则（Agent 读）
├── .setup_progress.json   # 引导式配置的进度（可删）
├── out/
│   ├── signals.txt        # 本次命中的信号（纯文本）
│   ├── unclassified_moodle.json  # 未命中，交 Agent 兜底
│   ├── verify_report.txt  # 四步校验结果
│   └── last_run.json      # 本次运行摘要
├── state/                 # 每门课的「已见过」记录（避免重复推送）
└── logs/
```

> 老版本把配置放在仓库 `scripts/` 里。首次运行会**自动迁移**到 `~/.moodle-killer/`，老文件留着只读兜底。

---

## 不用配置的自动行为（知道就好）

| 行为 | 什么时候发生 | 怎么算 |
|---|---|---|
| **自动接课** | 每次真跑（含 `mk fresh` 与定时任务）；试跑不碰配置 | 本学期（Moodle `inprogress` 分类）里还没登记的课自动接上；目录优先复用已有同名/同代号文件夹，找不到才在现有课程的父目录下新建。读不到学期分类就什么都不做 |
| **新课首轮静默** | 刚接上的课第一次扫描 | 拉文件、写状态基线，只报一行 `[已接上] …`；不逐条报、不勾 Done（首轮安全闸门） |
| **新活动也算新内容** | 每次扫描 | 课程页上任何 `/mod/<类型>/` 活动（网页/图书/链接/测验/讨论区…）新出现都会报；升级后第一次只写基线不报历史 |
| **摘课不动数据** | `mk new --yes`、`mk rm` | 只从 `courses.json` 移除盯课；已下载文件、`state/course_<id>.json`、`completion_snapshots.jsonl` 全部保留 |
| **文件夹名对齐磁盘** | `mk folder --apply`（`mk doctor` 也会提醒） | 配置路径与磁盘不一致时：改名对齐模板 / 只修好配置 / 都做不到就跳过并报警。**绝不合并**两个目录 |
| **「不再自动接上」名单** | `mk rm` 写入，`mk add` 撤出 | 记在 `~/.moodle-killer/ignored_courses.json`；自动接课遇到名单里的课直接跳过，否则移除的课第二天会被接回来 |

### 课程文件夹命名字段（`download.folder_template`）

Moodle 的课名通常长这样：`MAT203 Statistics 2026/09 Koh Siew Khew`（代号 + 课名 + 学期号 + 老师），模板把这四段拆成字段自由组合：

| 字段 | 取值示例 | 说明 |
|---|---|---|
| `{code}` | `MAT203` | 取法三层：① 课名挂着多个代号时按 `courses.json` 的 `code` 指哪个用哪个（如 `MPU1022/MPU3322` 按批次）；② 否则课名里的真代号；③ 课名里没有就用这门课的 `code`（如 `AAI`） |
| `{name}` | `Statistics` | 课名，已剔除代号、学期号、老师 |
| `{teacher}` | `Koh Siew Khew` | 学期号后面那截；认不出为空 |
| `{semester}` | `2026-09` | 由 `2026/09` 规范化 |
| `{shortname}` | `Stat` | Moodle 短名（想带上就得显式写） |
| `{fullname}` | `MAT203 Statistics 2026/09 Koh Siew Khew` | 原样全名 |

常见组合：`{code} {name}`（默认）· `{name}`（不要代号，如 `Abstract Algebra I`）· `{code} {name} {semester}`（同学期同名课要靠它区分）· `{code} {name} ({teacher})`。

**校验**：模板必须含至少一个能区分课程的字段（`{code}`/`{name}`/`{shortname}`/`{fullname}`），否则所有课会挤进同一个文件夹，`mk folder` 会直接拒绝。

### 一门课有多个代号时（批次 / 专业共用壳）

实测 XMUM 的 `MPU1022/MPU3322 Integrity & Anti-Corruption`：**第一学期学的用 MPU1022，之后几个学期学的用 MPU3322**，两个代号并排写在课名里。Moodle 上**没有**任何「这个学生该用哪个」的来源（课程页全文、分组、API 的 `idnumber` 全查过，课程资料 PDF 也只写并列两个）。所以只能人指定：

```bash
# 让这门课用 MPU3322 当代号（文件夹名、下载文件的前缀都会跟着变）
# 改 ~/.moodle-killer/courses.json 里该课的 "code": "MPU3322"，然后：
mk folder --apply
```

规则：**你在 `courses.json` 指定的代号只要出现在课名里，就用它**（否则按课名里第一个真代号）。这样既支持「一批课一个壳」的情形，也不会把无关的错代号硬套上去。

### 课程代号（Course Code）怎么来

| 来源 | 说明 | 实测（XMUM） |
|---|---|---|
| `manual` | `mk code <课名> <代号>` 人工指定 | 最高可信，永不自动覆盖 |
| `idnumber` | Moodle 官方「课程编号」字段 | **四门课全为空**（学校没填） |
| `name` | 课名里的真代号 | `MAT203 Statistics …` → `MAT203`；`MPU1022/MPU3322 …` → 需指定用哪个 |
| `material` | **课程资料里 syllabus 的「Course Code」字段** | `Abstract Algebra I` → **`MAT211`**（课名里根本没有！）；`Course Information_MAT203.pdf` → `MAT203` |
| `teams` | OneDrive/Teams 来源目录名交叉验证 | `MAT201 Mathematical Analysis I 202609` → `MAT201`（**需先确认是同一门课**） |

跑 `mk code` 看现状并深查，`mk code --apply` 写入。扫描时也会对**还没代号**的课自动查一次 —— 老师传上 syllabus 后，下次真跑就自动确认了。查不到就留空，绝不瞎填。

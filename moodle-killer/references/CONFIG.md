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
| `download.root` | `~/School` | 下载根目录。**不知道填哪就 `mk find`** —— 它扫一遍你电脑，列编号让你挑 |
| `download.per_course` | `true` | 每门课一个子文件夹 |
| `download.by_type` | `false` | **高级**：课内再按文件类型分一层 |
| `download.type_map` | 见下 | **高级**：类型关键词表 |

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

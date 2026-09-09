---
name: moodle-killer
description: Use when 配置/使用/排障学业通知（定时抓 Moodle 推送摘要）. 引导式配置 + 一句话改配置 + 5 种推送风格 + 每步校验.
---

# Moodle-killer

把「盯 Moodle 课程动态」这件事交给机器：定时抓 → 关键词打桶（零 LLM）→ 拿不准的交给 Agent 判断 → 每步校验 → 推一条 ≤5 行的高密度摘要。

**脚本先扛，Agent 兜底。** 能用规则判断的绝不花 token；每一步都验证，宁可报错也不推赌运气的结果。

---

## 一、装完就能用：三条命令

```bash
mk                  # 现在什么情况（配置/课程/最近一次运行/下次推送）
mk setup            # 一步步配好：问一块答一块，随时可停，下次接着问
mk test             # 试跑一次（不推送、不改状态），当场看到结果
```

想改哪项，一句话：

```bash
mk set 时间 07:00           # 改推送时间
mk output silent            # 换推送风格
mk add 数学分析             # 加一门课
mk find 概率                # 忘了文件夹在哪，让它帮你找
mk doctor                   # 出问题先跑这个
mk sandbox                  # 造个干净环境测，不碰你现有配置
```

`mk` 是唯一入口。**记不住命令没关系**，在聊天里直接说也行：

> 「配置 moodle」「moodle 状态」「moodle 改推送时间 07:00」「moodle 把成绩那条推给我」

---

## 二、在 Agent 聊天界面里配置（不用敲命令）

用户说「配置 moodle」时，不要凭想象问，**按下面这套走**：

1. 跑 `mk setup --list` → 拿到分块问题清单（JSON，每块有 `title` / `why` / `questions`）
2. **一次只问一块**，把 `why` 用人话讲给用户听（用户要知道为什么要问这个）
3. 用户答完，跑 `mk setup --answers '{"delivery.schedule":"08:30"}'` 写入
4. 回到第 1 步问下一块，直到 8 块走完

八块顺序（每块都可跳过，跳过不写值）：

| 块 | 问什么 | 为什么要问 |
|---|---|---|
| 1 账号 | Moodle 网址 / 学号 / 密码 | 登录抓通知；密码只存本机 |
| 2 课程 | 盯哪几门课 | 只扫你选的课，省时间少打扰 |
| 3 下载 | 下载到哪、要不要按类型分文件夹 | 课件/作业自动落地，省得天天点 |
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
| 盯哪几门课 / 下载目录 | `~/.moodle-killer/courses.json`（用 `mk add` / `mk rm`） |
| 关键词规则 | 脚本里的 `RULES`（改前先读 `references/CONFIG.md`） |

完整对照表 + 每项怎么改 → `references/AGENT-PLAYBOOK.md`

**2. 只做判断，别重写流程。** 兜底队列里拿不准的，逐条判断「明天早上看到还有用吗」——有用才推。

**3. 输出守契约：** ≤5 行（`full` 除外）、每行 `[类型] 课程: 内容`、零寒暄零空行。

**4. 不确定就不推。** 宁可漏提，不推模糊噪音。

**5. 校验有 ❌ 必须透出。** 静默 ≠ 故障，脚本没产出才允许 `[SILENT]`。

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
- 想试新改动、又怕弄乱现有配置 → `mk sandbox`（造台假电脑，测完一句删掉）→ `references/SANDBOX.md`
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
│   ├── sender.py               ← 推送通道（local/hermes/telegram/ntfy/webhook/whatsapp/none）
│   ├── verify.py               ← 四步校验 + 自检
│   ├── platform_support.py     ← 平台差异（Mac/Windows）唯一分支点
│   ├── pathfinder.py           ← 帮你找文件夹（mk find）
│   ├── sandbox.py              ← 测试沙盒（mk sandbox）
│   └── setup_moodle.py         ← 老入口（已并入 mk，保留兼容）
├── references/                 ← 给 Agent 和用户看的细则
│   ├── AGENT-PLAYBOOK.md       ← 用户想改 X，改哪个文件、怎么改
│   ├── CONFIG.md               ← 全部配置项 + 默认值 + 高级设置
│   ├── OUTPUT-MODES.md         ← 5 种风格的实际输出长什么样
│   ├── SANDBOX.md              ← 测试沙盒怎么用、隔离了什么
│   ├── TROUBLESHOOT.md         ← 排查树
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

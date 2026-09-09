# 给 Agent 的操作手册：用户说一句话，你改哪、怎么改

> 用户不会记得配置项叫什么，也不会去翻代码。**他要什么，你负责翻译成正确的文件 + 正确的命令。**
> 这份文档就是翻译表。改之前先在这里找到对应的行。

---

## 零、永远先做这两件事

```bash
mk status          # 现在什么配置、盯了哪些课、上次跑成功没
mk doctor          # 如果有问题，这里直接说哪坏了
```

改任何东西之前先 `mk status`，改完再 `mk test` 验证一次。**不要凭记忆猜当前值。**

---

## 一、用户想改 X → 改哪个文件

| 用户说的话 | 改什么 | 怎么改 |
|---|---|---|
| 「推送时间改成 7 点」 | `delivery.schedule` | `mk set 时间 07:00` |
| 「早上晚上各推一次」 | `delivery.schedule` | `mk set 时间 08:30,20:00` |
| 「周末别推了」 | `delivery.weekend` | `mk set 周末 false` |
| 「推到 Telegram」 | `delivery.channel` + token | `mk channel telegram`，再 `mk set delivery.telegram.bot_token <token>`、`mk set delivery.telegram.chat_id <id>` |
| 「手机上收」 | `delivery.channel=ntfy` | `mk channel ntfy`，再 `mk set delivery.ntfy.topic <自己起的名>` |
| 「别推了，安静点」 | `output.mode=silent` | `mk output silent` |
| 「只要紧急的」 | `output.mode=urgent` | `mk output urgent` |
| 「每天给我一条汇总」 | `output.mode=digest` | `mk output digest` |
| 「暂停几天」 | `advanced.paused=true` | `mk pause`（脚本照跑，只是不推） |
| 「加一门课」 | `courses.json` | `mk add 课程名` |
| 「这门课别推了」 | 该课的 mute 标记 | `mk mute 课程名` |
| 「下载存到某个目录」（说不清路径） | `download.root` | **`mk find`** —— 扫一遍你电脑，列编号让你挑 |
| 「下载存到 D 盘/某个目录」 | `download.root` | `mk set 下载目录 ~/School` |
| 「文件到底下到哪去了」 | — | `mk find 关键词`（如 `mk find 概率`） |
| 「课件和作业分开放」 | `download.by_type=true` | `mk set --advanced` 看高级项，或 `mk set 按类型 true` |
| 「换账号 / 密码错了」 | `moodle.user` / `moodle.password` | `mk setup` 重问第 1 块，或 `mk set 密码 xxx` |
| 「什么该推什么不该推」 | `user_requirements.md` | 见下面第三节 |
| 「加个关键词规则」 | 脚本里的 `RULES` | 见下面第四节 |

---

## 二、三个文件的分工（别搞混）

| 文件 | 住哪 | 装什么 | 谁能改 |
|---|---|---|---|
| `config.yaml` | `~/.moodle-killer/` | 账号、通道、时间、风格、下载目录 | **只用 `mk set` 改**，手改容易写坏缩进 |
| `courses.json` | `~/.moodle-killer/` | 盯哪几门课 + 每门的下载路径 | 用 `mk add` / `mk rm` |
| `user_requirements.md` | `~/.moodle-killer/` | 你的个性化规则（什么值得推/丢弃、课程专属要求） | **直接编辑这个文件**，Agent 读了执行 |

> `user_requirements.md` 是**给你（Agent）读的**，不是给脚本读的。
> 脚本负责产出 `out/unclassified_moodle.json`（拿不准的条目），你读了它 + 读了用户规则，再决定推不推。

模板在 `moodle-killer/templates/user_requirements.example.md`。用户没写过就直接从模板复制：

```bash
cp ~/Projects/Moodle-killer/moodle-killer/templates/user_requirements.example.md ~/.moodle-killer/user_requirements.md
```

---

## 三、兜底判断的标准动作

当 `mk test` / 定时任务输出里出现「有未命中项」时：

1. 读 `~/.moodle-killer/out/unclassified_moodle.json`（每项含 `title` / `body` / `course` / `url`）
2. 读 `~/.moodle-killer/user_requirements.md`（用户的规则）
3. 逐条问自己：**「这条信息，用户明天早上看到还有用吗？」**
   - 有用 → 压成一行 `[类型] 课程: 内容` 补进输出
   - 没用（验证码、已过期、纯通知噪音）→ 丢弃，别解释
4. 拿不准 → **丢弃**。宁可漏提，不推模糊噪音。

**注意**：不要重复脚本已经输出的条目（看 `out/signals.txt`）。

---

## 四、想改关键词规则

关键词规则在 `moodle-killer/scripts/moodle_prep.py` 顶部的 `RULES` 里，格式：

```python
("新作业", ["assignment", "作业", "due", "deadline", "submit"], "模板")
```

- 加关键词：往对应桶的列表里加，小写匹配
- 加新桶：照抄一行改名字和关键词
- 改完必须跑 `mk test` 确认没写坏（语法错误会让整个流程挂掉）

**改之前先备份或确认 git 干净**——这是代码文件，不是配置文件。

---

## 五、改完必须验证

```bash
mk test        # 试跑：不下载、不写状态、不推送，只走一遍并显示结果
mk doctor      # 体检：登录/课程/定时/权限/依赖逐项检查
```

- `mk test` 有 ❌ → 按 `references/TROUBLESHOOT.md` 排查，**不要把坏的配置留在线上**
- 改的是定时相关（时间/定时方式）→ 再跑 `mk schedule` 确认调度器状态

---

## 六、不要做的事

- ❌ 不要手改 `~/.moodle-killer/config.yaml` 的缩进（用 `mk set`）
- ❌ 不要把密码写进任何会进 git 的文件（只进 `~/.moodle-killer/config.yaml`）
- ❌ 不要同时装两种定时器（Hermes cron + launchd 会双推）
- ❌ 不要在用户没要求时扩大范围（别顺手重构脚本）

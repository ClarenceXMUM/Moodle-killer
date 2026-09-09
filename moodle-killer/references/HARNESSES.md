# 装到各个 AI 助手

一个命令：

```bash
./install.sh              # macOS / Linux：装到 3 个标准技能目录，并生成 mk 命令
./install.sh --dry-run    # 只看会做什么，不动文件
```

Windows 用 `install.ps1`（右键 → 用 PowerShell 运行）。

```bash
mk install                # 已经装过了，拉最新代码重新同步一遍
mk harnesses              # 看装到哪了、没读到怎么办
```

装的是**整个技能包**（`SKILL.md` + `scripts/` + `references/` + `templates/`），不是只拷一个 md —— 否则脚本跑不起来。

> **你的数据不在技能包里**。配置、课程、规则都在 `~/.moodle-killer/`，升级或重装技能包不会丢。

---

## 装到哪：就三个标准位置

**不再按助手逐个适配**——每个 Agent 自己知道自己的规矩，技能包只往这三个公认位置各装一份：

| 目录 | 谁读它 |
|---|---|
| `~/.agents/skills` | Codex CLI（用户级）、Gemini CLI、OpenClaw、OpenCode —— Agent Skills 开放标准的共享位置 |
| `~/.claude/skills` | Claude Code |
| `~/.hermes/skills` | Hermes Agent |

同一技能装到多处会打架：Gemini 发现同名技能在两个位置会当场报 `Skill conflict detected: ...`，Codex 也可能读成两份。`mk install` 会自动清掉旧位置的重复副本，不用手动管。

> **其他助手？** 不用我们操心。没读到就跟你的 Agent 说一句「把 moodle-killer 这个文件夹装成技能」，它自己知道该放哪。

`mk harnesses` 只回答两件事：**装到哪了、没读到怎么办**。

---

## 想确认它真读到了？（可选）

不用挨个查文档——**直接跟你的 Agent 说「moodle 状态」**，它回你状态就是读到了。

非要自己看一眼，这两条最常用：

```bash
hermes skills list | grep moodle-killer          # Hermes
codex debug prompt-input | grep moodle-killer    # Codex（Gemini 要真终端才输出）
```

判断标准不止「能看到名字」：**装的是整个目录**——里面要同时有 `SKILL.md`、`scripts/mk.py`、`references/`。只有 `SKILL.md` 就是脚本没跟过去，跑不起来。

**Codex 有个坑**：它会从工作目录一层层往上找 `.agents/skills`。站在 `~` 底下的某个目录里跑 Codex，可能同时读到两份、互相打架。`mk sandbox status` 里的「Codex 自查」会直接告诉你读到的是哪一份。

---

## 装不上？让 Agent 自己装

技能包就是一个目录，复制过去就行——**跟你的 Agent 说一句**：

> 「把 `~/Projects/Moodle-killer/moodle-killer` 这个文件夹装成技能」

它自己知道该放哪、要不要重启。这条路比查各家文档可靠，也正是我们不再逐个适配的原因。

---

## 装完之后

```bash
mk                 # 先看状态（会提示还没配置）
mk setup           # 配好
mk doctor          # 体检
```

在助手聊天里可以直接说：

> 「配置 moodle」「moodle 状态」「moodle 改推送时间 07:00」

---

## 卸载

```bash
mk uninstall            # 从 3 个标准位置移除（含历史遗留位置）
```

**数据不会删** —— `~/.moodle-killer/` 原样保留，重装后配置还在。

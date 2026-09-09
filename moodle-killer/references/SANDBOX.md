# 测试沙盒（mk sandbox）

## 为什么需要它

真机上已经装过、配过、跑过。直接拿真机测，看到的是「旧配置 + 新改动」的混合体——
出了问题分不清是代码的锅，还是历史残留的锅。

`mk sandbox` 在 `~/.moodle-killer-sandbox/` 里造一台**假电脑**：Codex 在里面看到的用户目录、
技能副本、配置、数据全是新的，和你现有的东西完全隔离。

## 一句话开始

```bash
mk sandbox              # 造好（已有就复用），告诉你下一步
mk sandbox codex        # 直接进沙盒里的 Codex
```

然后在 Codex 里说「帮我配置一下 moodle-killer」，走完整流程。

## 它到底隔离了什么

| 维度 | 沙盒里 | 你的真实环境 |
|---|---|---|
| 技能副本 | `~/.moodle-killer-sandbox/home/.agents/skills/moodle-killer` | `~/.agents/skills/moodle-killer`（不动） |
| 用户数据 | `.../home/.moodle-killer/`（全新，0 门课） | `~/.moodle-killer/`（不动） |
| `mk` 短命令 | `.../home/.local/bin/mk` | `~/.local/bin/mk`（不动） |
| Codex 工作目录 | `/tmp/moodle-killer-sandbox/work`（空的） | 你的真项目（不动） |
| 定时任务 | **不装**（`mk schedule` 会回「沙盒模式跳过」） | 现有的 launchd / cron / 任务计划（不动） |
| Codex 登录态 | 软链借用真实的 `~/.codex/auth.json` | 不用重新登录 |

## 命令

| 命令 | 作用 |
|---|---|
| `mk sandbox` | 建好沙盒（幂等），打印怎么用 |
| `mk sandbox codex` | 进沙盒里的 Codex（工作目录为空，`-s workspace-write`） |
| `mk sandbox shell` | 开一个沙盒终端，手动跑 `mk setup` / `mk status` 等 |
| `mk sandbox status` | 看沙盒现状 + 让 Codex 自查它读到的是哪一份技能 |
| `mk sandbox reset` | 技能重装、配置清空（真实环境不动） |
| `mk sandbox rm` | 整个删掉 |

`mk sandbox codex -- <参数>` 里的参数会原样传给 codex，例如
`mk sandbox codex -- --model gpt-5.5`。

## 沙盒里能测什么

- 引导式配置从头走一遍（`mk setup`，8 块）
- 改配置：`mk set 时间 07:00`、`mk add 数学分析`、`mk output silent`
- 找文件夹：`mk find`
- 试跑：`mk test`、`mk doctor`、`mk status`
- 换平台说法：`MOODLE_KILLER_PLATFORM=windows mk status`

## 沙盒里测不了什么

- **真实定时任务**：沙盒里 `mk schedule` 一律跳过，避免把任务装进你的系统。
  想验证定时逻辑，用 `mk sandbox shell` 然后手动跑一次 `mk test`。
- **真实推送投递**：通道会按沙盒环境重新探测（通常降级成本机通知）。
  要测真实投递，回到真机用 `mk test`。

## 两个已踩过的坑（改这个模块时注意）

1. **Codex 会从工作目录往上找 `.agents/skills`**。
   如果工作目录放在 `~/.moodle-killer-sandbox/work`，向上一定会撞到真实的
   `/Users/mac/.agents/skills`，同一个技能被读两份、互相打架。
   所以工作目录固定放 `/tmp` 下（`MOODLE_KILLER_SANDBOX_WORK` 可覆盖）。

2. **HOME 一换，用户级 Python 包会「消失」**。
   本机 `requests` / `PyYAML` 装在 `~/Library/Python/3.9/lib/python/site-packages`，
   靠 HOME 定位。沙盒把真实路径透传进 `PYTHONPATH` 才跑得起来。

## 自查

```bash
mk sandbox status
```

关键一行是 `Codex 自查`：
- `✅ 只看到沙盒那份` → 隔离成功
- `❌ 沙盒和真实副本都被读到了` → 没隔离住，先别测，把两处路径发出来

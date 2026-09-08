# Moodle-killer

![License](https://img.shields.io/badge/license-MIT-blue) ![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)


**框架无关的学业通知 Agent**：定时抓取 Moodle 课程动态 → 过滤噪音 → 关键词分类 → 高密度摘要推送。可在任意本地 Agent（Hermes 等）或纯 cron 环境运行。

```
[定时触发] → 抓取（Moodle HTTP）→ 噪音过滤 → 关键词打桶
     ├─ 命中 → 固定信号（零 LLM，秒级）
     ├─ 未命中 → out/unclassified_moodle.json → Agent 读 user_requirements.md（见下）兜底判断
     └→ 每步校验（登录/抓全/下载/课程扫全）→ 推送（WhatsApp/Telegram/Webhook）
```

## 特性

- **脚本先扛，Agent 兜底**：关键词能命中的零 LLM 开销；只有拿不准的才交给 Agent —— 快、稳、不超时
- **每步验证**：登录成功 / 通知抓全 / 下载落地 / 课程扫全，缺一即报
- **倒计时精确到时分**：`[新作业] ODE: Project 截止 06/20 10:00 (剩3天2小时15分)`
- **心跳回执**：无新内容也推 `[Moodle] 已扫5课 无新内容`，静默 ≠ 故障
- **时效性过滤**：验证码/邀请/限时类自动丢弃（隔日摘要无价值）
- **用户需求与代码分离**：个性化规则全在 `user_requirements.md`（由 `user_requirements.example.md` 复制生成），改需求不动代码

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置（一份文件管所有凭据）
cp config.example.yaml config.yaml
cp user_requirements.example.md user_requirements.md  # 你的个性化规则
#    编辑 config.yaml：填 Moodle 账号 + 发送通道

# 3. 课程发现与选择（登录 → 列出已加入课程 → 生成 courses.json）
python3 scripts/setup_moodle.py            # 交互式选择
python3 scripts/setup_moodle.py --all      # 全部加入
python3 scripts/setup_moodle.py --all --path ~/School   # 自定义下载根目录

# 4. 试跑
python3 scripts/moodle_prep.py

# 5. 定时（crontab 示例，每天 08:30）
30 8 * * * cd <repo>/scripts && python3 moodle_prep.py >> moodle.log 2>&1
```

## 发送通道

| 通道 | 配置 | 适用 |
|------|------|------|
| `whatsapp` | Hermes 网关用户（cron deliver=whatsapp） | Hermes 生态 |
| `telegram` | @BotFather 建 bot，填 token + chat_id | 通用，零门槛 |
| `webhook` | 任意 URL，POST `{"text": ...}` | 钉钉/飞书/企业微信/自建 |
| `none` | 只跑脚本看 stdout | 调试 |

Hermes 用户建议另设：`hermes config set cron.wrap_response false`（去掉投递英文框架，只收纯信号）。

## 目录结构

```
Moodle-killer/
├── docs/AGENT-SKILL.md       # Agent 细则（关键词规则/兜底判断/输出契约/验证清单）
├── user_requirements.example.md  # 用户个性化规则模板（cp 成自己的 user_requirements.md）
├── config.example.yaml       # 配置模板（账户/发送通道）
├── scripts/
│   ├── appconfig.py          # 配置加载器（config.yaml → env → 默认值）
│   ├── setup_moodle.py       # 课程发现 + 配置生成（Setup/onboarding）
│   ├── moodle_client.py      # Moodle HTTP 客户端（登录/通知/扫课/下载）
│   ├── moodle_prep.py        # Moodle 预处理（抓取+分类+校验）
│   ├── classify.py           # 通用关键词打桶
│   ├── verify.py             # 四步校验 + 行数校验
│   ├── sender.py             # 发送模块（telegram/webhook/whatsapp）
│   └── run_pipeline.sh       # 串联全流程
└── requirements.txt
```

## 设计原则

1. **脚本只做确定性的事**：抓取、通用过滤、倒计时计算、校验——人人一样，不用判断
2. **用户需求归文件**：课程专属规则、什么值得推、文件归类——Agent 读了执行，改需求不动代码
3. **Agent 只做判断**：兜底队列逐条裁决（「明天早上看到仍有用吗」），表达守契约
4. **每步可验证**：校验不过宁可报错，不推赌运气的结果

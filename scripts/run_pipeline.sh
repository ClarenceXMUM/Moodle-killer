#!/usr/bin/env bash
# 串联：Moodle 抓取 + 关键词打桶 + 四步校验 → 兜底 Agent（读 out/unclassified_moodle.json）→ 推送
# 框架无关；在任意本地 Agent / 办公智能体的 cron 里直接调用本脚本即可。
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/2 Moodle 预处理（抓取 → 打桶 → 校验）=="
python3 moodle_prep.py

echo "== 2/2 兜底 Agent（仅当 out/unclassified_moodle.json 有内容）=="
if [ -s out/unclassified_moodle.json ]; then
  echo "有未命中项，交 Agent 读取 out/unclassified_moodle.json 判断兜底。"
fi

#!/usr/bin/env python3
"""关键词打桶：命中即出固定信号，零 LLM；未命中 → UNCLASSIFIED 桶交给 Agent 兜底。

输入：抓取到的通知列表，每项为 dict，至少含 title/body/course（非必须字段用 .get 兜底）。
输出：stdout 打上 3–5 行高密度信号；UNCLASSIFIED 项以 JSON 写 unclassified.json 供 Agent 读。

框架无关：只用标准库。可在任意本地 Agent / 办公智能体里直接调用。
"""
import json
import sys
import re
from pathlib import Path

# --- 关键词规则表（可扩展） -----------------------------------------------
RULES = [
    ("NEW_ASSIGNMENT", ["assignment", "作业", "due", "deadline", "submit", "截止", "提交"],
     lambda n: f"[新作业] {n.get('course','')}: {n.get('title','')} 截止 {n.get('due','')}".strip()),
    ("GRADE", ["grade", "score", "成绩", "feedback", "评语", "分数"],
     lambda n: f"[成绩] {n.get('course','')}: {n.get('title','')} {n.get('score','')}".strip()),
    ("ANNOUNCEMENT", ["announcement", "公告", "notice", "通知", "announce"],
     lambda n: f"[公告] {n.get('course','')}: {n.get('title','')}".strip()),
    ("DOWNLOAD", ["file", "download", "附件", "resource", "下载"],
     lambda n: f"[附件] {n.get('course','')}: {n.get('title','')} 已下载".strip()),
]

MAX_LINES = 5


def classify(item: dict) -> tuple[str, str]:
    """返回 (bucket, signal_text)。命中返回桶名+固定信号；未命中返回 UNCLASSIFIED。"""
    haystack = " ".join(str(item.get(k, "")) for k in ("title", "body", "subject", "course"))
    haystack_l = haystack.lower()
    for bucket, keywords, fmt in RULES:
        if any(kw.lower() in haystack_l for kw in keywords):
            return bucket, fmt(item)
    return "UNCLASSIFIED", ""


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        print("[空] 无新通知", flush=True)
        return
    items = json.loads(raw) if raw.strip().startswith("[") else [{"title": raw.strip()}]

    signals, unclassified = [], []
    for it in items:
        bucket, text = classify(it)
        if bucket == "UNCLASSIFIED":
            unclassified.append(it)
        elif text:
            signals.append(text)

    # 输出契约定型：3–5 行
    out = signals[:MAX_LINES]
    print("\n".join(out) if out else "[无高价值信号]", flush=True)

    # 未命中项落盘，供 Agent 兜底读取
    if unclassified:
        Path("unclassified.json").write_text(
            json.dumps(unclassified, ensure_ascii=False, indent=2), encoding="utf-8")
        # 告诉 Agent 该去看哪些
        print(f"[兜底] {len(unclassified)} 条需 Agent 读取 unclassified.json", flush=True)


if __name__ == "__main__":
    main()

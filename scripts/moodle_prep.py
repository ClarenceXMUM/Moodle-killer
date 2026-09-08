#!/usr/bin/env python3
"""Moodle 预处理器：脚本先扛，Agent 兜底。

用已有的 moodle_scan.py 做确定性抓取（HTTP，非浏览器，快），
输出：
  signals.txt        —— 3–5 行高密度信号（关键词命中即出固定格式）
  unclassified.json  —— 未命中关键词、需 Agent 判断的项
  verify_report.txt  —— 四步校验结果（登录/抓全/下载落地/课程扫全）

stdout 只打印紧凑信号 + 校验状态，供 cron 把这段文本注入 agent 上下文。
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# 开源版：用 skill 自带的 moodle_client（凭据全部来自 config.yaml / env，无硬编码）
try:
    from moodle_client import MoodleClient
    REUSE = True
except Exception as e:
    REUSE = False
    print(f"[warn] moodle_client 加载失败: {e}", file=sys.stderr)

# 课程配置：courses.json（setup_moodle.py 生成）；无配置则报错提示先跑 setup
CONFIG = Path(__file__).resolve().parent / "courses.json"
if CONFIG.exists():
    try:
        COURSES = json.loads(CONFIG.read_text())
    except Exception as e:
        print(f"[warn] courses.json 解析失败: {e}", file=sys.stderr)
        COURSES = {}
else:
    COURSES = {}
if not COURSES:
    print("[hint] 无课程配置 —— 先运行 python3 setup_moodle.py 生成 courses.json", file=sys.stderr)

OUT_DIR = Path(__file__).resolve().parent / "out"
FALLBACK = OUT_DIR / "unclassified_moodle.json"
OUT_DIR.mkdir(exist_ok=True)

MAX_LINES = 5

RULES = [
    ("NEW_ASSIGNMENT", ["assignment", "作业", "due", "deadline", "截止", "submit", "提交", "project"],
     lambda c, t, d: f"[新作业] {c}: {t} 截止 {d}".strip()),
    ("DOWNLOAD", ["file", "resource", "附件", "下载", "pdf", "幻灯片", "slide", "note"],
     lambda c, t, d: f"[新文件] {c}: {t} 已下载".strip()),
    ("GRADE", ["grade", "score", "成绩", "分数", "feedback", "评语"],
     lambda c, t, d: f"[成绩] {c}: {t} {d}".strip()),
    ("ANNOUNCEMENT", ["announcement", "公告", "notice", "通知", "reminder"],
     lambda c, t, d: f"[公告] {c}: {t}".strip()),
]


def classify(course, title, extra=""):
    hay = f"{title} {extra}".lower()
    for bucket, kws, fmt in RULES:
        if any(kw.lower() in hay for kw in kws):
            return bucket, fmt(course, title, extra)
    return "UNCLASSIFIED", ""


def parse_due(s):
    """解析 Moodle due 文本 → datetime；失败返回 None。
    兼容 'Monday, 22 June 2026, 10:00 AM' / '22 Jun 2026, 10:00' 等常见格式。"""
    s = re.sub(r"\s+", " ", s).strip()
    fmts = [
        "%A, %d %B %Y, %I:%M %p", "%d %B %Y, %I:%M %p", "%d %B %Y, %H:%M",
        "%A, %d %B %Y", "%d %b %Y, %I:%M %p", "%d %b %Y, %H:%M", "%Y-%m-%d %H:%M",
    ]
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def countdown(due_dt):
    """精确到小时和分钟的剩余时间 → '剩3天2小时15分'；过期→'已过期'。"""
    delta = due_dt - datetime.now()
    if delta.total_seconds() <= 0:
        return "已过期"
    d = delta.days
    h, rem = divmod(delta.seconds, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}天")
    if h: parts.append(f"{h}小时")
    if m or not parts: parts.append(f"{m}分")
    return "剩" + "".join(parts)


def main():
    signals, unclassified, notes = [], [], []
    scan_ok_courses, scan_expect = [], []

    if not REUSE:
        print("❌ 无法复用抓取器，中止", file=sys.stderr)
        sys.exit(2)

    scanner = MoodleClient()

    # 1) 登录 + 校验
    login_ok = scanner.login()
    note = "✅ 登录成功" if login_ok else "❌ 登录失败"
    if not login_ok:
        print(note, file=sys.stderr)
        sys.exit(2)

    # 2) 通知系统（每条都抓 + 校验条数；过滤用户名等系统残渣）
    notifs = scanner.check_notifications()
    for n in notifs:
        # 残渣过滤：单个纯字母 token 且 ≤15 字符 → 多为用户名/系统字段，非真实通知
        if n.split() and len(n.split()) == 1 and n.isascii() and n.isalpha() and len(n) <= 15:
            continue
        bucket, sig = classify("通知", n)
        if bucket == "UNCLASSIFIED":
            unclassified.append({"type": "notification", "text": n})
        elif sig:
            signals.append(sig)

    # 3) 扫每门课 + 校验课程扫全
    for key, info in COURSES.items():
        scan_expect.append(info["name"].split(" ")[0])
        try:
            r = scanner.scan_course(info["id"], info["name"], download_dir=info.get("path", "."))
            scan_ok_courses.append(info["name"].split(" ")[0])
            # 新文件 → DOWNLOAD 桶
            for fname in r.get("new_files", []):
                bucket, sig = classify(info["name"], fname)
                if sig:
                    signals.append(sig)
            # 作业 → 状态感知（已提交不推；有 due 才拼截止；成绩单列）
            for a_id, a in r.get("assignments", {}).items():
                name = a.get("name", "作业")
                due = a.get("due", "").strip()
                status = a.get("status", "")
                graded = a.get("graded", "")
                # 已提交/已交 → 不推（无新行动）
                if "已提交" in status or "已交" in status or "submitted" in status.lower():
                    continue
                # 有成绩且已评 → 成绩桶
                if graded and graded not in ("未评分", "") and "未" not in str(graded):
                    signals.append(f"[成绩] {info['name']}: {name} {graded}")
                    continue
                # 新作业：仅当实际有 due 或未提交才推；due 带精确倒计时
                if due or (status and status not in ("未知", "")):
                    due_dt = parse_due(due) if due else None
                    if due_dt:
                        tail = f" 截止 {due_dt.strftime('%m/%d %H:%M')} ({countdown(due_dt)})"
                    elif due:
                        tail = f" 截止 {due}"
                    else:
                        tail = ""
                    signals.append(f"[新作业] {info['name']}: {name}{tail}")
        except Exception as e:
            notes.append(f"扫描失败 {key}: {e}")

    # 4) 去重 + 截断到 5 行
    seen, deduped = set(), []
    for s in signals:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    top = deduped[:MAX_LINES]

    # 5) 写输出
    (OUT_DIR / "signals.txt").write_text("\n".join(top), encoding="utf-8")
    (FALLBACK).write_text(
        json.dumps(unclassified, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6) 四步校验报告
    course_check = "✅ 课程扫全" if set(scan_ok_courses) >= set(scan_expect) else \
        f"❌ 缺课 {sorted(set(scan_expect) - set(scan_ok_courses))}"
    verify_report = [
        f"登录: {'✅' if login_ok else '❌'}",
        f"通知抓取: {len(notifs)} 条 {'✅' if notifs else '(0,无通知)'}",
        f"下载落地: 见 signals.txt（新文件已抓）",
        course_check,
        f"行数: {len(top)} ≤ {MAX_LINES} {'✅' if len(top) <= MAX_LINES else '❌'}",
    ]
    (OUT_DIR / "verify_report.txt").write_text("\n".join(verify_report), encoding="utf-8")

    # 7) 打印紧凑信号（注入 agent 上下文）——无信号时输出心跳回执
    print("# Moodle 预处理结果")
    if top:
        print("\n".join(top))
    else:
        print(f"[Moodle] 已扫{len(scan_ok_courses)}课 无新内容")
    if unclassified:
        print(f"[兜底] {len(unclassified)} 条需 Agent 读取 out/unclassified_moodle.json")
    print("# 校验：" + " ; ".join(verify_report))


if __name__ == "__main__":
    main()

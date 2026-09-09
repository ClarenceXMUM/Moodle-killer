#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""moodle_prep.py —— 抓取 + 判断 + 按你选的风格输出。脚本先扛，Agent 兜底。

产出（默认在 ~/.moodle-killer/out/）：
  signals.txt              本次要推的信号（已按输出风格裁剪）
  unclassified_moodle.json 关键词没命中、留给 Agent 判断的条目
  verify_report.txt        四步校验（登录 / 抓取 / 下载 / 课程扫全）
  last_run.json            本次运行摘要（mk status 读它显示「最近一次」）

stdout 是给 cron / Agent 读的紧凑文本。输出风格由 config.yaml 的 output.mode 决定：
  heartbeat 天天报到（无事也报一句）｜ silent 无事完全不说话
  digest 每天一条汇总 ｜ urgent 只报 24h 内截止和新成绩 ｜ full 全都报

命令行：
  --dry-run      只跑不下载、不写状态（mk test 用）
  --no-download  不下载，只列文件名
  --json         输出结构化结果
  --quiet        静默模式（无内容时不输出任何东西）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import config_store as cs
except Exception as e:  # pragma: no cover
    print("[fatal] 找不到 config_store.py：%s" % e, file=sys.stderr)
    sys.exit(2)

try:
    from moodle_client import MoodleClient
    REUSE = True
except Exception as e:
    REUSE = False
    print("[warn] moodle_client 加载失败: %s" % e, file=sys.stderr)


RULES = [
    ("NEW_ASSIGNMENT", ["assignment", "作业", "due", "deadline", "截止", "submit", "提交", "project"]),
    ("DOWNLOAD", ["file", "resource", "附件", "下载", "pdf", "幻灯片", "slide", "note"]),
    ("GRADE", ["grade", "score", "成绩", "分数", "feedback", "评语"]),
    ("ANNOUNCEMENT", ["announcement", "公告", "notice", "通知", "reminder"]),
]

BUCKET_LABEL = {"NEW_ASSIGNMENT": "新作业", "DOWNLOAD": "新文件", "GRADE": "成绩",
                "ANNOUNCEMENT": "公告", "UNCLASSIFIED": "待判断"}


def classify(course, title, extra=""):
    hay = "%s %s" % (title, extra)
    hay = hay.lower()
    for bucket, kws in RULES:
        if any(kw.lower() in hay for kw in kws):
            return bucket
    return "UNCLASSIFIED"


def parse_due(s):
    s = re.sub(r"\s+", " ", s or "").strip()
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
    delta = due_dt - datetime.now()
    if delta.total_seconds() <= 0:
        return "已过期"
    d = delta.days
    h, rem = divmod(delta.seconds, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append("%d天" % d)
    if h:
        parts.append("%d小时" % h)
    if m or not parts:
        parts.append("%d分" % m)
    return "剩" + "".join(parts)


def next_run(times, now=None):
    """根据 delivery.schedule 算出下一次运行时间（用于心跳文案）。"""
    now = now or datetime.now()
    cands = []
    for t in times or []:
        try:
            hh, mm = [int(x) for x in t.split(":")]
        except Exception:
            continue
        today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        cands.append(today if today > now else today + timedelta(days=1))
    return min(cands) if cands else None


# ── 采集 ───────────────────────────────────────────────────────────────────
def collect(scanner, courses, cfg, do_download=True):
    """返回 (items, unclassified, meta)。items 是 dict 列表。"""
    items, unclassified = [], []
    scan_ok, scan_expect, notes = [], [], []
    notif_count = 0

    notifs = scanner.check_notifications()
    notif_count = len(notifs)
    for n in notifs:
        if n.split() and len(n.split()) == 1 and n.isascii() and n.isalpha() and len(n) <= 15:
            continue  # 系统残渣
        bucket = classify("通知", n)
        if bucket == "UNCLASSIFIED":
            unclassified.append({"type": "notification", "text": n})
        else:
            items.append({"bucket": bucket, "text": "[%s] 通知: %s" % (BUCKET_LABEL[bucket], n),
                          "course": "通知", "title": n})

    for key, info in courses.items():
        name = info.get("name", key)
        scan_expect.append(name.split(" ")[0])
        if info.get("mute"):
            continue
        try:
            r = scanner.scan_course(info["id"], name,
                                    download=do_download,
                                    download_dir=info.get("path", "."))
            scan_ok.append(name.split(" ")[0])
            for fname in r.get("new_files", []):
                tail = "已下载" if do_download else "（试跑：未下载）"
                items.append({"bucket": "DOWNLOAD",
                              "text": "[新文件] %s: %s %s" % (name, fname, tail),
                              "course": name, "title": fname})
            for a_id, a in r.get("assignments", {}).items():
                aname = a.get("name", "作业")
                due = (a.get("due") or "").strip()
                status = a.get("status", "") or ""
                graded = a.get("graded", "") or ""
                if "已提交" in status or "已交" in status or "submitted" in status.lower():
                    continue
                if graded and graded not in ("未评分", "") and "未" not in str(graded):
                    items.append({"bucket": "GRADE",
                                  "text": "[成绩] %s: %s %s" % (name, aname, graded),
                                  "course": name, "title": aname, "graded": graded})
                    continue
                if due or (status and status not in ("未知", "")):
                    due_dt = parse_due(due) if due else None
                    if due_dt:
                        tail = " 截止 %s (%s)" % (due_dt.strftime("%m/%d %H:%M"), countdown(due_dt))
                    elif due:
                        tail = " 截止 %s" % due
                    else:
                        tail = ""
                    items.append({"bucket": "NEW_ASSIGNMENT",
                                  "text": "[新作业] %s: %s%s" % (name, aname, tail),
                                  "course": name, "title": aname,
                                  "due_dt": due_dt.strftime("%Y-%m-%d %H:%M") if due_dt else None})
        except Exception as e:
            notes.append("扫描失败 %s: %s" % (key, e))

    # 去重（同一条文本只留一次）
    seen, deduped = set(), []
    for it in items:
        if it["text"] not in seen:
            seen.add(it["text"])
            deduped.append(it)

    meta = {
        "scanned": len(scan_ok), "expected": len(scan_expect),
        "missing": sorted(set(scan_expect) - set(scan_ok)),
        "notifications": notif_count, "notes": notes,
    }
    return deduped, unclassified, meta


# ── 输出风格 ───────────────────────────────────────────────────────────────
def select(items, mode, max_lines):
    """按风格挑出要展示的条目。返回 (shown_items, extra_lines)。"""
    if mode == "urgent":
        keep = []
        for it in items:
            if it["bucket"] == "GRADE":
                keep.append(it)
            elif it["bucket"] == "NEW_ASSIGNMENT" and it.get("due_dt"):
                try:
                    due = datetime.strptime(it["due_dt"], "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                if 0 <= (due - datetime.now()).total_seconds() <= 24 * 3600:
                    keep.append(it)
        return keep[:max_lines] if max_lines else keep, []
    if mode == "full":
        return items, []
    return items[:max_lines] if max_lines else items, []


def render(mode, items, meta, cfg, out_dir):
    """返回要打印的行列表（不含最后的校验行）。"""
    lines = []
    times = cs.get_path(cfg, "delivery.schedule") or []
    nxt = next_run(times)
    nxt_txt = nxt.strftime("%m/%d %H:%M") if nxt else "（没设定时）"

    if mode == "silent" and not items:
        return []  # 安静：什么都不说

    if mode == "digest":
        lines.append("[Moodle 日报 %s] 扫了 %d 门课，%d 条更新" %
                     (datetime.now().strftime("%m/%d"), meta["scanned"], len(items)))
        lines += [it["text"] for it in items]
        if not items:
            lines.append("· 今天没有新内容")
        return lines

    if mode == "heartbeat":
        if items:
            lines += [it["text"] for it in items]
            lines.append("[Moodle] 已扫%d课 · 下次 %s" % (meta["scanned"], nxt_txt))
        else:
            lines.append("[Moodle] 已扫%d课 无新内容 · 下次 %s" % (meta["scanned"], nxt_txt))
        return lines

    if mode == "urgent":
        if items:
            lines += [it["text"] for it in items]
        return lines

    # full 及其他
    lines += [it["text"] for it in items]
    return lines


# ── 主流程 ─────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--dry-run", action="store_true", help="不下载、不写状态")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    cfg = cs.load_config()
    out_dir = cs.out_dir()
    mode = cs.get_path(cfg, "output.mode") or "heartbeat"
    max_lines = int(cs.get_path(cfg, "output.max_lines") or 5)
    courses = cs.load_courses()
    started = datetime.now()

    if not REUSE:
        print("❌ 无法复用抓取器，中止", file=sys.stderr)
        return 2
    if not courses:
        print("❌ 还没选课。跑 `mk add` 或 `mk setup`", file=sys.stderr)
        return 2

    if cs.get_path(cfg, "advanced.paused") and not args.dry_run:
        _write_last_run(out_dir, started, "paused", [], mode)
        if not args.quiet:
            print("[Moodle] 已暂停推送（mk resume 恢复）", file=sys.stderr)
        return 0

    try:
        scanner = MoodleClient()
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2

    if not scanner.login():
        print("❌ 登录失败：账号或密码不对（mk doctor 看怎么修）", file=sys.stderr)
        _write_last_run(out_dir, started, "login_failed", [], mode)
        return 2

    if args.dry_run:
        # 试跑：拿真实状态的副本比对 —— 你看到的就等于真跑会推的；但绝不写回真实状态
        dry = out_dir / ".dryrun_state"
        if dry.exists():
            shutil.rmtree(dry)
        real = cs.state_dir()
        if real.exists():
            shutil.copytree(real, dry)
        else:
            os.makedirs(dry, exist_ok=True)
        scanner.state_dir = str(dry)

    items, unclassified, meta = collect(
        scanner, courses, cfg, do_download=not (args.dry_run or args.no_download))

    shown, _extra = select(items, mode, max_lines)
    lines = render(mode, shown, meta, cfg, out_dir)

    # 落盘
    (out_dir / "signals.txt").write_text("\n".join(it["text"] for it in shown), encoding="utf-8")
    (out_dir / "unclassified_moodle.json").write_text(
        json.dumps(unclassified, ensure_ascii=False, indent=2), encoding="utf-8")

    course_check = "✅ 课程扫全" if not meta["missing"] else "❌ 缺课 %s" % meta["missing"]
    verify_report = [
        "登录: ✅",
        "通知抓取: %d 条 %s" % (meta["notifications"], "✅" if meta["notifications"] else "(0,无通知)"),
        "下载落地: 见 signals.txt（%d 条信号）" % len(shown),
        course_check,
        "风格: %s ｜ 输出 %d 行" % (mode, len(lines)),
    ]
    if meta["notes"]:
        verify_report.append("异常: " + "; ".join(meta["notes"]))
    (out_dir / "verify_report.txt").write_text("\n".join(verify_report), encoding="utf-8")
    if not args.dry_run:
        _write_last_run(out_dir, started, "ok", shown, mode)

    if args.json:
        print(json.dumps({
            "mode": mode,
            "scanned": meta["scanned"],
            "signals": [it["text"] for it in shown],
            "total_signals": len(items),
            "unclassified": len(unclassified),
            "verify": verify_report,
            "dry_run": args.dry_run,
        }, ensure_ascii=False, indent=2))
        return 0

    if lines:
        if mode == "silent":
            print("\n".join(lines))
        else:
            print("# Moodle 预处理结果")
            print("\n".join(lines))
    elif not args.quiet:
        # silent 且无内容：明确给一个机器可读标记，cron 侧据此闭嘴
        print("[SILENT]")

    if unclassified and mode != "silent":
        print("[兜底] %d 条需 Agent 读取 out/unclassified_moodle.json" % len(unclassified))
    if meta["missing"] and mode != "silent":
        print("# 校验：" + course_check)
    return 0


def _write_last_run(out_dir, started, status, shown, mode):
    try:
        (out_dir / "last_run.json").write_text(json.dumps({
            "time": started.strftime("%Y-%m-%d %H:%M"),
            "status": status,
            "mode": mode,
            "signals": len(shown),
            "summary": "推送 %d 条" % len(shown) if status == "ok" else status,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())

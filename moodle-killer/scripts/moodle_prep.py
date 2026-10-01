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
from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import platform_support as ps
import teams_sync
ps.configure_console()

try:
    import config_store as cs
except Exception as e:  # pragma: no cover
    print("[fatal] 找不到 config_store.py：%s" % e, file=sys.stderr)
    sys.exit(2)

try:
    from moodle_client import MoodleClient, resolve_course_code
    REUSE = True
except Exception as e:
    REUSE = False

    def resolve_course_code(entry, course_name=""):
        """moodle_client 挂了时的兜底：只从课名抠课程代号（没客户端=不下载，仅日志用）。"""
        m = re.match(r"\s*([A-Za-z]{2,6})[\s-]?(\d{2,4})\b",
                     str((entry or {}).get("name") or course_name or ""))
        return "%s%s" % (m.group(1).upper(), m.group(2)) if m else ""

    print("[warn] moodle_client 加载失败: %s" % e, file=sys.stderr)


RULES = [
    ("NEW_ASSIGNMENT", ["assignment", "作业", "due", "deadline", "截止", "submit", "提交", "project"]),
    ("DOWNLOAD", ["file", "resource", "附件", "下载", "pdf", "幻灯片", "slide", "note"]),
    ("GRADE", ["grade", "score", "成绩", "分数", "feedback", "评语"]),
    ("ANNOUNCEMENT", ["announcement", "公告", "notice", "通知", "reminder"]),
]

BUCKET_LABEL = {"NEW_ASSIGNMENT": "新作业", "DOWNLOAD": "新文件", "GRADE": "成绩",
                "ANNOUNCEMENT": "公告", "NEW_ACTIVITY": "新活动", "UNCLASSIFIED": "待判断"}

# 活动类型的人话名（Moodle 的 /mod/<type>/ 直接用英文，报出来得翻一下）
ACTIVITY_TYPE_CN = {
    "page": "网页", "book": "图书", "url": "链接", "label": "标签", "quiz": "测验",
    "forum": "讨论区", "choice": "投票", "feedback": "问卷", "lesson": "课程",
    "wiki": "维基", "workshop": "互评", "scorm": "课件包", "h5pactivity": "互动内容",
    "imscp": "课件包", "glossary": "词汇表", "chat": "聊天", "data": "数据库",
    "bigbluebuttonbn": "在线课堂", "zoom": "在线课堂", "lti": "外部工具", "attendance": "考勤",
}


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
    # Moodle 中文界面及数字日期；只在日期、时间完整时解释，坏日期不猜。
    m = re.search(r"(?<!\d)(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*"
                  r"(\d{1,2})\s*日?\s*(?:[,，T]|星期[一二三四五六日天]|周[一二三四五六日天]|\s)*"
                  r"(上午|下午)?\s*(\d{1,2})\s*[:：]\s*(\d{2})(?!\d)", s)
    if m:
        year, month, day, period, hour, minute = m.groups()
        hour = int(hour)
        if period:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if period == "下午" else 0)
        try:
            return datetime(int(year), int(month), int(day), hour, int(minute))
        except ValueError:
            return None
    # 英文月份不依赖操作系统 locale（中文 Windows 的 %B 不一定认 September）。
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}),?\s+(\d{1,2}):(\d{2})\s*(AM|PM)?\b",
                  s, re.IGNORECASE)
    if m:
        day, month, year, hour, minute, period = m.groups()
        months = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
        try:
            hour = int(hour)
            if period:
                if not 1 <= hour <= 12:
                    return None
                hour = hour % 12 + (12 if period.lower() == "pm" else 0)
            return datetime(int(year), months.index(month.lower()[:3]) + 1, int(day), hour, int(minute))
        except ValueError:
            return None
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
def _apply_completion(scanner, scan_results, cfg):
    """把本轮真的下载到的活动勾成「已完成」（保持课程进度条）。

    安全设计（独立评审定稿）：
      - **只勾** resource / folder —— 也就是本轮真的取到文件的那两类活动
      - 只勾「本轮下载成功」的活动（`scan_course` 里只有成功才进 completion_targets），
        且必须出现在课程页的活动列表里（拿不到按钮的一律跳过）
      - 已完成 → 跳过；按钮不存在 → 跳过 + WARN（**不熔断**，那只是站点没开完成度）
      - 课程状态文件缺失 → 该课**拒绝写**（那是「全部判成新文件」的批量误勾场景）
      - 本轮应勾数 > 上限 → **一条都不写**，报出来让用户确认（全有或全无）
      - 串行 + 1~3 秒抖动；每条写后**回读**，回读明确为「未完成」→ 立即熔断剩余
      - 永不发 completed=False（客户端层面直接抛错）

    ⚠️ 跳过类提示走独立的 `warnings`，**绝不进 meta["notes"]** —— 那一路会让整轮判定
    「扫描失败」并压制推送。完成度的失败只影响它自己，不拖垮下载与推送。
    """
    import time
    warnings = []
    if not cs.get_path(cfg, "completion.mark_done"):
        return {"enabled": False}
    raw_max = cs.get_path(cfg, "completion.max_marks")
    max_marks = raw_max if isinstance(raw_max, int) and raw_max > 0 else 15

    per_course, total = [], 0
    for _key, cname, _lbl, res, err in scan_results:
        if err is not None or not isinstance(res, dict):
            continue
        targets = res.get("completion_targets") or {}
        if not targets:
            continue
        if not res.get("state_existed"):
            warnings.append("跳过 %s：该课扫描状态文件不存在，本轮文件全算新，拒绝批量勾" % cname)
            continue
        per_course.append((cname, res.get("course_id"), targets))
        total += len(targets)

    if total == 0:
        return {"enabled": True, "candidates": 0, "marked": [], "skipped": 0,
                "failed": [], "warnings": warnings}
    if total > max_marks:
        warnings.append("本轮应勾 %d 条，超过上限 %d → 一条都没勾，确认后调高 completion.max_marks"
                        % (total, max_marks))
        return {"enabled": True, "candidates": total, "marked": [], "skipped": 0,
                "failed": [], "warnings": warnings, "refused_over_cap": total}

    marked, skipped, failed, snapshots = [], 0, [], []
    aborted = False
    for cname, course_id, targets in per_course:
        states, names = scanner.get_course_completion(course_id)
        if not states:
            warnings.append("跳过 %s：读不到活动开关（站点可能没开完成度）" % cname)
            skipped += len(targets)
            snapshots.append({"course": cname, "course_id": course_id, "tracked": 0,
                              "before_done": 0, "after_done": 0, "skipped": len(targets),
                              "marked": 0, "at": datetime.now().isoformat(timespec="seconds")})
            continue
        before = sum(1 for v in states.values() if v)
        c_marked = c_skipped = 0
        for cmid, aname in targets.items():
            label = names.get(cmid) or aname
            if cmid not in states:
                warnings.append("跳过「%s」：页面上没有可勾的开关" % label)
                skipped += 1
                c_skipped += 1
                continue
            if states[cmid] is True:
                skipped += 1                      # 本来就完成，不发请求
                c_skipped += 1
                continue
            if aborted:
                skipped += 1
                c_skipped += 1
                continue
            ok, detail = scanner.set_activity_completion(cmid)
            if not ok and "not_toggleable" in detail:
                warnings.append("跳过「%s」：不支持手动勾（%s）" % (label, detail))
                skipped += 1
                c_skipped += 1
                continue
            if not ok:
                failed.append("%s（%s）" % (label, detail))
                warnings.append("❌ 「%s」写失败：%s" % (label, detail))
                continue
            # 写后回读：只有明确「未完成」才熔断；读不到按钮不算失败
            states2, _ = scanner.get_course_completion(course_id)
            if states2.get(cmid) is False:
                aborted = True
                warnings.append("❌ 「%s」写后回读仍是未完成 → 熔断本轮剩余勾选" % label)
                failed.append("%s（回读不符）" % label)
                continue
            marked.append({"cmid": cmid, "name": label, "course": cname})
            c_marked += 1
            time.sleep(1 + (cmid % 3))            # 串行 + 1~3 秒抖动，别像脚本扫射
        states_after, _ = scanner.get_course_completion(course_id)
        after = sum(1 for v in states_after.values() if v)
        snapshots.append({"course": cname, "course_id": course_id,
                          "tracked": len(states_after), "before_done": before, "after_done": after,
                          "skipped": c_skipped, "marked": c_marked,
                          "at": datetime.now().isoformat(timespec="seconds")})
    if snapshots:
        try:
            p = cs.state_dir() / "completion_snapshots.jsonl"
            os.makedirs(str(cs.state_dir()), exist_ok=True)
            with open(str(p), "a", encoding="utf-8") as f:
                for s in snapshots:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")
        except OSError as e:
            warnings.append("快照写不进去（%s）" % e)
    return {"enabled": True, "candidates": total, "marked": marked, "skipped": skipped,
            "failed": failed, "snapshots": snapshots, "aborted": aborted, "warnings": warnings}


def collect(scanner, courses, cfg, do_download=True):
    """返回 (items, unclassified, meta)。items 是 dict 列表。"""
    items, unclassified = [], []
    scan_ok, scan_expect, notes = [], [], []
    notif_count = 0

    notifs = scanner.check_notifications() if scanner is not None else []
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

    active_courses = [(key, info) for key, info in courses.items() if not info.get("mute")]

    def _scan_one(k, inf):
        cname = inf.get("name", k)
        exp_lbl = cname.split(" ")[0]
        try:
            res = scanner.scan_course(inf["id"], cname,
                                      download=do_download,
                                      download_dir=inf.get("path", "."),
                                      course_code=resolve_course_code(inf, cname))
            return (k, cname, exp_lbl, res, None)
        except Exception as ex:
            return (k, cname, exp_lbl, None, ex)

    if len(active_courses) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(active_courses))) as executor:
            futures = [executor.submit(_scan_one, k, inf) for k, inf in active_courses]
            scan_results = [f.result() for f in futures]
    else:
        scan_results = [_scan_one(k, inf) for k, inf in active_courses]

    for key, name, exp_lbl, r, err in scan_results:
        scan_expect.append(exp_lbl)
        if err is not None:
            notes.append("扫描失败 %s: %s" % (key, err))
            continue
        scan_ok.append(exp_lbl)
        notes.extend(r.get("notes", []))
        for fname in r.get("new_files", []):
            tail = "已下载" if do_download else "（试跑：未下载）"
            items.append({"bucket": "DOWNLOAD",
                          "text": "[新文件] %s: %s %s" % (name, fname, tail),
                          "course": name, "title": fname})
        for iid, info in (r.get("new_activities") or {}).items():
            kind = ACTIVITY_TYPE_CN.get(info.get("type") or "", info.get("type") or "活动")
            items.append({"bucket": "NEW_ACTIVITY",
                          "text": "[新活动] %s: %s %s" % (name, kind, info.get("name", "")),
                          "course": name, "title": info.get("name", "")})
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

    # OneDrive sync supplies local Teams files; use the same signals and
    # verification path as Moodle downloads.
    try:
        teams_sources = teams_sync.load_sources()
    except (OSError, ValueError) as exc:
        teams_sources = {}
        notes.append("Teams 配置读取失败: %s" % exc)
    for key, info in teams_sources.items():
        if not isinstance(info, dict):
            notes.append("Teams 配置无效 %s: 应为对象" % key)
            continue
        if info.get("mute"):
            continue
        name = info.get("name", key)
        scan_expect.append(name)
        try:
            result = teams_sync.scan_source(key, info, download=do_download)
        except (OSError, ValueError, KeyError) as exc:
            notes.append("Teams 扫描失败 %s: %s" % (name, exc))
            continue
        scan_ok.append(name)
        notes.extend(result["notes"])
        for fname in result["new_files"]:
            tail = "已同步" if do_download else "（试跑：未同步）"
            items.append({"bucket": "DOWNLOAD",
                          "text": "[新文件] %s: %s %s" % (name, fname, tail),
                          "course": name, "title": fname})

    # 勾 Done：只在真跑（do_download）时做——试跑绝不碰账号
    completion = {"enabled": False, "dry_run": True}
    if do_download:
        try:
            completion = _apply_completion(scanner, scan_results, cfg)
        except Exception as exc:                 # 勾选出问题绝不拖垮下载与推送
            completion = {"enabled": True, "error": str(exc), "warnings": ["❌ 环节异常：%s" % exc]}

    # 去重（同一条文本只留一次）
    seen, deduped = set(), []
    for it in items:
        if it["text"] not in seen:
            seen.add(it["text"])
            deduped.append(it)

    has_teams = any(isinstance(info, dict) and not info.get("mute") for info in teams_sources.values())
    source_label = "Moodle+Teams" if scan_results and has_teams else ("Teams" if has_teams else "Moodle")
    meta = {
        "scanned": len(scan_ok), "expected": len(scan_expect),
        "missing": sorted(set(scan_expect) - set(scan_ok)),
        "notifications": notif_count, "notes": notes,
        "source_label": source_label,
        "completion": completion,
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
    if max_lines and mode in ("heartbeat", "digest"):
        return items[:max(0, max_lines - 1)], []
    return items[:max_lines] if max_lines else items, []


def render(mode, items, meta, cfg, out_dir):
    """返回要打印的行列表（不含最后的校验行）。"""
    lines = []
    source_label = meta.get("source_label", "Moodle")
    hidden = meta.get("hidden_items", 0)
    more = " · 另%d条见 %s" % (hidden, out_dir / "all_signals.txt") if hidden else ""
    times = cs.get_path(cfg, "delivery.schedule") or []
    nxt = next_run(times)
    nxt_txt = nxt.strftime("%m/%d %H:%M") if nxt else "（没设定时）"

    if mode == "silent" and not items:
        return []  # 安静：什么都不说

    if mode == "digest":
        lines.append("[%s 日报 %s] 扫了 %d 个来源，%d 条更新%s" %
                     (source_label, datetime.now().strftime("%m/%d"), meta["scanned"],
                      meta.get("total_items", len(items)), more))
        lines += [it["text"] for it in items]
        if not items:
            lines.append("· 今天没有新内容")
        return lines

    if mode == "heartbeat":
        if items:
            lines += [it["text"] for it in items]
            lines.append("[%s] 已扫%d课%s · 下次 %s" % (source_label, meta["scanned"], more, nxt_txt))
        else:
            lines.append("[%s] 已扫%d课 无新内容 · 下次 %s" % (source_label, meta["scanned"], nxt_txt))
        return lines

    if mode == "urgent":
        if items:
            lines += [it["text"] for it in items]
        return lines

    # full 及其他
    lines += [it["text"] for it in items]
    return lines


# ── 主流程 ─────────────────────────────────────────────────────────────────
def attach_new_courses(scanner, cfg, courses, download=True):
    """把「本学期但还没登记」的课自动接上 → 返回 (added, notes)。

    判据只认 Moodle 自己的学期分类（inprogress）。**读不到就什么都不做**——
    日常跑绝不能因为接口抽风就乱接课。接上后立刻扫一遍（真下载 + 写状态基线），
    但调用方只报一行汇总：新课首轮几十个文件，逐条报会把推送淹掉。
    """
    timeline = scanner.courses_timeline()
    # 非 dict = 接口没给出可用结果（含老客户端 / 打桩对象）→ 什么都不做
    if not isinstance(timeline, dict):
        return [], []
    inprogress = [c for c in (timeline.get("inprogress") or []) if isinstance(c, dict)]
    if not inprogress:
        return [], []
    known = {str(v.get("id")) for v in courses.values() if isinstance(v, dict)}
    # mk rm 摘掉的课别再自己回来（否则「移除」是假的）
    ignored = cs.load_ignored()
    fresh = [c for c in inprogress
             if str(c.get("id")) not in known and str(c.get("id")) not in ignored]
    if not fresh:
        return [], []

    from html import unescape
    try:
        teams_sources = teams_sync.load_sources()
    except (OSError, ValueError):
        teams_sources = {}
    taken = cs.seen_course_dirs(courses, teams_sources)
    pending = []
    for c in fresh:
        name = re.sub(r"\s+", " ", unescape(str(c.get("fullname") or c.get("shortname") or ""))).strip()
        if not name:
            continue
        short = re.sub(r"\s+", " ", unescape(str(c.get("shortname") or ""))).strip()
        code = resolve_course_code({"name": name, "shortname": short}, name) or ""
        # 新目录按「课程文件夹命名模板」起名（默认 {code} {name} → 「MAT203 Statistics」）
        path = cs.guess_course_dir(courses, cfg, name, code, teams_sources, taken=taken,
                                   folder_name=cs.render_folder_name(
                                       {"name": name, "code": code, "shortname": short}, cfg=cfg))
        key = cs.course_key(name, c.get("id"))
        entry = {"id": c.get("id"), "name": name, "path": path, "mute": False}
        if short:
            entry["shortname"] = short
        if code:
            entry["code"] = code
        courses[key] = entry
        taken = taken + [(os.path.normpath(path), code.upper())]
        pending.append((key, entry))
    if not pending:
        return [], []
    cs.save_courses(courses)

    added, notes = [], []
    for _key, entry in sorted(pending, key=lambda kv: kv[1]["name"]):
        try:
            res = scanner.scan_course(entry["id"], entry["name"], download=download,
                                      download_dir=entry["path"], course_code=entry.get("code") or "")
        except Exception as exc:
            notes.append("接上 %s 后首次扫描失败: %s" % (entry["name"], exc))
            added.append({"name": entry["name"], "path": entry["path"], "files": None,
                          "course_id": entry["id"]})
            continue
        added.append({"name": entry["name"], "path": entry["path"],
                      "files": len(res.get("new_files") or []),
                      "state_existed": bool(res.get("state_existed")), "course_id": entry["id"]})
    return added, notes


def attach_summary_item(info):
    """自动接课那一行汇总（新课首轮不逐条报，但必须让人看见接了什么、落在哪）。"""
    path = str(info.get("path") or "")
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    if info.get("files") is None:
        return "[已接上] %s: 本学期新课已接上，首次扫描失败（见校验行）→ %s" % (info["name"], path)
    if info.get("files"):
        return "[已接上] %s: 本学期新课，已拉了 %d 个文件 → %s" % (info["name"], info["files"], path)
    if info.get("state_existed"):
        # 之前接上过、只是不在配置里了：课件早拉过，别说成「没有文件」
        return "[已接上] %s: 本学期新课（课件之前已拉过，本轮没有新文件）→ %s" % (info["name"], path)
    return "[已接上] %s: 本学期新课（这门课暂时没有文件）→ %s" % (info["name"], path)


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
    try:
        teams_sources = teams_sync.load_sources()
    except (OSError, ValueError) as exc:
        print("❌ Teams 配置读取失败: %s" % exc, file=sys.stderr)
        return 2
    started = datetime.now()

    if courses and not REUSE:
        print("❌ 无法复用抓取器，中止", file=sys.stderr)
        return 2
    active_teams = [info for info in teams_sources.values()
                    if isinstance(info, dict) and not info.get("mute")]
    if not courses and not active_teams:
        print("❌ 尚未配置 Moodle 课程或 Teams 来源。跑 `mk add` / `mk setup`，或参考 references/TEAMS-SYNC.md", file=sys.stderr)
        return 2

    scanner = None
    if courses:
        try:
            scanner = MoodleClient()
        except SystemExit as e:
            print(str(e), file=sys.stderr)
            return 2

        with contextlib.redirect_stdout(sys.stderr if args.json else sys.stdout):
            logged_in = scanner.login()
        if not logged_in:
            print("❌ 登录失败：账号或密码不对（mk doctor 看怎么修）", file=sys.stderr)
            if not args.dry_run:
                _write_last_run(out_dir, started, "login_failed", [], mode)
            return 2

    # 代号查证：还没确认代号的课（课名里没有、Moodle 官方编号为空）→ 到课程资料里找
    # 「Course Code」字段（老师传的 syllabus），找到就记下来并从下一轮起不再查。
    code_notes = []
    if not args.dry_run:
        try:
            import course_code as cc
            try:
                _teams = teams_sync.load_sources()
            except (OSError, ValueError):
                _teams = {}
            pending = {k: v for k, v in courses.items()
                       if isinstance(v, dict) and not v.get("code")}
            if pending:
                changed = [row for row in cc.enrich(pending, cfg, _teams, deep=True, write=True)
                           if row["changed"]]
                if changed:
                    cs.save_courses(courses)      # enrich 是就地改的，courses 里已经是最新的
                    for row in changed:
                        code_notes.append("%s → %s（%s）" % (row["name"], row["code"], row["why"]))
        except Exception as exc:
            code_notes.append("❌ 代号查证出错：%s" % exc)

    # 自动接课：本学期在 Moodle 上但还没登记的课，接上并拉一次基线（试跑绝不碰配置）
    attached, attach_notes = [], []
    if scanner is not None and not args.dry_run:
        with contextlib.redirect_stdout(sys.stderr if args.json else sys.stdout):
            try:
                attached, attach_notes = attach_new_courses(
                    scanner, cfg, courses, download=not args.no_download)
            except Exception as exc:
                attach_notes = ["自动接课失败: %s" % exc]

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
        if scanner is not None:
            scanner.state_dir = str(dry)

    with contextlib.redirect_stdout(sys.stderr if args.json else sys.stdout):
        items, unclassified, meta = collect(
            scanner, courses, cfg, do_download=not (args.dry_run or args.no_download))

    meta["attach_notes"] = attach_notes
    meta["code_notes"] = code_notes
    for info in reversed(attached):
        items.insert(0, {"bucket": "ATTACHED", "text": attach_summary_item(info)})

    shown, _extra = select(items, mode, max_lines)
    meta["total_items"] = len(items)
    meta["hidden_items"] = len(items) - len(shown) if mode in ("heartbeat", "digest") else 0
    # 试跑的产出单独放：out/ 是「上一次真跑发生了什么」的记录，绝不能被测试覆盖。
    report_dir = out_dir / ".dryrun" if args.dry_run else out_dir
    if report_dir != out_dir:
        os.makedirs(report_dir, exist_ok=True)
    lines = render(mode, shown, meta, cfg, report_dir)
    if mode != "full":
        # 心跳/日报标题也计入五行契约。
        lines = lines[:max_lines]

    # 落盘
    (report_dir / "signals.txt").write_text("\n".join(it["text"] for it in shown), encoding="utf-8")
    (report_dir / "all_signals.txt").write_text("\n".join(it["text"] for it in items), encoding="utf-8")
    (report_dir / "unclassified_moodle.json").write_text(
        json.dumps(unclassified, ensure_ascii=False, indent=2), encoding="utf-8")

    course_check = "✅ 课程扫全" if not meta["missing"] else "❌ 缺课 %s" % meta["missing"]
    verify_report = [
        "登录: ✅" if scanner is not None else "登录: 跳过（仅 Teams 本地来源）",
        ("通知抓取: %d 条 %s" % (meta["notifications"], "✅" if meta["notifications"] else "(0,无通知)"))
        if scanner is not None else "通知抓取: 跳过（仅 Teams 本地来源）",
        "下载落地: 见 signals.txt（%d 条信号）" % len(shown),
        course_check,
        "风格: %s ｜ 输出 %d 行" % (mode, len(lines)),
    ]
    if meta["notes"]:
        verify_report.append("异常: ❌ " + "; ".join(meta["notes"]))

    # 先校验再投递。JSON 只是输出格式，不能绕过真实运行的发送步骤。
    status, exit_code = "ok", 0
    delivery = "试跑，不发送" if args.dry_run else "无内容，未发送"
    if meta["missing"] or meta["notes"]:
        status, exit_code = "scan_failed", 2
        delivery = "❌ 扫描校验失败，未发送"
    elif not args.dry_run:
        import sender
        channel, _why = sender.detect_channel(cfg)
        if cs.get_path(cfg, "advanced.paused"):
            status, delivery = "paused", "已暂停推送"
        elif not cs.get_path(cfg, "delivery.weekend") and started.weekday() >= 5:
            delivery = "周末不推送"
        elif channel == "none":
            delivery = "通道 none，未发送"
        elif lines and lines != ["[SILENT]"]:
            sent, delivery = sender.send_text("\n".join(lines), title="Moodle 学业提醒",
                                              channel=channel, cfg=cfg)
            if not sent:
                status, exit_code = "send_failed", 1
                delivery = "❌ " + delivery
    # 完成度（勾 Done）：逐条可追溯；跳过类只记报告，真失败必须透出（静默 ≠ 没事）
    comp = meta.get("completion") or {}
    if comp.get("enabled"):
        for s in comp.get("snapshots") or []:
            pct_b = (100.0 * s["before_done"] / s["tracked"]) if s["tracked"] else 0.0
            pct_a = (100.0 * s["after_done"] / s["tracked"]) if s["tracked"] else 0.0
            verify_report.append("完成度: %s %d/%d → %d/%d（%.0f%% → %.0f%%）｜已勾 %d ｜跳过 %d" % (
                s["course"], s["before_done"], s["tracked"], s["after_done"], s["tracked"],
                pct_b, pct_a, s.get("marked", 0), s.get("skipped", 0)))
        if not (comp.get("snapshots") or []):
            # 无事也必须显式出现：否则分不清「功能关了 / 坏了 / 真的没新文件」
            if comp.get("warnings") or comp.get("refused_over_cap"):
                # 有警告时不能说「没有新课件」——那会让人以为功能没生效，
                # 实际是这一轮被拒绝/跳过了（原因在同一条报告下面的 warnings 里）
                verify_report.append("完成度: 本轮未动完成度（已勾 0 ｜拒绝或跳过，原因见下）")
            else:
                verify_report.append("完成度: 本轮没有新下的课件，未动完成度（应勾 %d ｜已勾 0 ｜跳过 %d ｜失败 %d）" % (
                    comp.get("candidates", 0), comp.get("skipped", 0), len(comp.get("failed") or [])))
        if comp.get("marked"):
            verify_report.append("已勾 Done: " + "；".join(
                "%s(#%d)" % (m["name"], m["cmid"]) for m in comp["marked"][:8]))
        for w in comp.get("warnings") or []:
            verify_report.append("完成度: " + w)
        problems = [w for w in (comp.get("warnings") or []) if str(w).startswith("❌")]
        if problems and not args.json:
            # 抄到 stdout 上：定时任务里的 Agent 只看得到脚本输出，报告文件它看不到
            print("完成度 ❌ " + "；".join(str(x) for x in problems[:3]))
    elif comp.get("dry_run"):
        verify_report.append("完成度: 试跑，不动完成度")
    else:
        # 关了也要说一声：否则运维时分不清「关了」和「坏了」
        verify_report.append("完成度: 功能已关闭（completion.mark_done=false）")
    for note in meta.get("code_notes") or []:
        verify_report.append("课程代号: " + str(note))
    for note in meta.get("attach_notes") or []:
        verify_report.append("接课: ❌ " + str(note))
    if (meta.get("attach_notes") or []) and not args.json:
        # 定时任务里的 Agent 只看得到 stdout，报告文件它看不到
        print("接课 ❌ " + "；".join(str(x) for x in meta["attach_notes"][:3]))
    verify_report.append("投递: " + delivery)
    (report_dir / "verify_report.txt").write_text("\n".join(verify_report), encoding="utf-8")
    if not args.dry_run:
        _write_last_run(out_dir, started, status, shown, mode, delivery)
    elif not args.quiet and not args.json:
        print("（试跑：产出写在 %s，正常扫描的记录一个字都没动）" % report_dir)
    if exit_code:
        print(delivery, file=sys.stderr)

    if args.json:
        print(json.dumps({
            "mode": mode,
            "scanned": meta["scanned"],
            "signals": [it["text"] for it in shown],
            "total_signals": len(items),
            "unclassified": len(unclassified),
            "verify": verify_report,
            "dry_run": args.dry_run,
            "delivery": delivery,
            "status": status,
        }, ensure_ascii=False, indent=2))
        return exit_code

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
    return exit_code


def _write_last_run(out_dir, started, status, shown, mode, delivery=""):
    try:
        (out_dir / "last_run.json").write_text(json.dumps({
            "time": started.strftime("%Y-%m-%d %H:%M"),
            "status": status,
            "mode": mode,
            "signals": len(shown),
            "summary": delivery or status,
            # 谁跑的：空 = 定时任务；fresh = 手动完整重扫（mk fresh）
            "source": (os.environ.get("MOODLE_KILLER_SOURCE") or "").strip(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())

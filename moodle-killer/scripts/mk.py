#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mk —— Moodle-killer 的唯一命令行入口（也供 Agent 直接调用）。

记这几条就够了：
  mk              看现在什么情况（= mk status）
  mk setup        一步步配好（分块问询，随时可停）
  mk set 时间 07:00   改一项（说人话的键名也认）
  mk add / mk rm      加课 / 删课
  mk test         试跑一次，不推送（不写状态）
  mk fresh        立刻完整重扫一次（真跑，不占定时名额）
  mk doctor       体检，哪坏了直接告诉你

其余：mk output / mk channel / mk naming / mk mute / mk schedule / mk pause / mk resume / mk install / mk help
所有命令都支持 --json，方便 Agent 解析。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_support as ps  # noqa: E402
ps.configure_console()
import config_store as cs  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)


# ── 输出小工具 ─────────────────────────────────────────────────────────────
def _c(code, text):
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def ok(msg):
    print("✅ " + msg)


def warn(msg):
    print("⚠️  " + msg)


def bad(msg):
    print("❌ " + msg)


def _fmt(val):
    if isinstance(val, bool):
        return "是" if val else "否"
    if isinstance(val, list):
        return ", ".join(str(x) for x in val)
    return str(val)


def _mask(val):
    s = str(val or "")
    if not s:
        return "（没填）"
    if len(s) <= 4:
        return "****"
    return s[:2] + "****" + s[-2:]


# ── status ─────────────────────────────────────────────────────────────────
def cmd_status(args):
    cfg = cs.load_config()
    courses = cs.load_courses()
    try:
        import teams_sync
        teams_sources = teams_sync.load_sources()
    except (OSError, ValueError):
        teams_sources = {}
    paused = bool(cs.get_path(cfg, "advanced.paused"))
    mode = cs.get_path(cfg, "output.mode")
    ch = cs.get_path(cfg, "delivery.channel")
    times = cs.get_path(cfg, "delivery.schedule") or []

    if args.json:
        print(json.dumps({
            "home": str(cs.home()),
            "credentials": cs.credentials_ok(cfg),
            "moodle_url": cs.get_path(cfg, "moodle.url"),
            "moodle_user": cs.get_path(cfg, "moodle.user"),
            "courses": [{"key": k, "name": v.get("name"), "path": v.get("path"),
                         "mute": bool(v.get("mute"))} for k, v in courses.items()],
            "teams_sources": [{"key": k, "name": v.get("name", k), "source": v.get("source"),
                               "path": v.get("path"), "mute": bool(v.get("mute"))}
                              for k, v in teams_sources.items()],
            "output_mode": mode,
            "channel": ch,
            "schedule": times,
            "weekend": cs.get_path(cfg, "delivery.weekend"),
            "paused": paused,
            "download_root": cs.get_path(cfg, "download.root"),
            "download_by_type": cs.get_path(cfg, "download.by_type"),
            "download_naming": cs.get_path(cfg, "download.naming"),
            "download_name_template": cs.get_path(cfg, "download.name_template"),
            "last_run": _last_run(),
            "install": _install_info(),
        }, ensure_ascii=False, indent=2))
        return 0

    print(_c("1", "\n  Moodle-killer 现在的情况"))
    print(_c("90", "  数据目录：%s" % cs.home()))

    if not courses and teams_sources:
        print("\n  账号      Moodle 未配置（仅 Teams 本地来源）")
    else:
        creds = "已填" if cs.credentials_ok(cfg) else "缺"
        print("\n  账号      %s ｜ %s ｜ %s" % (
            cs.get_path(cfg, "moodle.url"), cs.get_path(cfg, "moodle.user") or "（没填学号）",
            ("密码" + creds)))
    print("  课程      %d 门" % len(courses))
    for k, v in list(courses.items())[:8]:
        mute = "（静音）" if v.get("mute") else ""
        print("            · %s%s → %s" % (v.get("name"), mute, v.get("path")))
    if len(courses) > 8:
        print("            · …还有 %d 门" % (len(courses) - 8))
    print("  Teams     %d 个本地同步来源" % len(teams_sources))
    for key, info in list(teams_sources.items())[:8]:
        print("            · %s%s → %s" % (info.get("name", key),
              "（静音）" if info.get("mute") else "", info.get("path", "（未设置）")))

    real_ch, why = str(ch), ""
    try:
        import sender
        _rc, why = sender.detect_channel(cfg)
        real_ch = str(_rc)
    except Exception:
        pass
    ch_label = cs.CHANNEL_LABELS.get(real_ch, real_ch)
    if why:
        ch_label += "（%s）" % why
    print("\n  推送      每天 %s ｜ %s ｜ 周末%s" % (
        ", ".join(times) or "（没设）", ch_label,
        "推" if cs.get_path(cfg, "delivery.weekend") else "不推"))
    print("  风格      %s（%s）" % (cs.MODE_LABELS.get(mode, mode), mode))
    nm = str(cs.get_path(cfg, "download.naming") or "default").strip().lower()
    nm_label = cs.NAMING_LABELS.get(nm, nm)
    if nm == "custom":
        nm_label += "：%s" % (cs.get_path(cfg, "download.name_template") or "")
    print("  命名      %s（%s）" % (nm_label, nm))
    if paused:
        print("  状态      " + _c("33", "已暂停推送（mk resume 恢复）"))
    own = [v.get("path") for v in courses.values() if v.get("path")]
    if courses and own and len(own) == len(courses):
        print("  下载      每门课各自指定（见上）｜ 按类型分=%s" % _fmt(cs.get_path(cfg, "download.by_type")))
    else:
        print("  下载      %s ｜ 按类型分=%s" % (
            cs.download_root(cfg), _fmt(cs.get_path(cfg, "download.by_type"))))

    last = _last_run()
    if last:
        src_tag = "（手动重扫）" if last.get("source") == "fresh" else ""
        print("\n  最近一次  %s%s ｜ %s" % (last.get("time", "?"), src_tag, last.get("summary", "")))
    else:
        print("\n  最近一次  " + _c("90", "还没跑过（mk test 试一下）"))

    inst = _install_info()
    if inst:
        print("  安装位置  %s" % "、".join(_harness_labels(inst.get("harnesses", []))))
    print(_c("90", "\n  想改哪项：mk set <项目> <值>    看全部项目：mk set\n"))
    return 0


def _last_run():
    p = cs.out_dir() / "last_run.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _install_info():
    p = cs.home() / "install.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _harness_labels(keys):
    """把 harness 的 key（hermes/claude…）翻成人看的名字。"""
    try:
        import harness_install as hi
        table = hi.HARNESSES
    except Exception:
        table = {}
    return [table.get(k, {}).get("label", k) for k in (keys or [])]


# ── set / get ──────────────────────────────────────────────────────────────
def cmd_set(args):
    cfg = cs.load_config()
    if not args.key:
        only_adv = getattr(args, "advanced", False)
        if only_adv:
            print(_c("1", "\n  高级设置（一般不用动，改了会立刻生效）：\n"))
        else:
            print(_c("1", "\n  能改的项目（点左边的名字即可）：\n"))
        for dotted, meta in cs.KEY_META.items():
            if bool(meta.get("advanced")) != only_adv:
                continue
            cur = cs.get_path(cfg, dotted)
            if meta.get("type") == "secret":
                cur = _mask(cur)
            print("  %-28s %-22s 现在：%s" % (dotted, meta.get("label", ""), _fmt(cur)))
        if only_adv:
            print(_c("90", "\n  例：mk set 按类型 true ｜ mk set 保留天数 60 ｜ mk set 严格 false"))
            print(_c("90", "  其他项：mk set\n"))
        else:
            print(_c("90", "\n  例：mk set 时间 07:00 ｜ mk set output silent ｜ mk set 密码 xxx"))
            print(_c("90", "  高级项（按类型分文件夹等）：mk set --advanced\n"))
        return 0

    try:
        dotted = cs.normalize_key(args.key)
    except KeyError as e:
        bad(str(e))
        return 2

    if args.value is None:
        cur = cs.get_path(cfg, dotted)
        if cs.KEY_META.get(dotted, {}).get("type") == "secret":
            cur = _mask(cur)
        meta = cs.KEY_META.get(dotted, {})
        print("%s（%s）= %s" % (dotted, meta.get("label", ""), _fmt(cur)))
        if meta.get("choices"):
            print("可选：" + " / ".join(meta["choices"]))
        if meta.get("example"):
            print("例：" + meta["example"])
        return 0

    try:
        val = cs.coerce(dotted, args.value)
    except (ValueError, TypeError) as e:
        bad(str(e))
        return 2
    cs.set_path(cfg, dotted, val)
    cs.save_config(cfg)
    ok("%s → %s" % (cs.KEY_META.get(dotted, {}).get("label", dotted), _fmt(val)))

    if dotted == "delivery.schedule":
        _sync_scheduler(times=val)
    if dotted == "moodle.password" or dotted == "moodle.user" or dotted == "moodle.url":
        okk, msg = _quick_login()
        print(("   ✅ " if okk else "   ❌ ") + msg)
    return 0


# ── add / rm ───────────────────────────────────────────────────────────────
def _slug(name, cid=None):
    return cs.course_key(name, cid)


def cmd_add(args):
    import onboarding
    cfg = cs.load_config()
    if not cs.credentials_ok(cfg):
        bad("还没配账号。先跑 mk setup（或 mk set 账号 xxx / mk set 密码 xxx）")
        return 2
    if not args.names:
        if not sys.stdin.isatty():
            try:
                from moodle_client import MoodleClient
                client = MoodleClient()
                # 列课程前必须先登录，否则拿到的是登录页，永远列出空列表。
                with contextlib.redirect_stdout(sys.stderr):
                    logged_in = client.login()
                if not logged_in:
                    bad("登录失败，先检查账号（mk doctor）")
                    return 2
                found = client.discover_courses()
            except SystemExit as e:
                bad(str(e))
                return 2
            except Exception as e:
                bad("拿课程列表失败：%s" % e)
                return 2
            if not found:
                warn("没拿到课程列表（可能是学校页面结构不同，或本学期还没放出课）")
                return 2
            if args.json:
                print(json.dumps(found, ensure_ascii=False, indent=2))
                return 0
            # 带上课程 ID：同名课程（如多个分组）只能靠 ID 区分。
            order = {"inprogress": 0, "future": 1, "unknown": 2, "past": 3}
            tags = {"inprogress": "本学期", "future": "下学期（还没开始）",
                    "past": "已结束", "unknown": "学期未知"}
            print("可选课程（用 mk add <名字> 添加）：")
            for c in sorted(found, key=lambda x: order.get(x.get("timeline"), 9)):
                tag = tags.get(c.get("timeline"), "")
                print("  · %-16s %s (id=%s)" % ("[%s]" % tag if tag else "", c.get("name"), c.get("id")))
            print(_c("90", "  「学期未知」的是 /my/ 页面里的旧课（接口不认它属于哪个学期），本学期的课优先。"))
            return 0
        okk, msg = onboarding.discover_and_pick(interactive=True)
        print(("✅ " if okk else "❌ ") + msg)
        return 0 if okk else 2

    try:
        from moodle_client import MoodleClient, resolve_course_code
        client = MoodleClient()
        if not client.login():
            bad("登录失败，先检查账号（mk doctor）")
            return 2
        found = client.discover_courses()
    except SystemExit as e:
        bad(str(e))
        return 2
    except Exception as e:
        bad("拿课程列表失败：%s" % e)
        return 2

    courses = cs.load_courses()
    base = os.path.expanduser(cs.get_path(cfg, "download.root") or "~/School")
    added, missed = [], []
    for name in args.names:
        hit = None
        for c in found:
            if name.lower() in (c.get("name") or "").lower():
                hit = c
                break
        if hit is None:
            missed.append(name)
            continue
        key = _slug(hit["name"], hit.get("id"))
        courses.setdefault(key, {})
        courses[key].update({"id": hit["id"], "name": hit["name"]})
        courses[key].setdefault("path", os.path.join(base, hit["name"], ""))
        courses[key].setdefault("mute", False)
        # 命名规则要用的课程代号与 Moodle 短名：课名里抠（MAT203）优先，抠不到用短名（AAI）
        if hit.get("shortname"):
            courses[key].setdefault("shortname", hit["shortname"])
        code = resolve_course_code(hit, hit["name"])
        if code:
            courses[key].setdefault("code", code)
        cs.unignore_course(hit["id"])                 # 手动加回来 = 撤出「不再自动接上」名单
        added.append(hit["name"])
    cs.save_courses(courses)
    if added:
        ok("已加：%s" % "、".join(added))
    if missed:
        warn("没找到：%s" % "、".join(missed))
        print(_c("90", "  可选：%s" % "、".join(c.get("name", "") for c in found[:20])))
    return 0 if added else 2


def cmd_rm(args):
    courses = cs.load_courses()
    name = args.name.lower()
    hits = [k for k, v in courses.items() if name in k.lower() or name in (v.get("name") or "").lower()]
    if not hits:
        bad("没找到这门课：%s（用 mk status 看课程列表）" % args.name)
        return 2
    for k in hits:
        print("  移除 %s" % courses[k].get("name"))
        if courses[k].get("id") is not None:
            cs.ignore_course(courses[k]["id"])       # 别让自动接课明天又把它接回来
        del courses[k]
    cs.save_courses(courses)
    ok("已移除 %d 门课（已下载的文件不动）" % len(hits))
    print(_c("90", "  本学期在 Moodle 上的课默认会自动接上，所以这次同时记进了「不再自动接上」名单；"))
    print(_c("90", "  想恢复盯课：mk add <名字>。只想安静不推：用 mk mute（不摘课）。"))
    return 0


def cmd_mute(args):
    courses = cs.load_courses()
    name = args.name.lower()
    hits = [k for k, v in courses.items() if name in k.lower() or name in (v.get("name") or "").lower()]
    if not hits:
        bad("没找到这门课：%s" % args.name)
        return 2
    for k in hits:
        courses[k]["mute"] = not bool(courses[k].get("mute"))
        print("  %s → %s" % (courses[k].get("name"), "静音" if courses[k]["mute"] else "恢复提醒"))
    cs.save_courses(courses)
    return 0


# ── 快捷方式 ───────────────────────────────────────────────────────────────
def _quick_login():
    try:
        import onboarding
        return onboarding.verify_login()
    except Exception as e:
        return False, "登录检查失败：%s" % e


def _sync_scheduler(times=None):
    """时间改了，顺手把系统定时器也改掉（不让配置和现实脱节）。

    但如果已经挂在 Agent 侧（Hermes 定时任务）上，就绝不再装一个系统定时器——
    两个调度源同时跑会互相抢：先跑的那个把新文件记成「已看过」，后跑的就只剩「无新内容」。
    """
    try:
        import onboarding
    except Exception as e:
        print("   ⚠️  定时器没更新：%s（可跑 mk schedule 重挂）" % e)
        return
    nxt = _hermes_cron_next_run()
    if nxt:
        print(_c("90", "   ✅ 已挂在 Hermes 定时任务上（下次 %s），不另装系统定时器（避免双跑抢信号）" % nxt))
        return
    try:
        okk, msg = onboarding.install_schedule("auto")
        print("   " + ("✅ " if okk else "⚠️ ") + msg)
    except Exception as e:
        print("   ⚠️  定时器没更新：%s（可跑 mk schedule 重挂）" % e)


def _demo_items():
    """造几条典型内容，用来演示 5 种风格到底长什么样（不联网、不推送）。"""
    now = datetime.now()
    soon = (now + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M")
    later = (now + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    return [
        {"bucket": "NEW_ASSIGNMENT", "due_dt": soon, "course": "离散数学", "title": "作业 5",
         "text": "[新作业] 离散数学: 作业 5 截止 %s (剩不到 1 天)" % soon[5:].replace("-", "/")},
        {"bucket": "NEW_ASSIGNMENT", "due_dt": later, "course": "写作", "title": "Essay 2",
         "text": "[新作业] 写作: Essay 2 截止 %s (剩 3 天)" % later[5:].replace("-", "/")},
        {"bucket": "GRADE", "course": "线性代数", "title": "期中小测",
         "text": "[成绩] 线性代数: 期中小测 已出分 85/100"},
        {"bucket": "ANNOUNCE", "course": "写作", "title": "调课通知",
         "text": "[公告] 写作: 周五的课调到 10:00"},
        {"bucket": "DOWNLOAD", "course": "线性代数", "title": "lecture05.pdf",
         "text": "[新文件] 线性代数: lecture05.pdf 已下载"},
    ]


_MODE_HINT = {
    "heartbeat": "天天报到：有内容报内容，没内容也报一句「已扫 N 课」，一天 1 条。",
    "silent": "安静：没内容一条都不发；有内容才说话。适合不想被打扰。",
    "digest": "每天一份汇总：把当天所有更新合成一条日报。",
    "urgent": "只报紧急：成绩 + 24 小时内到期的作业，其余一律不发。",
    "full": "全都报：不设行数上限，抓到什么都列出来。",
}


def _demo_modes():
    import moodle_prep as mp
    cfg = cs.load_config()
    max_lines = int(cs.get_path(cfg, "output.max_lines") or 5)
    meta = {"scanned": 5}
    items = _demo_items()
    print(_c("1", "\n  5 种推送风格的实际样子（下面内容只是举例）：\n"))
    for key in ("heartbeat", "silent", "digest", "urgent", "full"):
        label = cs.MODE_LABELS.get(key, key)
        cur = "  ← 你现在用的" if (cs.get_path(cfg, "output.mode") or "heartbeat") == key else ""
        print(_c("1", "  ── %s（mk output %s）%s" % (label, key, cur)))
        shown, _ = mp.select(items, key, max_lines)
        lines = mp.render(key, shown, meta, cfg, cs.out_dir())
        if not lines:
            print(_c("90", "      （没内容 → 一条都不发）"))
        for ln in lines:
            print("      " + ln)
        print(_c("90", "      " + _MODE_HINT[key]))
        print()
    print(_c("90", "  换风格：mk output <名字>     一天最多几行：mk set 单次最多几行 8\n"))
    return 0


def cmd_output(args):
    if getattr(args, "demo", False):
        return _demo_modes()
    return _set_or_show("output.mode", args.value, label="输出风格",
                        extra=_describe_modes)


def cmd_channel(args):
    if args.value == "test":
        return cmd_send(args)
    return _set_or_show("delivery.channel", args.value, label="推送通道")


def cmd_send(args):
    """显式测试通道，不抓课、不修改配置或扫描状态。"""
    import sender
    text = " ".join(getattr(args, "words", []) or []) or "[Moodle] 通道测试：这是一条学业提醒测试消息。"
    sent, detail = sender.send_text(text, title="Moodle 学业提醒")
    if args.json:
        print(json.dumps({"ok": sent, "detail": detail}, ensure_ascii=False))
    else:
        (ok if sent else bad)(detail)
    return 0 if sent else 1


def _set_or_show(dotted, value, label, extra=None):
    cfg = cs.load_config()
    if value is None:
        cur = cs.get_path(cfg, dotted)
        meta = cs.KEY_META.get(dotted, {})
        print("%s：%s" % (label, _fmt(cur)))
        if meta.get("choices"):
            for c in meta["choices"]:
                tag = (cs.MODE_LABELS.get(c) or cs.CHANNEL_LABELS.get(c)
                       or cs.NAMING_LABELS.get(c) or c)
                print("  %-10s %s" % (c, tag))
        if extra:
            extra()
        return 0
    try:
        val = cs.coerce(dotted, value)
    except ValueError as e:
        bad(str(e))
        return 2
    cs.set_path(cfg, dotted, val)
    cs.save_config(cfg)
    ok("%s → %s" % (label, _fmt(val)))
    if dotted == "delivery.schedule":
        _sync_scheduler(times=val)
    return 0


def _describe_modes():
    print(_c("90", "\n  心理预期："))
    rows = [
        ("heartbeat", "每天固定 1 条。有事列出来，没事就报一句「扫了 N 门课，没有新东西」。", "适合大多数人"),
        ("silent", "只在有内容时推。可能连着几天一条都没有，但 mk status 能看到它一直在跑。", "讨厌打扰"),
        ("digest", "每天固定时间合并成一条：昨天+今天的新东西一次看完。", "信息多、想一次看完"),
        ("urgent", "只推 24 小时内截止的作业和新出的成绩，其余一律不打扰。", "期末周 / 备考期"),
        ("full", "每条新公告、新文件都单独推。消息会多，适合开学第一周或排查问题。", "想全量掌握"),
    ]
    for name, desc, who in rows:
        print("  %-10s %s" % (name, desc))
        print("  %-10s " % "" + _c("90", "→ " + who))


# ── 文件命名 ───────────────────────────────────────────────────────────────
def _naming_samples(per_course=2):
    """用你课程里**真实**的文件名演示命名规则（离线、只读、不登录）。"""
    import moodle_client as mc
    cfg = cs.load_config()
    mode = str(cs.get_path(cfg, "download.naming") or "default").strip().lower()
    tpl = str(cs.get_path(cfg, "download.name_template") or mc.DEFAULT_NAME_TEMPLATE)
    day = datetime.now().strftime("%Y%m%d")
    rows = []
    for inf in (cs.load_courses() or {}).values():
        cname = inf.get("name") or ""
        code = mc.resolve_course_code(inf, cname)
        samples = []
        try:
            with open(os.path.join(str(cs.state_dir()), "course_%s.json" % inf.get("id")),
                      encoding="utf-8") as f:
                res = (json.load(f) or {}).get("resources") or {}
        except Exception:
            res = {}
        for iid, mname in list(res.items())[:per_course]:
            folder = mname.split(" / ")[0] if " / " in mname else ""
            raw = mname.split(" / ")[-1]
            stem, ext = os.path.splitext(raw)
            samples.append({"moodle": mname, "raw": raw, "stem": stem, "ext": ext,
                            "iid": str(iid), "folder": folder})
        if not samples:
            samples = [{"moodle": "（还没有记录，先按样子举例）", "raw": "Lecture 1.pdf",
                        "stem": "Lecture 1", "ext": ".pdf", "iid": "123456",
                        "folder": "Lecture Notes"}]
        rows.append((cname, code, mode, tpl, samples, day))
    return mode, tpl, rows


def _name_preview(sample, mode, tpl, code, cname, day):
    import moodle_client as mc
    stem, ext, iid = sample["stem"], sample["ext"], sample["iid"]
    # original 模式落地时用的是「活动ID-名字-摘要」（资料夹里的文件才有），这里按同一规则复现
    m = re.match(r"^folder:(\d+):([0-9a-f]+)$", iid)
    legacy = "%s-%s-%s%s" % (m.group(1), stem, m.group(2), ext) if m else ""
    return mc.build_filename(stem, ext, mode=mode, template=tpl, code=code, course=cname,
                             folder=sample["folder"], digest=iid, cmid=iid, day=day,
                             legacy=legacy)


def _print_naming_key():
    import moodle_client as mc
    print(_c("90", "  自定义模板可用的字段："))
    for k, desc in mc.TEMPLATE_FIELDS.items():
        print("    %-9s %s" % ("{%s}" % k, desc))


def _demo_naming():
    import moodle_client as mc
    mode, tpl, rows = _naming_samples(per_course=1)
    print(_c("1", "\n  同一份文件，四种命名规则分别会落地成什么名字（拿你自己的课举例）："))
    for cname, code, _m, _t, samples, day in rows:
        print("\n  " + _c("36", "%s（课程代号 %s）" % (cname, code or "无")))
        for sample in samples:
            print("    Moodle 里叫：%s" % sample["moodle"])
            for m in mc.NAMING_MODES:
                mark = _c("32", "   ← 你现在用的") if m == mode else ""
                print("      %-9s → %s%s" % (m, _name_preview(sample, m, tpl, code, cname, day), mark))
    print(_c("90", "\n  换：mk naming default|plain|original|custom"))
    print(_c("90", "  自定义：mk set 命名 custom  +  mk set 命名模板 \"{code}-{date}-{name}\"\n"))
    _print_naming_key()
    return 0


def cmd_naming(args):
    if getattr(args, "demo", False):
        return _demo_naming()
    rc = 0
    if args.value:
        rc = _set_or_show("download.naming", args.value, label="文件命名")
        if rc:
            return rc
    mode, tpl, rows = _naming_samples()
    label = cs.NAMING_LABELS.get(mode, mode)
    print("\n  文件命名：%s —— %s" % (mode, label))
    if mode == "custom":
        print(_c("90", "  你的模板：%s" % tpl))
    print(_c("90", "\n  拿你课程里真实的文件举例（左=在 Moodle 里的名字，右=落到你电脑上的名字）："))
    for cname, code, _m, _t, samples, day in rows:
        print("\n  " + _c("36", "%s   [课程代号 %s]" % (cname, code or "无（在 courses.json 里填 code 可指定）")))
        for sample in samples:
            new = _name_preview(sample, mode, tpl, code, cname, day)
            print("    %-38s → %s" % (sample["raw"][:37], new))
    print(_c("90", "\n  换规则：mk naming default|plain|original|custom ｜ 四种并排：mk naming --demo"))
    print(_c("90", "  Moodle 没写后缀的文件（如 Course Information），下载时会自动补上 .pdf/.zip"))
    print(_c("90", "  自定义：mk set 命名 custom  +  mk set 命名模板 \"{code}-{date}-{name}\"\n"))
    _print_naming_key()
    return rc


# ── test / doctor ──────────────────────────────────────────────────────────
# ── 课程文件夹命名（mk folder）──────────────────────────────────────────────
def _folder_config(load=True):
    """(courses, teams, teams_module)：课程与 Teams 来源各自有一份落盘目录。"""
    courses = cs.load_courses() if load else {}
    try:
        import teams_sync
        teams = teams_sync.load_sources() or {}
    except (OSError, ValueError):
        teams_sync, teams = None, {}
    return courses, teams, teams_sync


def _folder_entries(courses=None, teams=None):
    courses = cs.load_courses() if courses is None else courses
    if teams is None:
        teams = _folder_config()[1]
    out = []
    for key, info in (courses or {}).items():
        if isinstance(info, dict):
            out.append(("courses", key, info))
    for key, info in (teams or {}).items():
        if isinstance(info, dict):
            out.append(("teams", key, info))
    return out


def _folder_save(courses, teams, teams_module):
    cs.save_courses(courses or {})
    if teams_module is not None and teams is not None:
        teams_module.save_sources(teams)


def _folder_plan(entries, tpl):
    """算「磁盘实况 → 目标」。返回 (rows, conflicts)。

    ⚠️ 必须看磁盘，不能只比配置字符串：配置指向 A、磁盘上是 B 时（手动改过名、
    被别的工具搬过），只比字符串会判定「已符合」，下一次扫描就另建一个空目录，
    把同一门课的课件劈成两半。踩过：`Anti Corruption` 改名后又被改回 `MPU3322 …`，
    配置仍指向 `MPU1022 …`，工具一路说「没问题」。
    """
    rows, seen = [], {}
    for kind, key, entry in entries:
        # 配置里可能写着未展开的 ~/…，一律先展开再判断（否则会把好目录当成「没了」）
        cur = os.path.expanduser(str(entry.get("path") or ""))
        name = cs.render_folder_name(entry, tpl)
        base = os.path.dirname(os.path.normpath(cur)) if cur else cs.download_root()
        target = os.path.join(base, name) + os.sep
        found, ambiguous = "", []
        if cur and not os.path.isdir(cur):
            try:
                names = sorted(os.listdir(base))
            except OSError:
                names = []
            cands = []
            for d in names:
                full = os.path.join(base, d)
                if not os.path.isdir(full):
                    continue
                score = cs.dir_match_score(d, entry.get("name"), entry.get("code"), name)
                if score >= 0.5:
                    cands.append((score, full))
            if cands:
                best = max(c[0] for c in cands)
                top = [p for s, p in cands if s == best]
                # 并列第一 = 分不清是哪一个 → 一个都不许动（挑错 = 把课件搬进别的课）
                ambiguous = sorted(top) if len(top) > 1 else []
                found = top[0] if len(top) == 1 else ""
        rows.append({"kind": kind, "key": key, "entry": entry, "cur": cur,
                     "target": target, "found": found, "ambiguous": ambiguous,
                     "state": "", "source": ""})
        seen.setdefault(os.path.normpath(target), []).append(key)
    conflicts = {p for p, keys in seen.items() if len(keys) > 1}
    for row in rows:
        row["state"], row["source"] = _folder_state(row, conflicts)
    return rows, conflicts


def _folder_state(row, conflicts):
    """(状态, 要移动的源目录)。看不清就不动手 —— 这个命令绝不许产生「劈成两半」。"""
    cur, target, found = row["cur"], row["target"], row["found"]
    if not cur:
        return "只写配置（这门课还没有目录）", ""
    same = os.path.normpath(cur) == os.path.normpath(target)
    if row.get("ambiguous"):
        return ("❌ 歧义：磁盘上有多个像这门课的目录（%s），先自己确认，我不动"
                % "、".join(os.path.basename(p) for p in row["ambiguous"][:3])), ""
    if os.path.isdir(cur):
        if same:
            return "已经是这个名字", ""
        if os.path.normpath(target) in conflicts:
            return "❌ 冲突：多门课会算成同一个名字（改模板或改名）", ""
        if os.path.exists(target):
            # macOS 默认大小写不敏感：只差大小写时 samefile 为真 —— 那是同一个目录，
            # 不能当成「目标已存在」而永远跳过（大小写就再也修不好了）
            try:
                if os.path.samefile(cur, target):
                    return "只改大小写", cur
            except OSError:
                pass
            return "❌ 跳过：目标目录已存在（绝不合并）", ""
        return "重命名", cur
    # 配置指向的目录不在磁盘上：可能被手动改名/被别的工具搬走
    if found:
        if os.path.normpath(found) == os.path.normpath(target):
            return "修好配置（磁盘上已经是模板名，配置路径失效了）", ""
        if os.path.normpath(target) in conflicts:
            return "❌ 冲突：多门课会算成同一个名字（改模板或改名）", ""
        if os.path.exists(target):
            return "❌ 跳过：目标目录已存在（绝不合并）", ""
        return "重命名（配置路径已失效，磁盘上是「%s」）" % os.path.basename(found), found
    return "只写配置（目录还没建）", ""


def _folder_short(p):
    p = str(p or "")
    return p.replace(os.path.expanduser("~"), "~") if p else "（无）"


def _folder_render_rows(rows, verbose=False):
    for row in rows:
        entry, state = row["entry"], row["state"]
        who = "Teams" if row["kind"] == "teams" else "Moodle"
        mark = {"重命名": "→", "已经是这个名字": "＝"}.get(state, "·")
        if state.startswith("重命名"):
            mark = "→"
        print("  %-6s %-42s %s %s" % (who, (entry.get("name") or row["key"])[:40], mark,
                                      _folder_short(row["target"])))
        if state.startswith("重命名") or state.startswith("修好配置"):
            print(_c("33", "          %s" % state) if state.startswith("修好配置")
                  else _c("90", "          %s" % state))
            print(_c("90", "          现在：%s" % _folder_short(row["cur"] or row["source"])))
        elif state.startswith("❌"):
            print(_c("33", "          %s" % state))


def _folder_fields_help():
    print(_c("90", "  可用字段（组合起来就是文件夹名）："))
    for key, desc in cs.FOLDER_FIELDS.items():
        print("    %-11s %s" % ("{%s}" % key, desc))


def _folder_demo():
    entries = _folder_entries()
    if not entries:
        warn("还没盯任何课（mk add / mk new 之后再回来看）")
        return 2
    print(_c("1", "\n  同一批课，几种常见组合分别会得到什么文件夹名："))
    for tpl in ("{code} {name}", "{name}", "{code} {name} {semester}",
                "{code} {name} ({teacher})", "{fullname}"):
        rows, _ = _folder_plan(entries, tpl)
        mark = _c("32", "   ← 你现在用的") if tpl == (cs.get_path(cs.load_config(), "download.folder_template")) else ""
        print("\n  " + _c("36", tpl) + mark)
        for row in rows[:6]:
            print("    %s" % os.path.basename(os.path.normpath(row["target"])))
    _folder_fields_help()
    print(_c("90", "\n  换：mk folder \"{code} {name}\" ｜ 真的重命名现有文件夹：mk folder --apply\n"))
    return 0


def cmd_folder(args):
    """看/改课程文件夹的命名组合；--apply 按模板重命名现有文件夹。"""
    cfg = cs.load_config()
    if args.value is not None:
        # 显式给了值就得过得去校验（`mk folder ""` 是「空模板」，不是「查看」）
        try:
            cs.check_folder_template(args.value)
        except ValueError as e:
            bad(str(e))
            return 2
        cs.set_path(cfg, "download.folder_template", args.value.strip())
        cs.save_config(cfg)
        ok("课程文件夹命名模板 → %s" % args.value.strip())
    if getattr(args, "apply", False):
        return _folder_apply(cfg)
    if getattr(args, "demo", False):
        return _folder_demo()

    tpl = cs.get_path(cfg, "download.folder_template") or "{code} {name}"
    print(_c("1", "\n  课程文件夹命名：%s" % tpl))
    entries = _folder_entries()
    if not entries:
        warn("还没盯任何课（mk add / mk new 之后再回来看）")
        return 0
    rows, _conflicts = _folder_plan(entries, tpl)
    print(_c("90", "\n  按这个模板，你的课会是（＝已符合 ｜ →要动 ｜ ·只写配置）："))
    _folder_render_rows(rows, verbose=True)
    _folder_fields_help()
    print(_c("90", "\n  换组合：mk folder \"{code} {name} ({teacher})\" ｜ 并排看几种：mk folder --demo"))
    print(_c("90", "  真的把现有文件夹改成这个规则：mk folder --apply（先备份配置，逐条打印）\n"))
    return 0


def _folder_apply(cfg):
    """按当前模板重命名现有文件夹（先备份配置；冲突/目标已存在的一律跳过，不合并）。"""
    tpl = cs.get_path(cfg, "download.folder_template") or "{code} {name}"
    courses, teams, teams_module = _folder_config()
    rows, conflicts = _folder_plan(_folder_entries(courses, teams), tpl)
    print(_c("1", "\n  按「%s」对齐课程文件夹：" % tpl))
    _folder_render_rows(rows, conflicts and True)

    todo = [r for r in rows if r["state"].startswith("重命名") or r["state"] == "只改大小写"]
    config_only = [r for r in rows if r["state"].startswith(("只写配置", "修好配置"))]
    if not todo and not config_only:
        ok("不用改：磁盘和配置已经对得上。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = cs.home() / "backups" / ("%s_folder-rename" % stamp)
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("courses.json", "teams_sources.json"):
        src = cs.home() / name
        if src.exists():
            shutil.copy2(str(src), str(backup / name))
    print(_c("90", "\n  已备份配置：%s" % backup))

    moved, failed = [], []
    for row in todo:
        src, target = row["source"], row["target"]
        if not os.path.isdir(src):
            failed.append(row["entry"].get("name"))
            warn("源目录不见了，跳过：%s" % _folder_short(src))
            continue
        try:
            # 源和目标的尾斜杠都要去掉：目标带尾斜杠会被当成「目录名 + /」报 ENOTDIR；
            # **源带尾斜杠时 os.rename 会跟随软链**，把软链指向的真实目录搬走、留下死链接（实测踩过）
            src_clean, target_clean = src.rstrip(os.sep), target.rstrip(os.sep)
            if row["state"] == "只改大小写":
                # 同一个目录只差大小写：走两步，否则目标「已存在」会把自己绊住
                tmp = "%s.mk-tmp-%s" % (src_clean, datetime.now().strftime("%H%M%S"))
                shutil.move(src_clean, tmp)
                shutil.move(tmp, target_clean)
            else:
                shutil.move(src_clean, target_clean)
        except OSError as e:
            failed.append(row["entry"].get("name"))
            warn("重命名失败：%s（%s）" % (row["entry"].get("name"), e))
            continue
        row["entry"]["path"] = target
        moved.append((row["entry"].get("name"), os.path.basename(os.path.normpath(target))))
    for row in config_only:
        row["entry"]["path"] = row["target"]

    _folder_save(courses, teams, teams_module)
    for name, newname in moved:
        ok("%s → %s" % (name, newname))
    for row in config_only:
        if row["state"].startswith("修好配置"):
            print(_c("33", "  修好配置：%s 的路径指向磁盘上已有的 %s"
                     % (row["entry"].get("name"), os.path.basename(os.path.normpath(row["target"])))))
        else:
            print(_c("90", "  %s 只更新了配置路径（还没有文件夹，下次下载用新名字）" % row["entry"].get("name")))
    if failed:
        bad("%d 个没改成（见上面原因）" % len(failed))
        return 1
    print(_c("90", "\n  想换回去或换别的组合：mk folder \"<模板>\" 再 mk folder --apply\n"))
    return 0


def cmd_test(args):
    script = os.path.join(HERE, "moodle_prep.py")
    cmd = [sys.executable or "python3", script, "--dry-run"]
    if args.json:
        cmd.append("--json")
    env = dict(os.environ)
    env["MOODLE_KILLER_TEST"] = "1"
    print(_c("90", "  试跑中（不会真的推送）…\n"))
    p = subprocess.run(cmd, env=env)
    return p.returncode


def cmd_fresh(args):
    """立刻完整重扫一次：跟早上定时那一趟走同一条真跑路径，但不占定时名额。

    真跑 = 真登录 / 真下载 / 真勾 Done / 写正式 state 与 out（不是 mk test 的试跑）。
    全程不碰任何调度器，所以下一次定时排期原封不动 —— 这正是它和
    `hermes cron run` 那类「手动触发」的根本区别（后者会吃掉下一次排期）。
    """
    script = os.path.join(HERE, "moodle_prep.py")
    # flush：不刷的话这行会被缓冲到子进程输出之后，看起来像先跑后说
    say = (lambda *a: print(*a, flush=True, file=sys.stderr)) if args.json else \
        (lambda *a: print(*a, flush=True))
    before = _cron_next_run_at()
    say(_c("90", "  完整重扫中（真登录 / 真下载 / 真勾 Done，跟早上同一套；不碰定时器）…\n"))
    cmd = [sys.executable or "python3", script] + (["--json"] if args.json else [])
    env = dict(os.environ)
    env["MOODLE_KILLER_SOURCE"] = "fresh"  # 记进 last_run.json：这一趟是手动重扫
    code = subprocess.run(cmd, env=env).returncode
    after = _cron_next_run_at()
    if before:
        if after == before:
            say(_c("90", "\n  ✅ 定时名额没动：下次仍然 %s" % after))
        else:
            say(_c("33", "\n  ⚠️ 定时排期变了：%s → %s，跑 mk doctor 看一眼" % (before, after)))
    say(_c("90", "  这一趟已经把新内容记成「已看过」：下次定时照跑，但可能只剩一句心跳——"))
    say(_c("90", "  所以这一轮的信号就是今天要算数的那份，别让它只留在终端里。"))
    return code


def cmd_doctor(args):
    import onboarding
    cfg = cs.load_config()
    checks = []
    courses = cs.load_courses()
    try:
        import teams_sync
        teams_sources = teams_sync.load_sources()
        teams_error = None
    except (OSError, ValueError) as exc:
        teams_sources, teams_error = {}, str(exc)

    def add(level, name, detail, fix=""):
        checks.append({"level": level, "name": name, "detail": detail, "fix": fix})

    # 1 Python / 依赖
    v = sys.version_info
    if v >= (3, 9):
        add("ok", "Python 版本", "%d.%d.%d" % (v[0], v[1], v[2]))
    else:
        add("bad", "Python 版本", "%d.%d.%d 太旧" % (v[0], v[1], v[2]), "装 Python 3.9+")
    for mod in ("requests", "yaml"):
        try:
            __import__(mod)
            add("ok", "依赖 %s" % mod, "已安装")
        except ImportError:
            add("bad", "依赖 %s" % mod, "缺失", "pip install requests PyYAML")

    # 2 数据目录
    try:
        probe = cs.home() / ".write_probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        add("ok", "数据目录可写", str(cs.home()))
    except Exception as e:
        add("bad", "数据目录不可写", str(e), "检查 %s 权限" % cs.home())

    # 3 配置
    if cs.config_path().exists():
        add("ok", "配置文件", str(cs.config_path()))
    elif not courses and teams_sources:
        add("skip", "配置文件", "仅 Teams 来源；使用默认推送设置")
    else:
        add("bad", "配置文件", "不存在", "mk setup")
    if not courses and teams_sources:
        add("skip", "Moodle 账号", "未配置 Moodle 课程，仅扫描 Teams 本地来源")
    elif cs.credentials_ok(cfg):
        add("ok", "账号信息", "%s @ %s" % (cs.get_path(cfg, "moodle.user"), cs.get_path(cfg, "moodle.url")))
    else:
        add("bad", "账号信息", "没填全", "mk set 账号 xxx / mk set 密码 xxx")

    # 4 登录
    if not courses and teams_sources:
        add("skip", "登录测试", "仅 Teams 本地来源，不需要 Moodle 登录")
    elif cs.credentials_ok(cfg):
        okk, msg = onboarding.verify_login(cfg)
        add("ok" if okk else "bad", "登录测试", msg, "" if okk else "核对学号/密码，或先手动登录一次网站")
    else:
        add("skip", "登录测试", "跳过（账号没填）")

    # 5 课程
    if courses:
        add("ok", "已选课程", "%d 门" % len(courses))
        # 课程代号：很多学校 Moodle 不填官方编号、课名里也可能没有（实测 XMUM 的
        # Abstract Algebra I 要翻到 syllabus 的 Course Code 才拿到 MAT211）。
        # setup 阶段没攒够，后面文件名/文件夹名就只能退化成课名 —— 这里提前点出来。
        nocode = [v.get("name") for v in courses.values()
                  if isinstance(v, dict) and not v.get("code")]
        nocode = [n for n in nocode if n]
        if nocode:
            add("warn", "课程代号", "%d 门还没确认（%s）" % (len(nocode), "、".join(n[:18] for n in nocode[:3])),
                "跑 mk code 深查（翻课程资料里的 Course Code）；急用就 mk code <课名> <代号> 指定")
        else:
            add("ok", "课程代号", "%d 门都有（含来源标注）" % len(courses))
        missing = [v.get("name") for v in courses.values() if not v.get("path")]
        if missing:
            add("warn", "课程下载目录", "%s 没设路径" % "、".join(missing), "mk set 下载目录 <路径> 后重新 mk add")
    elif teams_sources:
        add("skip", "Moodle 课程", "0 门；Teams 来源单独运行")
    else:
        add("bad", "已选课程", "0 门", "mk add")

    if teams_error:
        add("bad", "Teams 配置", teams_error, "检查 ~/.moodle-killer/teams_sources.json")
    if teams_sources and not courses and not any(
            isinstance(info, dict) and not info.get("mute") for info in teams_sources.values()):
        add("bad", "Teams 来源", "所有来源都已静音", "在 teams_sources.json 中取消静音")
    for key, info in teams_sources.items():
        if not isinstance(info, dict) or not info.get("source") or not info.get("path"):
            add("bad", "Teams 来源 %s" % key, "缺 source 或 path", "检查 teams_sources.json")
        elif info.get("mute"):
            add("skip", "Teams 来源 %s" % key, "已静音")
        elif os.path.isdir(os.path.expanduser(info["source"])):
            add("ok", "Teams 来源 %s" % key, "本地目录存在")
        else:
            add("bad", "Teams 来源 %s" % key, "本地目录不可用", "检查 OneDrive 是否已同步")

    # 6 下载目录
    root = os.path.expanduser(cs.get_path(cfg, "download.root") or "")
    own = [(k, v.get("path")) for k, v in courses.items() if v.get("path")]
    if courses and own and len(own) == len(courses):
        # 每门课都自己指定了目录 → 根目录用不上，只看各课目录（缺的第一次下载会自动建）
        gone = [(k, p) for k, p in own if not os.path.isdir(os.path.expanduser(p))]
        if gone:
            # 区分「还没建」和「磁盘上是别的名字」：后者不修就会另建空目录、把课件劈成两半
            stray = []
            for key, p in gone:
                info = courses.get(key) or {}
                base = os.path.dirname(os.path.normpath(os.path.expanduser(p)))
                target = os.path.basename(os.path.normpath(p))
                try:
                    names = sorted(os.listdir(base))
                except OSError:
                    names = []
                for d in names:
                    full = os.path.join(base, d)
                    if os.path.isdir(full) and cs.dir_match_score(d, info.get("name"), info.get("code"), target) >= 0.5:
                        stray.append("%s（配置指向 %s）" % (d, target))
                        break
            if stray:
                add("bad", "下载目录对不上磁盘", "；".join(stray[:3]),
                    "先跑 mk folder 看清单，再 mk folder --apply 对齐（否则下次扫描会另建空目录、课件被劈成两半）")
            else:
                add("ok", "下载目录", "每门课各自指定（%d 门还没建，第一次下载自动创建）" % len(gone))
        else:
            add("ok", "下载目录", "每门课各自指定，%d 个目录都在" % len(own))
    elif root:
        if os.path.isdir(root):
            if os.access(root, os.W_OK):
                add("ok", "下载目录", root)
            else:
                add("bad", "下载目录不可写", root, "换个目录：mk set 下载目录 ~/School")
        else:
            add("warn", "下载目录不存在", root, "第一次运行会自动建，或先 mkdir -p %s" % root)

    # 7 通道
    ch = str(cs.get_path(cfg, "delivery.channel") or "")
    if ch == "auto":
        try:
            import sender
            real, why = sender.detect_channel(cfg)
            add("ok", "推送通道", "自动 → %s（%s）" % (cs.CHANNEL_LABELS.get(real, real), why))
        except Exception:
            add("ok", "推送通道", "自动")
    else:
        need = {"telegram": ["delivery.telegram.bot_token", "delivery.telegram.chat_id"],
                "ntfy": ["delivery.ntfy.topic"], "webhook": ["delivery.webhook.url"],
                "whatsapp": ["delivery.whatsapp.to"]}.get(ch, [])
        missing = [k for k in need if not cs.get_path(cfg, k)]
        if missing:
            add("bad", "推送通道 %s" % ch, "缺 %s" % "、".join(missing),
                "mk set %s <值>" % missing[0])
        else:
            add("ok", "推送通道", cs.CHANNEL_LABELS.get(ch, ch))

    # 8 定时器
    sched = _scheduler_status()
    add(sched[0], "定时任务", sched[1], sched[2])

    # 9 状态文件
    if courses:
        have = [k for k, v in courses.items() if cs.state_path(v.get("id")).exists()]
        add("ok" if len(have) == len(courses) else "warn", "扫描状态",
            "%d/%d 门课有记录" % (len(have), len(courses)),
            "" if len(have) == len(courses) else "首次运行会自动建，跑一次 mk test 即可")

    # 10 命令可用性
    mk_path = shutil.which("mk")
    if mk_path:
        add("ok", "mk 命令", mk_path)
    else:
        add("warn", "mk 命令不在 PATH", "要用完整路径跑", ps.path_hint())

    # 11 安装位置
    inst = _install_info()
    if inst:
        add("ok", "已装到的位置", "、".join(_harness_labels(inst.get("harnesses", []))) or "(未知)")
    else:
        add("warn", "还没装到任何助手", "只在仓库里跑",
            "让 Agent 把这个文件夹装成技能（它会自己找目录），或跑 install.sh / install.ps1")

    # 12 系统
    add("ok", "系统", "%s ｜ 定时用 %s ｜ 数据在 %s" % (
        ps.display_system(), ps.scheduler_short(), cs.home()))

    if args.json:
        print(json.dumps({"checks": checks,
                          "bad": sum(1 for c in checks if c["level"] == "bad"),
                          "warn": sum(1 for c in checks if c["level"] == "warn")},
                         ensure_ascii=False, indent=2))
        return 0

    print(_c("1", "\n  体检结果"))
    icon = {"ok": "✅", "warn": "⚠️ ", "bad": "❌", "skip": "➖"}
    for c in checks:
        print("  %s %-18s %s" % (icon[c["level"]], c["name"], c["detail"]))
        if c["fix"] and c["level"] in ("bad", "warn"):
            print("     " + _c("90", "→ " + c["fix"]))
    nbad = sum(1 for c in checks if c["level"] == "bad")
    nwarn = sum(1 for c in checks if c["level"] == "warn")
    print()
    if nbad:
        bad("%d 项必须修（见上面的 →）" % nbad)
    elif nwarn:
        warn("没硬伤，%d 项建议看一下" % nwarn)
    else:
        ok("全绿，可以放心让它自己跑")
    return 1 if nbad else 0


def _cron_moodle_job():
    """Hermes 定时任务表里 moodle 那条（dict；没有/读不到就是 None）。只读，绝不改调度。"""
    p = os.path.expanduser("~/.hermes/cron/jobs.json")
    if not os.path.exists(p):
        return None
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    jobs = d if isinstance(d, list) else d.get("jobs", d)
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    for j in jobs or []:
        if not isinstance(j, dict):
            continue
        if "moodle" in json.dumps(j, ensure_ascii=False).lower():
            return j
    return None


def _cron_next_run_at():
    """那条任务的下次排期（空串 = 没挂 / 查不到）。"""
    try:
        job = _cron_moodle_job() or {}
    except Exception:
        return ""
    return job.get("next_run_at") or job.get("next_run") or ""


def _hermes_cron_next_run():
    """线上其实靠 Hermes 的定时任务在跑，脚本自己看不见——这里去它的任务表里找。"""
    try:
        job = _cron_moodle_job()
    except Exception:
        return None
    if not job:
        return None
    return job.get("next_run_at") or job.get("next_run") or "（已挂）"


def _scheduler_status():
    """定时器现状。交给 platform_support 按系统判断，再补一条 Hermes 的情况。"""
    try:
        level, label, fix = ps.scheduler_status()
        if level == "warn":
            nxt = _hermes_cron_next_run()
            if nxt:
                return ("ok", "已挂在 Hermes 定时任务（下次 %s）" % nxt, "")
        return (level, label, fix)
    except Exception as e:
        return ("warn", "查不到定时任务（%s）" % e, "mk schedule auto")


def cmd_schedule(args):
    import onboarding
    cfg = cs.load_config()
    if not args.value:
        st = _scheduler_status()
        print("定时任务：%s" % st[1])
        print("推送时间：%s" % ", ".join(cs.get_path(cfg, "delivery.schedule") or []))
        print(_c("90", "  用法：mk schedule %s|off" % "|".join(k for k, _ in ps.scheduler_choices())))
        return 0
    v = args.value.lower()
    if v in ("off", "关", "停"):
        removed = _remove_scheduler()
        ok("已移除定时任务" + ("（%s）" % removed if removed else ""))
        return 0
    okk, msg = onboarding.install_schedule(v)
    print(("✅ " if okk else "⚠️ ") + msg)
    return 0 if okk else 1


def _remove_scheduler():
    try:
        removed = ps.remove_scheduler()
    except Exception:
        removed = []
    return "、".join(removed)


def cmd_code(args):
    """看 / 查 / 定课程代号（Course Code）。

    - 不带参数：把每门课的代号现状列出来，并**当场深查**（Moodle 官方编号 → 课名 →
      课程资料的「Course Code」字段 → Teams 来源交叉验证）
    - `--apply`：把查到的**已确认**代号写进配置
    - `mk code <课名关键词> <代号>`：人工指定（最高可信，之后不再被自动改）
    """
    import course_code as cc
    cfg = cs.load_config()
    courses = cs.load_courses()
    try:
        import teams_sync
        teams = teams_sync.load_sources()
    except (OSError, ValueError):
        teams = {}
    if not courses:
        warn("还没盯任何课（mk add / mk new 之后再回来看）")
        return 2

    if args.value:
        if not args.rest:
            bad("人工指定要两个参数：mk code <课名关键词> <代号>（如 mk code 抽象代数 MAT211）")
            return 2
        new_code = str(args.rest[0]).strip().upper()
        if not re.fullmatch(r"[A-Za-z0-9]{2,10}", new_code):
            bad("代号只接受字母数字（如 MAT211 / MPU3322）")
            return 2
        hit = args.value.lower()
        hits = [k for k, v in courses.items()
                if hit in k.lower() or hit in (v.get("name") or "").lower()]
        if not hits:
            bad("没找到这门课：%s" % args.value)
            print(_c("90", "  现在盯的课："))
            for v in courses.values():
                print(_c("90", "    · %s（现在代号：%s）"
                         % (v.get("name"), v.get("code") or "未确认")))
            return 2
        for k in hits:
            print("  %s：%s → %s" % (courses[k].get("name"), courses[k].get("code") or "(未确认)", new_code))
            courses[k]["code"] = new_code
            courses[k]["code_source"] = "manual"
        cs.save_courses(courses)
        ok("已指定 %d 门课的代号（来源=人工，之后不会被自动改）" % len(hits))
        print(_c("90", "  想让文件夹跟着改名：mk folder 看清单 → mk folder --apply\n"))
        return 0

    rows = cc.enrich(courses, cfg, teams, deep=True, write=getattr(args, "apply", False))
    print(_c("1", "\n  课程代号（Course Code）"))
    print(_c("90", "  来源优先级：人工 › Moodle 官方编号 › 课名 › 课程资料（syllabus 的 Course Code）› Teams 交叉验证；"))
    print(_c("90", "  **Moodle 短名（AAI/Stat/PDEs）不算代号**，只作缩写，绝不写进 code。\n"))
    unconfirmed = []
    for row in rows:
        if row["status"] == "confirmed":
            mark = _c("32", "✅")
            detail = "%s（%s）" % (row["code"], row["why"])
            if row["changed"]:
                detail += "  ← 已写入"
        elif row["status"] == "multi":
            mark = _c("33", "❓")
            detail = "多个候选：%s → 用 mk code \"%s\" <代号> 指定" % (
                "、".join(t for t, _ in row["candidates"][:4]), row["name"][:14])
            unconfirmed.append(row)
        else:
            mark = _c("33", "⚠️")
            detail = "未确认：%s" % row["why"]
            unconfirmed.append(row)
        print("  %s %-42s %s" % (mark, row["name"][:40], detail))
        if row["status"] != "confirmed" and row["shortname"]:
            print(_c("90", "        Moodle 短名 %s（只是缩写，不是代号）" % row["shortname"]))
    if unconfirmed:
        print(_c("90", "\n  %d 门还没拿到代号：课名里没有、Moodle 官方编号为空、课程资料里也还没出现。"
                 % len(unconfirmed)))
        print(_c("90", "  通常等老师把 syllabus 传上来、下次真跑会自动确认；急的话手动指定：mk code <课名> <代号>"))
    if getattr(args, "apply", False):
        if any(row["changed"] for row in rows):
            cs.save_courses(courses)
        ok("已把确认的代号写进 courses.json（%d 门有更新）" % sum(1 for r in rows if r["changed"]))
        print(_c("90", "  想让文件夹跟着改名：mk folder --apply\n"))
    else:
        print(_c("90", "\n  写入配置：mk code --apply ｜ 人工指定：mk code <课名> <代号>\n"))
    return 0


def cmd_new(args):
    """新学期切换：摘掉上学期的课，接上本学期的课。默认只预览，`--yes` 才动手。

    判据只有一条：Moodle 自己的学期分类（inprogress = 本学期）。读不到就不动手 ——
    这是**唯一**能分清「上学期 vs 本学期」的来源：`/my/` 页面给的是「最近访问」，
    拿它当依据会把去年的课也当成在上的课。
    """
    import moodle_prep
    cfg = cs.load_config()
    courses = cs.load_courses()
    try:
        from moodle_client import MoodleClient
        client = MoodleClient()
        with contextlib.redirect_stdout(sys.stderr):
            if not client.login():
                bad("登录失败，先跑 mk doctor")
                return 2
        with contextlib.redirect_stdout(sys.stderr):
            timeline = client.courses_timeline()
    except SystemExit as e:
        bad(str(e))
        return 2
    except Exception as e:
        bad("读学期清单失败：%s" % e)
        return 2

    inprogress = [c for c in (timeline.get("inprogress") or []) if isinstance(c, dict)] \
        if isinstance(timeline, dict) else []
    if not inprogress:
        bad("读不到本学期课程清单（Moodle 接口没返回）→ 什么都没动。先 mk doctor 查登录/网络。")
        return 2

    current = {str(c.get("id")) for c in inprogress}
    keep_ids = {str(v.get("id")) for v in courses.values() if isinstance(v, dict)}
    drop = [(k, v) for k, v in courses.items() if str(v.get("id")) not in current]
    add = [c for c in inprogress if str(c.get("id")) not in keep_ids]
    future = [c for c in (timeline.get("future") or []) if isinstance(c, dict)]

    from html import unescape

    def _nm(raw):
        # 接口的 fullname 是 HTML 转义过的（实测 "Integrity &amp; Anti-Corruption"）
        return re.sub(r"\s+", " ", unescape(str(raw or ""))).strip()

    print(_c("1", "\n  新学期切换（本学期 %d 门）" % len(inprogress)))
    for c in inprogress:
        mark = "已在盯" if str(c.get("id")) in keep_ids else "将接上"
        print("    [本学期] %-14s %s" % (mark, _nm(c.get("fullname") or c.get("shortname"))))
    for _k, v in drop:
        print("    [将摘掉] %-14s %s" % ("上学期", v.get("name")))
    for c in future:
        print("    [下学期] %-14s %s（还没开始，先不动）" % ("暂不处理", _nm(c.get("fullname"))))

    if not drop and not add:
        ok("已经是最新学期的状态，不用切换。")
        return 0
    if not args.yes:
        print(_c("90", "\n  以上为预览，没有动任何东西。摘掉的课**只取消盯课**："))
        print(_c("90", "  已下载的文件、state 记录都原样留着。确认执行：mk new --yes"))
        return 0

    # 真动手：先备份，再摘，再让接课逻辑把本学期的接上（含首次扫描与下载）
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = cs.home() / "backups" / ("%s_mk-new" % stamp)
    backup.mkdir(parents=True, exist_ok=True)
    cs.save_courses(courses)                       # 落一次当前状态，保证备份就是改动前的事实
    shutil.copy2(str(cs.courses_path()), str(backup / "courses.json"))
    print(_c("90", "  已备份：%s" % backup))
    for k, _v in drop:
        courses.pop(k, None)
    cs.save_courses(courses)
    if drop:
        ok("已摘掉 %d 门（上学期）：%s" % (len(drop), "、".join(v.get("name", "") for _k, v in drop)))

    attached, notes = moodle_prep.attach_new_courses(client, cfg, courses, download=True)
    for info in attached:
        ok(moodle_prep.attach_summary_item(info))
    for n in notes:
        warn(str(n))
    if attached:
        print(_c("90", "  新课首轮只报这一行、且不勾 Done（首轮安全闸门），从下一次扫描起正常逐条报。"))
    print(_c("90", "\n  下一步：mk status 复核；想立刻完整跑一遍：mk fresh"))
    return 0


def cmd_pause(args):
    cfg = cs.load_config()
    cs.set_path(cfg, "advanced.paused", True)
    cs.save_config(cfg)
    ok("已暂停推送。脚本照常扫描（状态会更新），只是不给你发消息。")
    print(_c("90", "  恢复：mk resume"))
    return 0


def cmd_resume(args):
    cfg = cs.load_config()
    cs.set_path(cfg, "advanced.paused", False)
    cs.save_config(cfg)
    ok("已恢复推送。")
    return 0


def cmd_find(args):
    """在电脑里找「像课件的文件夹」，列成编号给用户挑。"""
    import pathfinder as pf
    q = " ".join(getattr(args, "words", []) or []).strip()
    cfg = cs.load_config()
    courses = cs.load_courses()
    if q:
        items = pf.find(q, limit=8, min_score=0.4)
        title = "和「%s」像的文件夹" % q
    else:
        names = [str(c.get("name") or "") for c in courses.values()]
        items = pf.suggest_roots(names, limit=6)
        title = "像学校/课件的文件夹"

    if args.json:
        print(json.dumps([{"path": str(i["path"]), "score": i.get("score"),
                           "why": i.get("why"), "exists": os.path.isdir(str(i["path"]))}
                          for i in items], ensure_ascii=False, indent=2))
        return 0

    print(_c("1", "\n  " + title))
    if not items:
        print("  没找到。可以试试课程号（如 MAT107），或直接把文件夹拖进聊天窗口。")
        return 1
    print(pf.render(items, default_index=1))
    print(_c("90", "  用哪个：mk set 下载目录 <路径>"))
    return 0


# ── install ────────────────────────────────────────────────────────────────
def cmd_install(args):
    import harness_install
    targets = [args.harness] if args.harness else None
    return harness_install.install(targets=targets, all_harnesses=args.all)


def cmd_update(args):
    """从远端仓库拉取最新代码并同步到所有 Agent 目录。"""
    import harness_install
    info = harness_install.load_install_json()
    src_dir = Path(info.get("skill_dir") or cs.SKILL_DIR)
    repo = src_dir.parent if src_dir.name == "moodle-killer" else src_dir
    if not (repo / ".git").is_dir():
        if (cs.REPO_ROOT / ".git").is_dir():
            repo = cs.REPO_ROOT
        else:
            bad("未找到原始 Git 仓库，无法自动执行 git pull。请前往 GitHub 下载最新版本覆盖：\n  https://github.com/ClarenceXMUM/Moodle-killer")
            return 1

    print(_c("90", "  正在检查并从 GitHub 拉取最新版本 (%s)…" % repo))
    p = subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"], capture_output=True, text=True)
    if p.returncode != 0:
        bad("更新失败：%s" % (p.stderr or p.stdout).strip())
        print(_c("90", "  如果有本地修改冲突，可尝试先在仓库目录暂存修改后重试。"))
        return 1
    out = (p.stdout or "").strip()
    if "Already up to date" in out or "已经是最新" in out:
        ok("当前已是最新版本！")
    else:
        ok("代码已更新：%s" % out.splitlines()[-1])
    harness_install.install()
    return 0


def cmd_uninstall(args):
    import harness_install
    return harness_install.uninstall(args.harness)


def cmd_harnesses(args):
    import harness_install
    return harness_install.show_table()


# ── help ───────────────────────────────────────────────────────────────────
HELP = """\
  mk —— 学业通知小助手

  最常用的
    mk                  看现在什么情况
    mk setup            一步步配好（问一块答一块，随时可停）
    mk set 时间 07:00   改一项；不带值就显示当前值
    mk add / mk rm 课名  加课 / 删课（本学期新课一般会自己接上，见 mk new）
    mk new [--yes]      新学期切换：摘上学期的课、接本学期的课（默认只预览）
    mk code             看/查课程代号（Course Code）；--apply 写入，或 mk code 课名 代号 指定
    mk test             试跑一次（不推送、不写状态）
    mk fresh            立刻完整重扫一次（真跑，不占定时名额）
    mk doctor           体检，哪坏了直接说怎么修

  改细节
    mk output [风格]    heartbeat|silent|digest|urgent|full
    mk output --demo    并排看 5 种风格长什么样
    mk naming [规则]    default 课程代号-文件名 | plain 只用文件名 | original Moodle 原名 | custom 自定义
    mk naming --demo    并排看你四种规则分别会落地成什么文件名
    mk folder [模板]    课程文件夹怎么命名，如 {code} {name} → 「MAT203 Statistics」
    mk folder --demo    并排看几种组合分别会得到什么文件夹名
    mk folder --apply   按当前模板把现有课程文件夹真的改名（先备份配置）
    mk channel [通道]   auto|local|hermes|telegram|ntfy|webhook|whatsapp|none
    mk channel test     测试当前通道（实际发送一条消息）
    mk mute 课名         某门课单独静音 / 恢复
    mk find [关键词]     在电脑里找「像课件的文件夹」，列编号给你挑
    mk schedule [auto|manual|off]
    mk pause / mk resume 暂停 / 恢复推送
    mk set --advanced    看高级项（按文件类型分文件夹等）

  装到你的 Agent
    mk install          复制到 3 个标准技能目录（自动）
    mk update           拉取最新代码并自动重新同步
    mk harnesses        看装到哪了；没读到就让 Agent 自己装

  给 Agent 用
    任何命令加 --json 都能拿到结构化结果
    mk setup --list     拿引导式问题清单（JSON）
    mk setup --answers '{"delivery.schedule":"08:30"}'

  在聊天里也可以直接说：「配置 moodle」「moodle 状态」「moodle 改推送时间 07:00」
  「moodle 现在重扫一遍」= mk fresh（真跑一次，不占明早的定时名额）
"""


def build_parser():
    p = argparse.ArgumentParser(prog="mk", add_help=False,
                                description="Moodle-killer 命令行")
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--json", action="store_true", help="输出 JSON（给 Agent 解析）")
    sub = p.add_subparsers(dest="cmd")

    def add(name, fn, help_text, **kw):
        sp = sub.add_parser(name, help=help_text, add_help=False)
        sp.add_argument("-h", "--help", action="store_true")
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=fn, **kw)
        return sp

    add("status", cmd_status, "看现在什么情况")

    sp = add("setup", lambda a: _setup(a), "引导式配置")
    sp.add_argument("--advanced", action="store_true")
    sp.add_argument("--list", action="store_true")
    sp.add_argument("--answers", nargs="?", const="", default=None)
    sp.add_argument("--fresh", action="store_true")

    sp = add("set", cmd_set, "改一项配置")
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")
    sp.add_argument("--advanced", action="store_true")

    sp = add("add", cmd_add, "加课程")
    sp.add_argument("names", nargs="*")

    sp = add("rm", cmd_rm, "删课程")
    sp.add_argument("name")

    sp = add("mute", cmd_mute, "某门课静音/恢复")
    sp.add_argument("name")

    for name, fn in (("output", cmd_output), ("channel", cmd_channel),
                     ("naming", cmd_naming), ("folder", cmd_folder)):
        sp = add(name, fn, "看/改 %s" % name)
        sp.add_argument("value", nargs="?")
        if name in ("output", "naming", "folder"):
            sp.add_argument("--demo", action="store_true", help="打印各选项的实际样子")
        if name == "folder":
            sp.add_argument("--apply", action="store_true",
                            help="按模板重命名现有课程文件夹（先备份配置）")

    add("test", cmd_test, "试跑一次")
    add("fresh", cmd_fresh, "立刻完整重扫一次（真跑，不占定时名额）")
    add("doctor", cmd_doctor, "体检")

    sp = add("find", cmd_find, "在电脑里找文件夹")
    sp.add_argument("words", nargs="*")

    sp = add("schedule", cmd_schedule, "定时任务")
    sp.add_argument("value", nargs="?")

    sp = add("code", cmd_code, "看/查/定课程代号（Course Code）")
    sp.add_argument("value", nargs="?")
    sp.add_argument("rest", nargs="*")
    sp.add_argument("--apply", action="store_true", help="把查到的代号写进配置")

    sp = add("new", cmd_new, "新学期切换（摘上学期、接本学期）")
    sp.add_argument("--yes", action="store_true")

    add("pause", cmd_pause, "暂停推送")
    add("resume", cmd_resume, "恢复推送")

    sp = add("install", cmd_install, "装到助手")
    sp.add_argument("harness", nargs="?")
    sp.add_argument("--all", action="store_true")

    add("update", cmd_update, "从远端拉取最新代码并同步")

    sp = add("uninstall", cmd_uninstall, "从助手卸载")
    sp.add_argument("harness", nargs="?")

    add("harnesses", cmd_harnesses, "看各助手装在哪")
    add("help", lambda a: (print(HELP), 0)[1], "帮助")
    return p


def _setup(args):
    import onboarding
    argv = []
    if args.advanced:
        argv.append("--advanced")
    if args.list:
        argv.append("--list")
    if args.fresh:
        argv.append("--fresh")
    if args.answers is not None:
        argv += ["--answers", args.answers]
    return onboarding.main(argv)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cs.init_home()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "help", False) and not getattr(args, "cmd", None):
        print(HELP)
        return 0
    if not getattr(args, "cmd", None):
        return cmd_status(args)
    if getattr(args, "help", False):
        print(HELP)
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\n  已中断（没改的东西没动）")
        return 130


if __name__ == "__main__":
    sys.exit(main())

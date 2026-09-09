#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mk —— Moodle-killer 的唯一命令行入口（也供 Agent 直接调用）。

记这几条就够了：
  mk              看现在什么情况（= mk status）
  mk setup        一步步配好（分块问询，随时可停）
  mk set 时间 07:00   改一项（说人话的键名也认）
  mk add / mk rm      加课 / 删课
  mk test         试跑一次，不推送
  mk doctor       体检，哪坏了直接告诉你

其余：mk output / mk channel / mk schedule / mk pause / mk resume / mk install / mk help
所有命令都支持 --json，方便 Agent 解析。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_store as cs  # noqa: E402
import platform_support as ps  # noqa: E402
import sandbox as sb  # noqa: E402

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
            "output_mode": mode,
            "channel": ch,
            "schedule": times,
            "weekend": cs.get_path(cfg, "delivery.weekend"),
            "paused": paused,
            "download_root": cs.get_path(cfg, "download.root"),
            "download_by_type": cs.get_path(cfg, "download.by_type"),
            "last_run": _last_run(),
            "install": _install_info(),
        }, ensure_ascii=False, indent=2))
        return 0

    print(_c("1", "\n  Moodle-killer 现在的情况"))
    print(_c("90", "  数据目录：%s" % cs.home()))

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
        print("\n  最近一次  %s ｜ %s" % (last.get("time", "?"), last.get("summary", "")))
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


def cmd_get(args):
    cfg = cs.load_config()
    try:
        dotted = cs.normalize_key(args.key)
    except KeyError as e:
        bad(str(e))
        return 2
    val = cs.get_path(cfg, dotted)
    if args.json:
        print(json.dumps({"key": dotted, "value": val}, ensure_ascii=False))
    else:
        print(_fmt(val))
    return 0


# ── add / rm ───────────────────────────────────────────────────────────────
def _slug(name, cid=None):
    key = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in (name or "").lower())
    key = key.strip("_")[:24]
    return key or ("course%s" % (cid or "x"))


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
                found = MoodleClient().discover_courses()
                print("可选课程（用 mk add <名字> 添加）：")
                for c in found:
                    print("  · %s" % c.get("name"))
            except Exception as e:
                bad("拿课程列表失败：%s" % e)
                return 2
            return 0
        okk, msg = onboarding.discover_and_pick(interactive=True)
        print(("✅ " if okk else "❌ ") + msg)
        return 0 if okk else 2

    try:
        from moodle_client import MoodleClient
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
        del courses[k]
    cs.save_courses(courses)
    ok("已移除 %d 门课（已下载的文件不动）" % len(hits))
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
    """时间改了，顺手把系统定时器也改掉（不让配置和现实脱节）。"""
    try:
        import onboarding
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
    return _set_or_show("delivery.channel", args.value, label="推送通道")


def cmd_time(args):
    return _set_or_show("delivery.schedule", args.value, label="推送时间")


def _set_or_show(dotted, value, label, extra=None):
    cfg = cs.load_config()
    if value is None:
        cur = cs.get_path(cfg, dotted)
        meta = cs.KEY_META.get(dotted, {})
        print("%s：%s" % (label, _fmt(cur)))
        if meta.get("choices"):
            for c in meta["choices"]:
                tag = cs.MODE_LABELS.get(c) or cs.CHANNEL_LABELS.get(c) or c
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


# ── test / doctor ──────────────────────────────────────────────────────────
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


def cmd_doctor(args):
    import onboarding
    cfg = cs.load_config()
    checks = []

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
    else:
        add("bad", "配置文件", "不存在", "mk setup")
    if cs.credentials_ok(cfg):
        add("ok", "账号信息", "%s @ %s" % (cs.get_path(cfg, "moodle.user"), cs.get_path(cfg, "moodle.url")))
    else:
        add("bad", "账号信息", "没填全", "mk set 账号 xxx / mk set 密码 xxx")

    # 4 登录
    if cs.credentials_ok(cfg):
        okk, msg = onboarding.verify_login(cfg)
        add("ok" if okk else "bad", "登录测试", msg, "" if okk else "核对学号/密码，或先手动登录一次网站")
    else:
        add("skip", "登录测试", "跳过（账号没填）")

    # 5 课程
    courses = cs.load_courses()
    if courses:
        add("ok", "已选课程", "%d 门" % len(courses))
        missing = [v.get("name") for v in courses.values() if not v.get("path")]
        if missing:
            add("warn", "课程下载目录", "%s 没设路径" % "、".join(missing), "mk set 下载目录 <路径> 后重新 mk add")
    else:
        add("bad", "已选课程", "0 门", "mk add")

    # 6 下载目录
    root = os.path.expanduser(cs.get_path(cfg, "download.root") or "")
    own = [(k, v.get("path")) for k, v in courses.items() if v.get("path")]
    if courses and own and len(own) == len(courses):
        # 每门课都自己指定了目录 → 根目录用不上，只看各课目录（缺的第一次下载会自动建）
        gone = [p for _, p in own if not os.path.isdir(os.path.expanduser(p))]
        if gone:
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


def _hermes_cron_next_run():
    """线上其实靠 Hermes 的定时任务在跑，脚本自己看不见——这里去它的任务表里找。"""
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
        blob = json.dumps(j, ensure_ascii=False).lower()
        if "moodle" in blob:
            return j.get("next_run") or j.get("next_run_at") or "（已挂）"
    return None


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
    mk add / mk rm 课名  加课 / 删课
    mk test             试跑一次（不推送）
    mk doctor           体检，哪坏了直接说怎么修

  改细节
    mk output [风格]    heartbeat|silent|digest|urgent|full
    mk output --demo    并排看 5 种风格长什么样
    mk channel [通道]   auto|local|hermes|telegram|ntfy|webhook|whatsapp|none
    mk time [HH:MM]     一个或多个，如 08:30,20:00
    mk mute 课名         某门课单独静音 / 恢复
    mk find [关键词]     在电脑里找「像课件的文件夹」，列编号给你挑
    mk schedule [auto|manual|off]
    mk pause / mk resume 暂停 / 恢复推送
    mk set --advanced    看高级项（按文件类型分文件夹等）

  装到你的 Agent
    mk install          复制到 3 个标准技能目录（自动）
    mk harnesses        看装到哪了；没读到就让 Agent 自己装

  想试新改动又怕弄乱现有配置
    mk sandbox          造个干净环境（假 HOME + 空工作目录）
    mk sandbox codex    直接进沙盒里的 Codex；测完 mk sandbox rm

  给 Agent 用
    任何命令加 --json 都能拿到结构化结果
    mk setup --list     拿引导式问题清单（JSON）
    mk setup --answers '{"delivery.schedule":"08:30"}'

  在聊天里也可以直接说：「配置 moodle」「moodle 状态」「moodle 改推送时间 07:00」
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

    sp = add("get", cmd_get, "看一项配置")
    sp.add_argument("key")

    sp = add("add", cmd_add, "加课程")
    sp.add_argument("names", nargs="*")

    sp = add("rm", cmd_rm, "删课程")
    sp.add_argument("name")

    sp = add("mute", cmd_mute, "某门课静音/恢复")
    sp.add_argument("name")

    for name, fn in (("output", cmd_output), ("channel", cmd_channel), ("time", cmd_time)):
        sp = add(name, fn, "看/改 %s" % name)
        sp.add_argument("value", nargs="?")
        if name == "output":
            sp.add_argument("--demo", action="store_true", help="打印 5 种风格的实际样例")

    add("test", cmd_test, "试跑一次")
    add("doctor", cmd_doctor, "体检")

    sp = add("find", cmd_find, "在电脑里找文件夹")
    sp.add_argument("words", nargs="*")

    sp = add("sandbox", sb.cmd_sandbox, "给 Codex 造一个干净测试环境")
    sp.add_argument("action", nargs="?", default="create",
                    choices=["create", "codex", "shell", "status", "reset", "rm"])
    sp.add_argument("rest", nargs="*")

    sp = add("schedule", cmd_schedule, "定时任务")
    sp.add_argument("value", nargs="?")

    add("pause", cmd_pause, "暂停推送")
    add("resume", cmd_resume, "恢复推送")

    sp = add("install", cmd_install, "装到助手")
    sp.add_argument("harness", nargs="?")
    sp.add_argument("--all", action="store_true")

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

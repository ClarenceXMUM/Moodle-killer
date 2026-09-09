#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""onboarding.py —— 引导式配置：一次只问一件事，分块进行，随时可停可退。

三种用法（同一套问题，不会各说各话）：
  1. 终端交互：`mk setup`（人用，回车=用默认值）
  2. Agent 在聊天里问：`mk setup --list` 拿到问题清单（JSON），逐块问完再用
     `mk setup --answers '{"delivery.schedule":"08:30"}'` 一次写入
  3. 全自动/脚本：`mk setup --answers <json> [--advanced]`

分块（每块都有「为什么问」）：
  1 站点与账号    —— 登录验证通过才继续
  2 选课          —— 自动发现已加入课程，勾选
  3 下载目录      —— 每课一个文件夹；按类型再分（高级）
  4 推送时间      —— 可多时段；周末开关
  5 推送到哪      —— 8 种通道（默认 auto 跟随你平台），给推荐
  6 输出风格      —— 5 种，附心理预期
  7 定时怎么起    —— 按平台给（macOS launchd / Windows 任务计划程序 / cron / 手动）
  8 验收          —— 试跑 + 体检
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

import config_store as cs
import pathfinder as pf
import platform_support as ps

PROGRESS_FILE = ".setup_progress.json"
SKIP = ("跳过", "skip", "s", "先跳过")
BACK = ("上一步", "back", "b")
QUIT = ("退出", "quit", "q", "取消")


# ── 问题定义（唯一真相；CLI 与 Agent 都读这里） ──────────────────────────────
def build_blocks(advanced=False):
    blocks = [
        {
            "id": "account",
            "title": "第 1 块 · 你的 Moodle 账号",
            "why": "脚本要用它登录学校站点抓通知。密码只存在你本机，不上传。",
            "questions": [
                {"key": "moodle.url", "prompt": "学校 Moodle 的网址是什么？",
                 "default": "https://l.xmu.edu.my"},
                {"key": "moodle.user", "prompt": "登录用的学号 / 用户名？", "default": ""},
                {"key": "moodle.password", "prompt": "登录密码？（输入时不显示）",
                 "default": "", "secret": True},
            ],
            "verify": "login",
        },
        {
            "id": "courses",
            "title": "第 2 块 · 盯哪几门课",
            "why": "只扫你选的课，省时间也少打扰。之后随时能加/删。",
            "questions": [
                {"key": "__courses__", "prompt": "要盯的课程（自动发现后勾选）",
                 "default": "全部"},
            ],
            "action": "pick_courses",
        },
        {
            "id": "download",
            "title": "第 3 块 · 文件下载到哪",
            "why": "新课件/作业自动下载，省得你天天点。先在你电脑里找一圈，你挑一个就行。",
            "questions": [
                {"key": "download.by_type", "prompt": "要不要再按文件类型分（作业/讲义/教材）？",
                 "default": False, "type": "bool", "advanced": True},
            ],
            "action": "pick_download_root",
        },
        {
            "id": "schedule",
            "title": "第 4 块 · 什么时候推",
            "why": "挑你习惯看手机的时间。可以给多个时间点，比如早上和晚上各一次。",
            "questions": [
                {"key": "delivery.schedule", "prompt": "每天几点推？（多个用逗号隔开，如 08:30,20:00）",
                 "default": "08:30"},
                {"key": "delivery.weekend", "prompt": "周末也推吗？", "default": True, "type": "bool"},
            ],
        },
        {
            "id": "delivery",
            "title": "第 5 块 · 消息怎么送到你手上",
            "why": "默认「自动」——按你电脑上已有的方式送（装了 Hermes 就走它，没有就弹本机通知），你不用想。想指定别的 App 再改。",
            "questions": [
                {"key": "delivery.channel", "prompt": "推送方式？（回车=自动，推荐）", "default": "auto",
                 "type": "choice",
                 "choices": ["auto", "local", "hermes", "telegram", "ntfy", "webhook", "whatsapp", "none"]},
            ],
            "followups": {
                "telegram": ["delivery.telegram.bot_token", "delivery.telegram.chat_id"],
                "ntfy": ["delivery.ntfy.topic"],
                "webhook": ["delivery.webhook.url"],
                "whatsapp": ["delivery.whatsapp.to"],
            },
        },
        {
            "id": "output",
            "title": "第 6 块 · 推送风格",
            "why": "决定你一天大概收到几条消息，以及没事的时候安不安静。",
            "questions": [
                {"key": "output.mode", "prompt": "想要哪种风格？", "default": "heartbeat",
                 "type": "choice",
                 "choices": ["heartbeat", "silent", "digest", "urgent", "full"]},
            ],
        },
        {
            "id": "scheduler",
            "title": "第 7 块 · 怎么让它自己跑起来",
            "why": "配置好只是有了机器，还要挂上定时器才会每天自己动。%s 上用 %s，你不用管细节。" % (
                ps.display_system(), ps.scheduler_short()),
            "questions": [
                {"key": "__schedule_how__", "prompt": "用哪种定时方式？", "default": "auto",
                 "type": "choice", "choices": [k for k, _ in ps.scheduler_choices()]},
            ],
            "action": "install_schedule",
        },
        {
            "id": "verify",
            "title": "第 8 块 · 验收",
            "why": "装完就跑一遍，当场看到结果，不留到明天才发现坏的。",
            "questions": [],
            "action": "final_check",
        },
    ]
    if not advanced:
        for b in blocks:
            b["questions"] = [q for q in b["questions"] if not q.get("advanced")]
    return blocks


# ── 工具 ───────────────────────────────────────────────────────────────────
def _c(code, text):
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def _progress_path():
    return cs.home() / PROGRESS_FILE


def load_progress():
    p = _progress_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done_blocks": [], "answers": {}}


def save_progress(prog):
    _progress_path().write_text(json.dumps(prog, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_progress():
    p = _progress_path()
    if p.exists():
        p.unlink()


def _ask(prompt, default=None, secret=False):
    """返回 (action, value)。action ∈ {'value','skip','back','quit'}"""
    hint = ""
    if default not in (None, ""):
        shown = "******" if secret else default
        hint = _c("90", "（默认 %s，直接回车即可）" % shown)
    line = _c("36", "? ") + prompt + (" " + hint if hint else "") + "\n  "
    try:
        if secret:
            import getpass
            raw = getpass.getpass("  ")
        else:
            raw = input(line).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "quit", None
    if raw in QUIT:
        return "quit", None
    if raw in BACK:
        return "back", None
    if raw in SKIP or (raw == "" and default in (None, "")):
        return "skip", None
    if raw == "":
        return "value", default
    return "value", raw


def _apply_answers(answers, advanced=False):
    """把 {dotted_key: raw_value} 写进 config；返回 (ok, messages)。"""
    cfg = cs.load_config()
    msgs = []
    for key, raw in answers.items():
        if key.startswith("__"):
            continue
        try:
            dotted = cs.normalize_key(key)
            val = cs.coerce(dotted, raw)
            cs.set_path(cfg, dotted, val)
            msgs.append("✓ %s = %s" % (cs.KEY_META.get(dotted, {}).get("label", dotted), _fmt(val)))
        except (KeyError, ValueError) as e:
            return False, ["❌ %s: %s" % (key, e)]
    cs.save_config(cfg)
    return True, msgs


def _fmt(val):
    if isinstance(val, bool):
        return "是" if val else "否"
    if isinstance(val, list):
        return ", ".join(str(x) for x in val)
    return str(val)


def verify_login(cfg=None):
    """真的登一次，别等定时任务那天才发现密码错了。"""
    cfg = cfg or cs.load_config()
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from moodle_client import MoodleClient
        client = MoodleClient()
        with contextlib.redirect_stdout(io.StringIO()):  # 客户端自己也会打印，这里只要结果
            ok = bool(client.login())
        return ok, "登录成功，账号可用"
    except SystemExit as e:
        return False, str(e)
    except Exception as e:
        return False, "登录失败：%s" % e


def discover_and_pick(interactive=True, auto_all=False):
    """登录 → 发现课程 → 勾选 → 写入 courses.json。返回 (ok, message)。"""
    cfg = cs.load_config()
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from moodle_client import MoodleClient
        client = MoodleClient()
        if not client.login():
            return False, "登录失败，先检查第 1 块的账号密码"
        found = client.discover_courses()
    except SystemExit as e:
        return False, str(e)
    except Exception as e:
        return False, "发现课程失败：%s" % e
    if not found:
        return False, "没发现任何课程，确认账号已选课"

    if auto_all or not interactive:
        selected = found
    else:
        print()
        for i, c in enumerate(found, 1):
            print("  %2d. %s  (id=%s)" % (i, c["name"], c["id"]))
        raw = input(_c("36", "? ") + "要盯哪几门？输入序号，逗号分隔；回车=全部\n  ").strip()
        if raw in QUIT:
            return False, "已取消"
        if not raw:
            selected = found
        else:
            idxs = [int(x) for x in raw.replace("，", ",").split(",") if x.strip().isdigit()]
            selected = [found[i - 1] for i in idxs if 1 <= i <= len(found)]

    base = os.path.expanduser(cs.get_path(cfg, "download.root") or "~/School")
    courses = cs.load_courses()
    for c in selected:
        key = c["name"].lower()
        key = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in key)
        key = key.strip("_")[:24] or "course%s" % c["id"]
        courses.setdefault(key, {})
        courses[key].update({"id": c["id"], "name": c["name"]})
        courses[key].setdefault("path", os.path.join(base, c["name"], ""))
        courses[key].setdefault("mute", False)
    cs.save_courses(courses)
    return True, "已选 %d 门课，配置写入 %s" % (len(selected), cs.courses_path())


# ── 交互式主流程 ───────────────────────────────────────────────────────────
def run_interactive(advanced=False, resume=True):
    cs.init_home()
    prog = load_progress() if resume else {"done_blocks": [], "answers": {}}
    blocks = build_blocks(advanced=advanced)

    print(_c("1", "\n  Moodle-killer 配置向导"))
    print(_c("90", "  一次问一块，回车用默认值；随时输入「跳过」「上一步」「退出」。"))
    print(_c("90", "  中途退出不会丢，下次接着问。\n"))

    pending = [b for b in blocks if b["id"] not in prog.get("done_blocks", [])]
    if not pending:
        print("  配置已经走完一遍了。想重来：mk setup --fresh\n")
        return 0

    answers = dict(prog.get("answers", {}))
    i = 0
    while i < len(pending):
        block = pending[i]
        print(_c("1", "\n  " + block["title"]))
        print(_c("90", "  为什么问：%s" % block["why"]))

        if block.get("action") == "pick_courses":
            ok, msg = discover_and_pick(interactive=True)
            print("  " + ("✅ " if ok else "❌ ") + msg)
            if not ok:
                print(_c("90", "  稍后可以重来：mk add"))
            prog.setdefault("done_blocks", []).append(block["id"])
            save_progress(prog)
            i += 1
            continue

        if block.get("action") == "pick_download_root":
            ok, msg = pick_download_root(interactive=True)
            print("  " + ("✅ " if ok else "⚠️ ") + msg)
            prog.setdefault("done_blocks", []).append(block["id"])
            save_progress(prog)
            i += 1
            continue

        if block.get("action") == "install_schedule":
            opts = "，".join("%s=%s" % (k, v) for k, v in ps.scheduler_choices())
            raw = input(_c("36", "? ") + "用哪种定时方式？（%s）\n  " % opts).strip() or "auto"
            if raw in QUIT:
                break
            ok, msg = install_schedule(raw)
            print("  " + ("✅ " if ok else "⚠️ ") + msg)
            prog.setdefault("done_blocks", []).append(block["id"])
            save_progress(prog)
            i += 1
            continue

        if block.get("action") == "final_check":
            ok, report = final_check()
            print("  " + report)
            prog.setdefault("done_blocks", []).append(block["id"])
            save_progress(prog)
            i += 1
            continue

        back = False
        for q in block["questions"]:
            default = q.get("default")
            if q["key"] in answers and resume:
                default = answers[q["key"]]
            if q.get("type") == "choice":
                choices = q.get("choices", [])
                print(_c("90", "  可选项：" + " / ".join(
                    "%s=%s" % (c, cs.MODE_LABELS.get(c) or cs.CHANNEL_LABELS.get(c) or c) for c in choices)))
            action, value = _ask(q["prompt"], default=default, secret=q.get("secret"))
            if action == "quit":
                save_progress(prog)
                print(_c("90", "\n  进度已存。下次 mk setup 接着问。"))
                return 0
            if action == "back":
                back = True
                break
            if action == "skip":
                continue
            try:
                dotted = cs.normalize_key(q["key"])
                answers[dotted] = cs.coerce(dotted, value)
            except (KeyError, ValueError) as e:
                print("  ❌ " + str(e))
                # 重问同一题
                action, value = _ask(q["prompt"], default=default, secret=q.get("secret"))
                if action == "value":
                    answers[cs.normalize_key(q["key"])] = cs.coerce(cs.normalize_key(q["key"]), value)

        if back and i > 0:
            i -= 1
            prev = pending[i]
            prog["done_blocks"] = [b for b in prog.get("done_blocks", []) if b != prev["id"]]
            save_progress(prog)
            continue

        # 块内写入 + 块级校验
        ok, msgs = _apply_answers(answers, advanced=advanced)
        for m in msgs:
            print("  " + m)
        if not ok:
            i += 1
            continue

        if block.get("verify") == "login":
            print(_c("90", "  …正在用这个账号登录一次"))
            ok, msg = verify_login()
            print("  " + ("✅ " if ok else "❌ ") + msg)
            if not ok:
                print(_c("90", "  账号没通，先不往下走。改完再跑 mk setup。"))
                save_progress(prog)
                return 2

        # 通道追问
        if block.get("followups"):
            ch = answers.get("delivery.channel")
            for key in block["followups"].get(ch, []):
                meta = cs.KEY_META.get(key, {})
                action, value = _ask(meta.get("label", key), default=meta.get("example", ""),
                                     secret=meta.get("type") == "secret")
                if action == "value":
                    answers[key] = cs.coerce(key, value)
            _apply_answers(answers, advanced=advanced)

        prog.setdefault("done_blocks", []).append(block["id"])
        prog["answers"] = answers
        save_progress(prog)
        i += 1

    print(_c("1", "\n  配好了。"))
    print("  看看现在什么情况：mk status")
    print("  试跑一次：       mk test")
    print("  以后想改哪项：   mk set 时间 07:00")
    clear_progress()
    return 0


# ── 路径：先帮你找，再让你挑 ───────────────────────────────────────────────
def pick_download_root(interactive=True, cfg=None):
    """扫描本机，列出「像学校/课件」的文件夹，让用户挑一个（回车=推荐）。
    选完继续问每门课自己的文件夹。返回 (ok, message)。"""
    cfg = cfg or cs.load_config()
    courses = cs.load_courses()
    names = [c.get("name") or "" for c in courses.values()]

    items, seen = [], set()

    def add(path, why, score=0.0, missing=None):
        p = os.path.abspath(ps.expand_path(str(path)))
        k = os.path.normcase(p)
        if k in seen:
            return
        seen.add(k)
        items.append({"path": p, "why": why, "score": score,
                      "missing": (not os.path.isdir(p)) if missing is None else missing})

    cur = str(cs.get_path(cfg, "download.root") or "")
    if cur:
        add(cur, "你现在配的", 99)
    for it in pf.suggest_roots(names, limit=4):
        add(it["path"], it["why"], it["score"])
    dflt = ps.default_download_root()
    add(dflt, "给你新建一个", 1)
    items.sort(key=lambda x: (bool(x.get("missing")), -x["score"], len(x["path"])))

    if not interactive:
        chosen = items[0]["path"]
        cs.set_path(cfg, "download.root", chosen)
        cs.save_config(cfg)
        return True, "下载根目录：%s" % chosen

    picked = pf.pick("课件下载到哪？", items, default_index=1)
    if not picked.get("path"):
        return False, "没选，先跳过（以后：mk set 下载目录）"
    root = picked["path"]
    if not os.path.isdir(root):
        print(_c("90", "  这个文件夹还不存在，我给你建一个。"))
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as e:
            return False, "建不了 %s：%s" % (root, e)
    cs.set_path(cfg, "download.root", root)
    cs.save_config(cfg)
    print("  ✅ 下载根目录：%s" % pf._short(root))

    ok2, msg2 = pick_course_dirs(root, interactive=True, cfg=cfg)
    return True, msg2


def pick_course_dirs(root, interactive=True, cfg=None):
    """给每门课定一个文件夹：先找现有的同名文件夹，找不到就用 root/课名。"""
    cfg = cfg or cs.load_config()
    courses = cs.load_courses()
    if not courses:
        return True, "还没选课，跳过每门课的文件夹（以后：mk add）"

    dirs = pf.scan_dirs([root] + ps.default_scan_roots(), depth=3)
    used, lines = set(), []
    for key in sorted(courses):
        c = courses[key]
        name = c.get("name") or key
        default = os.path.join(root, name)
        cands = []
        seen = set()
        for it in pf.suggest_course_dir(name, c.get("id"), root=root, dirs=dirs):
            k = os.path.normcase(str(it["path"]))
            if k in seen or k == os.path.normcase(default):
                continue
            seen.add(k)
            cands.append(it)
        if default not in used:
            cands.append({"path": Path(default), "why": "用这个位置", "score": 0.0})
            used.add(default)

        if interactive:
            print(_c("1", "\n   『%s』" % name))
            picked = pf.pick("这门课的文件夹？", cands, default_index=len(cands))
            chosen = picked.get("path") or default
        else:
            existing = [x for x in cands if os.path.isdir(x["path"]) and x["score"] >= 0.6]
            chosen = existing[0]["path"] if existing else default

        c["path"] = str(chosen)
        if not os.path.isdir(str(chosen)):
            try:
                os.makedirs(str(chosen), exist_ok=True)
            except OSError:
                pass
        lines.append("%s → %s" % (name, pf._short(chosen)))
    cs.save_courses(courses)
    return True, "每门课的文件夹：\n     " + "\n     ".join(lines)


# ── 定时任务安装 ───────────────────────────────────────────────────────────
def _runner_command():
    return "%s %s" % (sys.executable or "python3",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "moodle_prep.py"))


def install_schedule(kind="auto"):
    """把每日任务挂到系统上。怎么挂（launchd / cron / 任务计划程序）由 platform_support 决定，
    这里只负责把「跑什么、几点跑、日志写哪」告诉它。返回 (ok, message)。"""
    cfg = cs.load_config()
    times = cs.get_path(cfg, "delivery.schedule") or ["08:30"]
    cmd = _runner_command()
    log = cs.logs_dir() / "moodle.log"
    return ps.install_scheduler(times, cmd, str(log), kind=kind)


# ── 验收 ───────────────────────────────────────────────────────────────────
def final_check():
    cfg = cs.load_config()
    lines = []
    ok_all = True

    if not cs.credentials_ok(cfg):
        lines.append("❌ 账号没填全（mk set 账号 / mk set 密码）")
        ok_all = False
    else:
        ok, msg = verify_login(cfg)
        lines.append(("✅ " if ok else "❌ ") + "登录：" + msg)
        ok_all = ok_all and ok

    courses = cs.load_courses()
    if courses:
        lines.append("✅ 课程：%d 门" % len(courses))
    else:
        lines.append("❌ 还没选课（mk add）")
        ok_all = False

    mode = cs.get_path(cfg, "output.mode")
    ch = cs.get_path(cfg, "delivery.channel")
    try:
        import sender
        real_ch, why = sender.detect_channel(cfg)
        ch_text = "%s（%s）" % (cs.CHANNEL_LABELS.get(real_ch, real_ch), why)
    except Exception:
        ch_text = str(cs.CHANNEL_LABELS.get(ch, ch))
    root = cs.download_root(cfg)
    lines.append("✅ 系统：%s ｜ 定时：%s" % (ps.display_system(), ps.scheduler_short()))
    lines.append("✅ 风格：%s ｜ 推送：%s ｜ 时间：%s" % (
        cs.MODE_LABELS.get(mode, mode), ch_text,
        ", ".join(cs.get_path(cfg, "delivery.schedule") or [])))
    lines.append("✅ 下载到：%s" % root)
    missing = []
    for c in courses.values():
        c = c or {}
        p = str(c.get("path") or "")
        if p and not os.path.isdir(p):
            missing.append(str(c.get("name") or "未命名"))
    if missing:
        lines.append("⚠️ 这几个课程文件夹不存在，会自动新建：%s" % "、".join(missing))

    if ok_all:
        lines.append("")
        lines.append("下一步：mk test（试跑，不推送）")
    return ok_all, "\n  ".join(lines)


# ── 入口 ───────────────────────────────────────────────────────────────────
def list_questions(advanced=False):
    """给 Agent 读的问题清单（JSON）。"""
    out = []
    for b in build_blocks(advanced=advanced):
        qs = []
        for q in b["questions"]:
            meta = cs.KEY_META.get(q["key"], {})
            qs.append({
                "key": q["key"],
                "ask": q["prompt"],
                "default": q.get("default"),
                "type": q.get("type") or meta.get("type", "str"),
                "choices": q.get("choices") or meta.get("choices"),
                "secret": bool(q.get("secret")),
            })
        out.append({"id": b["id"], "title": b["title"], "why": b["why"],
                    "questions": qs, "action": b.get("action"), "verify": b.get("verify")})
    return out


def main(argv):
    advanced = "--advanced" in argv
    if "--list" in argv:
        print(json.dumps(list_questions(advanced=advanced), ensure_ascii=False, indent=2))
        return 0
    if "--answers" in argv:
        i = argv.index("--answers")
        if i + 1 >= len(argv):
            print("用法：mk setup --answers '{\"delivery.schedule\":\"08:30\"}'")
            return 2
        try:
            answers = json.loads(argv[i + 1])
        except Exception as e:
            print("❌ answers 不是合法 JSON：%s" % e)
            return 2
        ok, msgs = _apply_answers(answers, advanced=advanced)
        for m in msgs:
            print(m)
        if ok and cs.credentials_ok():
            ok2, msg = verify_login()
            print(("✅ " if ok2 else "❌ ") + msg)
        return 0 if ok else 2
    if "--fresh" in argv:
        clear_progress()
    if not sys.stdin.isatty():
        print("非交互环境。用：mk setup --list 拿问题清单，再 mk setup --answers '<json>'")
        return 2
    return run_interactive(advanced=advanced)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

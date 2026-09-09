#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify.py —— 校验：宁可什么都不推，也不推错的东西。

三种跑法：
  python3 verify.py              读环境变量（给 CI / 老流程用）
  python3 verify.py --run        检查最近一次真实运行（signals.txt / last_run.json）
  python3 verify.py --self       自检整个工具包（导入、配置、路径、安装位置、定时器）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def check_login(logged_in, hint=""):
    if logged_in:
        return True, ("登录成功 %s" % hint).strip()
    return False, ("登录失败！%s —— 中止后续，避免把错页当通知" % hint).strip()


def check_complete(expected, actual):
    if expected == actual:
        return True, "抓取完整 (%s 条都已入库)" % expected
    return False, "抓取不完整！页面 %s 条 / 入库 %s 条" % (expected, actual)


def check_download(path, min_bytes=1):
    if not path:
        return False, "无下载文件路径"
    p = Path(path)
    if p.exists() and p.stat().st_size >= min_bytes:
        return True, "文件落地 ok (%s, %s bytes)" % (p.name, p.stat().st_size)
    return False, "文件未落地！%s" % path


def check_courses(course_roster, fetched_courses):
    missing = [c for c in course_roster if c not in fetched_courses]
    if not missing:
        return True, "课程扫全 (%s 门)" % len(fetched_courses)
    return False, "缺课！%s 未扫到" % missing


def check_lines(output, max_lines=5):
    n = len([ln for ln in output.splitlines() if ln.strip()])
    if n <= max_lines:
        return True, "行数 %s ≤ %s" % (n, max_lines)
    return False, "行数 %s 超限 > %s，需重写" % (n, max_lines)


# ── --run：检查最近一次真实运行 ────────────────────────────────────────────
def check_run():
    import config_store as cs
    cfg = cs.load_config()
    out = cs.out_dir()
    checks = []
    max_lines = int(cs.get_path(cfg, "output.max_lines") or 5)

    sig = out / "signals.txt"
    if sig.exists():
        checks.append(check_lines(sig.read_text(encoding="utf-8"), max_lines))
    else:
        checks.append((False, "还没有 signals.txt（先跑 mk test）"))

    lr = out / "last_run.json"
    if lr.exists():
        try:
            d = json.loads(lr.read_text(encoding="utf-8"))
            ok = d.get("status") == "ok"
            checks.append((ok, "最近一次运行：%s %s（%s）" % (
                d.get("time"), d.get("status"), d.get("summary", ""))))
        except Exception as e:
            checks.append((False, "last_run.json 读不了：%s" % e))
    else:
        checks.append((False, "还没有 last_run.json（先跑 mk test）"))

    vr = out / "verify_report.txt"
    if vr.exists():
        txt = vr.read_text(encoding="utf-8")
        checks.append((("❌" not in txt), "上次校验报告：%s" % txt.replace("\n", " ; ")))
    else:
        checks.append((False, "还没有 verify_report.txt"))

    return checks


# ── --self：自检工具包本身 ─────────────────────────────────────────────────
def check_self():
    checks = []

    v = sys.version_info
    checks.append((v >= (3, 9), "Python %d.%d.%d（需要 ≥3.9）" % (v[0], v[1], v[2])))

    for mod in ("yaml", "requests"):
        try:
            __import__(mod)
            checks.append((True, "依赖 %s 可用" % mod))
        except Exception as e:
            checks.append((False, "缺依赖 %s：%s（pip3 install -r requirements.txt）" % (mod, e)))

    try:
        import config_store as cs
        cs.init_home()
        checks.append((True, "配置目录可写：%s" % cs.home()))
        cfg = cs.load_config()
        checks.append((bool(cfg), "配置能解析（%s）" % cs.config_path()))
        ok = cs.credentials_ok(cfg)
        checks.append((ok, "账号密码 %s" % ("已填" if ok else "还没填 → mk set 账号 xxx")))
        n = len(cs.load_courses())
        checks.append((n > 0, "已选课程 %d 门%s" % (n, "" if n else " → mk add")))
    except Exception as e:
        checks.append((False, "config_store 出错：%s" % e))

    for f in ("mk.py", "moodle_prep.py", "moodle_client.py", "sender.py",
              "onboarding.py", "harness_install.py", "config_store.py",
              "platform_support.py", "pathfinder.py", "sandbox.py"):
        checks.append(((HERE / f).exists(), "脚本存在：%s" % f))

    try:
        import harness_install as hi
        inst = hi.load_install_json()
        if inst:
            checks.append((True, "已装到：%s" % ", ".join(inst.get("harnesses", []))))
        else:
            checks.append((False, "还没装到任何助手 → mk install"))
        checks.append(((HERE.parent / "SKILL.md").exists(), "SKILL.md 就位"))
    except Exception as e:
        checks.append((False, "harness_install 出错：%s" % e))

    shim = Path(os.path.expanduser("~/.local/bin/mk"))
    checks.append((shim.exists(),
                   "命令 mk %s（%s）" % ("已就位" if shim.exists() else "还没装 → ./install.sh", shim)))

    return checks


def _report(checks):
    failed = [m for ok, m in checks if not ok]
    if failed:
        print("❌ 未通过：")
        for m in failed:
            print("  - " + m)
        print("\n通过 %d/%d" % (len(checks) - len(failed), len(checks)))
        return 2
    print("✅ 全部通过（%d 项）" % len(checks))
    for _ok, m in checks:
        print("  ✓ " + m)
    return 0


def main(argv):
    if "--self" in argv:
        return _report(check_self())
    if "--run" in argv:
        return _report(check_run())

    checks = [
        check_login(os.getenv("LOGIN_OK", "1") == "1"),
        check_complete(int(os.getenv("PAGE_COUNT", "0")), int(os.getenv("STORE_COUNT", "0"))),
        check_download(os.getenv("DOWNLOAD_PATH")),
        check_courses(json.loads(os.getenv("COURSE_ROSTER", "[]")),
                      json.loads(os.getenv("FETCHED_COURSES", "[]"))),
        check_lines(os.getenv("OUTPUT", "")),
    ]
    return _report(checks)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

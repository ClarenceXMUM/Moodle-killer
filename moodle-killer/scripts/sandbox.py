#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sandbox.py —— 给 Codex 造一台「全新电脑」来测试。

为什么要有这个：
  你在真机上已经装过、配过、跑过。再拿真机测，看到的是「旧状态 + 新改动」的混合，
  出了问题分不清是代码的锅还是历史残留的锅。

它在 ~/.moodle-killer-sandbox/ 里造一个假 HOME：

  · Codex 只看到沙盒里这一份技能，看不到你真实的 3 处安装副本
  · 配置从零开始：没有旧的 config.yaml / courses.json / sent.log / state
  · 不碰真实的定时任务（不会往 launchd / 任务计划程序里塞任何东西）
  · Codex 登录态用软链借用，不用重新登录
  · 真实环境完全不动 —— 沙盒里所有写入都落在 ~/.moodle-killer-sandbox/ 里

测完一句话删干净：mk sandbox rm
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_support as ps  # noqa: E402

ENV_SANDBOX = "MOODLE_KILLER_SANDBOX"
ENV_ROOT = "MOODLE_KILLER_SANDBOX_DIR"
DEFAULT_ROOT = "~/.moodle-killer-sandbox"
SKILL_NAME = "moodle-killer"


# ── 沙盒在哪 ───────────────────────────────────────────────────────────────
def root() -> Path:
    """沙盒根目录。默认 ~/.moodle-killer-sandbox，可用 MOODLE_KILLER_SANDBOX_DIR 覆盖。"""
    return Path(os.path.expanduser(os.environ.get(ENV_ROOT) or DEFAULT_ROOT))


def home() -> Path:
    """假 HOME —— Codex 眼里的「用户目录」。"""
    return root() / "home"


def work() -> Path:
    """Codex 的工作目录（空的，让它没东西可改）。

    必须放在 home 之外：Codex 会从工作目录一层层往上找 .agents/skills，
    如果工作目录在 ~/ 底下，往上一定会撞到你真实的 ~/.agents/skills，
    于是同一个技能被读两份。放 /tmp 下面，往上只有 /private 和 /，撞不到。
    """
    forced = os.environ.get("MOODLE_KILLER_SANDBOX_WORK")
    if forced:
        return Path(os.path.expanduser(forced))
    if ps.is_windows():
        return Path(tempfile.gettempdir()) / "moodle-killer-sandbox" / "work"
    return Path("/tmp/moodle-killer-sandbox/work")


def skill_dir() -> Path:
    """沙盒里那份技能的位置（Codex 读的用户级技能目录）。"""
    return home() / ".agents" / "skills" / SKILL_NAME


def data_dir() -> Path:
    """沙盒里的用户数据目录（配置、课程、日志都在这里）。"""
    return home() / ".moodle-killer"


def shim() -> Path:
    """沙盒里的 mk 短命令。"""
    return home() / ".local" / "bin" / ("mk.cmd" if ps.is_windows() else "mk")


def in_sandbox() -> bool:
    return bool(os.environ.get(ENV_SANDBOX))


def exists() -> bool:
    return home().exists()


def _real_user_site() -> str:
    """真实 HOME 下的 Python 用户级包目录。

    坑：很多人（包括这台机器）把 requests / PyYAML 装在
    ~/Library/Python/3.9/lib/python/site-packages。HOME 一换，Python 就去
    沙盒里找，包全「消失」。所以把真实路径透传进去。
    """
    try:
        import site
        p = site.getusersitepackages()
        return p if p and os.path.isdir(p) else ""
    except Exception:
        return ""


# ── 环境变量：让子进程活在沙盒里 ───────────────────────────────────────────
def env_for_sandbox(base=None) -> dict:
    """在真实环境上盖一层，把「家」和「数据」都指向沙盒。"""
    env = dict(base if base is not None else os.environ)
    h = str(home())
    env["HOME"] = h
    env["USERPROFILE"] = h          # Windows 的「家」
    env["MOODLE_KILLER_HOME"] = str(data_dir())
    env[ENV_SANDBOX] = "1"
    bindir = str(shim().parent)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    us = _real_user_site()
    if us:
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = us + (os.pathsep + prev if prev else "")
    return env


# ── 建沙盒 ────────────────────────────────────────────────────────────────
def _link(src: Path, dst: Path):
    """软链；Windows 没权限就退回复制。返回 (ok, 说明)。"""
    if not src.exists():
        return False, "没有 %s" % src.name
    if dst.is_symlink():
        try:
            if Path(os.readlink(str(dst))) == src:
                return True, "已链好"
        except OSError:
            pass
        dst.unlink()
    elif dst.exists():
        return True, "已存在"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(str(src), str(dst))
        return True, "已链"
    except OSError as e:
        try:
            shutil.copy2(str(src), str(dst))
            return True, "已复制（软链不行：%s）" % e
        except OSError as e2:
            return False, str(e2)


def _install_skill(verbose=False) -> tuple:
    """把技能装进沙盒 HOME。用真的安装流程（子进程 + 沙盒环境变量），不自己造轮子。"""
    mk = Path(__file__).resolve().parent / "mk.py"
    try:
        r = subprocess.run([sys.executable, str(mk), "install", "codex"],
                           env=env_for_sandbox(), capture_output=True, text=True, timeout=180)
    except Exception as e:
        return False, "装技能失败：%s" % e
    out = (r.stdout or "") + (r.stderr or "")
    if verbose:
        print(out.rstrip())
    if skill_dir().joinpath("SKILL.md").exists():
        return True, "已装到 %s" % skill_dir()
    return False, "没装成功（exit %s）\n%s" % (r.returncode, out.strip()[-500:])


def _codex_sees_skill() -> tuple:
    """让 Codex 自己报它看到了哪一份 moodle-killer。返回 (True/False/None, 说明)。"""
    codex = shutil.which("codex")
    if not codex:
        return None, "这台机器上没装 codex"
    try:
        r = subprocess.run([codex, "debug", "prompt-input"],
                           env=env_for_sandbox(), cwd=str(work()),
                           capture_output=True, text=True, timeout=180)
    except Exception as e:
        return None, "跑 codex 自查失败：%s" % e
    blob = (r.stdout or "") + (r.stderr or "")
    files = sorted(set(re.findall(r"file:\s*([^\s\")]+)", blob)))
    hits = [p for p in files if SKILL_NAME in p]
    if not hits:
        return False, "Codex 没看到 %s" % SKILL_NAME
    sandbox_hits = [p for p in hits if "moodle-killer-sandbox" in p]
    other_hits = [p for p in hits if "moodle-killer-sandbox" not in p]
    if sandbox_hits and not other_hits:
        return True, "只看到沙盒那份 ✅ %s" % sandbox_hits[0]
    if sandbox_hits and other_hits:
        return False, ("沙盒和真实副本都被读到了，会打架：\n     %s"
                       % "\n     ".join(hits))
    return False, "看到的不是沙盒那份：%s" % "、".join(other_hits)


def create(verbose=True) -> int:
    r = root()
    h = home()
    w = work()
    if verbose:
        print("\n  建沙盒：%s\n" % r)
    for d in (h, w, data_dir(), shim().parent):
        d.mkdir(parents=True, exist_ok=True)

    ok, msg = _install_skill(verbose=verbose)
    if verbose:
        print("  %s 技能：%s" % ("✅" if ok else "❌", msg))
    if not ok:
        return 1

    # 登录态：只借用，不复制密钥内容（软链指向真实文件）
    real_codex = Path(os.path.expanduser("~/.codex"))
    links = []
    for name in ("auth.json", "config.toml"):
        okk, m = _link(real_codex / name, h / ".codex" / name)
        links.append((name, okk, m))
    if verbose:
        for name, okk, m in links:
            if name == "auth.json":
                print("  %s 登录态：%s" % ("✅" if okk else "⚠️ ", m))
    if not any(n == "auth.json" and o for n, o, _ in links):
        if verbose:
            print("     没借到登录态，进 Codex 后按提示 codex login 一次")

    _write_readme()
    _write_meta()

    seen, why = _codex_sees_skill()
    if verbose:
        icon = {True: "✅", False: "❌", None: "⚠️ "}[seen]
        print("  %s Codex 自查：%s" % (icon, why))
    if verbose:
        print("\n  下一步：mk sandbox codex        （直接进沙盒里的 Codex）")
        print("          mk sandbox shell        （进沙盒终端，手动跑 mk 命令）")
        print("          mk sandbox status       （看沙盒里现在什么情况）")
        print("          mk sandbox rm           （测完删干净）\n")
    return 0 if seen is not False else 1


def _write_readme():
    p = root() / "README.txt"
    p.write_text(
        "Moodle-killer 测试沙盒\n"
        "======================\n\n"
        "这是一台「假电脑」：\n"
        "  home/     Codex 眼里的用户目录（技能、配置、数据全在这里）\n"
        "  work/     Codex 的工作目录（空的，改不到你的真项目）\n"
        "            实际路径：%s\n\n"
        "你的真实环境没有被碰过。测完直接删这个文件夹，或跑 mk sandbox rm。\n\n"
        "进去测：\n"
        "  mk sandbox codex     直接开 Codex\n"
        "  mk sandbox shell     开一个沙盒终端\n\n"
        "沙盒里能做：\n"
        "  · mk setup           从零走一遍引导式配置\n"
        "  · mk status / doctor 看状态\n"
        "  · mk find            找文件夹\n"
        "  · mk test            试跑一次\n\n"
        "沙盒里不做：\n"
        "  · 不装真实定时任务（mk schedule 会告诉你「沙盒模式跳过」）\n"
        "  · 不碰你真实的 ~/.moodle-killer 和 ~/.agents/skills\n\n"
        "删掉：mk sandbox rm\n" % work(),
        encoding="utf-8")
    return p


def _write_meta():
    (root() / "sandbox.json").write_text(json.dumps({
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "root": str(root()),
        "home": str(home()),
        "work": str(work()),
        "skill": str(skill_dir()),
        "data": str(data_dir()),
        "borrowed": ["~/.codex/auth.json", "~/.codex/config.toml"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 看状态 ────────────────────────────────────────────────────────────────
def status(args=None) -> int:
    print("\n  沙盒：%s  %s" % (root(), "（存在）" if exists() else "（还没建，跑 mk sandbox）"))
    if not exists():
        return 0
    rows = [
        ("技能副本", skill_dir() / "SKILL.md"),
        ("数据目录", data_dir()),
        ("mk 短命令", shim()),
        ("Codex 登录态", home() / ".codex" / "auth.json"),
    ]
    for name, p in rows:
        if p.exists() or p.is_symlink():
            extra = ""
            if name == "数据目录":
                cfg = p / "config.yaml"
                extra = "（已配置）" if cfg.exists() else "（全新，没配置过）"
            print("  ✅ %-12s %s%s" % (name, p, extra))
        else:
            print("  ⚠️  %-12s 缺" % name)
    seen, why = _codex_sees_skill()
    print("  %s Codex 自查：%s" % ({True: "✅", False: "❌", None: "⚠️ "}[seen], why))
    real = Path(os.path.expanduser("~/.moodle-killer"))
    print("\n  真实环境（没被沙盒碰过）：")
    for p in (real, Path(os.path.expanduser("~/.agents/skills/" + SKILL_NAME))):
        print("     %s %s" % ("·" if p.exists() else "·", p))
    print("")
    return 0


# ── 进去 ──────────────────────────────────────────────────────────────────
def _launch(argv, env, cwd):
    try:
        os.chdir(str(cwd))
        os.execvpe(argv[0], argv, env)
    except OSError as e:
        print("  启动失败：%s" % e)
        return 1
    return 0


def run_codex(extra=None) -> int:
    if not exists():
        create(verbose=True)
    codex = shutil.which("codex")
    if not codex:
        print("  这台机器上没装 codex。装好后再来，或用 mk sandbox shell。")
        return 1
    argv = [codex, "-C", str(work()), "-s", "workspace-write", "-a", "on-request"]
    if extra:
        argv += list(extra)
    print("\n  进沙盒 Codex（工作目录 %s）\n" % work())
    print("  试着说：帮我配置一下 moodle-killer\n")
    return _launch(argv, env_for_sandbox(), work())


def run_shell(extra=None) -> int:
    if not exists():
        create(verbose=True)
    shell = os.environ.get("SHELL") or "/bin/sh"
    if ps.is_windows():
        shell = os.environ.get("COMSPEC") or "cmd.exe"
    print("\n  沙盒终端：HOME=%s" % home())
    print("  mk setup / mk status / mk find 都能直接跑。exit 退出。\n")
    argv = [shell] + list(extra or [])
    return _launch(argv, env_for_sandbox(), work())


# ── 重置 / 删除 ───────────────────────────────────────────────────────────
def reset(args=None) -> int:
    if not exists():
        return create()
    print("\n  重置沙盒（技能重装、配置清空，真实环境不动）\n")
    for d in (skill_dir(), data_dir()):
        if d.exists():
            shutil.rmtree(str(d), ignore_errors=True)
    rc = create(verbose=True)
    # create 里的 mk install 会顺手建一份默认配置；沙盒要保持「像台新电脑」，
    # 所以把配置删掉，让 mk setup 从零开始问。
    for name in ("config.yaml", "courses.json"):
        p = data_dir() / name
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    if rc == 0:
        print("  沙盒已回到「没配过」的状态：进去跑 mk setup 就是全新体验。\n")
    return rc


def remove(args=None) -> int:
    r = root()
    w = work()
    # 工作目录在 /tmp 下，父目录是沙盒专用的，一起清掉别留空壳
    extra = w.parent if w.parent.name == "moodle-killer-sandbox" else w
    gone = []
    for p in (r, extra):
        if p.exists():
            gone.append(p)
    if not gone:
        print("\n  沙盒本来就不存在，没什么可删的。\n")
        return 0
    n = sum(1 for p in gone for _ in p.rglob("*"))
    for p in gone:
        shutil.rmtree(str(p), ignore_errors=True)
    print("\n  🗑  已删除（%d 个文件）：" % n)
    for p in gone:
        print("     %s" % p)
    print("  你的真实环境一直没动过。\n")
    return 0


# ── mk 子命令入口 ─────────────────────────────────────────────────────────
def cmd_sandbox(args) -> int:
    action = (getattr(args, "action", None) or "create").lower()
    rest = list(getattr(args, "rest", None) or [])
    table = {
        "create": lambda: create(),
        "codex": lambda: run_codex(rest),
        "shell": lambda: run_shell(rest),
        "status": lambda: status(),
        "reset": lambda: reset(),
        "rm": lambda: remove(),
        "remove": lambda: remove(),
    }
    fn = table.get(action)
    if not fn:
        print("\n  用法：mk sandbox [create|codex|shell|status|reset|rm]\n")
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(cmd_sandbox(type("A", (), {"action": "create", "rest": []})()))

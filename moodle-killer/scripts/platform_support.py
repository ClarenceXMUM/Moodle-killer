#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""platform_support.py —— 一个系统一套说法。

为什么单独一个文件：Mac / Windows / Linux 在四件事上完全不一样——
  * 文件夹长什么样（`~/School` vs `C:\\Users\\你\\Documents\\School`）
  * 每天怎么自动跑（launchd vs 任务计划程序 vs cron）
  * 短命令装哪、怎么进 PATH（`~/.local/bin` + `.zshrc` vs `%USERPROFILE%` + setx）
  * 怎么弹一条本地通知（osascript vs PowerShell vs notify-send）

这些全集中在这里。别的模块只调函数，不再散落 `if sys.platform == "darwin"`。

调试技巧：设 `MOODLE_KILLER_PLATFORM=windows|mac|linux` 可以在本机模拟另一个
系统的分支（Windows 上没法真跑，靠这个验证逻辑）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ENV_PLATFORM = "MOODLE_KILLER_PLATFORM"

# 真机是不是 Windows。用于防止「平台模拟测试」误改本机 PATH / 注册表。
REAL_WINDOWS = (os.name == "nt")
SIMULATED = bool((os.environ.get(ENV_PLATFORM) or "").strip())


# ── 我是谁 ─────────────────────────────────────────────────────────────────
def system() -> str:
    """返回 'mac' | 'windows' | 'linux'（可用 MOODLE_KILLER_PLATFORM 覆盖，测试用）。"""
    forced = (os.environ.get(ENV_PLATFORM) or "").strip().lower()
    if forced in ("mac", "macos", "darwin", "osx"):
        return "mac"
    if forced in ("windows", "win", "win32", "nt"):
        return "windows"
    if forced in ("linux", "gnu/linux"):
        return "linux"
    if sys.platform == "darwin":
        return "mac"
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    return "linux"


def display_system() -> str:
    return {"mac": "macOS", "windows": "Windows", "linux": "Linux"}[system()]


def is_mac() -> bool:
    return system() == "mac"


def is_windows() -> bool:
    return system() == "windows"


def is_linux() -> bool:
    return system() == "linux"


def in_sandbox() -> bool:
    """是不是在测试沙盒里跑（mk sandbox 造的那台「假电脑」）。"""
    return bool(os.environ.get("MOODLE_KILLER_SANDBOX"))


def python_exe() -> str:
    """这个系统上「叫 Python」的通常是哪个命令（给生成的定时任务/快捷命令用）。"""
    if is_windows():
        return sys.executable or "python"
    return sys.executable or "python3"


# ── 路径：说人话 ↔ 真实路径 ─────────────────────────────────────────────────
def expand_path(raw) -> str:
    """把用户随手给的东西变成可用路径。

    认这些写法：
      ~/School                Mac/Linux 习惯
      C:\\Users\\你\\Documents   Windows 习惯
      %USERPROFILE%\\School    Windows 环境变量
      "带引号 的路径" / 拖进来变成的 /Users/你/My\\ Folder
      file:///Users/you/School  （从浏览器/访达拖过来的）
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    # 拖拽常见产物
    if s.startswith("file://"):
        from urllib.parse import unquote, urlparse
        u = urlparse(s)
        s = unquote((u.netloc + u.path) if u.netloc else u.path)
        if re.match(r"^/[A-Za-z]:", s):     # file:///C:/Users → C:/Users
            s = s[1:]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    # 终端里拖文件常带反斜杠转义的空格
    s = s.replace("\\ ", " ")
    if is_windows():
        s = s.replace("/", os.sep)
    # Windows 的 %USERPROFILE% 这类
    if "%" in s:
        s = re.sub(r"%([^%]+)%",
                   lambda m: os.environ.get(m.group(1), m.group(0)), s)
    s = os.path.expandvars(s)
    if s.startswith("~") or not is_absolute_like(s):
        s = os.path.expanduser(s)
    return s


def is_absolute_like(s: str) -> bool:
    if not s:
        return False
    if is_windows():
        return bool(re.match(r"^[A-Za-z]:[\\/]", s)) or s.startswith("\\\\")
    return s.startswith("/")


def home() -> Path:
    return Path(os.path.expanduser("~"))


def default_download_root() -> str:
    """下载根目录的默认值——按各系统习惯给，用户回车即用。

    Mac/Linux：`~/School`（家目录下直接一个，短）
    Windows：  `~/Documents/School`（Windows 用户不习惯往家目录根上放东西）
    """
    if is_windows():
        return os.path.join(os.path.expanduser("~"), "Documents", "School")
    return os.path.expanduser("~/School")


def path_example() -> str:
    if is_windows():
        return r"C:\Users\你\Documents\School"
    return "~/School"


def candidate_roots():
    """扫描候选文件夹时该去看哪些地方（按各系统真实习惯）。"""
    h = os.path.expanduser("~")
    if is_windows():
        bases = [
            os.path.join(h, "Desktop"),
            os.path.join(h, "Documents"),
            os.path.join(h, "Downloads"),
            os.path.join(h, "OneDrive", "Desktop"),
            os.path.join(h, "OneDrive", "Documents"),
            os.path.join(h, "OneDrive", "桌面"),
            os.path.join(h, "桌面"),
            os.path.join(h, "文档"),
        ]
    elif is_mac():
        bases = [
            os.path.join(h, "Desktop"),
            os.path.join(h, "Documents"),
            os.path.join(h, "Downloads"),
            os.path.join(h, "School"),
        ]
    else:
        bases = [
            os.path.join(h, "Desktop"),
            os.path.join(h, "Documents"),
            os.path.join(h, "Downloads"),
            os.path.join(h, "School"),
        ]
    out, seen = [], set()
    for b in bases:
        if b not in seen and os.path.isdir(b):
            seen.add(b)
            out.append(Path(b))
    return out


def default_scan_roots():
    """即使目录还不存在，也把这些位置当作「将来会放东西的地方」列出来。"""
    roots = candidate_roots()
    if not roots:
        h = Path(os.path.expanduser("~"))
        for name in (("Documents",) if is_windows() else ("School", "Desktop")):
            roots.append(h / name)
    return roots


# ── 快捷命令 `mk` ──────────────────────────────────────────────────────────
def bin_dir() -> Path:
    """短命令装到哪。Mac/Linux 用 ~/.local/bin；Windows 用数据目录下的 bin。"""
    if is_windows():
        base = os.environ.get("MOODLE_KILLER_HOME") or os.path.join(
            os.path.expanduser("~"), ".moodle-killer")
        return Path(base) / "bin"
    return Path(os.path.expanduser("~/.local/bin"))


def shim_name() -> str:
    return "mk.cmd" if is_windows() else "mk"


def shim_path() -> Path:
    return bin_dir() / shim_name()


def write_shim(script_py: str) -> Path:
    """生成 mk 快捷命令（Windows 是 .cmd，其他是带执行位的脚本）。"""
    d = bin_dir()
    d.mkdir(parents=True, exist_ok=True)
    py = python_exe()
    if is_windows():
        p = d / "mk.cmd"
        p.write_text(
            "@echo off\r\n"
            "rem Moodle-killer 快捷命令（由 mk install 生成）\r\n"
            "\"%s\" \"%s\" %%*\r\n" % (py, script_py),
            encoding="utf-8", newline="")
        return p
    p = d / "mk"
    p.write_text(
        "#!/bin/sh\n"
        "# Moodle-killer 快捷命令（由 mk install 生成）\n"
        "exec \"%s\" \"%s\" \"$@\"\n" % (py, script_py),
        encoding="utf-8")
    try:
        os.chmod(str(p), 0o755)
    except OSError:
        pass
    return p


def path_contains(d) -> bool:
    want = os.path.normcase(os.path.normpath(str(d)))
    for part in (os.environ.get("PATH") or "").split(os.pathsep):
        if part and os.path.normcase(os.path.normpath(part)) == want:
            return True
    return False


def ensure_on_path() -> tuple:
    """把短命令目录加进 PATH。返回 (是否成功, 给人看的说明)。"""
    d = bin_dir()
    if path_contains(d):
        return True, "`%s` 已在 PATH 里" % d

    if is_windows():
        if not REAL_WINDOWS:
            return False, ("（模拟测试）不会真的改 PATH。真机上会把 %s 加进用户 Path" % d)
        # setx 会截断超长 PATH 且会去重失败；用 PowerShell 改「用户级」环境变量更稳
        try:
            script = ("$d='%s'; $p=[Environment]::GetEnvironmentVariable('Path','User'); "
                      "if ($p -notlike \"*$d*\") { [Environment]::SetEnvironmentVariable("
                      "'Path', ($p.TrimEnd(';') + ';' + $d), 'User') }") % str(d)
            r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return True, ("已把 %s 加进你的 PATH（重开一个终端窗口生效）" % d)
            return False, ("没能自动加 PATH。手动加：系统设置 → 环境变量 → Path → 新增 %s"
                           % d)
        except Exception as e:
            return False, "没能自动加 PATH（%s）。手动把 %s 加进 Path 变量" % (e, d)

    rc = Path(os.path.expanduser("~/.zshrc" if is_mac() else "~/.bashrc"))
    line = '\nexport PATH="%s:$PATH"  # added by moodle-killer\n' % d
    try:
        cur = rc.read_text(encoding="utf-8") if rc.exists() else ""
        if str(d) not in cur:
            with open(str(rc), "a", encoding="utf-8") as f:
                f.write(line)
        return True, ("已把 %s 写进 %s（重开终端或 source %s 生效）" % (d, rc.name, rc.name))
    except Exception as e:
        return False, "没写进 shell 配置（%s）。手动加：export PATH=\"%s:$PATH\"" % (e, d)


# ── 每天怎么自动跑 ─────────────────────────────────────────────────────────
LAUNCHD_LABEL = "com.moodlekiller.daily"


def path_hint() -> str:
    """给用户看的一句话：怎么让 `mk` 这个短命令能用。"""
    d = bin_dir()
    if is_windows():
        return "把 %s 加进「用户环境变量 Path」，然后重开终端" % d
    return "把 %s 加进 PATH（重跑 install.sh 会自动做）" % d


def scheduler_kind() -> str:
    """这个系统默认用哪种定时器。"""
    if is_mac():
        return "launchd"
    if is_windows():
        return "schtasks"
    return "cron"


def scheduler_name() -> str:
    return {"launchd": "macOS 定时（launchd）",
            "schtasks": "Windows 任务计划程序",
            "cron": "系统 cron"}[scheduler_kind()]


def scheduler_short() -> str:
    """光秃秃的名字（句子里已经说过系统名了，别再来一遍）。"""
    return {"launchd": "launchd",
            "schtasks": "任务计划程序",
            "cron": "cron"}[scheduler_kind()]


def scheduler_choices():
    """给引导向导用的选项（每个系统只列自己认识的）。"""
    k = scheduler_kind()
    out = [("auto", "自动选（推荐）")]
    if k == "launchd":
        out += [("launchd", "macOS 定时（launchd）"), ("cron", "cron（进阶）")]
    elif k == "schtasks":
        out += [("schtasks", "任务计划程序")]
    else:
        out += [("cron", "cron")]
    out += [("manual", "先不挂，我自己手动跑")]
    return out


def _win_quote(s: str) -> str:
    return '"%s"' % s


def _launchd_plist(times, cmd, log) -> dict:
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": ["/bin/bash", "-lc", "%s >> %s 2>&1" % (cmd, log)],
        "StartCalendarInterval": [{"Hour": int(t.split(":")[0]), "Minute": int(t.split(":")[1])}
                                  for t in times],
        "RunAtLoad": False,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def launchd_plist_path() -> Path:
    return Path(os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCHD_LABEL))


def install_scheduler(times, cmd, log, kind="auto") -> tuple:
    """挂每日定时。返回 (ok, message)。kind: auto|launchd|cron|schtasks|manual"""
    # 沙盒里绝不碰真实系统：launchd / crontab / schtasks 一旦装进去就是全机生效，
    # 测试完还得手工清，很容易留下「幽灵推送」。
    if in_sandbox():
        return True, "沙盒模式：不装真实定时任务（要测定时就在沙盒里手动跑 mk test）"
    times = list(times or ["08:30"])
    if kind in ("manual", "off"):
        return True, "先不挂定时器。手动跑：%s" % manual_hint()
    if kind == "auto":
        kind = scheduler_kind()

    if kind == "launchd" and is_mac():
        return _install_launchd(times, cmd, log)
    if kind == "schtasks" and is_windows():
        return _install_schtasks(times, cmd, log)
    if kind == "cron":
        return _install_cron(times, cmd, log)
    # 选了一个这个系统没有的（比如在 Windows 上要 launchd）→ 退回本系统默认
    fallback = scheduler_kind()
    if fallback != kind:
        ok, msg = install_scheduler(times, cmd, log, fallback)
        return ok, "%s 上没有 %s，已改用%s。%s" % (display_system(), kind, scheduler_name(), msg)
    return True, "没挂定时器。手动跑：%s" % manual_hint()


def _install_launchd(times, cmd, log) -> tuple:
    import plistlib
    dest = launchd_plist_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(str(dest), "wb") as f:
        plistlib.dump(_launchd_plist(times, cmd, log), f)
    try:
        subprocess.run(["launchctl", "unload", str(dest)], capture_output=True)
        p = subprocess.run(["launchctl", "load", str(dest)], capture_output=True, text=True)
        if p.returncode != 0:
            return False, "写好了 %s，但 launchctl load 失败：%s" % (dest, (p.stderr or "").strip())
    except Exception as e:
        return False, "写好了 %s，但加载失败：%s" % (dest, e)
    return True, "已挂到 macOS 定时（launchd）：每天 %s" % ", ".join(times)


def _install_cron(times, cmd, log) -> tuple:
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except Exception:
        existing = ""
    lines = [ln for ln in existing.splitlines() if "moodle_prep.py" not in ln]
    for t in times:
        hh, mm = t.split(":")
        lines.append("%s %s * * * %s >> %s 2>&1" % (mm, hh, cmd, log))
    try:
        p = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                           capture_output=True, text=True)
        if p.returncode == 0:
            return True, "已写进 cron：每天 %s 跑一次" % ", ".join(times)
        return False, "写 cron 失败：%s" % (p.stderr or "").strip()
    except Exception as e:
        return False, "写 cron 失败：%s" % e


def _install_schtasks(times, cmd, log) -> tuple:
    """Windows：一个时间点一个计划任务（任务名带时间，方便重装时覆盖）。"""
    ok_any, msgs = False, []
    for t in times:
        name = "MoodleKiller-%s" % t.replace(":", "")
        tr = "%s >> %s 2>&1" % (cmd, log)
        try:
            p = subprocess.run(
                ["schtasks", "/Create", "/F", "/SC", "DAILY", "/TN", name,
                 "/TR", tr, "/ST", t],
                capture_output=True, text=True, timeout=60)
            if p.returncode == 0:
                ok_any = True
                msgs.append("已建计划任务 %s（每天 %s）" % (name, t))
            else:
                msgs.append("建 %s 失败：%s" % (name, (p.stderr or p.stdout or "").strip()))
        except Exception as e:
            msgs.append("建 %s 失败：%s" % (name, e))
    if not ok_any:
        return False, "；".join(msgs)
    return True, "已挂到 Windows 任务计划程序：%s" % "；".join(msgs)


def remove_scheduler() -> list:
    """把挂过的定时器摘掉，返回摘了哪些。"""
    # 沙盒里同样不许动：否则会把真实环境里正在用的任务一起卸掉。
    if in_sandbox():
        return []
    removed = []
    if is_mac():
        p = launchd_plist_path()
        if p.exists():
            try:
                subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
            except Exception:
                pass
            try:
                p.unlink()
            except OSError:
                pass
            removed.append("launchd")
    if is_windows():
        try:
            out = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=60).stdout
            for line in out.splitlines():
                if "MoodleKiller-" in line:
                    name = line.split(",")[0].strip('" ')
                    subprocess.run(["schtasks", "/Delete", "/F", "/TN", name],
                                   capture_output=True, timeout=60)
                    removed.append(name)
        except Exception:
            pass
        return removed
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        lines = [ln for ln in existing.splitlines() if "moodle_prep.py" not in ln]
        if len(lines) != len(existing.splitlines()):
            subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                           capture_output=True, text=True)
            removed.append("crontab")
    except Exception:
        pass
    return removed


def _cron_has_job() -> bool:
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=20).stdout
        return "moodle_prep.py" in out
    except Exception:
        return False


def scheduler_status() -> tuple:
    """(level, 说明, 修复建议)  level ∈ ok|warn —— mk doctor 按这三段渲染。"""
    if is_mac():
        if launchd_plist_path().exists():
            return "ok", "已挂 macOS 定时（launchd）", ""
        if _cron_has_job():
            return "ok", "已写进 crontab", ""
        return "warn", "没挂定时器", "mk schedule auto"
    if is_windows():
        try:
            out = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=60).stdout
            hits = [ln.split(",")[0].strip('" ') for ln in out.splitlines()
                    if "MoodleKiller-" in ln]
            if hits:
                return "ok", "已挂 Windows 计划任务（%s）" % "、".join(hits), ""
            return "warn", "没挂计划任务", "mk schedule auto"
        except Exception:
            return "warn", "查不到计划任务（schtasks 不可用）", "mk schedule taskscheduler"
    if _cron_has_job():
        return "ok", "已写进 crontab", ""
    return "warn", "没挂定时器", "mk schedule auto"


def manual_hint() -> str:
    return "mk test"


# ── 本地通知 ───────────────────────────────────────────────────────────────
def notify(title, text) -> bool:
    """弹一条本机通知（失败不影响主流程）。"""
    title = title or "Moodle-killer"
    safe = (text or "").replace("\n", " ")[:200]
    try:
        if is_mac():
            s = safe.replace('"', "'")
            t = title.replace('"', "'")
            subprocess.run(["osascript", "-e",
                            'display notification "%s" with title "%s"' % (s, t)],
                           check=False, timeout=10)
            return True
        if is_windows():
            ps = ("[reflection.assembly]::loadwithpartialname('System.Windows.Forms');"
                  "[System.Windows.Forms.MessageBox]::Show('%s','%s')"
                  % (safe.replace("'", "''"), title.replace("'", "''")))
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           check=False, timeout=20)
            return True
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title, safe], check=False, timeout=10)
            return True
    except Exception:
        pass
    return False


# ── 自检 ───────────────────────────────────────────────────────────────────
def info() -> dict:
    return {
        "system": system(),
        "display": display_system(),
        "python": python_exe(),
        "home": str(home()),
        "default_download_root": default_download_root(),
        "bin_dir": str(bin_dir()),
        "shim": str(shim_path()),
        "on_path": path_contains(bin_dir()),
        "scheduler": scheduler_kind(),
        "scheduler_name": scheduler_name(),
        "scan_roots": [str(p) for p in candidate_roots()],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(info(), ensure_ascii=False, indent=2))

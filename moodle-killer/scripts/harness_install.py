#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""harness_install.py —— 把技能包放到「助手们默认会读的地方」。

设计取向（2026-09 用户定调）：**不做逐个助手的适配**。
每个 Agent 自己知道自己的规矩——Codex 读 ~/.agents/skills，Claude Code 读
~/.claude/skills，Hermes 读 ~/.hermes/skills。我们要做的只有两件：

  1. 把技能包整包复制到下面这几个**业界通用的标准目录**（少一个文件就跑不起来，
     所以是整包复制 + 复制完自检）
  2. 万一某个助手没读到，别让用户去查文档——让用户直接跟它的 Agent 说一句
     「把这个文件夹装成技能」，Agent 自己会放

历史误装位置（LEGACY_DIRS）只在安装时顺手清理，不再作为目标。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_store as cs  # noqa: E402
import platform_support as ps  # noqa: E402

SKILL_NAME = "moodle-killer"
SRC = cs.SKILL_DIR  # .../moodle-killer/
BUNDLE_ITEMS = ["SKILL.md", "scripts", "references", "templates"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "config.yaml", "courses.json",
                                "out", "*.tmp", ".DS_Store")

# 就这三个标准位置，不再按助手分叉。
# 下面的 key 只是「这个目录叫什么、谁读它」的标签，**不代表为某个助手做了适配**；
# 安装流程对所有助手一视同仁（同一个 _copy_bundle，同一份代码）。
HARNESSES = {
    "agents": {
        "label": "共享标准目录",
        "who": "Codex / Gemini / OpenClaw / OpenCode 等",
        "global": ["~/.agents/skills"],
        "project": [".agents/skills"],
    },
    "claude": {
        "label": "Claude Code",
        "who": "Claude Code",
        "global": ["~/.claude/skills"],
        "project": [".claude/skills"],
    },
    "hermes": {
        "label": "Hermes Agent",
        "who": "Hermes Agent",
        "global": ["~/.hermes/skills"],
        "project": [".hermes/skills"],
    },
}
PRIMARY_ORDER = ["agents", "claude", "hermes"]

# 早期版本装过、但现在不再作为目标的目录（留在这儿好让安装时顺手清理掉旧副本）。
# 典型场景：Gemini 把 ~/.agents/skills 当 ~/.gemini/skills 的别名，两处都有会报冲突。
LEGACY_DIRS = ["~/.codex/skills", "~/.gemini/skills", "~/.openclaw/skills",
               "~/.config/opencode/skills", "~/.vibe/skills", "~/.agent-skills"]

# 本机装了哪些 Agent —— 只用于在 `mk harnesses` 里告诉用户「谁可能读到」，
# 不参与安装决策（装哪三个目录是固定的）。
AGENT_BINS = ["codex", "claude", "hermes", "gemini", "opencode", "openclaw"]


# ── 检测（只看不装） ────────────────────────────────────────────────────────
def detected():
    """本机命令存在、说明装了的 Agent。仅用于显示。"""
    return [b for b in AGENT_BINS if shutil.which(b)]


def show_table(args=None):
    """只回答两件事：装到哪了、没读到怎么办。不再列各家助手的路径差异。"""
    print("\n  技能装在哪（%d 个标准位置）\n" % len(PRIMARY_ORDER))
    for key in PRIMARY_ORDER:
        base = HARNESSES[key]["global"][0]
        here = os.path.join(os.path.expanduser(base), SKILL_NAME)
        mark = "✅" if os.path.exists(os.path.join(here, "SKILL.md")) else "·"
        print("  %s %-46s %s" % (mark, here, HARNESSES[key]["who"]))
    found = detected()
    if found:
        print("\n  本机检测到：%s（都能读到上面的共享目录）" % "、".join(found))
    print("\n  某个助手没读到？不用查文档——直接跟它的 Agent 说：")
    print("     「把 %s 这个文件夹装成技能」" % SKILL_NAME)
    print("  它自己知道该放哪。装：mk install ｜ 卸：mk uninstall\n")
    return 0


def _target_dirs(project=False):
    """要复制的目标目录清单（去重靠 realpath，在 install 里做）。"""
    out = []
    for key in PRIMARY_ORDER:
        bases = HARNESSES[key]["project"] if project else HARNESSES[key]["global"]
        for b in bases:
            p = os.path.expanduser(b) if b.startswith("~") else b
            out.append(os.path.join(p, SKILL_NAME))
    return out


def _prune_other_copies(keep):
    """删掉已知位置里、但不在本次安装清单里的旧副本。

    为什么要有：Gemini 把 ~/.agents/skills 当 ~/.gemini/skills 的同层级别名，
    同一技能两处都有会当场报「Skill conflict detected」；Codex 也可能出现两份。
    """
    keep_real = {os.path.realpath(d) for d in keep}
    removed = []
    bases = list(LEGACY_DIRS)
    for key in PRIMARY_ORDER:
        bases.extend(HARNESSES[key]["global"])
    for base in dict.fromkeys(bases):
        d = os.path.join(os.path.expanduser(base), SKILL_NAME)
        if os.path.realpath(d) in keep_real or not os.path.isdir(d):
            continue
        try:
            shutil.rmtree(d)
            removed.append(d)
        except Exception:
            pass
    return removed


# ── 安装 ───────────────────────────────────────────────────────────────────
def _copy_bundle(dest):
    """整包复制（缺一个文件就等于装了个空壳，所以必须整包）。"""
    os.makedirs(dest, exist_ok=True)
    for item in BUNDLE_ITEMS:
        src = os.path.join(SRC, item)
        if not os.path.exists(src):
            continue
        dst = os.path.join(dest, item)
        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=IGNORE)
        else:
            shutil.copy2(src, dst)
    # 清掉旧版本遗留的多余文件（比如已废弃的 agents/ 目录），保证副本与源一致
    keep = set(BUNDLE_ITEMS) | {"config.yaml", "courses.json", "out", "install.json"}
    for name in os.listdir(dest):
        if name in keep:
            continue
        p = os.path.join(dest, name)
        try:
            shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)
        except OSError:
            pass
    # 自检：三个必须项
    missing = [i for i in ("SKILL.md", "scripts", "references")
               if not os.path.exists(os.path.join(dest, i))]
    return missing


def install(targets=None, all_harnesses=False, project=False):
    """装到 3 个标准位置。`targets` 保留只为兼容旧用法（不再按助手分叉）。"""
    cs.init_home()
    if targets:
        print("  ℹ️  不再按助手分叉安装，统一装到 %d 个标准位置。" % len(PRIMARY_ORDER))

    installed_dirs = []
    done_real = set()
    for dest in _target_dirs(project=project):
        real = os.path.realpath(dest)
        if real in done_real:
            continue
        try:
            missing = _copy_bundle(dest)
            if missing:
                print("  ❌ %s：复制后缺 %s" % (dest, "、".join(missing)))
                continue
            done_real.add(real)
            installed_dirs.append(dest)
            print("  ✅ %s" % dest)
        except Exception as e:
            print("  ❌ %s：%s" % (dest, e))

    # 顺手清掉旧位置的重复副本：同一技能装在两处，Gemini 会当场报冲突。
    if installed_dirs and not project:
        gone = _prune_other_copies(installed_dirs)
        if gone:
            print("  🧹 清掉旧位置的重复副本（同一技能两处会让 Gemini 报冲突）：")
            for g in gone:
                print("     - %s" % g)

    if installed_dirs:
        _write_install_json(installed_dirs)
        shim = _make_shim(installed_dirs[0])
        if shim:
            print("  ✅ 命令 mk → %s" % shim)
        print("\n  验证：mk doctor ｜ 或直接 mk status")
        print("  没读到？跟你的 Agent 说一句：「把 %s 这个文件夹装成技能」，它自己知道放哪。"
              % SKILL_NAME)
    else:
        print("\n  ❌ 一个都没装上。看看 references/HARNESSES.md，或让 Agent 自己装。")
        return 1
    return 0


def _write_install_json(dirs):
    info = {
        "skill_dir": str(SRC),
        "harnesses": list(PRIMARY_ORDER),
        "dirs": [str(d) for d in dirs],
        "installed_at": datetime.now().isoformat(timespec="seconds"),
    }
    (cs.home() / "install.json").write_text(json.dumps(info, ensure_ascii=False, indent=2),
                                            encoding="utf-8")


def load_install_json():
    """读回安装记录（mk install / verify --self 用）。没有就返回 {}。"""
    p = cs.home() / "install.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _make_shim(skill_dir):
    """生成 mk 命令。指向已安装的副本（仓库删了也能用）。
    Mac/Linux 是 ~/.local/bin/mk，Windows 是数据目录下 bin\\mk.cmd —— 交给 platform_support。"""
    target = os.path.join(skill_dir, "scripts", "mk.py")
    if not os.path.exists(target):
        return None
    try:
        shim = ps.write_shim(target)
    except Exception as e:
        print("  ⚠️  mk 命令没装上：%s" % e)
        return None
    ok, msg = ps.ensure_on_path()
    if not ok:
        print("  ℹ️  %s" % msg)
    return str(shim)


def uninstall(harness=None):
    """卸掉 3 个标准位置（含历史位置），配置和数据不动。"""
    removed = []
    bases = []
    for key in PRIMARY_ORDER:
        bases.extend(HARNESSES[key]["global"])
        bases.extend(HARNESSES[key]["project"])
    bases.extend(LEGACY_DIRS)
    for base in dict.fromkeys(bases):
        p = os.path.join(os.path.expanduser(base), SKILL_NAME)
        if os.path.isdir(p):
            try:
                shutil.rmtree(p)
                removed.append(p)
            except Exception:
                pass
    for shim in (ps.bin_dir() / "mk", ps.bin_dir() / "mk.cmd"):
        if shim.exists():
            try:
                shim.unlink()
            except OSError:
                pass
    print("  已移除：%s" % ("、".join(removed) or "（没有装过）"))
    print("  ℹ️  你的配置和数据还在 %s，没动。" % cs.home())
    return 0


def main(argv):
    if not argv or argv[0] in ("--list", "list", "harnesses"):
        return show_table()
    if argv[0] == "--project":
        return install(project=True)
    return install(targets=argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

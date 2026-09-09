#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pathfinder.py —— 帮用户把文件夹找出来，而不是让他打字。

用户的原话：选路径门槛太高。所以这个模块的职责是——
  1. 在他电脑的常见位置里浅扫一遍（不会翻遍整个硬盘）
  2. 把「像学校课件」和「像某门课」的文件夹挑出来，按像的程度排序
  3. 列成带编号的候选，用户回一个数字就行（回车=用推荐的那个）
  4. 不想用候选的，直接把文件夹拖进聊天窗口或粘贴路径，一样认

设计取舍：
  * 只扫 3 层、最多 6000 个目录，跳过系统目录和隐藏目录——快且不越界
  * 匹配用「规范化后的包含 + 词重合 + 模糊相似度」三者取最大，避免英文/中文
    混排时漏掉
  * 一个候选只说一条人话理由（why），不堆分数
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:
    import platform_support as ps
except ImportError:  # 允许单独跑
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import platform_support as ps

MAX_DEPTH = 3
MAX_DIRS = 6000

# 这些目录进去没意义，还慢
SKIP_NAMES = {
    ".git", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "site-packages", "Library", "Applications", "AppData", "Windows",
    "Program Files", "Program Files (x86)", "System32", "System Volume Information",
    "$RECYCLE.BIN", ".Trash", ".cache", ".npm", ".local", ".hermes", ".claude",
    ".codex", ".agents", ".gemini", ".openclaw", ".moodle-killer", "anaconda3",
    "miniconda3", "photoslibrary.photoslibrary",
}

# 「这个文件夹像学校相关」的线索词
SCHOOL_HINTS = [
    "school", "university", "uni", "college", "campus", "study", "course",
    "courses", "semester", "moodle", "xmum", "xmu", "academic", "class",
    "学校", "大学", "课程", "课件", "上课", "学期", "学业", "作业", "笔记",
]


# ── 名字比对 ───────────────────────────────────────────────────────────────
def norm(s: str) -> str:
    """只留字母数字和汉字，其余（空格、下划线、括号、连字符…）全去掉。"""
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def tokens(s: str) -> set:
    """英文按词切，中文按字切（中文没有空格，按字重合比按词靠谱）。"""
    out = set()
    for chunk in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", str(s or "").lower()):
        if not chunk:
            continue
        if re.search(r"[\u4e00-\u9fff]", chunk):
            out.update(list(chunk))
            if len(chunk) > 1:
                out.add(chunk)
        else:
            out.add(chunk)
    return out


def similarity(query: str, name: str) -> float:
    """0~1，越大越像。空 query 返回 0。"""
    q, n = norm(query), norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if q in n or n in q:
        short, long_ = (q, n) if len(q) <= len(n) else (n, q)
        return 0.62 + 0.38 * (len(short) / float(len(long_)))
    tq, tn = tokens(query), tokens(name)
    if tq and tn:
        inter = len(tq & tn)
        if inter:
            jac = inter / float(len(tq | tn))
            cover = inter / float(min(len(tq), len(tn)))
            base = 0.35 * jac + 0.4 * cover
            if base >= 0.45:
                return round(min(base, 0.95), 3)
    import difflib
    r = difflib.SequenceMatcher(None, q, n).ratio()
    return round(r if r >= 0.6 else 0.0, 3)


# ── 扫描 ───────────────────────────────────────────────────────────────────
def scan_dirs(roots=None, depth=MAX_DEPTH, budget=MAX_DIRS) -> list:
    """浅扫常见位置下的目录。返回 Path 列表（不含 root 本身）。"""
    roots = [Path(p) for p in (roots or ps.default_scan_roots())]
    out, seen, stack = [], set(), []
    for r in roots:
        if r.is_dir():
            stack.append((r, 0))
    while stack and len(seen) < budget:
        cur, d = stack.pop()
        try:
            key = os.path.normcase(str(cur))
            if key in seen:
                continue
            seen.add(key)
            with os.scandir(str(cur)) as it:
                for e in it:
                    if len(seen) >= budget:
                        break
                    try:
                        if not e.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if e.name.startswith(".") or e.name in SKIP_NAMES:
                        continue
                    if e.name.endswith((".app", ".photoslibrary", ".fcpbundle")):
                        continue
                    child = Path(e.path)
                    out.append(child)
                    if d + 1 < depth:
                        stack.append((child, d + 1))
        except (OSError, PermissionError):
            continue
    return out


def _why(score: float, query: str, name: str) -> str:
    if norm(query) and norm(query) == norm(name):
        return "名字完全对上"
    if norm(query) and norm(query) in norm(name):
        return "名字里含「%s」" % query
    if score >= 0.7:
        return "名字和「%s」很像" % query
    if score >= 0.45:
        return "名字部分对上「%s」" % query
    return "看起来像学校相关"


def find(query, roots=None, limit=5, min_score=0.45, dirs=None) -> list:
    """给一个关键词（课程名 / 「学校」），返回候选：[{path, score, why}]"""
    pool = dirs if dirs is not None else scan_dirs(roots)
    scored = []
    for p in pool:
        s = similarity(query, p.name)
        if s >= min_score:
            scored.append({"path": p, "score": s, "why": _why(s, query, p.name)})
    scored.sort(key=lambda x: (-x["score"], len(str(x["path"]))))
    return scored[:limit]


def find_many(queries, roots=None, limit=5, min_score=0.45) -> dict:
    """一次扫、多个关键词各找一遍（避免每门课重扫一遍硬盘）。"""
    dirs = scan_dirs(roots)
    return {q: find(q, dirs=dirs, limit=limit, min_score=min_score) for q in queries}


def looks_like_school(name: str) -> float:
    n = str(name or "").lower()
    hit = sum(1 for h in SCHOOL_HINTS if h in n)
    return min(1.0, hit * 0.34)


def suggest_roots(course_names=(), extra_roots=None, limit=5, dirs=None) -> list:
    """给「课件下载到哪」推荐根目录。

    评分 = 名字像学校 + 里面有几门课的同名子文件夹（这个信号最强）。
    """
    pool = dirs if dirs is not None else scan_dirs(extra_roots)
    by_parent = {}
    for p in pool:
        by_parent.setdefault(str(p.parent), []).append(p)

    cands = []
    # 1) 常见位置本身（Desktop / Documents / Downloads）永远是选项，但排在后面
    for r in (extra_roots or ps.default_scan_roots()):
        r = Path(r)
        if r.is_dir():
            cands.append({"path": r, "score": 0.2, "why": "常见位置"})

    for parent, children in by_parent.items():
        name = Path(parent).name
        name_score = looks_like_school(name)
        course_hits = 0
        for cn in course_names:
            best = max([similarity(cn, c.name) for c in children] or [0])
            if best >= 0.55:
                course_hits += 1
        if not name_score and not course_hits:
            continue
        score = min(1.0, name_score * 0.5 + course_hits * 0.28)
        if course_hits:
            why = "里面有 %d 个文件夹和你的课名对得上" % course_hits
        else:
            why = "名字像学校相关"
        cands.append({"path": Path(parent), "score": score, "why": why})

    # 去重（同一个路径只留分最高的那条）
    best = {}
    for c in cands:
        k = os.path.normcase(str(c["path"]))
        if k not in best or c["score"] > best[k]["score"]:
            best[k] = c
    out = sorted(best.values(), key=lambda x: (-x["score"], len(str(x["path"]))))
    return out[:limit]


def suggest_course_dir(course_name, course_id=None, root=None, dirs=None) -> list:
    """给某门课找「它自己的文件夹」候选（在下载根目录 + 常见位置里找）。"""
    roots = []
    if root:
        roots.append(Path(ps.expand_path(root)))
    roots += ps.default_scan_roots()
    pool = dirs if dirs is not None else scan_dirs(roots, depth=3)
    out = []
    for p in pool:
        s = similarity(course_name, p.name)
        if course_id:
            s = max(s, similarity(str(course_id), p.name))
        if s >= 0.55:
            out.append({"path": p, "score": s, "why": _why(s, course_name, p.name)})
    out.sort(key=lambda x: (-x["score"], len(str(x["path"]))))
    return out[:3]


# ── 交互：列编号，让用户回一个数字 ─────────────────────────────────────────
def render(items, default_index=1, indent="    ") -> str:
    lines = []
    for i, it in enumerate(items, 1):
        mark = "  ← 推荐" if i == default_index else ""
        why = "（%s）" % it["why"] if it.get("why") else ""
        if it.get("missing"):
            why += "（还没有，选它会给你建）"
        lines.append("%s%2d) %s%s%s" % (indent, i, _short(it["path"]), why, mark))
    return "\n".join(lines)


def _short(p) -> str:
    s = str(p)
    h = os.path.expanduser("~")
    if s.startswith(h):
        s = "~" + s[len(h):]
    return s


def resolve(spec, items):
    """把「2」/「1,3」/「回车」/路径 解析成选中的候选（或 None 表示自己输入）。"""
    s = str(spec or "").strip()
    if s == "":
        return [items[0]] if items else []
    s = s.replace("，", ",").replace("、", ",")
    if all(x.strip().isdigit() for x in s.split(",") if x.strip()):
        picked = []
        for x in s.split(","):
            x = x.strip()
            if x.isdigit() and 1 <= int(x) <= len(items):
                picked.append(items[int(x) - 1])
        return picked
    return []   # 不是数字 → 调用方按「用户自己给了路径」处理


def pick(prompt, items, default_index=1, allow_custom=True, input_fn=None,
         print_fn=print, style=None) -> dict:
    """让用户从候选里挑一个。返回 {path, custom} 或 {'path': None} 表示放弃。

    input_fn 默认在调用时取内置 input —— 这样测试里替换 builtins.input 才生效
    （写成默认参数会在 import 时就把原始 input 绑死）。
    """
    input_fn = input_fn or input
    c = style or (lambda code, text: text)
    print_fn("")
    print_fn(c("90", "  找到这些，挑一个就行："))
    print_fn(render(items, default_index=default_index))
    tail = "回车=%d" % default_index
    if allow_custom:
        tail += "，或者直接把文件夹拖进来 / 粘贴完整路径"
    raw = input_fn(c("36", "? ") + prompt + "（%s）\n  " % tail).strip()

    # 回车 = 选「推荐」那一项（不是永远选第一条）
    if not raw and items:
        idx = max(1, min(default_index, len(items)))
        return {"path": str(items[idx - 1]["path"]), "custom": False}

    picked = resolve(raw, items)
    if picked:
        return {"path": str(picked[0]["path"]), "custom": False}
    if raw and allow_custom:
        p = ps.expand_path(raw)
        if os.path.isdir(p):
            return {"path": p, "custom": True}
        return {"path": p, "custom": True, "missing": True}
    if not raw and items:
        return {"path": str(items[default_index - 1]["path"]), "custom": False}
    return {"path": None}


if __name__ == "__main__":
    import json
    q = sys.argv[1:] or ["school"]
    res = find_many(q)
    print(json.dumps({k: [{"path": str(c["path"]), "score": c["score"], "why": c["why"]}
                          for c in v] for k, v in res.items()},
                     ensure_ascii=False, indent=2))

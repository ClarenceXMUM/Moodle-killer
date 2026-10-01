#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""课程代号（Course Code）的获取与确认 —— `mk code` 与自动接课共用。

**为什么单独一个模块**：Moodle 接口在很多学校（实测 XMUM）根本不填官方「课程编号」字段，
课名里也可能没有代号（`Abstract Algebra I 2026/09 Ali Azimi`）。代号只能从多处凑，
且必须标清「从哪来的、算不算确认了」—— 否则后面用短名（AAI）冒充代号，文件夹名、文件名
就和真编码打架。

来源优先级（查到就停，越上越可信）：

| 来源 | 含义 | 可信度 |
|---|---|---|
| `manual` | 人工指定（`mk code 课名 代号`） | 最高，永不自动覆盖 |
| `idnumber` | Moodle 官方「课程编号」字段 | 权威（XMUM 为空） |
| `name` | 课名里的真代号（`MAT203`）；多个则需人指定 | 高 |
| `material` | 课程资料：syllabus 里的「Course Code」字段 → 退回文件名/目录名 | 高 / 中 |
| `teams` | 用 OneDrive/Teams 来源目录名交叉验证 | 中 |

Moodle 短名（`AAI` / `Stat` / `PDEs`）**不是**代号 —— 只作缩写显示，绝不写进 `code`。
"""
from __future__ import annotations

import os
import re
import glob

# 「字母+数字」那种真代号：MAT203 / MPU1022 / MAT211 / MAT418
CODE_RE = re.compile(r"\b([A-Z]{2,6}\d{3,4}[A-Z]?)\b")
# 明确写着「课程代号」的地方（syllabus 里最靠得住）
LABELED_RE = re.compile(
    r"(?:course\s*codes?|subject\s*code|module\s*code|课程代号|课程编号|课程代码)\s*[:：]?\s*"
    r"((?:[A-Z]{2,6}\d{3,4}[A-Z]?)(?:\s*[/、,，&and]+\s*[A-Z]{2,6}\d{3,4}[A-Z]?)*)",
    re.IGNORECASE)
SYLLABUS_HINT = ("course information", "course outline", "course-info", "syllabus",
                 "课程信息", "教学大纲", "courseinfo")
SOURCE_LABEL = {"manual": "人工指定", "idnumber": "Moodle 官方编号", "name": "课名里",
                "material": "课程资料", "material-name": "文件名/目录名", "teams": "Teams 来源",
                "shortname": "Moodle 短名（不是代号）"}


def extract_codes(text, labeled_only=False):
    """文本里出现的课程代号（去重、保序）。labeled_only=True 时只认明写「Course Code」的地方。"""
    text = str(text or "")
    out = []
    for m in LABELED_RE.finditer(text):
        for tok in CODE_RE.findall(m.group(1)):
            if tok not in out:
                out.append(tok)
    if labeled_only:
        return out
    for tok in CODE_RE.findall(text):
        if tok not in out:
            out.append(tok)
    return out


def _pdf_text_head(path, pages=2):
    """PDF 前几页文本；没装解析器/读不动就返回空串（绝不因为读不了资料而报错）。"""
    try:
        import fitz  # pymupdf
    except Exception:
        return ""
    try:
        doc = fitz.open(path)
        txt = "\n".join(str(doc[i].get_text()) for i in range(min(pages, doc.page_count)))
        doc.close()
        return txt
    except Exception:
        return ""


def _syllabus_first(paths):
    """课程资料里先把「像大纲」的排前面（Course-Information / syllabus / outline…）。"""
    def rank(p):
        low = os.path.basename(p).lower()
        return (0 if any(h in low for h in SYLLABUS_HINT) else 1, low)
    return sorted(paths, key=rank)


def _codes_in_dir(path, deep=False):
    """课程落盘目录里能找到的代号 → [(code, 来源说明)]，按可信度排序。

    deep=True 时读 syllabus 的「Course Code」字段；否则只看目录名与文件名（快、每次扫描都能跑）。
    """
    out = []
    if not path or not os.path.isdir(path):
        return out
    for tok in extract_codes(os.path.basename(os.path.normpath(path))):
        out.append((tok, "目录名"))
    files = []
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            if n.startswith("."):
                continue
            files.append(os.path.join(root, n))
    for full in files:
        base = os.path.basename(full)
        # 只认「像本课大纲」的文件名：共享教材、别的课留下的文件都可能带着**别的课**的代号
        # （实测坑：课程文件夹里的教材/杂项文件名会给出错误代号）
        if not any(h in base.lower() for h in SYLLABUS_HINT):
            continue
        for tok in extract_codes(base):
            out.append((tok, "文件名 %s" % base))
    if deep:
        for full in _syllabus_first([f for f in files if f.lower().endswith(".pdf")])[:4]:
            text = _pdf_text_head(full)
            for tok in extract_codes(text, labeled_only=True):
                out.append((tok, "%s 里的 Course Code" % os.path.basename(full)))
            if not extract_codes(text, labeled_only=True):
                for tok in extract_codes(text)[:6]:
                    out.append((tok, "%s 正文" % os.path.basename(full)))
    # 去重（保留第一次出现的说法）
    seen, uniq = set(), []
    for tok, why in out:
        if tok not in seen:
            seen.add(tok)
            uniq.append((tok, why))
    return uniq


def lookup(entry, teams_sources=None, deep=True, courses=None):
    """查一门课的代号。返回 dict：

    status: confirmed（一个候选，可信来源）｜multi（多个候选，要人指定）｜none（没查到）
    code: 确认的代号（未确认时为 ""）
    source / why: 从哪来、证据说明
    candidates: 所有候选（含来源说明），供人做决定
    """
    entry = entry or {}
    name = str(entry.get("name") or "")
    shortname = str(entry.get("shortname") or "")
    stored = str(entry.get("code") or "").strip()
    stored_src = str(entry.get("code_source") or "").strip()
    cands = []

    if stored and stored_src == "manual":
        return {"status": "confirmed", "code": stored, "source": "manual",
                "why": "你指定的（不再自动改）", "candidates": [(stored, "人工指定")]}

    name_codes = [t for t in extract_codes(name) if t != shortname.upper()]
    for tok in name_codes:
        cands.append((tok, "课名"))
    if len(name_codes) == 1:
        return {"status": "confirmed", "code": name_codes[0], "source": "name",
                "why": "课名里只有一个代号", "candidates": cands}
    if len(name_codes) > 1 and stored in name_codes:
        # 名字里挂着多个代号（MPU1022/MPU3322 这种按批次/专业分的）→ 你在配置里指定的那个为准
        return {"status": "confirmed", "code": stored, "source": "name",
                "why": "课名里多个代号，按配置指定的这个", "candidates": cands}

    mat = _codes_in_dir(entry.get("path"), deep=deep)
    for tok, why in mat:
        cands.append((tok, why))
    strong = [c for c in mat if "Course Code" in c[1]]
    if len(strong) == 1:
        return {"status": "confirmed", "code": strong[0][0], "source": "material",
                "why": strong[0][1], "candidates": cands}
    mat_codes = list(dict.fromkeys(t for t, _ in mat))
    if len(mat_codes) == 1 and not name_codes:
        source = "material" if any("Course Code" in w for _, w in mat) else "material-name"
        return {"status": "confirmed", "code": mat_codes[0], "source": source,
                "why": mat[0][1], "candidates": cands}

    # Teams 交叉验证：拿 OneDrive/Teams 来源的目录名对一遍
    team_codes = []
    for key, info in (teams_sources or {}).items():
        if not isinstance(info, dict):
            continue
        blob = "%s %s %s" % (info.get("name") or "", info.get("source") or "",
                             info.get("path") or "")
        hits = [t for t in extract_codes(blob) if t != str(info.get("shortname") or "").upper()]
        # 「交叉验证」的前提是**同一门课**：Teams 来源名字跟这门课的课名要有共同实词
        # （或代号直接对上）。少了这一条，没代号的课会把别的 Teams 来源的代号认成自己的
        # —— 实测坑：Abstract Algebra I 被认成 MAT201（那是另一门 Teams 课）。
        same_course = bool(set(_words(name)) & set(_words(info.get("name") or ""))) or \
            bool(name_codes and set(hits) & set(name_codes))
        if hits and same_course:
            for tok in hits:
                cands.append((tok, "Teams 来源 %s" % (info.get("name") or key)))
                team_codes.append(tok)
    if not name_codes and len(set(team_codes)) == 1:
        return {"status": "confirmed", "code": team_codes[0], "source": "teams",
                "why": "Teams 来源目录名", "candidates": cands}

    pool = list(dict.fromkeys(t for t, _ in cands if t != shortname.upper()))
    if len(pool) == 1:
        why = next(w for t, w in cands if t == pool[0])
        src = "material" if "Course Code" in why else ("teams" if "Teams" in why else "material-name")
        return {"status": "confirmed", "code": pool[0], "source": src, "why": why, "candidates": cands}
    if len(pool) > 1:
        return {"status": "multi", "code": "", "source": "", "why": "有 %d 个候选，需要你指定（mk code <课名> <代号>）" % len(pool),
                "candidates": cands}
    return {"status": "none", "code": "", "source": "", "why": "Moodle 与课程资料里都没找到", "candidates": cands}


def _words(name):
    return [w.lower() for w in re.findall(r"[A-Za-z]{4,}", str(name or ""))]


def enrich(courses, cfg=None, teams_sources=None, deep=True, write=False):
    """给所有课程查一遍代号。write=True 时把**确认了的**写回条目。

    返回 [{key, name, old, status, code, source, why, candidates, changed}]。
    """
    rows = []
    for key, entry in (courses or {}).items():
        if not isinstance(entry, dict):
            continue
        res = lookup(entry, teams_sources=teams_sources, deep=deep)
        old = str(entry.get("code") or "")
        row = {"key": key, "name": entry.get("name") or key, "shortname": entry.get("shortname") or "",
               "old": old, "old_source": entry.get("code_source") or "", **res}
        row["changed"] = bool(write and res["status"] == "confirmed" and res["code"]
                              and (res["code"] != old or entry.get("code_source") != res["source"]))
        if row["changed"]:
            entry["code"] = res["code"]
            entry["code_source"] = res["source"]
            entry.setdefault("code_shortname", str(entry.get("shortname") or ""))
        rows.append(row)
    return rows


def label(entry):
    """人类可读的代号说明，如「MAT211（课程资料）」。没确认就照实说。"""
    code = str((entry or {}).get("code") or "")
    src = str((entry or {}).get("code_source") or "")
    if not code:
        return "未确认"
    return "%s（%s）" % (code, SOURCE_LABEL.get(src, src or "来源未知"))

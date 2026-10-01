#!/usr/bin/env python3
"""Moodle HTTP 客户端（开源版）：登录 / 通知 / 课程发现 / 课程活动 / 下载。

从 moodle_scan.py 提炼的通用客户端——**无任何硬编码凭据**，全部经 appconfig 读配置。
保留原 scan 的解析逻辑（已在生产验证），供 moodle_prep.py 复用。
"""
import hashlib
import json
import locale
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

import config_store as cs


class _Links(HTMLParser):
    """收集链接和可见名称，兼容 Moodle 常见的 span / 图标嵌套。

    Moodle 把活动类型标签（如「文件」「File」）塞在 class="accesshide" 的 span 里，
    那是给读屏软件用的，不该混进名字——否则下载出来会变成 Course-Information-文件.pdf。
    """
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.links, self.href, self.parts = [], None, []
        self.stack = []          # 每层 span/div 是不是「读屏专用」的文字
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in ("span", "div"):
            self.stack.append("accesshide" in (dict(attrs).get("class") or ""))
        if tag == "a":
            self.href, self.parts = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self.href is not None and not any(self.stack):
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append((self.href, re.sub(r"\s+", " ", "".join(self.parts)).strip()))
            self.href = None
        if tag in ("span", "div") and self.stack:
            self.stack.pop()


# ── 文件名规则（download.naming）────────────────────────────────────────────
NAMING_MODES = ("default", "plain", "original", "custom")
DEFAULT_NAME_TEMPLATE = "{code}-{name}"

# custom 模板里能用的占位符（文档 / mk naming 都用这份）
TEMPLATE_FIELDS = {
    "code": "课程代号（MAT203）",
    "course": "课程名（Statistics）",
    "name": "原文件名，不含后缀（Animals）",
    "ext": "后缀，含点（.pdf）",
    "type": "类型桶（Assignment / Slides / …）",
    "folder": "Moodle 资料夹名（Lecture Notes (2026/09)）",
    "date": "下载日期（20260929）",
    "hash": "唯一短摘要（只有资料夹里的文件才有）",
    "cmid": "Moodle 活动 id",
}


def safe_component(text):
    """清成能当文件名的一段：去路径分隔符/非法字符，保留中文，空格→横线。

    规则与落地时的清洗（`download_file` 里的 `safe = re.sub(r"[^\\w\\s-]", "", stem)`）**保持一致**，
    否则 `mk naming` 给你看的名字会和真落地的名字不一样——预览骗人比不预览更糟。
    """
    s = str(text or "").replace("/", " ").replace("\\", " ")
    s = re.sub(r"[^\w\s-]", " ", s)
    s = re.sub(r"[-\s]+", "-", s)
    return s.strip("-")


def _finish(name):
    """最后一道：把后缀摘出来单独保住，主体走 safe_component（别把 `.pdf` 里的点当非法字符清掉）。"""
    raw = str(name or "")
    stem, ext = os.path.splitext(raw)
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
        stem, ext = raw, ""
    return safe_component(stem) + ext


def derive_course_code(course_name, fallback=""):
    """从课名里抠课程代号：`MAT203 Statistics 2026/09` → `MAT203`。

    抠不到就退回 fallback（Moodle 短名，如 Stat / PDEs / AAI）；都没有就返回空串，
    调用方会退化成「不带代号」的名字，绝不因此丢文件。
    """
    m = re.match(r"\s*([A-Za-z]{2,6})[\s-]?(\d{2,4})\b", str(course_name or ""))
    if m:
        return "%s%s" % (m.group(1).upper(), m.group(2))
    return safe_component(fallback)


def resolve_course_code(entry, course_name=""):
    """课程代号优先级：courses.json 里手填的 `code` > 课名里抠 > Moodle 短名。"""
    entry = entry or {}
    explicit = str(entry.get("code") or "").strip()
    if explicit:
        return safe_component(explicit)
    return derive_course_code(entry.get("name") or course_name, entry.get("shortname") or "")


def build_filename(stem, ext, *, mode="default", template="", code="", course="",
                   kind="", folder="", digest="", cmid="", day="", legacy=""):
    """按用户选的规则算出落地文件名（不含目录）。纯函数，方便单测。

    - `default`  → `<课程代号>-<原文件名>`，如 `MAT203-Animals.txt`（没有代号时自动少掉那根横线）
    - `plain`    → 只用原文件名，如 `Animals.txt`
    - `original` → Moodle 原始名（资料夹里的文件带活动 ID + 摘要，绝对唯一，老用户兼容）
    - `custom`   → 用 `download.name_template` 渲染（占位符见 TEMPLATE_FIELDS）

    模板写错、渲染为空、代号缺失等情况一律退回原名——**宁可名字朴素，也不能丢文件**。
    """
    stem = re.sub(r"\s+", " ", str(stem or "")).strip()
    ext = str(ext or "")
    plain = "%s%s" % (stem, ext) if stem else ""
    if mode == "original":
        return _finish(legacy or plain)
    if mode == "plain":
        return _finish(plain)
    if mode not in NAMING_MODES:
        mode = "default"
    tpl = (str(template).strip() or DEFAULT_NAME_TEMPLATE) if mode == "custom" else DEFAULT_NAME_TEMPLATE
    values = {
        "code": safe_component(code),
        "course": safe_component(course),
        "name": stem,
        "ext": ext,
        "type": safe_component(kind),
        "folder": safe_component(folder),
        "date": str(day or ""),
        "hash": str(digest or ""),
        "cmid": str(cmid or ""),
    }
    out = tpl
    for key, val in values.items():
        out = out.replace("{%s}" % key, val)
    out = re.sub(r"\{[^}]*\}", "", out)     # 模板里写错的占位符直接丢掉，别让它变成文件名
    out = re.sub(r"\s+", " ", out).strip()
    if not out:
        return _finish(plain)
    if ext and not out.lower().endswith(ext.lower()):
        # 模板没给后缀时补上；给了就一个都不多写
        out += ext
    return _finish(out) or _finish(plain)


def unique_path(fp, content):
    """同名文件已存在时怎么办：内容一样就复用原名（重跑不长出副本），不一样才排 -2/-3…。

    只做“不覆盖别人”这一件事——静默覆盖会把两个不同文件写成一个，那是丢数据。
    """
    if not os.path.exists(fp):
        return fp
    try:
        with open(fp, "rb") as f:
            if f.read() == content:
                return fp
    except OSError:
        pass
    base, ext = os.path.splitext(fp)
    for i in range(2, 100):
        cand = "%s-%d%s" % (base, i, ext)
        if not os.path.exists(cand):
            return cand
    return "%s-%d%s" % (base, int(time.time()), ext)


class MoodleClient:
    def __init__(self, base_url=None, username=None, password=None, state_dir=None):
        cfg = cs.load_config()
        url = cs.get_path(cfg, "moodle.url") or ""
        user = cs.get_path(cfg, "moodle.user") or ""
        pwd = cs.get_path(cfg, "moodle.password") or ""
        self.cfg = cfg
        self.base_url = (base_url or url).rstrip("/")
        self.username = username or user
        self.password = password or pwd
        self.state_dir = state_dir or str(cs.state_dir())
        os.makedirs(self.state_dir, exist_ok=True)
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        self.notifications = []
        self._creds_ok = bool(self.username and self.password)
        if not self._creds_ok:
            raise SystemExit("❌ 还没填 Moodle 账号。跑 `mk setup`，或 `mk set 账号 你的学号` + `mk set 密码 xxx`")

    # ---------- 登录 ----------
    def login(self):
        r = self.session.get(f"{self.base_url}/login/index.php")
        r.raise_for_status()
        m = re.search(r'name="logintoken" value="([^"]+)"', r.text)
        if not m:
            print("❌ 找不到登录 token")
            return False
        r = self.session.post(f"{self.base_url}/login/index.php", data={
            "logintoken": m.group(1), "username": self.username, "password": self.password,
        }, allow_redirects=True)
        if "loginerrors" in r.text or "Invalid login" in r.text:
            print("❌ 登录失败（账号或密码错误）")
            return False
        r = self.session.get(f"{self.base_url}/my/")
        if self.username in r.text or "logout" in r.text.lower():
            print("✅ 登录成功")
            return True
        print("⚠️ 登录态未确认，继续尝试")
        return True

    # ---------- 通知 ----------
    def check_notifications(self):
        r = self.session.get(f"{self.base_url}/message/output/popup/notifications.php")
        r.raise_for_status()
        for pat in (
            r'<div class="notification[^"]*"[^>]*>.*?<a[^>]*>([^<]+)</a>.*?</div>',
            r'class="notification".*?<a[^>]*>([^<]+)</a>',
            r'<div class="card-body"[^>]*>.*?<h6[^>]*>([^<]+)</h6>',
            r'<div class="media-body"[^>]*>.*?class="subject"[^>]*>([^<]+)',
        ):
            for m_ in re.findall(pat, r.text, re.DOTALL)[:10]:
                clean = re.sub(r"<[^>]+>", "", m_).strip()
                if clean and clean not in self.notifications:
                    self.notifications.append(clean)
        try:
            r2 = self.session.get(
                f"{self.base_url}/message/output/popup/notifications.php?view=1&limit=10&offset=0")
            for m_ in re.finditer(r'<a[^>]*class="[^"]*subject[^"]*"[^>]*>([^<]+)</a>', r2.text):
                clean = m_.group(1).strip()
                if clean and clean not in self.notifications:
                    self.notifications.append(clean)
        except Exception:
            pass
        return self.notifications

    # ---------- 课程发现（setup 用）----------
    def discover_courses(self):
        """列出该账号的已选课程。

        ⚠️ `/my/` 只列最近 **10 门**，更多的藏在「更多……」那一页，而那一页是 JS 渲染的、
        静态抓不到（实测 0 个课程链接）。所以还要问一次 Moodle 自己的接口，两边取并集——
        否则新学期刚加上的课会被 10 门上限挤掉，表现为「明明在 Moodle 里却抓不到」。
        """
        courses, seen = [], {}

        def remember(cid, name, shortname=""):
            cid = str(cid or "")
            # 接口给的 fullname 是 HTML 转义过的（实测："Integrity &amp; Anti-Corruption"）。
            # 不还原就会漏进 courses.json、推送文本和自动生成的下载目录名里。
            name = unescape(re.sub(r"\s+", " ", name or "")).strip()
            shortname = unescape(re.sub(r"\s+", " ", shortname or "")).strip()
            if not (cid.isdigit() and name):
                return
            cur = seen.get(cid)
            if cur is None:
                seen[cid] = {"id": int(cid), "name": name}
                if shortname:
                    seen[cid]["shortname"] = shortname
                courses.append(seen[cid])
                return
            # Moodle 课程页会把长课名截成 `…` 结尾，接口给的才是全名。
            # 先扫到的是页面那份（截断版），所以同一个 id 后来拿到更长、
            # 且自己不是截断版的名字时必须覆盖——否则课名会一直停在省略号，
            # 连带 courses.json 和推送里都是「Partial Differential Equations 2...」。
            if len(name) > len(cur["name"]) and not name.endswith("..."):
                cur["name"] = name
            # 短名只有接口有（如 Stat / PDEs / AAI），课名里抠不出课程代号时拿它兜底。
            if shortname and not cur.get("shortname"):
                cur["shortname"] = shortname

        for u in (f"{self.base_url}/my/", f"{self.base_url}/course/index.php"):
            try:
                r = self.session.get(u)
            except Exception:
                continue
            for href, name in _Links(r.text).links:
                if "course/view.php" in href:
                    m_ = re.search(r"[?&]id=(\d+)", href)
                    if m_:
                        remember(m_.group(1), name)
        timeline = self.courses_timeline()
        cls_of = {}
        for cls in ("inprogress", "future", "past"):
            for c in timeline.get(cls) or []:
                if isinstance(c, dict) and c.get("id") is not None:
                    cls_of.setdefault(str(c["id"]), cls)
        for cls in self.TIMELINE_CLASSES:
            for c in timeline.get(cls) or []:
                if isinstance(c, dict):
                    remember(c.get("id"), c.get("fullname") or c.get("shortname"),
                             c.get("shortname") or "")
        for c in courses:
            # 页面扫到的旧课不在任何分类里 → unknown（不是「上学期」，别拿它当依据删课）
            c["timeline"] = cls_of.get(str(c["id"]), "unknown")
        return courses

    # Moodle 自己的学期分类。前端只显示「本学期」，接口能分开给 —— 这是分清
    # 「本学期 vs 上学期」唯一可靠依据：`/my/` 页面给的是「最近访问」，
    # 实测 10 门里混着 6 门去年的课，拿它当「已选课」会一直误判。
    TIMELINE_CLASSES = ("inprogress", "future", "past", "all")

    def courses_timeline(self):
        """按学期分类取已选课程 → {"inprogress": [...], "future": [...], "past": [...], "all": [...]}。

        读不到（没 sesskey、接口报错、网络不通）返回 **空 dict** —— 调用方必须按
        「学期未知」处理，绝不据空结果判断某门课是不是上学期的。
        """
        try:
            r = self.session.get(f"{self.base_url}/my/")
            m_ = re.search(r'"sesskey":"(\w+)"', r.text) or \
                re.search(r'name="sesskey" value="(\w+)"', r.text)
            if not m_:
                return {}
            method = "core_course_get_enrolled_courses_by_timeline_classification"
            payload = [{"index": i, "methodname": method,
                        "args": {"offset": 0, "limit": 0, "classification": cls, "sort": "fullname"}}
                       for i, cls in enumerate(self.TIMELINE_CLASSES)]
            resp = self.session.post(
                f"{self.base_url}/lib/ajax/service.php?sesskey={m_.group(1)}&info={method}",
                data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=30)
            entries = resp.json() or []
        except Exception:
            return {}
        out = {}
        for cls, entry in zip(self.TIMELINE_CLASSES, entries):
            if isinstance(entry, dict) and not entry.get("error"):
                out[cls] = ((entry.get("data") or {}).get("courses")) or []
        return out

    def _enrolled_courses_api(self):
        """打平的四类结果（保留给老调用方；新代码请用 courses_timeline）。"""
        tl = self.courses_timeline()
        out = []
        for cls in self.TIMELINE_CLASSES:
            out.extend(tl.get(cls) or [])
        return out

    # ---------- 完成度（勾 Done）----------
    def get_sesskey(self):
        """拿当前会话的 sesskey（写操作必须带）。掉登录时返回 None。"""
        try:
            r = self.session.get(f"{self.base_url}/my/")
            m_ = re.search(r'"sesskey":"(\w+)"', r.text) or \
                re.search(r'name="sesskey" value="(\w+)"', r.text)
            return m_.group(1) if m_ else None
        except Exception:
            return None

    def get_course_completion(self, course_id):
        """读课程页每个活动的完成状态。

        返回 (states, names)：
          states = {cmid: True 已完成 / False 未完成}，**只含页面上真的有那个开关的活动**
                   （站点没开完成度、或该活动不支持手动勾的，不会出现在这里 → 调用方按「跳过」处理）
          names  = {cmid: 活动名}，用于输出可追溯
        页面上的开关取值：`manual:undo` = 已完成；`manual:mark-done` = 未完成。
        """
        states, names = {}, {}
        try:
            r = self.session.get(f"{self.base_url}/course/view.php?id={course_id}")
        except Exception:
            return states, names
        for tag in re.finditer(r'<button[^>]*data-action="toggle-manual-completion"[^>]*>', r.text):
            raw = tag.group(0)
            m_toggle = re.search(r'data-toggletype="([^"]+)"', raw)
            m_cmid = re.search(r'data-cmid="(\d+)"', raw)
            if not (m_toggle and m_cmid):
                continue
            cmid = int(m_cmid.group(1))
            states[cmid] = m_toggle.group(1).endswith("undo")
            m_name = re.search(r'data-activityname="([^"]*)"', raw)
            if m_name:
                names[cmid] = m_name.group(1)
        return states, names

    def set_activity_completion(self, cmid, completed=True):
        """把一个活动勾成「已完成」。

        **只允许勾上**：`completed=False` 会直接抛错——取消别人的完成度不在本工具职责内，
        代码层面堵死，任何 bug 都不可能把进度拉下来。

        成功 = 五个条件全中（缺一不可）：
          HTTP 200 · JSON 可解析 · 顶层无 error/exception · data.status 为真 · warnings 为空
        返回 (ok, detail)。
        """
        if not completed:
            raise ValueError("只允许 completed=True（本工具永不取消完成度）")
        method = "core_completion_update_activity_completion_status_manually"
        sesskey = self.get_sesskey()
        if not sesskey:
            return False, "拿不到 sesskey：登录态已失效，本轮不写"
        try:
            resp = self.session.post(
                f"{self.base_url}/lib/ajax/service.php?sesskey={sesskey}&info={method}",
                data=json.dumps([{"index": 0, "methodname": method,
                                  "args": {"cmid": int(cmid), "completed": True}}]),
                headers={"Content-Type": "application/json"}, timeout=30)
        except Exception as e:
            return False, "请求失败：%s" % e
        if resp.status_code != 200:
            return False, "HTTP %s" % resp.status_code
        try:
            out = resp.json()
        except Exception:
            return False, "返回不是 JSON（多半被登录页顶掉了）"
        entry = (out or [{}])[0] if isinstance(out, list) else (out or {})
        if entry.get("error") or entry.get("exception"):
            code = (entry.get("exception") or {}).get("errorcode") or "error"
            msg = (entry.get("exception") or {}).get("message") or ""
            return False, "%s %s" % (code, msg)
        data = entry.get("data") or {}
        if data.get("status") is not True:
            # 活动没开手动完成度、或不是手动类型 —— 这不是写失败，是「不能勾」，调用方按跳过处理
            return False, "not_toggleable（status=%s）" % data.get("status")
        if data.get("warnings"):
            return False, "warnings: %s" % str(data.get("warnings"))[:120]
        return True, "已勾完成"

    # ---------- 课程活动 ----------
    def get_course_activities(self, course_id):
        r = self.session.get(f"{self.base_url}/course/view.php?id={course_id}")
        r.raise_for_status()
        html = r.text
        acts = {"resources": {}, "assignments": {}, "urls": {}, "quizzes": {}, "forums": {},
                "activities": {}}
        folders = {}
        for href, name in _Links(html).links:
            full = urljoin(r.url, href)
            parsed = urlparse(full)
            iid = (parse_qs(parsed.query).get("id") or [""])[0]
            if not iid.isdigit() or not name or parsed.netloc != urlparse(self.base_url).netloc:
                continue
            if "/mod/resource/" in full:
                acts["resources"].setdefault(iid, {"name": name, "url": full})
            elif "/mod/folder/" in full:
                folders.setdefault(iid, {"name": name, "url": full})
            elif "/mod/assign/" in full:
                acts["assignments"].setdefault(iid, {"name": name, "url": full})
            elif "/mod/url/" in full:
                acts["urls"].setdefault(iid, {"name": name, "url": full})
            elif "/mod/quiz/" in full:
                acts["quizzes"].setdefault(iid, {"name": name, "url": full})
            elif "/mod/forum/" in full:
                acts["forums"].setdefault(iid, {"name": name, "url": full})
        # 全类型活动清单：只认 resource/folder/assign/url/quiz/forum 是不够的——
        # 老师加的「网页 / 图书 / 标签 / 外部工具」等类型会被整块丢掉，
        # 表现就是「Moodle 上明明有东西，它从没提过」（实测：Anti-Corruption 的
        # 一个 page 活动从来没出现在任何信号里）。这里把所有 /mod/<类型>/ 都收下，
        # 类型标记留给上层决定怎么报。
        for href, name in _Links(html).links:
            full = urljoin(r.url, href)
            parsed = urlparse(full)
            mm = re.search(r"/mod/([a-z_]+)/", parsed.path)
            iid = (parse_qs(parsed.query).get("id") or [""])[0]
            if not (mm and iid.isdigit() and name and parsed.netloc == urlparse(self.base_url).netloc):
                continue
            acts["activities"].setdefault(iid, {"name": name, "type": mm.group(1), "url": full})
        if folders:
            with ThreadPoolExecutor(max_workers=min(6, len(folders))) as executor:
                future_to_folder = {executor.submit(self.get_folder_files, iid, folder): iid for iid, folder in folders.items()}
                for f in as_completed(future_to_folder):
                    try:
                        acts["resources"].update(f.result())
                    except Exception:
                        pass
        return acts

    def get_folder_files(self, folder_id, folder):
        """资料夹逐文件入队：忽略下载参数，新增文件仍能被下一次差量发现。"""
        r = self.session.get(folder["url"], timeout=30)
        r.raise_for_status()
        resources = {}
        for href, _name in _Links(r.text).links:
            full = urljoin(r.url, href)
            parsed = urlparse(full)
            if (parsed.netloc != urlparse(self.base_url).netloc or
                    "/pluginfile.php/" not in parsed.path or "/mod_folder/content/" not in parsed.path):
                continue
            # 同名文件可出现在不同子目录。身份与落地名都带路径摘要，避免互相覆盖。
            identity = unquote(parsed.path)
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
            filename = identity.rsplit("/", 1)[-1]
            stem, ext = os.path.splitext(filename)
            key = "folder:%s:%s" % (folder_id, digest)
            resources.setdefault(key, {
                "name": "%s / %s" % (folder["name"], filename), "url": full,
                "download_name": "%s-%s-%s%s" % (folder_id, stem, digest, ext),
                # 命名规则要用到的原件信息（folder 名 / 真实文件名 / 唯一摘要）
                "folder": folder["name"], "filename": filename, "hash": digest,
            })
        return resources

    def check_assignment_details(self, assign_url):
        try:
            r = self.session.get(assign_url)
            r.raise_for_status()
            html = r.text
            res = {"name": "", "due": "", "status": "未知", "graded": "未评分"}
            m_ = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
            if m_:
                res["name"] = m_.group(1).strip()
            for pat in (r"Due\s*date[^:]*:\s*([^<]+)", r'class="[^"]*due[^"]*"[^>]*>([^<]+)',
                        r"Cut-off\s*date[^:]*:\s*([^<]+)"):
                dm = re.search(pat, html, re.IGNORECASE)
                if dm:
                    res["due"] = dm.group(1).strip()
                    break
            if not res["due"]:
                # 中文标签、表格单元格和新版 activity-dates 都先还原成可见文字。
                plain = unescape(re.sub(r"<[^>]+>", " ", html))
                dm = re.search(r"(?:截止日期|截止时间|到期日期|Due\s*date|Due|Cut-off\s*date)\s*[:：]?\s*"
                               r"(.+?)(?=提交状态|评分状态|Submission|Grading|$)",
                               re.sub(r"\s+", " ", plain), re.IGNORECASE)
                if dm:
                    date = re.search(r"\d{4}\s*(?:年|[-/.])\s*\d{1,2}\s*(?:月|[-/.])\s*\d{1,2}"
                                     r"\s*日?[^\d]{0,20}\d{1,2}[:：]\d{2}|"
                                     r"(?:[A-Za-z]+,\s*)?\d{1,2}\s+[A-Za-z]+\s+\d{4},?\s*"
                                     r"\d{1,2}:\d{2}(?:\s*[AP]M)?", dm.group(1), re.IGNORECASE)
                    if date:
                        res["due"] = date.group(0).strip()
            if "submissionstatus" in html:
                sm = re.search(r'class="submissionstatussubmitted[^"]*"[^>]*>([^<]+)', html)
                if sm:
                    res["status"] = "已提交"
                elif 'id="id_submitbutton"' in html or "add submission" in html.lower():
                    res["status"] = "未提交"
            tm = re.search(r"Submission\s*status[^<]*<td[^>]*>\s*([^<]+)", html, re.DOTALL)
            if tm:
                res["status"] = tm.group(1).strip()
            gm = re.search(r"Grading\s*status[^<]*<td[^>]*>\s*([^<]+)", html, re.DOTALL)
            if gm:
                res["graded"] = gm.group(1).strip()
            return res
        except Exception as e:
            return {"name": "", "due": "", "status": f"错误: {e}", "graded": ""}

    # ---------- 下载 ----------
    def type_subdir(self, resource_name):
        """按文件名猜类型，返回子文件夹名（高级设置 download.by_type 用）。

        规则来自 config.yaml 的 download.type_map，用户可以自己加关键词。
        """
        tmap = cs.get_path(self.cfg, "download.type_map") or {}
        low = (resource_name or "").lower()
        for folder, keywords in tmap.items():
            for kw in (keywords or []):
                if kw and str(kw).lower() in low:
                    return folder
        return "Other"

    def download_file(self, resource_url, resource_name, download_dir, subdir=None):
        if subdir:
            download_dir = os.path.join(download_dir, subdir)
        os.makedirs(download_dir, exist_ok=True)
        stem, named_ext = os.path.splitext(resource_name)
        # 无扩展名的 Moodle 活动标题沿用旧命名；有扩展名时避免 lecturepdf.pdf。
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", named_ext):
            stem, named_ext = resource_name, ""
        safe = re.sub(r"[^\w\s-]", "", stem)
        safe = re.sub(r"[-\s]+", "-", safe).strip("-") or "file"
        r = self.session.get(resource_url, allow_redirects=True, timeout=60)
        r.raise_for_status()

        def _save(content, ext):
            fp = os.path.join(download_dir, f"{safe}{ext}")
            # 命名规则（尤其 default）会重名：内容一样就复用，不一样才排 -2，绝不互相覆盖
            fp = unique_path(fp, content)
            with open(fp, "wb") as f:
                f.write(content)
            print(f"   ✅ 已下载: {os.path.basename(fp)} ({len(content)} bytes)")
            return os.path.basename(fp), True

        def _file_response(resp):
            """只保存文件响应，登录页/错误页不能伪装成下载成功。"""
            ct = resp.headers.get("Content-Type", "").lower()
            prefix = resp.content.lstrip()[:100].lower()
            attachment = "attachment" in resp.headers.get("Content-Disposition", "").lower()
            if not resp.content or (("html" in ct or prefix.startswith((b"<!doctype html", b"<html")))
                                    and not attachment):
                return None
            ext = named_ext or os.path.splitext(unquote(urlparse(resp.url).path))[1]
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext) or ext.lower() == ".php":
                ext = ""
            if "pdf" in ct or resp.content[:4] == b"%PDF":
                ext = ".pdf"
            elif resp.content[:4] == b"PK\x03\x04" or "application/zip" in ct:
                ext = ext if ext.lower() in (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp") else ".zip"
            return _save(resp.content, ext or ".bin")

        saved = _file_response(r)
        if saved:
            return saved
        for href, _name in _Links(r.text).links:
            fu = urljoin(r.url, href)
            if "pluginfile.php" not in urlparse(fu).path:
                continue
            fr = self.session.get(fu, allow_redirects=True, timeout=60)
            fr.raise_for_status()
            saved = _file_response(fr)
            if saved:
                return saved
        ct = r.headers.get("Content-Type", "")
        print(f"   ⚠️ 无法下载 {resource_name} (content-type: {ct})")
        return None, False

    # ---------- 状态跟踪（diff 出新文件）----------
    def _state_path(self, course_id):
        return os.path.join(self.state_dir, f"course_{course_id}.json")

    def diff_new_files(self, course_id, activities):
        sp = self._state_path(course_id)
        prev = {}
        if os.path.exists(sp):
            try:
                try:
                    with open(sp, encoding="utf-8") as f:
                        prev = json.load(f)
                except UnicodeDecodeError:
                    # 旧版 Windows 用系统代码页写状态；首次读取后统一转为 UTF-8。
                    with open(sp, encoding=locale.getpreferredencoding(False)) as f:
                        prev = json.load(f)
            except Exception:
                prev = {}
        prev_res = prev.get("resources", {})
        new = {iid: info for iid, info in activities.get("resources", {}).items()
               if iid not in prev_res}
        acts_now = activities.get("activities", {})
        # 文件/文件夹/作业已有各自的出口（下载 / 作业逻辑），这里只挑「以前没有出口」的类型，
        # 否则同一条会报两次。
        old_acts = prev.get("activities")
        new_acts = {}
        if old_acts is not None:
            new_acts = {iid: info for iid, info in acts_now.items()
                        if iid not in old_acts and info.get("type") not in ("resource", "folder", "assign")}
        new_state = {
            "last_scan": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "resources": {k: v["name"] for k, v in activities.get("resources", {}).items()},
            "urls": {k: v["name"] for k, v in activities.get("urls", {}).items()},
            # activities 缺席 = 还没建基线（升级前的老 state，或刚接上的新课）→ 上层只写基线不报，
            # 否则第一次升级就会把历史上所有网页/测验一次性翻出来。
            "activities": {k: v["name"] for k, v in acts_now.items()},
        }
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(new_state, f, ensure_ascii=False, indent=2)
        return new, prev.get("assignments", {}), new_acts

    # ---------- 单课扫描（prep 主入口）----------
    def pick_name(self, info, *, cmid="", code="", course="", kind="", mode="default",
                  template="", day=None):
        """把一条 Moodle 资源算成落地文件名（规则见 build_filename）。"""
        raw = info.get("filename") or info.get("name") or ""
        legacy = info.get("download_name") or raw
        stem, ext = os.path.splitext(raw)
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
            stem, ext = raw, ""
        return build_filename(stem, ext, mode=mode, template=template, code=code, course=course,
                              kind=kind or "", folder=info.get("folder", ""),
                              digest=info.get("hash", ""), cmid=cmid,
                              day=day or datetime.now().strftime("%Y%m%d"), legacy=legacy)

    def scan_course(self, course_id, course_name, download=True, download_dir=None, by_type=None,
                    course_code=""):
        # 状态文件缺失 = 这门课所有文件都会被判成「新」→ 完成度批量误勾的高风险场景，先记下来
        state_existed = os.path.exists(self._state_path(course_id))
        acts = self.get_course_activities(course_id)
        new_files, prev_assign, new_acts = self.diff_new_files(course_id, acts)
        result = {"name": course_name, "new_files": [], "assignments": {}, "new_activities": {},
                  "notes": [],
                  "completion_targets": {}, "state_existed": state_existed,
                  "course_id": course_id}
        if by_type is None:
            by_type = bool(cs.get_path(self.cfg, "download.by_type"))
        if download:
            mode = str(cs.get_path(self.cfg, "download.naming") or "default").strip().lower()
            template = str(cs.get_path(self.cfg, "download.name_template") or DEFAULT_NAME_TEMPLATE)
            code = course_code or derive_course_code(course_name)
            for iid, info in new_files.items():
                sub = self.type_subdir(info["name"]) if by_type else None
                name = self.pick_name(info, cmid=iid, code=code, course=course_name,
                                      kind=sub or "", mode=mode, template=template)
                shown = "%s  → %s/" % (name, sub) if sub else name
                print(f"   📥 下载: {shown}  （Moodle 名：{info['name']}）")
                try:
                    fname, ok = self.download_file(info["url"], name, download_dir or ".", subdir=sub)
                except (requests.RequestException, OSError):
                    ok = False
                if ok:
                    result["new_files"].append(info["name"])
                    # 只有真的下载成功、且磁盘上文件非空的活动才进「可勾」清单
                    cmid = self._completion_cmid(iid, info, download_dir or ".")
                    if cmid:
                        result["completion_targets"][cmid] = info["name"]
                else:
                    result["notes"].append("下载失败：%s" % info["name"])
                    # 不把失败文件记为已见；下次运行继续下载。
                    sp = self._state_path(course_id)
                    with open(sp, encoding="utf-8") as f:
                        state = json.load(f)
                    state["resources"].pop(iid, None)
                    with open(sp, "w", encoding="utf-8") as f:
                        json.dump(state, f, ensure_ascii=False, indent=2)
        else:
            result["new_files"] = [info["name"] for _, info in new_files.items()]
        result["new_activities"] = {iid: info for iid, info in new_acts.items()}
        assigns = acts.get("assignments", {})
        if assigns:
            with ThreadPoolExecutor(max_workers=min(6, len(assigns))) as executor:
                future_to_iid = {executor.submit(self.check_assignment_details, info["url"]): iid for iid, info in assigns.items()}
                for f in as_completed(future_to_iid):
                    iid = future_to_iid[f]
                    try:
                        result["assignments"][iid] = f.result()
                    except Exception as e:
                        result["assignments"][iid] = {"name": "", "due": "", "status": f"错误: {e}", "graded": ""}
        return result

    def _completion_cmid(self, iid, info, download_dir):
        """从资源 key 反解出「该勾哪一条活动」的 cmid。拿不准就返回 None（宁可不勾）。

        - 纯数字 key（`/mod/resource/view.php?id=<cmid>`）→ 这个数字就是 cmid
        - `folder:<fid>:<hash>`（资料夹里的一个文件）→ **整个资料夹只勾 `<fid>` 这一条**
        - 其它形态一律拒绝（绝不从 file URL 里瞎凑 id；`/course/view.php?id=` 那种是课程 ID，绝不能当 cmid）
        """
        iid = str(iid)
        if iid.isdigit():
            return int(iid)
        m_ = re.match(r"^folder:(\d+):", iid)
        if m_:
            return int(m_.group(1))
        return None

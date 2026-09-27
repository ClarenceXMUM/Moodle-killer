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
        courses, seen = [], set()
        for u in (f"{self.base_url}/my/", f"{self.base_url}/course/index.php"):
            try:
                r = self.session.get(u)
            except Exception:
                continue
            for m_ in re.finditer(r'href="[^"]*course/view\.php\?id=(\d+)"[^>]*>([^<]+)</a>', r.text):
                cid, name = m_.group(1), re.sub(r"\s+", " ", m_.group(2)).strip()
                if cid not in seen and name:
                    seen.add(cid)
                    courses.append({"id": int(cid), "name": name})
        return courses

    # ---------- 课程活动 ----------
    def get_course_activities(self, course_id):
        r = self.session.get(f"{self.base_url}/course/view.php?id={course_id}")
        r.raise_for_status()
        html = r.text
        acts = {"resources": {}, "assignments": {}, "urls": {}, "quizzes": {}, "forums": {}}
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
            with open(fp, "wb") as f:
                f.write(content)
            print(f"   ✅ 已下载: {safe}{ext} ({len(content)} bytes)")
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
        new_state = {
            "last_scan": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "resources": {k: v["name"] for k, v in activities.get("resources", {}).items()},
            "urls": {k: v["name"] for k, v in activities.get("urls", {}).items()},
        }
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(new_state, f, ensure_ascii=False, indent=2)
        return new, prev.get("assignments", {})

    # ---------- 单课扫描（prep 主入口）----------
    def scan_course(self, course_id, course_name, download=True, download_dir=None, by_type=None):
        acts = self.get_course_activities(course_id)
        new_files, prev_assign = self.diff_new_files(course_id, acts)
        result = {"name": course_name, "new_files": [], "assignments": {}, "notes": []}
        if by_type is None:
            by_type = bool(cs.get_path(self.cfg, "download.by_type"))
        if download:
            for iid, info in new_files.items():
                sub = self.type_subdir(info["name"]) if by_type else None
                if sub:
                    print(f"   📥 下载: {info['name']}  → {sub}/")
                else:
                    print(f"   📥 下载: {info['name']}")
                try:
                    fname, ok = self.download_file(info["url"], info.get("download_name", info["name"]),
                                                   download_dir or ".", subdir=sub)
                except (requests.RequestException, OSError):
                    ok = False
                if ok:
                    result["new_files"].append(info["name"])
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

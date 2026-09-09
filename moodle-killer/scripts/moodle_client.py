#!/usr/bin/env python3
"""Moodle HTTP 客户端（开源版）：登录 / 通知 / 课程发现 / 课程活动 / 下载。

从 moodle_scan.py 提炼的通用客户端——**无任何硬编码凭据**，全部经 appconfig 读配置。
保留原 scan 的解析逻辑（已在生产验证），供 moodle_prep.py 复用。
"""
import json
import os
import re
from datetime import datetime
from urllib.parse import urlparse

import requests

import config_store as cs


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
        generic = []
        for m_ in re.finditer(r'href="([^"]*?id=(\d+))"[^>]*>([^<]+)</a>', html):
            href, iid, name = m_.group(1), m_.group(2), re.sub(r"\s+", " ", m_.group(3)).strip()
            if "/mod/" not in href:
                continue
            full = href if href.startswith("http") else self.base_url + href
            generic.append((iid, name, full))
        for iid, name, full in generic:
            if "/mod/resource/" in full:
                acts["resources"].setdefault(iid, {"name": name, "url": full})
            elif "/mod/assign/" in full:
                acts["assignments"].setdefault(iid, {"name": name, "url": full})
            elif "/mod/url/" in full:
                acts["urls"].setdefault(iid, {"name": name, "url": full})
            elif "/mod/quiz/" in full:
                acts["quizzes"].setdefault(iid, {"name": name, "url": full})
            elif "/mod/forum/" in full:
                acts["forums"].setdefault(iid, {"name": name, "url": full})
        return acts

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
        safe = re.sub(r"[^\w\s-]", "", resource_name)
        safe = re.sub(r"[-\s]+", "-", safe).strip("-") or "file"
        r = self.session.get(resource_url, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")

        def _save(content, ext):
            fp = os.path.join(download_dir, f"{safe}{ext}")
            with open(fp, "wb") as f:
                f.write(content)
            print(f"   ✅ 已下载: {safe}{ext} ({len(content)} bytes)")
            return os.path.basename(fp), True

        if "application/pdf" in ct or r.content[:4] == b"%PDF":
            return _save(r.content, ".pdf")
        if "application/zip" in ct or r.content[:4] == b"PK\x03\x04":
            return _save(r.content, ".zip")

        html = r.text
        for m_ in re.finditer(r'href="([^"]*pluginfile\.php[^"]*)"', html):
            fu = m_.group(1)
            if not fu.startswith("http"):
                fu = self.base_url + fu
            fr = self.session.get(fu, allow_redirects=True)
            if len(fr.content) > 1000:
                c = fr.headers.get("Content-Type", "")
                if "pdf" in c or fr.content[:4] == b"%PDF":
                    return _save(fr.content, ".pdf")
                if "zip" in c or fr.content[:4] == b"PK\x03\x04":
                    return _save(fr.content, ".zip")
                ext = os.path.splitext(urlparse(fu).path)[1] or ".bin"
                return _save(fr.content, ext)
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
                prev = json.load(open(sp))
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
        with open(sp, "w") as f:
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
                fname, ok = self.download_file(info["url"], info["name"],
                                               download_dir or ".", subdir=sub)
                if ok:
                    result["new_files"].append(info["name"])
        else:
            result["new_files"] = [info["name"] for _, info in new_files.items()]
        for iid, info in acts.get("assignments", {}).items():
            d = self.check_assignment_details(info["url"])
            result["assignments"][iid] = d
        return result

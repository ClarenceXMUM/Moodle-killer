#!/usr/bin/env python3
"""Moodle 课程发现 + 配置生成（Setup/onboarding —— 让任何人用起来都明白）。

用法：
  1. 在 .env 填 MOODLE_URL / MOODLE_USER / MOODLE_PASS
  2. python3 setup_moodle.py            # 列出已加入课程，手动选择
     python3 setup_moodle.py --all      # 自动把全部已加入课程写进配置
     python3 setup_moodle.py --all --path ~/Desktop/XMUM   # 自定义下载根目录
  3. 生成 courses.json，供 moodle_prep.py 使用

发现逻辑：登录后抓取「我的课程」页（/my/ 与 /course/index.php），解析 course/view.php?id 链接。
"""
import json
import os
import re
import sys
from pathlib import Path
import requests

# 复用 moodle_scan 的登录逻辑（但凭据从 env 读）
sys.path.insert(0, os.path.expanduser("~/Documents/Hermes Artifacts"))
import moodle_scan  # noqa: E402

ENV_PREFIX = "MOODLE_"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG = SCRIPT_DIR / "courses.json"


def creds():
    return {
        "url": os.getenv("MOODLE_URL", "https://l.xmu.edu.my"),
        "user": os.getenv("MOODLE_USER", moodle_scan.USERNAME),
        "pass": os.getenv("MOODLE_PASS", moodle_scan.PASSWORD),
    }


def login(session, url, user, pwd):
    # 复用 moodle_scan 的登录 token 流程
    r = session.get(f"{url}/login/index.php")
    m = re.search(r'name="logintoken" value="([^"]+)"', r.text)
    token = m.group(1) if m else ""
    session.post(f"{url}/login/index.php", data={
        "logintoken": token, "username": user, "password": pwd,
    }, allow_redirects=True)
    r = session.get(f"{url}/my/")
    ok = user in r.text or "logout" in r.text.lower()
    return ok


def discover(session, url):
    """返回 [{id, name}]，从「我的课程」页解析 course/view.php?id 链接。"""
    courses, seen = [], set()
    urls = [f"{url}/my/", f"{url}/course/index.php"]
    for u in urls:
        try:
            r = session.get(u)
        except Exception:
            continue
        for m in re.finditer(r'href="[^"]*course/view\.php\?id=(\d+)"[^>]*>([^<]+)</a>', r.text):
            cid = m.group(1)
            name = re.sub(r"\s+", " ", m.group(2)).strip()
            if cid not in seen and name:
                seen.add(cid)
                courses.append({"id": int(cid), "name": name})
    return courses


def main():
    c = creds()
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    if not login(session, c["url"], c["user"], c["pass"]):
        print("❌ 登录失败，检查 .env 的 MOODLE_* 或网络")
        sys.exit(2)

    discovered = discover(session, c["url"])
    print(f"✅ 已加入课程 {len(discovered)} 门：")
    for i, cr in enumerate(discovered, 1):
        print(f"  {i:2d}. {cr['name']} (id={cr['id']})")

    if not discovered:
        print("未发现课程，请检查账号是否注册了课程")
        sys.exit(1)

    # 选择
    if "--all" in sys.argv:
        selected = discovered
        print("→ 将全部课程加入配置")
    else:
        picks = input("输入要加入的序号（逗号分隔，如 1,3,5；回车全选）：").strip()
        idxs = [int(x) for x in picks.replace("，", ",").split(",") if x.strip().isdigit()] if picks else list(range(1, len(discovered) + 1))
        selected = [discovered[i - 1] for i in idxs if 1 <= i <= len(discovered)]

    # 下载根目录
    base = os.path.expanduser("~")
    for i, arg in enumerate(sys.argv):
        if arg == "--path" and i + 1 < len(sys.argv):
            base = os.path.expanduser(sys.argv[i + 1])
            break
    else:
        base = os.path.join(base, "Desktop", "XMUM")

    # 生成配置（保留旧 state 文件路径）
    old = {}
    if CONFIG.exists():
        try:
            old = json.loads(CONFIG.read_text())
        except Exception:
            old = {}
    out = {}
    for cr in selected:
        key = re.sub(r"[^a-zA-Z0-9]+", "_", cr["name"].lower()).strip("_")[:20] or f"course{cr['id']}"
        out[key] = {
            "id": cr["id"],
            "name": cr["name"],
            "path": os.path.join(base, cr["name"], ""),
            "state": old.get(key, {}).get("state", f"/Users/{os.environ.get('USER','')}/.hermes/moodle_state/{key}.json"),
        }

    CONFIG.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n✅ 已写入 {CONFIG}")
    print(f"  {len(out)} 门课。下载根目录：{base}")
    print("→ 之后 daily 任务用 moodle_prep.py 读取本配置。")
    print("提示：可手动编辑 courses.json 调整下载路径 / 状态文件")


if __name__ == "__main__":
    main()

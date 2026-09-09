#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""config_store.py —— 配置与数据目录的唯一真相源。

设计原则（为什么这样分）：
  * **技能包只读，用户数据在外**：技能装到任意 harness 的目录里，升级/覆盖不会
    动到用户数据；所有可变状态放在 MOODLE_KILLER_HOME（默认 ~/.moodle-killer/）。
  * **一个文件管账号，一个文件管课程**：config.yaml 放站点/推送/下载/输出风格；
    courses.json 放每门课。改需求不用碰代码。
  * **旧版兼容**：老用户把配置放在仓库 scripts/ 里，本模块会自动找到并迁移。

目录结构（默认 ~/.moodle-killer/）：
  config.yaml            账号 + 推送 + 下载 + 输出风格（chmod 600）
  courses.json           每门课：id / 名称 / 下载目录 / 是否静音
  user_requirements.md   给 Agent 读的个性化判断规则
  state/course_<id>.json 每门课已见过的文件（用于 diff 出新文件）
  out/                   signals.txt / unclassified_moodle.json / verify_report.txt
  logs/                  运行日志
  .setup_progress.json   引导式配置的断点（中断后可续）
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML 是硬依赖，缺了要明确报错
    yaml = None

import platform_support as ps

ENV_HOME = "MOODLE_KILLER_HOME"
DEFAULT_HOME = "~/.moodle-killer"
SKILL_DIR = Path(__file__).resolve().parent.parent  # .../moodle-killer/
REPO_ROOT = SKILL_DIR.parent

# 旧版位置（迁移用；只读，不写）
LEGACY = {
    "config": [SKILL_DIR / "scripts" / "config.yaml", REPO_ROOT / "config.yaml"],
    "courses": [SKILL_DIR / "scripts" / "courses.json", REPO_ROOT / "courses.json"],
    "requirements": [REPO_ROOT / "user_requirements.md", SKILL_DIR / "user_requirements.md"],
    "state": [Path("~/.notification-agent/moodle_state").expanduser()],
}

# ── 默认配置（全新用户的起点；全部可用 mk set 改） ────────────────────────────
DEFAULTS = {
    "moodle": {"url": "https://l.xmu.edu.my", "user": "", "password": ""},
    "delivery": {
        "channel": "auto",              # auto|hermes|whatsapp|telegram|webhook|ntfy|local|none
        "schedule": ["08:30"],          # 一个或多个 HH:MM
        "weekend": True,                # 周末是否推送
        "timezone": "Asia/Kuala_Lumpur",
        "telegram": {"bot_token": "", "chat_id": ""},
        "webhook": {"url": ""},
        "ntfy": {"server": "https://ntfy.sh", "topic": ""},
        "whatsapp": {"to": ""},
        "email": {"to": ""},
    },
    "download": {
        "root": "",                     # 空 = 用本系统的习惯位置（见 platform_support.default_download_root）
        "per_course": True,
        "by_type": False,               # 高级：按文件类型再分一层
        "type_map": {
            "Assignment": ["assignment", "homework", "作业", "tutorial"],
            "Slides": ["slide", "lecture", "chapter", "笔记"],
            "Textbook": ["textbook", "book", "reading", "参考"],
            "Other": [],
        },
    },
    "output": {
        "mode": "heartbeat",            # heartbeat|silent|digest|urgent|full
        "max_lines": 5,
        "lang": "zh",
    },
    "advanced": {
        "verify_strict": True,          # 校验不过就不推
        "keep_days": 30,                # 状态文件保留天数（0=永久）
        "paused": False,                # 暂停推送（脚本照跑，不推送）
    },
}

# ── 配置项元数据：驱动 mk set / mk status / 引导式问询 / 文档 ──────────────────
# type: str|int|bool|time_list|choice|secret|path
KEY_META = {
    "moodle.url": dict(label="Moodle 站点地址", type="str", example="https://l.xmu.edu.my"),
    "moodle.user": dict(label="学号 / 用户名", type="str"),
    "moodle.password": dict(label="密码", type="secret"),
    "delivery.channel": dict(label="推送到哪", type="choice",
                             choices=["auto", "hermes", "whatsapp", "telegram", "webhook",
                                      "ntfy", "local", "none"]),
    "delivery.schedule": dict(label="推送时间", type="time_list", example="08:30 或 08:30,20:00"),
    "delivery.weekend": dict(label="周末是否推送", type="bool"),
    "delivery.timezone": dict(label="时区", type="str", example="Asia/Kuala_Lumpur"),
    "delivery.telegram.bot_token": dict(label="Telegram Bot Token", type="secret"),
    "delivery.telegram.chat_id": dict(label="Telegram Chat ID", type="str"),
    "delivery.webhook.url": dict(label="Webhook 地址", type="str"),
    "delivery.ntfy.server": dict(label="ntfy 服务器", type="str", example="https://ntfy.sh"),
    "delivery.ntfy.topic": dict(label="ntfy 主题（自己起个别人猜不到的名字）", type="str"),
    "delivery.whatsapp.to": dict(label="WhatsApp 号码", type="str", example="60123456789"),
    "delivery.email.to": dict(label="邮箱地址", type="str"),
    "download.root": dict(label="下载根目录", type="path", example=ps.path_example()),
    "download.per_course": dict(label="每门课一个文件夹", type="bool"),
    "download.by_type": dict(label="按文件类型再分文件夹（高级）", type="bool", advanced=True),
    "output.mode": dict(label="输出风格", type="choice",
                        choices=["heartbeat", "silent", "digest", "urgent", "full"]),
    "output.max_lines": dict(label="单次最多几行", type="int"),
    "output.lang": dict(label="语言", type="choice", choices=["zh", "en"]),
    "advanced.verify_strict": dict(label="校验不过就不推（高级）", type="bool", advanced=True),
    "advanced.keep_days": dict(label="状态保留天数（高级）", type="int", advanced=True),
    "advanced.paused": dict(label="暂停推送（高级）", type="bool", advanced=True),
}

# 说人话就能改：别名 → 配置键
KEY_ALIASES = {
    "time": "delivery.schedule", "时间": "delivery.schedule", "推送时间": "delivery.schedule",
    "schedule": "delivery.schedule", "几点": "delivery.schedule",
    "output": "output.mode", "输出": "output.mode", "风格": "output.mode", "模式": "output.mode",
    "channel": "delivery.channel", "通道": "delivery.channel", "推送": "delivery.channel",
    "推送到哪": "delivery.channel", "推送方式": "delivery.channel",
    "weekend": "delivery.weekend", "周末": "delivery.weekend",
    "folder": "download.root", "下载目录": "download.root", "目录": "download.root",
    "下载到": "download.root", "root": "download.root",
    "bytype": "download.by_type", "分类": "download.by_type", "按类型": "download.by_type",
    "url": "moodle.url", "站点": "moodle.url", "网站": "moodle.url",
    "user": "moodle.user", "账号": "moodle.user", "学号": "moodle.user",
    "password": "moodle.password", "密码": "moodle.password",
    "maxlines": "output.max_lines", "行数": "output.max_lines",
    "lang": "output.lang", "语言": "output.lang",
    "keepdays": "advanced.keep_days", "保留天数": "advanced.keep_days",
    "paused": "advanced.paused", "暂停": "advanced.paused",
    "strict": "advanced.verify_strict", "严格": "advanced.verify_strict",
    "校验": "advanced.verify_strict",
}

MODE_LABELS = {
    "heartbeat": "天天报到",
    "silent": "安静，只报事",
    "digest": "每天一份汇总",
    "urgent": "只报紧急",
    "full": "全都报",
}

CHANNEL_LABELS = {
    "auto": "自动（用你平台上已有的推送方式）",
    "hermes": "Hermes 聊天（cron 投递）",
    "whatsapp": "WhatsApp（Hermes 网关）",
    "telegram": "Telegram Bot",
    "webhook": "Webhook（钉钉/飞书/自建）",
    "ntfy": "ntfy 手机推送（免注册）",
    "local": "本机通知 + 本地文件",
    "none": "不推送（只跑脚本）",
}


# ── 路径 ───────────────────────────────────────────────────────────────────
def home() -> Path:
    """用户数据目录。MOODLE_KILLER_HOME 可覆盖（多账号/测试用）。"""
    p = Path(os.path.expanduser(os.environ.get(ENV_HOME) or DEFAULT_HOME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _first_existing(paths):
    for p in paths:
        if p.exists():
            return p
    return None


def config_path() -> Path:
    """新位置优先；没有但旧位置有 → 用旧的（首次运行自动迁移）。"""
    new = home() / "config.yaml"
    if new.exists():
        return new
    old = _first_existing(LEGACY["config"])
    if old:
        migrate(quiet=True)
        if new.exists():
            return new
        return old
    return new


def courses_path() -> Path:
    new = home() / "courses.json"
    if new.exists():
        return new
    old = _first_existing(LEGACY["courses"])
    if old:
        return old
    return new


def requirements_path() -> Path:
    new = home() / "user_requirements.md"
    if new.exists():
        return new
    old = _first_existing(LEGACY["requirements"])
    if old:
        return old
    return new


def state_dir() -> Path:
    p = home() / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def out_dir() -> Path:
    p = home() / "out"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = home() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(course_id) -> Path:
    return state_dir() / ("course_%s.json" % course_id)


# ── 读写 ───────────────────────────────────────────────────────────────────
def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    """默认值 ← config.yaml ← 环境变量（MOODLE_URL / MOODLE_USER / MOODLE_PASS）。"""
    cfg = copy.deepcopy(DEFAULTS)
    p = config_path()
    if p.exists():
        if yaml is None:
            print("[warn] 缺少 PyYAML，无法读 config.yaml：pip install PyYAML", file=sys.stderr)
        else:
            try:
                user = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if isinstance(user, dict):
                    cfg = _deep_merge(cfg, user)
            except Exception as e:
                print("[warn] config.yaml 解析失败: %s" % e, file=sys.stderr)
    m = cfg.setdefault("moodle", {})
    m["url"] = os.getenv("MOODLE_URL", m.get("url") or "")
    m["user"] = os.getenv("MOODLE_USER", m.get("user") or "")
    m["password"] = os.getenv("MOODLE_PASS", m.get("password") or "")
    return cfg


def save_config(cfg: dict) -> Path:
    """原子写 + chmod 600（里面有密码）。"""
    if yaml is None:
        raise RuntimeError("缺少 PyYAML：pip install PyYAML")
    p = home() / "config.yaml"
    tmp = p.with_suffix(".yaml.tmp")
    text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(p))
    if not ps.is_windows():          # Windows 没有 POSIX 权限位，靠用户目录隔离
        try:
            os.chmod(str(p), 0o600)
        except OSError:
            pass
    return p


def download_root(cfg=None) -> str:
    """下载根目录（展开后的绝对路径）。没配就按本系统习惯给。"""
    cfg = cfg or load_config()
    raw = get_path(cfg, "download.root") or ""
    if raw:
        return ps.expand_path(raw)
    return ps.default_download_root()


def load_courses() -> dict:
    p = courses_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as e:
            print("[warn] courses.json 解析失败: %s" % e, file=sys.stderr)
    return {}


def save_courses(courses: dict) -> Path:
    p = home() / "courses.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(courses, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(p))
    return p


# ── 点路径取值/设值 ────────────────────────────────────────────────────────
def normalize_key(key: str) -> str:
    k = (key or "").strip()
    if k in KEY_ALIASES:
        return KEY_ALIASES[k]
    low = k.lower().replace(" ", "")
    if low in KEY_ALIASES:
        return KEY_ALIASES[low]
    if k in KEY_META or k in _all_dotted(DEFAULTS):
        return k
    # 宽松匹配：只写末段也能命中唯一键
    tail = k.split(".")[-1]
    hits = [d for d in _all_dotted(DEFAULTS) if d.split(".")[-1] == tail]
    if len(hits) == 1:
        return hits[0]
    raise KeyError("不认识的配置项：%s（跑 mk status 看全部可改项）" % key)


def _all_dotted(d, prefix=""):
    out = []
    for k, v in d.items():
        path = prefix + k
        if isinstance(v, dict):
            out.extend(_all_dotted(v, path + "."))
        else:
            out.append(path)
    return out


def get_path(cfg: dict, dotted: str):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_path(cfg: dict, dotted: str, value):
    parts = dotted.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value
    return cfg


def coerce(dotted: str, raw):
    """把命令行字符串转成正确类型（bool / int / 时间列表 / 路径）。"""
    meta = KEY_META.get(dotted, {})
    t = meta.get("type", "str")
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "y", "on", "是", "开", "要", "推", "true"):
            return True
        if s in ("0", "false", "no", "n", "off", "否", "关", "不", "不推"):
            return False
        raise ValueError("这应该填「是/否」（也可以用 true/false、on/off）")
    if t == "int":
        return int(str(raw).strip())
    if t == "time_list":
        return parse_times(raw)
    if t == "path":
        return ps.expand_path(raw)
    if t == "choice":
        choices = meta.get("choices", [])
        s = str(raw).strip().lower()
        if s in choices:
            return s
        # 允许说中文/宽松说法
        fuzzy = {"天天": "heartbeat", "心跳": "heartbeat", "默认": "heartbeat",
                 "安静": "silent", "静默": "silent", "不打扰": "silent",
                 "汇总": "digest", "日报": "digest", "摘要": "digest",
                 "紧急": "urgent", "只报紧急": "urgent",
                 "全部": "full", "全都": "full", "全量": "full",
                 "本地": "local", "本机": "local", "通知": "local",
                 "telegram": "telegram", "电报": "telegram",
                 "自动": "auto", "你决定": "auto", "看情况": "auto",
                 "不推送": "none", "关闭": "none", "无": "none"}
        for k, v in fuzzy.items():
            if k in s and v in choices:
                return v
        raise ValueError("只能填其中之一：%s" % " / ".join(choices))
    return str(raw).strip()


def parse_times(raw):
    """'08:30' / '08:30,20:00' / '8:30 20:00' / '08:30 和 20:00' → ['08:30','20:00']"""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,，、;；/\s]+|和|以及", str(raw))
    out = []
    for it in items:
        it = it.strip()
        if not it:
            continue
        m = re.match(r"^(\d{1,2})[:：.]?(\d{2})?$", it)
        if not m:
            raise ValueError("时间格式看不懂：%s（写成 08:30 这样）" % it)
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            raise ValueError("时间超出范围：%s" % it)
        out.append("%02d:%02d" % (h, mi))
    if not out:
        raise ValueError("至少要给一个时间，例如 08:30")
    return sorted(set(out))


# ── 迁移 ───────────────────────────────────────────────────────────────────
def migrate(quiet=False) -> dict:
    """把旧位置的数据搬进用户数据目录；已存在的文件不覆盖。返回搬了什么。"""
    moved = []
    h = home()

    def _cp(src: Path, dst: Path, secret=False):
        if not src.exists() or dst.exists():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        if secret:
            try:
                os.chmod(str(dst), 0o600)
            except OSError:
                pass
        moved.append("%s → %s" % (src, dst))
        return True

    _cp(_first_existing(LEGACY["config"]) or Path("/nonexistent"), h / "config.yaml", secret=True)
    _cp(_first_existing(LEGACY["courses"]) or Path("/nonexistent"), h / "courses.json")
    _cp(_first_existing(LEGACY["requirements"]) or Path("/nonexistent"), h / "user_requirements.md")

    # 状态文件：老目录 course_<id>.json 直接搬
    for old_state in LEGACY["state"]:
        if not old_state.exists():
            continue
        for f in sorted(old_state.glob("course_*.json")):
            _cp(f, state_dir() / f.name)

    # 课程下载路径里出现 ~/Desktop/XMUM 这类老路径时不动（用户可能就想放那）
    if not quiet and moved:
        print("📦 已从旧位置迁移 %d 项到 %s" % (len(moved), h))
        for m in moved:
            print("   " + m)
    return {"moved": moved, "home": str(h)}


def init_home(force=False):
    """确保目录骨架存在；config.yaml 不存在时从模板生成一份空的。"""
    h = home()
    for d in ("state", "out", "logs"):
        (h / d).mkdir(parents=True, exist_ok=True)
    cfg_file = h / "config.yaml"
    if force or not cfg_file.exists():
        save_config(copy.deepcopy(DEFAULTS))
    req = h / "user_requirements.md"
    if not req.exists():
        tpl = SKILL_DIR / "templates" / "user_requirements.example.md"
        if tpl.exists():
            req.write_text(tpl.read_text(encoding="utf-8"), encoding="utf-8")
    return h


def credentials_ok(cfg=None):
    cfg = cfg or load_config()
    m = cfg.get("moodle", {})
    return bool(m.get("url") and m.get("user") and m.get("password"))


if __name__ == "__main__":
    # 自检：python3 config_store.py
    init_home()
    c = load_config()
    print("home      :", home())
    print("config    :", config_path())
    print("courses   :", courses_path())
    print("output    :", get_path(c, "output.mode"))
    print("schedule  :", get_path(c, "delivery.schedule"))
    print("课程数    :", len(load_courses()))

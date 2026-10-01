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
        "naming": "default",            # default|plain|original|custom（见 moodle_client.build_filename）
        "name_template": "{code}-{name}",   # 仅 custom 用；default 等价于这个模板
        "folder_template": "{code} {name}",  # 课程文件夹怎么命名（字段见 FOLDER_FIELDS）
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
    "completion": {
        "mark_done": True,              # 下载成功的活动自动勾上 Moodle 的 Done（保持课程进度）
        "max_marks": 15,                # 单轮最多勾几条；超过就一条都不勾，先报给用户确认
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
    "download.naming": dict(label="下载文件怎么命名", type="choice",
                            choices=["default", "plain", "original", "custom"],
                            example="mk set 命名 默认 ｜ mk set 命名 custom ｜ mk set 命名模板 \"{code}-{date}-{name}\""),
    "download.folder_template": dict(label="课程文件夹命名模板", type="str",
                                     example="{code} {name} 或 {code} {name} {semester}"),
    "download.name_template": dict(label="自定义命名模板（naming=custom 时生效）", type="str",
                                   example="{code}-{name}"),
    "output.mode": dict(label="输出风格", type="choice",
                        choices=["heartbeat", "silent", "digest", "urgent", "full"]),
    "output.max_lines": dict(label="单次最多几行", type="int"),
    "output.lang": dict(label="语言", type="choice", choices=["zh", "en"]),
    "advanced.verify_strict": dict(label="校验不过就不推（高级）", type="bool", advanced=True),
    "advanced.keep_days": dict(label="状态保留天数（高级）", type="int", advanced=True),
    "advanced.paused": dict(label="暂停推送（高级）", type="bool", advanced=True),
    "completion.mark_done": dict(label="下载后自动勾 Done（保持课程进度）", type="bool",
                                 advanced=True),
    "completion.max_marks": dict(label="单轮最多勾几条（超过就一条都不勾）", type="int",
                                 advanced=True),
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
    "naming": "download.naming", "命名": "download.naming", "自动命名": "download.naming",
    "文件名": "download.naming", "文件命名": "download.naming", "怎么命名": "download.naming",
    "template": "download.name_template", "name_template": "download.name_template",
    "模板": "download.name_template", "命名模板": "download.name_template",
    "文件夹": "download.folder_template", "文件夹命名": "download.folder_template",
    "文件夹模板": "download.folder_template", "folder_template": "download.folder_template",
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

NAMING_LABELS = {
    "default": "课程代号-文件名（MAT203-Animals.txt）",
    "plain": "只用文件名（Animals.txt）",
    "original": "Moodle 原始名（资料夹文件带 ID 和摘要）",
    "custom": "按你的模板（download.name_template）",
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


# ── 新课落地到哪里 ─────────────────────────────────────────────────────────
_STOPWORDS = {"and", "the", "for", "with", "from", "general", "class", "course", "courses",
              "introduction", "intro", "module", "modules", "section", "part", "test", "exam"}


def _significant_words(text):
    """课名里的实词：英文取 ≥4 字母的词（去停用词），中文取 ≥2 字的连续段。

    课程代号（MAT203）/年份/学期号（2027/01）都会被自然滤掉。
    """
    latin = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", str(text or ""))
             if w.lower() not in _STOPWORDS}
    cjk = {w for w in re.findall(r"[\u4e00-\u9fff]{2,}", str(text or "")) if w not in _STOPWORDS}
    return latin | cjk


def course_key(name, cid=None):
    """课名 → courses.json 的键（稳定、可读；同名课靠 id 兜底区分）。"""
    key = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in (name or "").lower())
    key = key.strip("_")[:24]
    return key or ("course%s" % (cid or "x"))


# ── 课程文件夹怎么命名 ─────────────────────────────────────────────────────
# Moodle 的课名长这样：`MAT203 Statistics 2026/09 Koh Siew Khew`
#   = 代号 + 课名 + 学期号 + 老师。把它拆成字段，用户用模板自由组合。
_CODE_TOKEN_RE = re.compile(r"\b[A-Za-z]{2,6}[\s/-]?\d{2,4}\b")   # MAT203 / MPU1022 / MAT418
_SEMESTER_RE = re.compile(r"\b(\d{4})\s*[/-]\s*(\d{1,2})\b")       # 2026/09
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|]')                             # 路径里不能出现的字符

FOLDER_FIELDS = {
    "code": "课程代号：课名里的 MAT203 优先，没有就用这门课的 code（如 AAI）；都没有才为空",
    "name": "课程名称（去掉代号 / 学期号 / 老师）",
    "teacher": "老师（学期号后面那截，认不出就为空）",
    "semester": "学期（2026/09 → 2026-09）",
    "shortname": "Moodle 短名（Stat / PDEs）",
    "fullname": "Moodle 里的原样全名",
}


def split_course_name(fullname, code="", shortname="", code_source=""):
    """课名 → 可组合的字段（认不出的一律留空，绝不瞎猜）。"""
    raw = re.sub(r"\s+", " ", str(fullname or "")).strip()
    semester, head, tail = "", raw, ""
    m = _SEMESTER_RE.search(raw)
    if m:
        semester = "%s-%02d" % (m.group(1), int(m.group(2)))
        head = raw[:m.start()].strip(" -_,，:：")
        tail = raw[m.end():].strip(" -_,，:：")
    body = head
    for tok in _CODE_TOKEN_RE.findall(body):
        body = body.replace(tok, " ")
    body = re.sub(r"^\s*(?:and|und|&)\s+", "", body, flags=re.IGNORECASE)   # 「MAT301 and MAT418 …」
    body = re.sub(r"\s+", " ", body).strip(" -_,，:：/")
    # 课程代号：只认「字母+数字」那种真代号（MAT203 / MPU1022）。
    # Moodle 的短名（AAI / Stat / PDEs）是绰号，直接当代号会把「Abstract Algebra I」
    # 变成「AAI Abstract Algebra I」——不是用户要的；要短名请在模板里写 {shortname}。
    stored = str(code or "").strip()
    # 短名（AAI / Stat / PDEs）不是代号 —— 它跟真编码（MAT211 这种）会打架。
    # 只认「真代号的形状」或「有可信来源标注」的 code，短名一律不当代号用。
    trusted = bool(re.fullmatch(r"[A-Za-z]{2,6}\d{2,4}", stored)) or \
        str(code_source or "").strip() in ("manual", "idnumber", "name", "material",
                                           "material-name", "teams")
    if not trusted:
        stored = ""
    real = _CODE_TOKEN_RE.search(head) or _CODE_TOKEN_RE.search(raw)
    if (stored and re.fullmatch(r"[A-Za-z0-9]{2,8}", stored)
            and stored.lower() in raw.lower()):
        # 课名里同时挂着多个代号时（实测：「MPU1022/MPU3322 …」，按入学批次分，
        # 第一学期学的用 1022、之后几个学期用 3322），**以你在 courses.json 里指定的为准** ——
        # Moodle 页面上没有「这个学生该用哪个」的任何来源（页面/分组/idnumber 全查过）。
        course_code = stored
    elif real:
        course_code = re.sub(r"\s+", "", real.group(0))
    elif re.fullmatch(r"[A-Za-z0-9]{2,8}", stored):
        # 课名里没有真代号 → 用这门课自己的代号（courses.json 的 code）。
        # 实测 XMUM 的「Abstract Algebra I 2026/09 Ali Azimi」全名没代号、官方 idnumber 也是空的，
        # 只有短名 AAI —— 没有这一步，它永远拿不到代号（文件夹名就只能是课名本身）。
        # 只认**单个词**：Moodle 会出现「Corruption 2026/09」这种带空格/斜杠的短名，不能塞进文件夹名。
        course_code = stored
    else:
        course_code = ""
    return {
        "code": course_code,
        "name": body or head,
        "teacher": tail,
        "semester": semester,
        "shortname": str(shortname or "").strip(),
        "fullname": raw,
    }


def check_folder_template(tpl):
    """模板体检：字段名要认识、不能为空、至少有一个能区分课程的字段。"""
    tpl = str(tpl or "")
    if not tpl.strip():
        raise ValueError("模板不能是空的")
    unknown = sorted(set(re.findall(r"\{(\w+)\}", tpl)) - set(FOLDER_FIELDS))
    if unknown:
        raise ValueError("不认识的字段：%s。可用：%s"
                         % ("、".join(unknown), " ".join("{%s}" % k for k in FOLDER_FIELDS)))
    if not re.search(r"\{(code|name|shortname|fullname)\}", tpl):
        raise ValueError("模板里至少要有一个能区分课程的字段（{code} / {name} / {shortname} / {fullname}），"
                         "否则所有课会挤进同一个文件夹")
    return tpl


def render_folder_name(entry, template=None, cfg=None):
    """按模板渲染出这门课的文件夹名（非法字符转义、空字段自动不占位）。"""
    entry = entry or {}
    if template is None:
        cfg = cfg if cfg is not None else load_config()
        template = get_path(cfg, "download.folder_template") or "{code} {name}"
    fields = split_course_name(entry.get("name"), entry.get("code") or "",
                               entry.get("shortname") or "",
                               entry.get("code_source") or "")
    out = str(template)
    for key in FOLDER_FIELDS:
        out = out.replace("{%s}" % key, fields.get(key) or "")
    out = re.sub(r"[（(]\s*[)）]", "", out)          # 字段为空留下的空括号，别留在名字里
    out = re.sub(r"\s+", " ", out).strip()
    out = _ILLEGAL_RE.sub("-", out)
    return out.strip(" .-_")


def dir_match_score(dirname, name, code="", folder_name=""):
    """目录名与这门课的匹配度（0~1）：模板名一致 = 1.0；含课程代号 = 1.0；否则看实词命中率。

    英文要命中 ≥2 个实词（单个 analysis 会误撞别的课）；中文一个 ≥2 字的词就够。
    """
    low = str(dirname or "").lower()
    if folder_name and low == str(folder_name).lower():
        return 1.0
    code_up = str(code or "").strip().upper()
    if code_up and code_up.lower() in low:
        return 1.0
    words = _significant_words(name)
    if not words:
        return 0.0
    hit_latin = [w for w in words if w[0].isascii() and w in low]
    hit_cjk = [w for w in words if not w[0].isascii() and w in low]
    if len(hit_latin) < 2 and not hit_cjk:
        return 0.0
    return (len(hit_latin) + len(hit_cjk)) / float(len(words))


def seen_course_dirs(courses, teams_sources=None):
    """现有课程/来源各自占用的目录 → [(规范化路径, 课程代号)]，给 guess_course_dir 当「已占用」清单。"""
    out = []
    for info in list((courses or {}).values()) + list((teams_sources or {}).values()):
        if isinstance(info, dict) and info.get("path"):
            out.append((os.path.normpath(str(info["path"])), (info.get("code") or "").strip().upper()))
    return out


def guess_course_dir(courses, cfg, name, code="", teams_sources=None, taken=(), folder_name=""):
    """给刚接上的新课猜一个落地目录（**只算路径，不建目录、不写配置**）。

    规则（站在「用户其实已经建好文件夹」这个现实上）：
      1. 基准目录 = 现有课程/来源路径里出现最多的父目录（实践中就是 `Knowledge/`）；
         一门都没有 → 退回 `download.root`。
      2. 基准目录下已经有像这门的文件夹 → 复用。判「像」：目录名含**这门课的代号**，
         或课名实词命中 ≥2 个（且过半）——单个词命中不算，否则
         「MAT303 Real Analysis」会撞上「MAT201 Mathematical Analysis 1」。
      3. **已被另一门课占用的目录不复用**（代号不同即另一门；代号相同 = 同一门课换学期，允许）。
         否则「Anti-Corruption」的目录会被下学期的「Anti-Corruption II」悄悄接手。
      4. 都不像 → 基准目录/课名（与 `mk add` 同一套，只是根从 School 换成课都在的地方）。
    """
    code_up = (code or "").strip().upper()
    taken = {os.path.normpath(p): (c or "") for p, c in taken}

    def blocked(path):
        owner = taken.get(os.path.normpath(path))
        if owner is None:
            return False
        return bool(code_up and owner and owner != code_up)   # 两边都有代号且不同 = 另一门课

    paths = []
    for info in list((courses or {}).values()):
        if isinstance(info, dict) and info.get("path"):
            paths.append(str(info["path"]))
    for info in list((teams_sources or {}).values()):
        if isinstance(info, dict) and info.get("path"):
            paths.append(str(info["path"]))
    parents = [os.path.dirname(os.path.normpath(p)) for p in paths]
    base = max(set(parents), key=parents.count) if parents else ""
    if not base or not os.path.isdir(base):
        base = download_root(cfg)

    best, best_score = "", 0.0
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        entries = []
    for entry in entries:
        full = os.path.join(base, entry)
        if not os.path.isdir(full) or blocked(full):
            continue
        score = dir_match_score(entry, name, code_up, folder_name)
        if score > best_score:
            best, best_score = full, score
    if best and best_score >= 0.5:
        return best + os.sep
    # 新建目录时用模板渲染名（默认 {code} {name} → 「MAT203 Statistics」），而不是原样全名
    return os.path.join(base, (folder_name or str(name or "")).strip() or "未命名课程") + os.sep


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
def ignored_path() -> Path:
    return home() / "ignored_courses.json"


def load_ignored() -> set:
    """「被 mk rm 摘掉、别再自动接回来」的课程 id 集合。

    自动接课是按「本学期在 Moodle 上」来的，不带这个名单的话 `mk rm` 会变成假的——
    早上摘掉、白天又自己回来。
    """
    p = ignored_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    ids = data.get("ids") if isinstance(data, dict) else data
    return {str(i) for i in (ids or [])}


def save_ignored(ids) -> Path:
    p = ignored_path()
    p.write_text(json.dumps({"ids": sorted({str(i) for i in (ids or [])})},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def ignore_course(course_id) -> None:
    save_ignored(load_ignored() | {str(course_id)})


def unignore_course(course_id) -> None:
    save_ignored(load_ignored() - {str(course_id)})


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
        # 允许说中文/宽松说法；一个词能指多个配置值时用元组（按顺序取第一个合法的）
        fuzzy = {"天天": "heartbeat", "心跳": "heartbeat", "默认": ("heartbeat", "default"),
                 "安静": "silent", "静默": "silent", "不打扰": "silent",
                 "汇总": "digest", "日报": "digest", "摘要": "digest",
                 "紧急": "urgent", "只报紧急": "urgent",
                 "全部": "full", "全都": "full", "全量": "full",
                 "本地": "local", "本机": "local", "通知": "local",
                 "telegram": "telegram", "电报": "telegram",
                 "自动": "auto", "你决定": "auto", "看情况": "auto",
                 "不推送": "none", "关闭": "none", "无": "none",
                 # 文件命名（download.naming）
                 "代号": "default", "课号": "default", "课程代号": "default", "带课号": "default",
                 "简洁": "plain", "纯文件名": "plain", "只要名字": "plain", "不带代号": "plain",
                 "原始": "original", "原名": "original", "原样": "original", "老样子": "original",
                 "自定义": "custom", "自己定": "custom", "自己写": "custom", "模板": "custom"}
        for k, v in fuzzy.items():
            if k in s:
                for cand in (v if isinstance(v, tuple) else (v,)):
                    if cand in choices:
                        return cand
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

#!/usr/bin/env python3
"""配置加载器：config.yaml（本地，不入库）→ 环境变量 → 默认值。

所有脚本通过它读凭据与发送配置，开源后任何人只需填一份 config.yaml。
"""
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"


def _empty():
    return {
        "moodle": {"url": "https://l.xmu.edu.my", "user": "", "password": ""},
        "delivery": {"channel": "none"},
    }


def _apply_env(cfg):
    """环境变量可覆盖 config.yaml（CI/无文件场景）。"""
    m = cfg.setdefault("moodle", {})
    m["url"] = os.getenv("MOODLE_URL", m.get("url") or "")
    m["user"] = os.getenv("MOODLE_USER", m.get("user") or "")
    m["password"] = os.getenv("MOODLE_PASS", m.get("password") or "")
    return cfg


def load():
    """返回配置 dict；config.yaml 缺失时返回 env/默认并带提示。"""
    cfg = _empty()
    if CONFIG_PATH.exists():
        try:
            import yaml
            user = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update({kk: vv for kk, vv in v.items() if vv not in (None, "")})
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[warn] config.yaml 解析失败: {e}", file=__import__('sys').stderr)
    else:
        print(f"[warn] 未找到 {CONFIG_PATH} —— 复制 config.example.yaml 为 config.yaml 并填写", file=__import__('sys').stderr)
    return _apply_env(cfg)


def moodle_creds(cfg=None):
    cfg = cfg or load()
    m = cfg.get("moodle", {})
    return m.get("url", ""), m.get("user", ""), m.get("password", "")


def delivery_channel(cfg=None):
    cfg = cfg or load()
    d = cfg.get("delivery", {})
    return d.get("channel", "none"), d

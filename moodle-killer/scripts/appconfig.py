#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""appconfig.py —— 兼容层（保留旧函数名，内部已交给 config_store）。

老脚本用 `from appconfig import moodle_creds`，新代码请直接用 config_store。
配置现在住在 ~/.moodle-killer/config.yaml（不是技能目录里的 config.yaml），
这样技能包升级/覆盖不会碰到你的账号和设置。
"""
from __future__ import annotations

import os
import sys

try:
    import config_store as cs
except ImportError:  # 允许从任意目录 import
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config_store as cs


def load():
    """返回完整配置 dict（默认值 ← 配置文件 ← 环境变量）。"""
    return cs.load_config()


def moodle_creds(cfg=None):
    cfg = cfg or load()
    return (cs.get_path(cfg, "moodle.url") or "",
            cs.get_path(cfg, "moodle.user") or "",
            cs.get_path(cfg, "moodle.password") or "")


def delivery_channel(cfg=None):
    cfg = cfg or load()
    return (cs.get_path(cfg, "delivery.channel") or "none",
            cs.get_path(cfg, "delivery") or {})


if __name__ == "__main__":
    # 让老用户能直接 `python3 appconfig.py` 看配置在哪、对不对
    print("配置目录: %s" % cs.home())
    print("配置文件: %s" % cs.config_path())
    cfg = cs.load_config()
    ok = cs.credentials_ok(cfg)
    missing = [k for k in ("user", "password")
               if not cs.get_path(cfg, "moodle." + k)]
    print("凭据: %s%s" % ("✅ 已填" if ok else "❌ 缺 " + "、".join(missing),
                          "" if ok else "（跑 `mk setup` 补上）"))

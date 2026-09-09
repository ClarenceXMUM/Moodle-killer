#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup_moodle.py —— 老入口，保留只为不破坏旧脚本/旧文档。

现在所有事情都由 `mk` 统一管：
    python3 setup_moodle.py           → 等价于 mk setup
    python3 setup_moodle.py --list    → 等价于 mk setup --list
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if __name__ == "__main__":
    print("提示：setup_moodle.py 已并入 `mk setup`（这个文件继续可用，但推荐用 mk）\n")
    import onboarding
    sys.exit(onboarding.main(sys.argv[1:]))

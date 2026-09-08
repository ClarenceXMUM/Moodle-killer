#!/usr/bin/env python3
"""四步校验（产品视角硬要求）+ 行数校验。

每步都验证，缺一不可，否则推送=赌运气。框架无关，只用标准库。

校验点：
  1. 登录成功      —— 确认已进入登录后状态
  2. 抓取完整      —— 页面条目计数 == 入库计数
  3. 下载落地      —— 文件真实写到磁盘（存在 + 大小>0）
  4. 课程扫全      —— 本学期课程清单与抓取结果逐一对账
  5. 推送行数      —— 输出 ≤ 5 行
"""
import json
import os
import sys
from pathlib import Path


def check_login(logged_in: bool, hint: str = "") -> tuple[bool, str]:
    if logged_in:
        return True, f"登录成功 {hint}".strip()
    return False, f"登录失败！{hint} —— 中止后续，避免把错页当通知".strip()


def check_complete(expected: int, actual: int) -> tuple[bool, str]:
    if expected == actual:
        return True, f"抓取完整 ({expected} 条都已入库)"
    return False, f"抓取不完整！页面 {expected} 条 / 入库 {actual} 条"


def check_download(path: str | None, min_bytes: int = 1) -> tuple[bool, str]:
    if not path:
        return False, "无下载文件路径"
    p = Path(path)
    if p.exists() and p.stat().st_size >= min_bytes:
        return True, f"文件落地 ok ({p.name}, {p.stat().st_size} bytes)"
    return False, f"文件未落地！{path}"


def check_courses(course_roster: list[str], fetched_courses: list[str]) -> tuple[bool, str]:
    missing = [c for c in course_roster if c not in fetched_courses]
    if not missing:
        return True, f"课程扫全 ({len(fetched_courses)} 门)"
    return False, f"缺课！{missing} 未扫到"


def check_lines(output: str, max_lines: int = 5) -> tuple[bool, str]:
    n = len([ln for ln in output.splitlines() if ln.strip()])
    if n <= max_lines:
        return True, f"行数 {n} ≤ {max_lines}"
    return False, f"行数 {n} 超限 > {max_lines}，需重写"


def main():
    # 从环境变量/JSON 读实际结果（接入 fetch_*.py 后填充真实值）
    checks = [
        check_login(os.getenv("LOGIN_OK", "1") == "1"),
        check_complete(int(os.getenv("PAGE_COUNT", "0")), int(os.getenv("STORE_COUNT", "0"))),
        check_download(os.getenv("DOWNLOAD_PATH")),
        check_courses(
            json.loads(os.getenv("COURSE_ROSTER", "[]")),
            json.loads(os.getenv("FETCHED_COURSES", "[]")),
        ),
        check_lines(os.getenv("OUTPUT", "")),
    ]

    failed = [msg for ok, msg in checks if not ok]
    if failed:
        print("❌ 未通过校验：", flush=True)
        for msg in failed:
            print("  - " + msg, flush=True)
        sys.exit(2)
    print("✅ 全部校验通过", flush=True)
    for ok, msg in checks:
        print("  ✓ " + msg, flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()

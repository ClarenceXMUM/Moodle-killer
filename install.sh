#!/usr/bin/env bash
# Moodle-killer 一键安装（macOS / Linux）
#   Windows 用户请用 install.ps1（右键 → 用 PowerShell 运行）
#
# 用法：
#   前置条件：您需要先克隆此仓库：
#     git clone https://github.com/ClarenceXMUM/Moodle-killer.git
#     cd Moodle-killer
#   然后执行：
#     ./install.sh              # 装到 3 个标准技能目录 + 生成 mk 命令
#     ./install.sh --dry-run    # 只看会做什么，不动文件
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$HERE/moodle-killer"
SCRIPTS="$SKILL_DIR/scripts"
PY="$(command -v python3 || true)"
VENV="${MOODLE_KILLER_HOME:-$HOME/.moodle-killer}/venv"

say() { printf '%s\n' "$*"; }
die() { printf '❌ %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin) OS="macOS" ;;
  Linux)  OS="Linux" ;;
  *)      die "这个脚本是给 macOS / Linux 用的。Windows 请运行 install.ps1" ;;
esac

[ -n "$PY" ] || die "没找到 python3，先装 Python 3.9+（macOS: brew install python）"
[ -f "$SCRIPTS/mk.py" ] || die "技能包不完整：$SCRIPTS/mk.py 不存在"
[ -f "$HERE/requirements.txt" ] || die "缺少 requirements.txt，请重新下载完整仓库"
# 自愈创建的环境长期保留，mk 快捷命令与定时任务都使用同一个解释器。
if [ -x "$VENV/bin/python" ]; then PY="$VENV/bin/python"; fi

say "== 1/3 检查环境（${OS}）=="
"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 9):
    sys.exit("Python 版本太旧：%d.%d（需要 3.9+）" % sys.version_info[:2])
print("  Python %d.%d.%d" % sys.version_info[:3])
EOF

check_deps() {
  "$PY" - <<'EOF'
import sys
missing = []
for mod, pkg in (("requests", "requests"), ("yaml", "PyYAML")):
    try:
        __import__(mod)
        print("  ✅ %s" % pkg)
    except ImportError:
        missing.append(pkg)
if missing:
    print("  ⚠️  缺依赖：%s" % "、".join(missing))
    sys.exit(1)
EOF
}

if ! check_deps; then
  if [ "${1:-}" = "--dry-run" ]; then
    say "  （dry-run）正式安装会自动补装 requirements.txt；必要时创建 $VENV"
  else
    say "  自动补装依赖…"
    if ! "$PY" -m pip install -r "$HERE/requirements.txt"; then
      if ! "$PY" -m pip install --user -r "$HERE/requirements.txt"; then
        # PEP 668 禁止改系统环境时走隔离环境，不使用 --break-system-packages。
        say "  系统/用户安装失败（可能受 PEP 668 限制），改用独立环境：$VENV"
        "$PY" -m venv "$VENV" || die "无法创建虚拟环境；Debian/Ubuntu 请先安装 python3-venv，或安装带 venv 的 Python 后重试"
        PY="$VENV/bin/python"
        "$PY" -m pip install -r "$HERE/requirements.txt" || die "依赖安装失败，请检查网络、包镜像和目录权限后重跑安装"
      fi
    fi
    check_deps || die "依赖复检失败，已停止安装。请用当前 Python 安装 requirements.txt：$PY"
  fi
fi

say ""
say "== 2/3 装到你的 Agent =="
if [ "${1:-}" = "--dry-run" ]; then
  say "  （dry-run）会执行：python3 $SCRIPTS/mk.py install"
  say "  技能包位置：$SKILL_DIR"
  exit 0
fi
"$PY" "$SCRIPTS/mk.py" install

say ""
say "== 3/3 下一步 =="
say "  mk           看现在什么情况"
say "  mk setup     一步步配好（账号 / 课程 / 文件放哪 / 时间 / 风格）"
say "  mk test      试跑一次（不推送）"
say ""
say "  新开一个终端让 mk 命令生效。"

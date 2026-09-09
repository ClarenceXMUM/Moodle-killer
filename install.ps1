# Moodle-killer 一键安装（Windows / PowerShell）
#
# 用法：在这个文件夹里右键 → “在终端中打开” → 运行：
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# 做的事：
#   1) 检查 Python 和依赖
#   2) 把技能包装进 3 个标准技能目录（Codex / Claude Code / Hermes 都读得到）
#   3) 生成 mk 命令并加进用户 PATH
$ErrorActionPreference = "Stop"

$Here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillDir  = Join-Path $Here "moodle-killer"
$Scripts   = Join-Path $SkillDir "scripts"

function Say($msg) { Write-Host $msg }
function Die($msg) { Write-Host "❌ $msg" -ForegroundColor Red; exit 1 }

# ── 1/3 找 Python ───────────────────────────────────────────────────────────
Say "== 1/3 检查环境（Windows）=="
$py = $null
foreach ($cand in @("py", "python", "python3")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) {
        if ($cand -eq "py") { $py = @($cmd.Source, "-3") } else { $py = @($cmd.Source) }
        break
    }
}
if (-not $py) { Die "没找到 Python。去 https://www.python.org/downloads/ 装 3.9+，装的时候勾上 “Add Python to PATH”" }

$pyExe = $py[0]
$pyArgs = @()
if ($py.Count -gt 1) { $pyArgs += $py[1] }

& $pyExe @pyArgs -c @"
import sys
if sys.version_info < (3, 9):
    sys.exit('Python 版本太旧：%d.%d（需要 3.9+）' % sys.version_info[:2])
print('  Python %d.%d.%d' % sys.version_info[:3])
missing = []
for mod, pkg in (('requests', 'requests'), ('yaml', 'PyYAML')):
    try:
        __import__(mod)
        print('  OK %s' % pkg)
    except ImportError:
        missing.append(pkg)
if missing:
    print('  缺依赖：%s' % ', '.join(missing))
    print('  装：%s -m pip install --user %s' % (sys.executable, ' '.join(missing)))
"@

if (-not (Test-Path (Join-Path $Scripts "mk.py"))) { Die "技能包不完整：$Scripts\mk.py 不存在" }

# ── 2/3 安装 ────────────────────────────────────────────────────────────────
Say ""
Say "== 2/3 装到你的 Agent =="
& $pyExe @pyArgs (Join-Path $Scripts "mk.py") install

# ── 3/3 下一步 ──────────────────────────────────────────────────────────────
Say ""
Say "== 3/3 下一步 =="
Say "  mk           看现在什么情况"
Say "  mk setup     一步步配好（账号 / 课程 / 文件放哪 / 时间 / 风格）"
Say "  mk test      试跑一次（不推送）"
Say ""
Say "  新开一个终端窗口，让 mk 命令生效。"

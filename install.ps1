# Moodle-killer 一键安装（Windows / PowerShell）
# 
# 用法：在这个文件夹里右键 → “在终端中打开” → 运行：
#   powershell -ExecutionPolicy Bypass -File .\\install.ps1
# 
# 前置条件：您需要先克隆此仓库：
#   git clone https://github.com/ClarenceXMUM/Moodle-killer.git
#   cd Moodle-killer
#
# 做的事：
#   1) 检查 Python 和依赖
#   2) 把技能包装进 3 个标准技能目录（Codex / Claude Code / Hermes 都读得到）
#   3) 生成 mk 命令并加进用户 PATH
param([switch]$DryRun)
$ErrorActionPreference = "Stop"

$Here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillDir  = Join-Path $Here "moodle-killer"
$Scripts   = Join-Path $SkillDir "scripts"

function Say($msg) { Write-Host $msg }
function Die($msg) { Write-Host "❌ $msg" -ForegroundColor Red; exit 1 }
$requirements = Join-Path $Here "requirements.txt"
if (-not (Test-Path (Join-Path $Scripts "mk.py"))) { Die "技能包不完整：$Scripts\mk.py 不存在" }
if (-not (Test-Path $requirements)) { Die "缺少 requirements.txt，请重新下载完整仓库" }

# ── 1/3 找 Python ───────────────────────────────────────────────────────────
Say "== 1/3 检查环境（Windows）=="
$py = $null
foreach ($cand in @("py", "python", "python3")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) {
        $candidateArgs = @()
        if ($cand -eq "py") { $candidateArgs = @("-3") }
        & $cmd.Source @candidateArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $py = @($cmd.Source) + $candidateArgs
            break
        }
    }
}
if (-not $py) { Die '没找到 Python 3.9+。去 https://www.python.org/downloads/ 安装，勾上 Add Python to PATH' }

$pyExe = $py[0]
$pyArgs = @()
if ($py.Count -gt 1) { $pyArgs += $py[1] }

& $pyExe @pyArgs -c @"
import sys
if sys.version_info < (3, 9):
    sys.exit('Python 版本太旧：%d.%d（需要 3.9+）' % sys.version_info[:2])
print('  Python %d.%d.%d' % sys.version_info[:3])
"@
if ($LASTEXITCODE -ne 0) { Die "Python 环境检查失败，已停止安装" }

$checkDeps = @"
import sys
missing = []
for mod, pkg in (('requests', 'requests'), ('yaml', 'PyYAML')):
    try:
        __import__(mod)
        print('  OK %s' % pkg)
    except ImportError:
        missing.append(pkg)
if missing:
    print('  缺依赖：%s' % ', '.join(missing))
    sys.exit(1)
"@

& $pyExe @pyArgs -X utf8 -c $checkDeps
if ($LASTEXITCODE -ne 0) {
    if ($DryRun) {
        Say "  （dry-run）正式安装会自动补装 requirements.txt"
    } else {
        Say "  自动补装依赖…"
        & $pyExe @pyArgs -m pip install -r $requirements
        if ($LASTEXITCODE -ne 0) { Die "依赖安装失败。请检查网络、Python pip 和目录权限，再重新运行 install.ps1" }
        & $pyExe @pyArgs -X utf8 -c $checkDeps
        if ($LASTEXITCODE -ne 0) { Die "依赖复检失败，已停止安装；请确认 requests 和 PyYAML 能在当前 Python 中导入" }
    }
}
if ($DryRun) { Say "  （dry-run）会安装技能包并生成 mk 命令"; exit 0 }

# ── 2/3 安装 ────────────────────────────────────────────────────────────────
Say ""
Say "== 2/3 装到你的 Agent =="
& $pyExe @pyArgs (Join-Path $Scripts "mk.py") install
if ($LASTEXITCODE -ne 0) { Die "技能安装失败，请查看上方错误；修复后重新运行 install.ps1" }

# ── 3/3 下一步 ──────────────────────────────────────────────────────────────
Say ""
Say "== 3/3 下一步 =="
Say "  mk           看现在什么情况"
Say "  mk setup     一步步配好（账号 / 课程 / 文件放哪 / 时间 / 风格）"
Say "  mk test      试跑一次（不推送）"
Say ""
Say "  新开一个终端窗口，让 mk 命令生效。"

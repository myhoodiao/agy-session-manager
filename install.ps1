param(
    [string]$PythonExe = "",
    [string]$BackupDir = "",
    [string]$BackendDir = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "=== [1/5] 检查 Python 3.10+ 运行环境 ===" -ForegroundColor Cyan

function Resolve-PythonExe {
    param([string]$ExplicitPath)
    if ($ExplicitPath -and (Test-Path $ExplicitPath)) {
        return (Resolve-Path $ExplicitPath).Path
    }
    $candidates = @()
    $localPyDir = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $localPyDir) {
        $candidates += Get-ChildItem -Path $localPyDir -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -ExpandProperty FullName
    }
    $cmdPy = Get-Command python -ErrorAction SilentlyContinue
    if ($cmdPy -and $cmdPy.Source -notmatch "WindowsApps") {
        $candidates += $cmdPy.Source
    }
    foreach ($cand in $candidates) {
        if (Test-Path $cand) {
            try {
                $verOut = & $cand -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                if ($LASTEXITCODE -eq 0 -and $verOut) {
                    $parts = $verOut.Trim().Split(".")
                    if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 10) {
                        return $cand
                    }
                }
            } catch {}
        }
    }
    throw "未找到可用的 Python 3.10+，请先安装 Python 或通过 -PythonExe 指定路径。"
}

$ResolvedPython = Resolve-PythonExe -ExplicitPath $PythonExe
$PyVer = & $ResolvedPython --version
Write-Host "  已定位 Python: $ResolvedPython ($PyVer)" -ForegroundColor Green

Write-Host "=== [2/5] 解析安装目录与默认备份路径 ===" -ForegroundColor Cyan

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PluginRoot = Join-Path $env:USERPROFILE ".gemini\config\plugins\agy-session-manager"

if (-not $BackupDir) {
    if (Test-Path "D:\") {
        $BackupDir = "D:\Agy Backups"
    } else {
        $BackupDir = Join-Path $env:USERPROFILE "Agy Backups"
    }
}
if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
}

# 如果显式指定了 BackendDir，或当前仓库位于固定非临时目录（如 D:\00_Project\Agy-Session-Manager），则复用；
# 同时也将 scripts/ 同步复制到插件目录内，确保即使删除临时克隆目录也能独立运行。
$TempPrefix = [System.IO.Path]::GetTempPath().TrimEnd('\')
if ($BackendDir) {
    $EffectiveRoot = (Resolve-Path $BackendDir).Path
} elseif (-not $RepoRoot.StartsWith($TempPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    $EffectiveRoot = $RepoRoot
} else {
    $EffectiveRoot = $PluginRoot
}

Write-Host "  插件安装路径: $PluginRoot" -ForegroundColor Green
Write-Host "  引擎运行目录: $EffectiveRoot" -ForegroundColor Green
Write-Host "  默认备份路径: $BackupDir" -ForegroundColor Green

Write-Host "=== [3/5] 部署全局插件、脚本、技能与门禁规则 ===" -ForegroundColor Cyan

foreach ($sub in @("scripts", "rules", "skills\agy-session-manager")) {
    $dir = Join-Path $PluginRoot $sub
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

Copy-Item -Path (Join-Path $RepoRoot "scripts\session_manager.py") -Destination (Join-Path $PluginRoot "scripts\session_manager.py") -Force
Copy-Item -Path (Join-Path $RepoRoot "scripts\session_manager_mcp.py") -Destination (Join-Path $PluginRoot "scripts\session_manager_mcp.py") -Force
Copy-Item -Path (Join-Path $RepoRoot "plugin\plugin.json") -Destination (Join-Path $PluginRoot "plugin.json") -Force
Copy-Item -Path (Join-Path $RepoRoot "plugin\skills\agy-session-manager\SKILL.md") -Destination (Join-Path $PluginRoot "skills\agy-session-manager\SKILL.md") -Force

$RuleContent = @"
# agy-session-manager 插件铁律约束

当用户要求检索、盘点、重命名或清理本机 Agy 会话（Thread）时，必须无条件遵守以下约束：

1. **先只读预览、停下来等确认、再执行（不可绕过）**：
   - 修改或删除任何会话前，必须先调用 `scan_agy_sessions` 读取真实会话内容与产物，再调用 `preview_session_changes` 生成并向用户完整展示**《清理排查清单》**（写明保留/拟清理/待确认及判断依据）和**《改名前后对照表》**（写明分类、原名、15~25字中文新名、字数与进入命令），然后**立即结束本轮输出**。
   - 只有当用户在下一条消息中明确回复确认后，才允许携带一次性 `confirmation_token` 与用户确认原话调用 `execute_session_changes`。
   - 严禁在同一轮对话中连续调用 `preview_session_changes` 与 `execute_session_changes`，严禁代用户填写确认原话或自行跳过预览直接改库。

2. **防误杀硬约束（严禁以步数/轮次少武断判删）**：
   - 严禁仅因对话轮次少（如只有 1~2 轮）就判为无效会话。
   - 必须依据 `scan_agy_sessions` 返回的真实对话首尾摘要与 `brain/<id>` 产物判断：凡包含有效结论、交付产物或复查价值的会话一律保留；对拿不准的会话必须放入 `uncertain` 待确认列表询问用户。
   - 仅将纯冒烟/连通性测试、0 轮空白会话、以及重复开启后中途断掉且已有完整替代会话的废会话列为 `deletes`。

3. **会话命名规范硬约束**：
   - 全部统一使用**简体中文**，长度控制在 **15~25 个字以内**（不含省略号 `...`/`…`、不用“新会话”等模糊词）。
   - **项目类会话**：固定采用 **`项目名称-简短说明`**（如 `Agy会话管家-需求对齐与基线建立`、`工作邮件插件-迁移至Agy与门禁验收`）。
   - **工具/运维类会话**：采用统一的 4 字中文分类前缀 + 具体完成事项，如 **`规则配置-xxx`**、**`插件与钩子-xxx`**、**`系统运维-xxx`**、**`日常工具-xxx`**、**`知识库-xxx`**。

4. **单一正式后端与完整备份核验**：
   - 唯一通过 `agy-session-manager` MCP 工具执行会话改名与清理，禁止手写临时 SQL 或直接删除文件绕过 `$BackupDir` 备份与四处一致性回读核验。
"@

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $PluginRoot "rules\AGENTS.md"), $RuleContent, $Utf8NoBom)

$McpScriptPath = Join-Path $EffectiveRoot "scripts\session_manager_mcp.py"
$McpConfigObj = [ordered]@{
    mcpServers = [ordered]@{
        "session-manager" = [ordered]@{
            command = $ResolvedPython
            args    = @("-X", "utf8", $McpScriptPath)
            env     = [ordered]@{
                AGY_SESSION_MANAGER_ROOT = $EffectiveRoot
                AGY_SESSION_BACKUP_DIR   = $BackupDir
                PYTHONIOENCODING         = "utf-8"
            }
        }
    }
}
$McpJson = $McpConfigObj | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText((Join-Path $PluginRoot "mcp_config.json"), $McpJson, $Utf8NoBom)
Write-Host "  已生成 mcp_config.json 与安全门禁规则" -ForegroundColor Green

Write-Host "=== [4/5] 运行离线沙盒单元测试 ===" -ForegroundColor Cyan
& $ResolvedPython -B -X utf8 -m unittest discover -s (Join-Path $RepoRoot "tests") -v
if ($LASTEXITCODE -ne 0) {
    throw "离线沙盒单元测试未通过，请检查输出日志。"
}

Write-Host "=== [5/5] 执行 MCP JSON-RPC 握手与工具列表自检 ===" -ForegroundColor Cyan
$SmokeInput = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}' + "`n" + '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' + "`n"
$SmokeOut = ($SmokeInput | & $ResolvedPython -X utf8 $McpScriptPath) | Out-String
if ($LASTEXITCODE -ne 0 -or $SmokeOut -notmatch "scan_agy_sessions" -or $SmokeOut -notmatch "execute_session_changes") {
    throw "MCP 服务握手自检失败：$SmokeOut"
}

Write-Host ""
Write-Host "✅ agy-session-manager 安装与自检全部通过！新开或重启 agy 窗口即可生效。" -ForegroundColor Green

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RepoRoot "backend"
$LockPath = Join-Path $BackendDir "requirements.lock"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "명령이 실패했습니다 (exit $LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

$UvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $UvCommand) {
    throw "uv가 필요합니다."
}

Push-Location $BackendDir
try {
    Invoke-Checked $UvCommand.Source @("pip", "compile", "pyproject.toml", "--all-extras", "--universal", "--no-header", "--output-file", $LockPath)
    Write-Host "잠금 파일 갱신: $LockPath"
}
finally {
    Pop-Location
}

[CmdletBinding()]
param(
    [switch]$LiveApi,
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RepoRoot "backend"

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

if (-not $PythonPath) {
    $ProjectPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $ProjectPython) {
        $PythonPath = $ProjectPython
    }
    else {
        $PythonCommand = Get-Command python -ErrorAction Stop
        $PythonPath = $PythonCommand.Source
    }
}
elseif (Test-Path -LiteralPath $PythonPath) {
    $PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
}
else {
    $PythonPath = (Get-Command $PythonPath -ErrorAction Stop).Source
}

$PreviousPythonPath = $env:PYTHONPATH
$PreviousPythonUtf8 = $env:PYTHONUTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = if ($PreviousPythonPath) {
    "$BackendDir$([IO.Path]::PathSeparator)$PreviousPythonPath"
}
else {
    $BackendDir
}

Push-Location $RepoRoot
try {
    Invoke-Checked $PythonPath @("scripts\check_no_secrets.py")
    Invoke-Checked $PythonPath @("-m", "compileall", "-q", "backend\panel")
    Invoke-Checked $PythonPath @("-m", "pytest", "-q", "backend\tests")
    Invoke-Checked $PythonPath @("-m", "pip", "check")
    Invoke-Checked $PythonPath @("-m", "pip_audit", "-r", "backend\requirements.lock")

    if ($LiveApi) {
        $env:GOOD_LISTENER_RUN_LIVE_TESTS = "1"
        Invoke-Checked $PythonPath @("scripts\live_api_smoke.py")
    }
    else {
        Write-Host "실제 API smoke test 생략. 실행하려면 -LiveApi를 지정하세요."
    }
}
finally {
    Remove-Item Env:GOOD_LISTENER_RUN_LIVE_TESTS -ErrorAction SilentlyContinue
    if ($null -eq $PreviousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $PreviousPythonPath
    }
    if ($null -eq $PreviousPythonUtf8) {
        Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONUTF8 = $PreviousPythonUtf8
    }
    Pop-Location
}

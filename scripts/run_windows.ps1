[CmdletBinding()]
param(
    [string]$Executable = "",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Executable) {
    $Executable = Join-Path $RepoRoot "dist\good-listener\good-listener.exe"
}
$Executable = (Resolve-Path -LiteralPath $Executable).Path

$DataDir = Join-Path $env:LOCALAPPDATA "GoodListener\data"
$env:GOOD_LISTENER_DB_PATH = Join-Path $DataDir "good-listener.db"
$env:GOOD_LISTENER_AUDIO_DIR = Join-Path $DataDir "audio"
$env:GOOD_LISTENER_KEY_PATH = Join-Path $DataDir "master.key.dpapi"
New-Item -ItemType Directory -Force -Path $env:GOOD_LISTENER_AUDIO_DIR | Out-Null

$PromptedForKey = $false
if (-not $env:OPENAI_API_KEY) {
    $SecureKey = Read-Host "OPENAI_API_KEY (현재 실행 프로세스에만 사용)" -AsSecureString
    $Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
    try {
        $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
        $PromptedForKey = $true
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)
    }
}

try {
    & $Executable --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) {
        throw "Good Listener가 비정상 종료했습니다 (exit $LASTEXITCODE)."
    }
}
finally {
    if ($PromptedForKey) {
        Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
    }
}

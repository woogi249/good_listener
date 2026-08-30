[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12",
    [string]$CertificateThumbprint = "",
    [switch]$BuildInstaller
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RepoRoot "backend"
$VenvPath = Join-Path $env:LOCALAPPDATA "GoodListener\build\venv-0.1.0"
$PythonPath = Join-Path $VenvPath "Scripts\python.exe"
$LockPath = Join-Path $RepoRoot "backend\requirements.lock"
$SpecPath = Join-Path $RepoRoot "packaging\good-listener.spec"
$DistPath = Join-Path $RepoRoot "dist\good-listener"
$ExePath = Join-Path $DistPath "good-listener.exe"

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
    throw "uv가 필요합니다. https://docs.astral.sh/uv/ 에서 설치하세요."
}

Push-Location $RepoRoot
try {
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        if (Test-Path -LiteralPath $VenvPath) {
            throw "불완전한 빌드 환경입니다. 확인 후 제거하세요: $VenvPath"
        }
        Invoke-Checked $UvCommand.Source @("venv", "--seed", "--python", $PythonVersion, $VenvPath)
    }
    Invoke-Checked $PythonPath @("-m", "pip", "install", "-r", $LockPath)
    Invoke-Checked $PythonPath @("-m", "pip", "install", "--no-deps", (Join-Path $RepoRoot "backend"))
    Invoke-Checked $PythonPath @((Join-Path $RepoRoot "scripts\check_no_secrets.py"))
    Invoke-Checked $PythonPath @("-m", "compileall", "-q", (Join-Path $RepoRoot "backend\panel"))
    $PreviousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = if ($PreviousPythonPath) {
        "$BackendDir$([IO.Path]::PathSeparator)$PreviousPythonPath"
    }
    else {
        $BackendDir
    }
    try {
        Invoke-Checked $PythonPath @("-m", "pytest", "-q", (Join-Path $RepoRoot "backend\tests"))
    }
    finally {
        if ($null -eq $PreviousPythonPath) {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        }
        else {
            $env:PYTHONPATH = $PreviousPythonPath
        }
    }
    Invoke-Checked $PythonPath @("-m", "pip", "check")
    Invoke-Checked $PythonPath @("-m", "PyInstaller", "--clean", "--noconfirm", $SpecPath)

    if (-not (Test-Path -LiteralPath $ExePath)) {
        throw "PyInstaller 결과를 찾지 못했습니다: $ExePath"
    }

    $SbomPath = Join-Path $DistPath "good-listener-sbom.cdx.json"
    Invoke-Checked $PythonPath @("-m", "pip_audit", "-r", $LockPath, "--format", "cyclonedx-json", "--output", $SbomPath)
    if (-not (Test-Path -LiteralPath $SbomPath)) {
        throw "SBOM 결과를 찾지 못했습니다: $SbomPath"
    }

    if ($CertificateThumbprint) {
        $SignTool = Get-Command signtool.exe -ErrorAction SilentlyContinue
        if (-not $SignTool) {
            throw "서명을 요청했지만 signtool.exe를 찾지 못했습니다."
        }
        Invoke-Checked $SignTool.Source @("sign", "/sha1", $CertificateThumbprint, "/fd", "SHA256", "/tr", "http://timestamp.digicert.com", "/td", "SHA256", $ExePath)
    }

    if ($BuildInstaller) {
        $Inno = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if (-not $Inno) {
            throw "-BuildInstaller를 사용하려면 Inno Setup 6의 ISCC.exe가 PATH에 있어야 합니다."
        }
        Invoke-Checked $Inno.Source @((Join-Path $RepoRoot "packaging\good-listener.iss"))
        $InstallerPath = Join-Path $RepoRoot "dist\installer\good-listener-0.1.0-windows-x64.exe"
        if (-not (Test-Path -LiteralPath $InstallerPath)) {
            throw "Installer 결과를 찾지 못했습니다: $InstallerPath"
        }
        if ($CertificateThumbprint) {
            Invoke-Checked $SignTool.Source @("sign", "/sha1", $CertificateThumbprint, "/fd", "SHA256", "/tr", "http://timestamp.digicert.com", "/td", "SHA256", $InstallerPath)
        }
    }

    Write-Host "Windows 배포 폴더: $DistPath"
}
finally {
    Pop-Location
}

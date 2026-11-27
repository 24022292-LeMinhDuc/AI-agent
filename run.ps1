param(
    [switch]$SetupOnly
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$projectRoot = $PSScriptRoot
$env:STREAMLIT_HOME = Join-Path $projectRoot ".streamlit"
if (-not (Test-Path -LiteralPath $env:STREAMLIT_HOME)) { New-Item -ItemType Directory -Path $env:STREAMLIT_HOME | Out-Null }
$venvPath = Join-Path $projectRoot ".venv"
$pythonPath = Join-Path $venvPath "Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$requirementsMarker = Join-Path $venvPath ".requirements.sha256"

function Assert-SupportedPython([string]$executable) {
    & $executable -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "Cần Python 3.12 trở lên để chạy ứng dụng."
    }
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "Không tìm thấy Python. Cài Python 3.12+ rồi chạy lại .\run.ps1."
    }

    Assert-SupportedPython $pythonCommand.Source
    & $pythonCommand.Source -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Không tạo được môi trường .venv (exit code $LASTEXITCODE)."
    }
}
Assert-SupportedPython $pythonPath

$requirementsHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash
$installedRequirementsHash = if (Test-Path -LiteralPath $requirementsMarker) {
    (Get-Content -LiteralPath $requirementsMarker -Raw).Trim()
} else {
    ""
}

if ($requirementsHash -ne $installedRequirementsHash) {
    Write-Host "Cài đặt hoặc cập nhật thư viện trong .venv..."
    & $pythonPath -m pip install --quiet --disable-pip-version-check --progress-bar off -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Cài thư viện thất bại (exit code $LASTEXITCODE)."
    }

    Set-Content -LiteralPath $requirementsMarker -Value $requirementsHash -NoNewline
}

if ($SetupOnly) {
    Write-Host "Môi trường sẵn sàng tại .venv."
    exit 0
}

Push-Location -LiteralPath $projectRoot
try {
    & $pythonPath -m streamlit run (Join-Path $projectRoot "app.py") `
        --server.address 127.0.0.1 --browser.gatherUsageStats false
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

$Python = (Get-Command python -ErrorAction Stop).Source
$LlvmRoot = if ([string]::IsNullOrWhiteSpace($env:UTGEN_LLVM_ROOT)) {
    "C:\Program Files\LLVM"
} else {
    $env:UTGEN_LLVM_ROOT
}
$env:UTGEN_LLVM_ROOT = $LlvmRoot

foreach ($Path in @(
    (Join-Path $LlvmRoot "bin\clang.exe"),
    (Join-Path $LlvmRoot "bin\libclang.dll"),
    (Join-Path $ProjectRoot "requirements.txt"),
    (Join-Path $ProjectRoot "generate_from_config.spec"),
    (Join-Path $ProjectRoot "config.yaml")
)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}

function Invoke-Python {
    param([string[]]$Arguments)

    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
}

Invoke-Python @("-m", "pip", "install", "-r", (Join-Path $ProjectRoot "requirements.txt"))
Invoke-Python @("-m", "pip", "install", "pyinstaller")
Invoke-Python @(
    "-m", "PyInstaller", "--clean", "--noconfirm",
    "--distpath", (Join-Path $ProjectRoot "dist"),
    "--workpath", (Join-Path $ProjectRoot ".pyinstaller_build"),
    (Join-Path $ProjectRoot "generate_from_config.spec")
)

Copy-Item -LiteralPath (Join-Path $ProjectRoot "config.yaml") `
    -Destination (Join-Path $ProjectRoot "dist\config.yaml") -Force

Write-Host "Built: $(Join-Path $ProjectRoot 'dist\utgen.exe')"
Write-Host "Config: $(Join-Path $ProjectRoot 'dist\config.yaml')"

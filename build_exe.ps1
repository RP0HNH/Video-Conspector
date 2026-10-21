# Build VideoConspector.exe -> dist\VideoConspector (onedir).
# Usage: powershell -File build_exe.ps1 [-Clean]
#   -Clean  drop the PyInstaller cache (build\) before building.
# The build always runs inside .venv created from a non-Anaconda Python.
param([switch]$Clean, [switch]$NoOpen)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = ".\.venv\Scripts\python.exe"
$cfg = if (Test-Path ".venv\pyvenv.cfg") { Get-Content ".venv\pyvenv.cfg" -Raw } else { "" }

# A venv based on Anaconda makes PyInstaller scan conda DLLs and packages.
if ((Test-Path $venvPython) -and $cfg -match "(?i)conda") {
    Write-Host "The existing .venv is based on Anaconda - recreating it."
    Remove-Item -Recurse -Force .venv
}

if (-not (Test-Path $venvPython)) {
    $base = $null
    foreach ($candidate in @("-3.12", "-3.11", "-3.10")) {
        $path = & py $candidate -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $path -and $path -notmatch "(?i)conda") { $base = $candidate; break }
    }
    if (-not $base) { throw "Install Python 3.10-3.12 from python.org (Anaconda is not supported)." }
    Write-Host "Creating .venv with py $base"
    & py $base -m venv .venv
}

& $venvPython -m pip install -q --disable-pip-version-check -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# Intermediate files go to .pyi_cache. The old build\ folder held a half-made
# VideoConspector.exe without _internal that is easy to launch by mistake.
if (Test-Path build) { Remove-Item -Recurse -Force build }
$pyArgs = @("--noconfirm", "--workpath", ".pyi_cache")
if ($Clean) { $pyArgs += "--clean" }
& $venvPython -m PyInstaller @pyArgs VideoConspector.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Copy-Item .env.example dist\VideoConspector\.env.example -Force
$exe = (Resolve-Path dist\VideoConspector\VideoConspector.exe).Path
Write-Host ""
Write-Host "DONE. Run this file (keep the whole folder next to it):"
Write-Host $exe
if (-not $NoOpen) { Invoke-Item (Split-Path $exe) }

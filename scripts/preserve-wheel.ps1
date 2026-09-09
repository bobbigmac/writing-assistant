#!/usr/bin/env pwsh
# Preserves the locally-built llama-cpp-python wheel so we never need to
# rebuild from source. Run this after a successful build.
#
# Usage:
#   .\scripts\preserve-wheel.ps1
#
# This searches pip's cache and the build temp dir for the wheel, copies it
# to wheels/, and writes a reinstall script that uses the cached wheel.
#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$wheelsDir = Join-Path $repoRoot 'wheels'
if (-not (Test-Path $wheelsDir)) {
    New-Item -ItemType Directory -Path $wheelsDir | Out-Null
}

# Find the wheel in pip cache and build temp dirs
$candidates = @()
$candidates += Get-ChildItem 'C:\Users\bobbi\AppData\Local\Temp' -Recurse -Filter 'llama_cpp_python*.whl' -ErrorAction SilentlyContinue -Depth 4
$candidates += Get-ChildItem 'C:\Users\bobbi\AppData\Local\pip\cache' -Recurse -Filter 'llama_cpp_python*.whl' -ErrorAction SilentlyContinue -Depth 5

$wheel = $candidates | Sort-Object LastWriteTime -Descending | Select-Object -First 1

if (-not $wheel) {
    Write-Error 'No llama_cpp_python wheel found. Build may still be in progress or failed.'
    exit 1
}

$dest = Join-Path $wheelsDir $wheel.Name
Copy-Item $wheel.FullName $dest -Force
Write-Host "Preserved wheel: $dest ($([math]::Round($wheel.Length / 1MB, 1)) MB)"

# Write the reinstall script
$reinstallScript = @'
@echo off
REM Reinstall the locally-built llama-cpp-python wheel (Haswell-safe, CUDA 12.4, sm_89)
REM This avoids rebuilding from source. Run from the repo root.
REM
REM Usage: scripts\reinstall-llama-cpp.bat
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat" amd64 >nul 2>&1
set CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4
.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps wheels\%1
if errorlevel 1 (
    echo Failed to install wheel. Check the wheels\ directory.
    exit /b 1
)
echo Installed llama-cpp-python from cached wheel.
'@

$batPath = Join-Path $PSScriptRoot 'reinstall-llama-cpp.bat'
Set-Content -Path $batPath -Value $reinstallScript -Encoding ASCII
Write-Host "Wrote reinstall script: $batPath"

# Also write a PowerShell version
$psScript = @'
# Reinstall the locally-built llama-cpp-python wheel (Haswell-safe, CUDA 12.4, sm_89)
# This avoids rebuilding from source. Run from the repo root.
#
# Usage: .\scripts\reinstall-llama-cpp.ps1
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$wheelsDir = Join-Path $repoRoot 'wheels'
$wheel = Get-ChildItem $wheelsDir -Filter 'llama_cpp_python*.whl' | Select-Object -First 1
if (-not $wheel) {
    Write-Error 'No wheel found in wheels\. Run preserve-wheel.ps1 first.'
    exit 1
}
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
& $venvPython -m pip install --force-reinstall --no-deps $wheel.FullName
Write-Host "Installed $($wheel.Name) from cached wheel."
'@

$psPath = Join-Path $PSScriptRoot 'reinstall-llama-cpp.ps1'
Set-Content -Path $psPath -Value $psScript -Encoding UTF8
Write-Host "Wrote reinstall script: $psPath"

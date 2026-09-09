# Reassemble the split 7z chunks and install the wheel.
# Usage: .\scripts\restore-wheel.ps1
# Requires 7z in PATH (chocolatey install 7zip).
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$wheelsDir = Join-Path $repoRoot 'wheels'

# Find chunks
$chunks = Get-ChildItem $wheelsDir -Filter 'llama_cpp_python.7z.part*' | Sort-Object Name
if ($chunks.Count -eq 0) {
    Write-Error "No wheel chunks found in $wheelsDir. Expected llama_cpp_python.7z.part0, .part1, ..."
    exit 1
}
Write-Host "Found $($chunks.Count) chunk(s):"
$chunks | ForEach-Object { Write-Host "  $($_.Name) ($([math]::Round($_.Length/1MB,1)) MB)" }

# Reassemble into a single .7z
$combined = Join-Path $wheelsDir 'llama_cpp_python.7z'
$totalLen = ($chunks | Measure-Object Length -Sum).Sum
$bytes = New-Object byte[] $totalLen
$offset = 0
foreach ($chunk in $chunks) {
    $chunkBytes = [System.IO.File]::ReadAllBytes($chunk.FullName)
    [Array]::Copy($chunkBytes, 0, $bytes, $offset, $chunkBytes.Length)
    $offset += $chunkBytes.Length
}
[System.IO.File]::WriteAllBytes($combined, $bytes)
Write-Host "Reassembled: $combined ($([math]::Round($totalLen/1MB,1)) MB)"

# Extract with 7z
$sevenZip = (Get-Command 7z -ErrorAction SilentlyContinue).Source
if (-not $sevenZip) { $sevenZip = (Get-Command 7za -ErrorAction SilentlyContinue).Source }
if (-not $sevenZip) {
    Write-Error "7z not found. Install with: choco install 7zip"
    exit 1
}
& $sevenZip x $combined "-o$wheelsDir" -y | Out-Null
Remove-Item $combined -Force
Write-Host "Extracted wheel to $wheelsDir"

# Install
$wheel = Get-ChildItem $wheelsDir -Filter 'llama_cpp_python*.whl' | Select-Object -First 1
if (-not $wheel) {
    Write-Error "No .whl found after extraction"
    exit 1
}
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating .venv..."
    python -m venv (Join-Path $repoRoot '.venv')
}
& $venvPython -m pip install --force-reinstall --no-deps $wheel.FullName
& $venvPython -m pip install numpy diskcache jinja2 typing-extensions
Write-Host "Installed $($wheel.Name)"
Write-Host "Done. Test with: .\.venv\Scripts\python.exe -m llm.run smoke"

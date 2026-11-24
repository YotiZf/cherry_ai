$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    py -3 -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv. Install Python 3.10–3.13 first.' }
}
& $python -m pip install -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Application dependency installation failed.' }
& $python -m pip install -r (Join-Path $projectRoot 'requirements-dev.txt')
if ($LASTEXITCODE -ne 0) { throw 'Development dependency installation failed.' }
Write-Host 'Development environment is ready.'
Write-Host 'Run checks with: .\.venv\Scripts\python.exe -m unittest discover -s tests'
Write-Host 'Lint with:       .\.venv\Scripts\ruff.exe check .'

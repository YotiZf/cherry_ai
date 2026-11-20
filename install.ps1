$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3 -m venv (Join-Path $projectRoot '.venv')
    } else {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python) { throw 'Python 3.10–3.13 is required. Install Python and run this script again.' }
        & $python.Source -m venv (Join-Path $projectRoot '.venv')
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
}

& $venvPython -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 14) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Use Python 3.10, 3.11, 3.12 or 3.13 for this setup.' }

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Could not update pip.' }
& $venvPython -m pip install -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python package installation failed. Check the error above and try again.' }

$dataDir = Join-Path $projectRoot 'ai_workspace_data'
$voiceDir = Join-Path $dataDir 'voices'
New-Item -ItemType Directory -Path $voiceDir -Force | Out-Null

$templates = @(
    @{ Source = 'plugins.example.json'; Destination = 'plugins.json' },
    @{ Source = 'voice.example.json'; Destination = 'voice.json' }
)
foreach ($template in $templates) {
    $target = Join-Path $dataDir $template.Destination
    if (-not (Test-Path -LiteralPath $target)) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $template.Source) -Destination $target
    }
}
$keyFile = Join-Path $projectRoot 'key.env'
if (-not (Test-Path -LiteralPath $keyFile)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'key.env.example') -Destination $keyFile
}

Push-Location -LiteralPath $voiceDir
try {
    foreach ($voiceName in @('en_US-lessac-medium', 'de_DE-thorsten-medium')) {
        $onnx = Join-Path $voiceDir ($voiceName + '.onnx')
        $config = Join-Path $voiceDir ($voiceName + '.onnx.json')
        if ((Test-Path -LiteralPath $onnx) -and (Test-Path -LiteralPath $config)) { continue }
        Write-Host "Downloading Piper voice: $voiceName"
        & $venvPython -m piper.download_voices $voiceName
        if ($LASTEXITCODE -ne 0) { throw "Could not download Piper voice $voiceName." }
    }
} finally {
    Pop-Location
}

Write-Host ''
Write-Host 'Setup is complete.'
$appPath = Join-Path $projectRoot 'ai_workspace_voice.py'
$startCommand = '& "{0}" "{1}"' -f $venvPython, $appPath
Write-Host "Start Cherry with: $startCommand"
Write-Host 'Add provider credentials to key.env before using cloud models.'

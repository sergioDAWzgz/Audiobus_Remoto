# Build audiobus-agent.exe with PyInstaller (Windows).
#
# IMPORTANT: this needs a python.org / Microsoft Store Python 3.10-3.12, NOT the
# MSYS2 mingw Python — the mingw build has no binary wheels for Pillow and tries
# to compile it from source (which fails on missing jpeg headers), and PyInstaller
# produces a broken .exe there.
#
# To make `./build.ps1` reliable, this script prefers a local virtualenv at
# agent/.venv. Create it once with a native Windows Python:
#     py -3.11 -m venv .venv
#     .\.venv\Scripts\python -m pip install -r requirements.txt pyinstaller
# After that, just run:
#     ./build.ps1
#
# Result: dist/audiobus-agent.exe (also copied to ../public/downloads/ so the
# /share page can offer it on the next 'wrangler deploy').

$ErrorActionPreference = "Stop"

# Run from the script's own directory so the relative paths below (requirements.txt,
# audiobus_agent.py, dist/) resolve no matter where the script is invoked from.
Set-Location -Path $PSScriptRoot

# Fail loudly if a native command (pip / PyInstaller) returns non-zero —
# $ErrorActionPreference does not catch native exit codes, so without this a failed
# build would silently copy a stale exe.
function Invoke-OrThrow {
    param([scriptblock]$Cmd)
    & $Cmd
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE" }
}

# Pick the interpreter: the project venv if present, otherwise whatever `python`
# is on PATH.
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    $py = $venvPy
    Write-Host "Using project venv: $py"
} else {
    $py = "python"
    Write-Host "No agent/.venv found; falling back to 'python' on PATH."
}

$ver = (& $py --version) 2>&1
Write-Host "Python: $ver"

# Guard against the MSYS2/mingw Python, which cannot build this .exe.
$pyPath = (& $py -c "import sys; print(sys.executable)") 2>&1
if ($pyPath -match "msys|mingw") {
    Write-Error @"
This Python is the MSYS2/mingw build ($pyPath), which cannot build the agent .exe
(no Pillow wheels; PyInstaller fails). Create a native-Windows venv instead:
    py -3.11 -m venv .venv
    .\.venv\Scripts\python -m pip install -r requirements.txt pyinstaller
then re-run ./build.ps1.
"@
}

Invoke-OrThrow { & $py -m pip install --upgrade pip }
Invoke-OrThrow { & $py -m pip install -r requirements.txt pyinstaller }

Invoke-OrThrow { & $py -m PyInstaller `
    --onefile `
    --name audiobus-agent `
    --windowed `
    --icon logo.ico `
    audiobus_agent.py }

$exe = Join-Path $PSScriptRoot "dist/audiobus-agent.exe"
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "Built: $exe"
    $dest = Join-Path $PSScriptRoot "../public/downloads/audiobus-agent.exe"
    Copy-Item $exe $dest -Force
    Write-Host "Copied to: $dest"
    Write-Host "It will be published on the next 'wrangler deploy'."
} else {
    Write-Error "Build failed: $exe not found."
}

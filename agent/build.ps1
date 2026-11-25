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

# Fail loudly if PyInstaller (a native command) returns non-zero — $ErrorActionPreference
# does not catch native exit codes, so without this a failed build would silently
# upload a stale exe.
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

# --collect-all pulls in the native libraries these packages ship that PyInstaller's
# static analysis misses: PyAV's bundled ffmpeg DLLs (av), and aiortc's media/ICE
# stack (aioice, and pylibsrtp's libsrtp). cryptography/cffi/numpy are covered by
# PyInstaller's built-in hooks.
# NOTE: no --uac-admin. The agent launches NON-elevated (no UAC on open); it relaunches itself
# elevated on demand when the viewer requests elevated access (see on-demand-elevation feature).
Invoke-OrThrow { & $py -m PyInstaller `
    --onefile `
    --name audiobus-agent `
    --windowed `
    --add-binary "openh264.dll;." `
    --add-binary "SumatraPDF.exe;." `
    --icon logo.ico `
    --version-file version_info.txt `
    --collect-all av `
    --collect-all aiortc `
    --collect-all aioice `
    --collect-all pylibsrtp `
    --collect-all pystray `
    audiobus_agent.py }

$exe = Join-Path $PSScriptRoot "dist/audiobus-agent.exe"
if (-not (Test-Path $exe)) {
    Write-Error "Build failed: $exe not found."
}
Write-Host ""
Write-Host "Built: $exe"

# --- Optional Authenticode code signing ------------------------------------
# Signs the exe ONLY when a certificate is configured, so plain local builds still
# work unsigned. A CA-issued cert (OV/EV) gives a STABLE trusted identity that
# survives every rebuild and sharply cuts AV/SmartScreen false positives; a
# SELF-SIGNED cert does NOT help and should not be used here.
#
# Configure exactly one cert source as an environment variable before building:
#   $env:SIGN_PFX = "C:\path\to\cert.pfx"; $env:SIGN_PW = "<pfx password>"   # file cert (OV)
#   $env:SIGN_THUMBPRINT = "<sha1 thumbprint>"                               # token/HSM/cert store (EV)
# Optional timestamp server (RFC3161); defaults below so the signature stays
# valid after the cert expires:
#   $env:SIGN_TS_URL = "http://timestamp.sectigo.com"
$tsUrl = if ($env:SIGN_TS_URL) { $env:SIGN_TS_URL } else { "http://timestamp.sectigo.com" }
if ($env:SIGN_PFX -or $env:SIGN_THUMBPRINT) {
    # signtool ships with the Windows SDK and is usually NOT on PATH; find it.
    $signtool = (Get-Command signtool.exe -ErrorAction SilentlyContinue).Source
    if (-not $signtool) {
        $signtool = (Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" `
                        -ErrorAction SilentlyContinue |
                     Sort-Object FullName -Descending | Select-Object -First 1).FullName
    }
    if (-not $signtool) {
        Write-Error "SIGN_* is set but signtool.exe was not found. Install the Windows 10/11 SDK (Signing Tools) or add signtool to PATH."
    }
    Write-Host "Signing with: $signtool"
    if ($env:SIGN_PFX) {
        if (-not $env:SIGN_PW) { Write-Error "SIGN_PFX is set but SIGN_PW (the .pfx password) is not." }
        Invoke-OrThrow { & $signtool sign /fd sha256 /tr $tsUrl /td sha256 /f $env:SIGN_PFX /p $env:SIGN_PW $exe }
    } else {
        Invoke-OrThrow { & $signtool sign /fd sha256 /tr $tsUrl /td sha256 /sha1 $env:SIGN_THUMBPRINT $exe }
    }
    Invoke-OrThrow { & $signtool verify /pa $exe }
    Write-Host "Signed + timestamped + verified: $exe"
} else {
    Write-Host "NOTE: not code-signed (set SIGN_PFX+SIGN_PW, or SIGN_THUMBPRINT, to sign)."
    Write-Host "      Unsigned builds get a NEW hash each rebuild and can trip AV heuristics (e.g. Kaspersky blocking the spawned cap-worker copies)."
}

# The agent exe is distributed via GitHub Releases (no longer R2). The /share download
# link points at:
#   https://github.com/sergioDAWzgz/Audiobus_Remoto/releases/download/<tag>/audiobus-agent.exe
# Attach this build to the matching release (pin the href to the same <tag> when you cut a
# new one, since the repo also ships -jpeg releases and GitHub's "latest" is repo-wide):
Write-Host ""
Write-Host "Next: attach this build to the GitHub release the /share link points at:"
Write-Host "  gh release upload <tag>-webrtc `"$exe`" --clobber"

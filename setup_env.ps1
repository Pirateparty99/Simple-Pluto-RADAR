<#
Create a self-contained Python virtual environment for Simple-Pluto-RADAR.

The venv lives in .venv\ next to this script, so the whole project is
portable. Nothing is installed outside the repo except the native libiio
library, which pip cannot provide (see the check at the end).

Usage:
    .\setup_env.ps1
    .\setup_env.ps1 -Recreate
    .\setup_env.ps1 -Python C:\Python312\python.exe
    .\setup_env.ps1 -SystemSitePackages

If PowerShell refuses to run the script, allow it for this session with:
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>

[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$Recreate,
    [switch]$SystemSitePackages
)

$ErrorActionPreference = "Stop"

$RepoRoot = $PSScriptRoot
$VenvDir = Join-Path $RepoRoot ".venv"
$Requirements = Join-Path $RepoRoot "requirements.txt"

# Native commands write progress and warnings to stderr. With
# $ErrorActionPreference = "Stop", redirecting that stream turns ordinary
# output into a terminating NativeCommandError, so run native calls through
# this helper instead and judge success by the exit code.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [switch]$Quiet)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Quiet) {
            & $Exe @Arguments 2>&1 | Out-Null
        } else {
            & $Exe @Arguments
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

if (-not (Test-Path $Requirements)) {
    throw "$Requirements not found. Run this script from a full checkout."
}

# --- Pick an interpreter ---------------------------------------------------

$PythonExplicit = [bool]$Python

if (-not $Python) {
    foreach ($candidate in @("py", "python", "python3")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            $Python = $candidate
            break
        }
    }
}

if (-not $Python) {
    throw "No Python interpreter found. Install Python 3.8+ or pass -Python <path>."
}

# "py" is the launcher, not an interpreter -- ask it for a 3.x.
$PythonArgs = @()
if ($Python -eq "py") { $PythonArgs = @("-3") }

$code = Invoke-Native $Python ($PythonArgs + @("-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)")) -Quiet
if ($code -ne 0) {
    throw "That Python is too old, or could not be run. pyadi-iio needs Python 3.8+."
}

$version = & $Python @PythonArgs -V
Write-Host "==> Using $version"

# --- Create the venv -------------------------------------------------------

if ($Recreate -and (Test-Path $VenvDir)) {
    Write-Host "==> Removing existing $VenvDir"
    Remove-Item -Recurse -Force $VenvDir
}

$VenvPy = Join-Path $VenvDir "Scripts\python.exe"

if (Test-Path $VenvDir) {
    # An existing venv keeps whatever interpreter and site-packages policy it
    # was built with, so creation-time flags are a no-op here. Say so rather
    # than pretending they took effect.
    if ($PythonExplicit) {
        Write-Warning "-Python only applies when creating a venv. Pass -Recreate to rebuild with $Python."
    }
    if ($SystemSitePackages) {
        Write-Warning "-SystemSitePackages only applies when creating a venv. Pass -Recreate to rebuild with it."
    }

    if (-not (Test-Path $VenvPy)) {
        throw "$VenvDir exists but has no interpreter at $VenvPy. Rebuild it with: .\setup_env.ps1 -Recreate"
    }

    Write-Host "==> Reusing existing venv at $VenvDir"
} else {
    Write-Host "==> Creating venv at $VenvDir"
    $venvArgs = $PythonArgs + @("-m", "venv")
    if ($SystemSitePackages) { $venvArgs += "--system-site-packages" }
    $venvArgs += $VenvDir

    $code = Invoke-Native $Python $venvArgs
    if ($code -ne 0) { throw "Could not create the virtual environment." }
}

# --- Install dependencies --------------------------------------------------

Write-Host "==> Upgrading pip"
Invoke-Native $VenvPy @("-m", "pip", "install", "--upgrade", "pip", "--quiet") -Quiet | Out-Null

Write-Host "==> Installing dependencies from requirements.txt"
$code = Invoke-Native $VenvPy @("-m", "pip", "install", "-r", $Requirements)
if ($code -ne 0) { throw "Dependency installation failed." }

# --- Check for the native libiio library -----------------------------------
#
# pyadi-iio depends on pylibiio, a pure-Python ctypes wrapper that loads
# libiio.dll at import time. Without the DLL, "import adi" fails no matter
# what pip installed.

Write-Host "==> Checking for native libiio"
$code = Invoke-Native $VenvPy @("-c", "import adi") -Quiet
if ($code -eq 0) {
    Write-Host "    import adi: OK"
} else {
    Write-Warning @"
Could not import adi -- libiio.dll is probably missing. The venv itself is
fine, but you need libiio at the OS level.

Install it with the Analog Devices Windows installer:
    https://github.com/analogdevicesinc/libiio/releases

Then reopen your shell so the new PATH entry takes effect and verify with:
    iio_info -u ip:pluto.local
"@
}

# --- Done ------------------------------------------------------------------

Write-Host @"

Done. Activate the environment with:

    .\.venv\Scripts\Activate.ps1

Then run the radar with:

    python main.py

"@

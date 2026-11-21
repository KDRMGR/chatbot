# STEALTHBOT - create the Python virtual environment OUTSIDE the OneDrive folder and install dependencies.
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#
# Windows Smart App Control blocks unsigned native extensions (.pyd). Every dependency must therefore be
# pure Python. SQLAlchemy's default Windows wheel contains compiled modules, so this script installs its
# pure-Python (py3-none-any) wheel explicitly BEFORE the other requirements.
$ErrorActionPreference = 'Stop'
$venv = Join-Path $env:LOCALAPPDATA 'stealthbot\venv'
$backend = Join-Path $PSScriptRoot '..\backend'
$py = "$venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Creating virtual environment at $venv"
    py -3 -m venv $venv
}
& $py -m pip install --upgrade pip

$pyver = & $py -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$wheelDir = Join-Path $env:TEMP 'stealthbot_wheels'
New-Item -ItemType Directory -Force -Path $wheelDir | Out-Null
Get-ChildItem $wheelDir -Filter 'sqlalchemy*.whl' -ErrorAction SilentlyContinue | Remove-Item -Force
Write-Host "Fetching pure-Python SQLAlchemy wheel (Python $pyver)"
& $py -m pip download sqlalchemy --no-deps --only-binary=:all: --platform any --python-version $pyver --implementation py -d $wheelDir
$wheel = Get-ChildItem $wheelDir -Filter 'sqlalchemy*-none-any.whl' | Select-Object -First 1
& $py -m pip install --force-reinstall --no-deps $wheel.FullName

$req = if (Test-Path "$backend\requirements.lock") { 'requirements.lock' } else { 'requirements.txt' }
Write-Host "Installing from $req"
& $py -m pip install -r "$backend\$req"

# Safety net: fail loudly if any compiled extension from the policy-blocked set slipped in.
$bad = Get-ChildItem "$venv\Lib\site-packages" -Recurse -Include *.pyd -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match 'pydantic_core|sqlalchemy|greenlet' }
if ($bad) { Write-Warning "Blocked-prone native modules present:"; $bad | ForEach-Object { Write-Warning $_.FullName } }
Write-Host "Done. Run tests from the backend folder:  $py -m pytest"

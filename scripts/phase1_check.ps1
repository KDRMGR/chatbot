# STEALTHBOT - Phase 1 environment check (READ-ONLY: changes nothing on the system).
# Run from the project root in Windows PowerShell 5.1 or PowerShell 7:
#   powershell -ExecutionPolicy Bypass -File scripts\phase1_check.ps1
# Keep this file ASCII-only (PowerShell 5.1 reads BOM-less files as ANSI).

$ErrorActionPreference = 'Continue'
$script:fails = 0
$script:warns = 0

function Report([string]$level, [string]$name, [string]$detail) {
    $tag = switch ($level) { 'PASS' { '[ OK ]' } 'WARN' { '[WARN]' } 'FAIL' { '[FAIL]' } default { '[INFO]' } }
    if ($level -eq 'FAIL') { $script:fails++ }
    if ($level -eq 'WARN') { $script:warns++ }
    $color = switch ($level) { 'PASS' { 'Green' } 'WARN' { 'Yellow' } 'FAIL' { 'Red' } default { 'Gray' } }
    Write-Host ("{0} {1,-26} {2}" -f $tag, $name, $detail) -ForegroundColor $color
}

Write-Host "`n=== STEALTHBOT Phase 1 environment check ===`n"

# --- OS ---
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
Report 'INFO' 'Machine' "$($cs.Manufacturer) $($cs.Model)"
Report 'INFO' 'CPU' "$($cpu.Name) ($($cpu.NumberOfCores)C/$($cpu.NumberOfLogicalProcessors)T)"
if ([int]$os.BuildNumber -ge 19045) { Report 'PASS' 'Windows' "$($os.Caption) build $($os.BuildNumber)" }
else { Report 'FAIL' 'Windows' "$($os.Caption) build $($os.BuildNumber) is too old" }

# --- RAM / disk ---
$totalGB = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
$freeGB = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
if ($freeGB -ge 6) { Report 'PASS' 'RAM free' "$freeGB GB free of $totalGB GB" }
elseif ($freeGB -ge 4) { Report 'WARN' 'RAM free' "$freeGB GB free of $totalGB GB (close Chrome etc. before streaming)" }
else { Report 'FAIL' 'RAM free' "$freeGB GB free of $totalGB GB" }
$drive = Get-PSDrive -Name ($PWD.Drive.Name) -ErrorAction SilentlyContinue
if ($drive) {
    $diskFree = [math]::Round($drive.Free / 1GB, 1)
    if ($diskFree -ge 20) { Report 'PASS' 'Disk free' "$diskFree GB on $($drive.Name):" }
    else { Report 'FAIL' 'Disk free' "$diskFree GB on $($drive.Name): (need >= 20 GB)" }
}

# --- Power ---
$plan = (powercfg /getactivescheme) -replace '^.*\((.*)\).*$', '$1'
if ($plan -match 'High|Ultimate') { Report 'PASS' 'Power plan' $plan } else { Report 'WARN' 'Power plan' "$plan (High performance recommended for 3-6 h sessions)" }
$bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue
if ($bat) {
    if ($bat.BatteryStatus -eq 2) { Report 'PASS' 'AC power' 'plugged in' } else { Report 'WARN' 'AC power' 'running on battery - plug in before benchmarking' }
}

# --- Tooling ---
function CheckTool([string]$cmd, [string]$versionArg = '--version', [string]$level = 'FAIL') {
    $c = Get-Command $cmd -ErrorAction SilentlyContinue
    if (-not $c) { Report $level $cmd 'not found'; return }
    $out = & $cmd $versionArg 2>&1      # capture everything: do not cut the pipeline short
    $code = $LASTEXITCODE
    $v = [string]($out | Select-Object -First 1)
    if ($code -eq 0 -and $v -match '\d+\.\d+') { Report 'PASS' $cmd $v }
    else { Report $level $cmd "found at $($c.Source) but does not run: $v" }
}

# Python: the 'py' launcher is the reliable entry point. A bare 'python' on this
# machine can resolve to the Microsoft Store stub in WindowsApps.
$pyOut = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $pyOut = (& py -3 --version 2>&1 | Select-Object -First 1) -as [string] }
if ($pyOut -match 'Python (\d+)\.(\d+)') {
    if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 12) { Report 'PASS' 'python (py -3)' $pyOut }
    else { Report 'FAIL' 'python (py -3)' "$pyOut (need 3.12+)" }
}
else { Report 'FAIL' 'python (py -3)' 'py launcher / Python 3 not found' }
$bareOut = & python --version 2>&1
$bareCode = $LASTEXITCODE
$bare = [string]($bareOut | Select-Object -First 1)
if ($bareCode -ne 0 -or $bare -notmatch 'Python \d') {
    Report 'WARN' "bare 'python'" 'resolves to the Microsoft Store stub - use "py" (or disable the App execution alias)'
}

CheckTool 'git'
CheckTool 'node' '--version' 'WARN'   # only needed from Phase 6 (dashboard)
CheckTool 'ollama' '--version' 'FAIL'

# --- NVIDIA ---
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $smi) {
    foreach ($p in 'C:\Windows\System32\nvidia-smi.exe', 'C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe') {
        if (Test-Path $p) { $smi = Get-Item $p; break }
    }
}
$nv = Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -like 'PCI\VEN_10DE*' -and $_.InstanceId -notlike '*HDAUDIO*' -and $_.FriendlyName -notmatch 'Audio' -and $_.InstanceId -like 'PCI\*' -and $_.FriendlyName -notmatch 'Controller' }
if ($nv) { Report 'INFO' 'NVIDIA PCI device' (($nv | ForEach-Object { "$($_.FriendlyName) [$($_.InstanceId.Split('\')[1])]" }) -join '; ') }
if (-not $smi) {
    Report 'FAIL' 'NVIDIA driver' 'nvidia-smi not found - no NVIDIA display driver is installed'
}
else {
    $q = & $smi.Source '--query-gpu=name,driver_version,memory.total,memory.used' '--format=csv,noheader' 2>&1
    if ($LASTEXITCODE -ne 0) { Report 'FAIL' 'NVIDIA driver' "nvidia-smi failed: $q" }
    else {
        $parts = ($q | Select-Object -First 1) -split ',\s*'
        $drv = [double]$parts[1]
        if ($drv -ge 551.61) { Report 'PASS' 'NVIDIA driver' "$($parts[1]) (Ollama needs >= 551.61 on Windows)" }
        else { Report 'FAIL' 'NVIDIA driver' "$($parts[1]) is below Ollama's minimum 551.61" }
        Report 'INFO' 'GPU' "$($parts[0]) | VRAM total $($parts[2]) | in use $($parts[3])"
    }
}

# --- Ollama server ---
if ($env:OLLAMA_HOST -and $env:OLLAMA_HOST -notmatch '^(127\.0\.0\.1|localhost)') {
    Report 'FAIL' 'OLLAMA_HOST' "$($env:OLLAMA_HOST) exposes Ollama beyond loopback - remove it"
}
try {
    $v = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 3
    Report 'PASS' 'Ollama server' "reachable, version $($v.version)"
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3
    $names = ($tags.models | ForEach-Object { $_.name }) -join ', '
    if ($names) { Report 'INFO' 'Installed models' $names } else { Report 'WARN' 'Installed models' 'none yet' }
}
catch { Report 'WARN' 'Ollama server' 'not reachable on 127.0.0.1:11434 (start Ollama)' }

Write-Host ("`nSummary: {0} FAIL, {1} WARN`n" -f $script:fails, $script:warns)
if ($script:fails -gt 0) { exit 1 } else { exit 0 }

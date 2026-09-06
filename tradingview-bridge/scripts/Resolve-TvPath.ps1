# Resolve-TvPath.ps1 — Dynamic TradingView Desktop path resolver.
# Survives Microsoft Store version updates (no hardcoded version in path).
# Dot-source from launcher scripts:  . "$PSScriptRoot\Resolve-TvPath.ps1"
# Then call:  $tvPath = Get-TradingViewPath  (returns $null if not found)

function Get-TradingViewPath {
    # 1. Microsoft Store / MSIX install (preferred — version-agnostic)
    try {
        $pkg = Get-AppxPackage -Name "TradingView.Desktop" -ErrorAction Stop |
               Select-Object -First 1
        if ($pkg -and $pkg.InstallLocation) {
            $exe = Join-Path $pkg.InstallLocation "TradingView.exe"
            if (Test-Path $exe) { return $exe }
        }
    } catch {}

    # 2. Classic desktop installs
    $candidates = @()
    if ($env:LOCALAPPDATA)       { $candidates += Join-Path $env:LOCALAPPDATA "TradingView\TradingView.exe" }
    if ($env:PROGRAMFILES)       { $candidates += Join-Path $env:PROGRAMFILES "TradingView\TradingView.exe" }
    if (${env:PROGRAMFILES(X86)}){ $candidates += Join-Path ${env:PROGRAMFILES(X86)} "TradingView\TradingView.exe" }
    foreach ($p in $candidates) {
        if (Test-Path $p) { return $p }
    }

    # 3. Anywhere on PATH
    try {
        $w = (where.exe TradingView.exe 2>$null | Select-Object -First 1)
        if ($w -and (Test-Path $w.Trim())) { return $w.Trim() }
    } catch {}

    return $null
}

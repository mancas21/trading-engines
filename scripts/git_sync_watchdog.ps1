# git_sync_watchdog.ps1 - keeps GitHub in sync with the local repo.
#
# WHY THIS EXISTS (2026-11-12)
# ===========================
# The repo was 99 commits (~2 months) ahead of GitHub and nobody noticed.
# The commits were made by hand and nobody ran `git push`: Git is
# local-first, so a commit writes to .git/ and the divergence grows in
# silence. The fix that closes the loop on the ordinary path is the
# post-commit hook (.git/hooks/post-commit), which pushes on every commit.
#
# This watchdog is the SECOND line of defence, for the cases the hook
# cannot cover:
#   - a commit made by a tool that does not run git in this working tree
#   - a push that failed at 3am with the machine asleep
#   - a hook deleted or bypassed (`--no-verify`, `git commit` from an IDE
#     that ignores hooks)
# It only pushes commits that already exist locally; it NEVER stages,
# never commits and never touches the working tree.
#
# WHAT IT DOES
# ============
# - reads the upstream of the current branch and counts the commits it is
#   ahead (`git rev-list --count @{u}..HEAD`);
# - if the count is 0, it does nothing and stays silent;
# - if there is something to push, it pushes the CURRENT branch to its
#   upstream and logs the outcome.
#
# BLAST DOORS
# ===========
# - scripts\.git_sync_watchdog_pause -> if this file exists the watchdog
#   does nothing. Create it for intentional downtime; delete it to re-arm.
# - It never stages, never commits, never resets and never force-pushes.
#   A non-fast-forward remote is reported, not forced.
# - Write-Host, never Write-Output, inside functions: an output-stream
#   write made every check truthy forever in a sibling watchdog
#   (bridge_watchdog.ps1 documents that failure). Do not repeat it here.
# - ASCII ONLY: this file has no BOM; PS 5.1 can misread non-ASCII bytes.
#
# EXIT CODES: 0 = nothing to do or pushed OK, 1 = there was something to
# push and it failed (the next run retries).

param(
  [switch]$Force,          # push even if the last run already failed
  [int]$TimeoutSec = 60
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir
$LogFile   = Join-Path $ScriptDir 'git_sync_watchdog.log'
$PauseFile = Join-Path $ScriptDir '.git_sync_watchdog_pause'

if (Test-Path $PauseFile) { exit 0 }

function Write-Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')][$PID] $msg"
  for ($i = 0; $i -lt 5; $i++) {
    try { Add-Content -Path $LogFile -Value $line -ErrorAction Stop; return }
    catch { Start-Sleep -Milliseconds 200 }
  }
}

Set-Location $Root
if (-not (Test-Path (Join-Path $Root '.git'))) { exit 0 }

# GIT_TERMINAL_PROMPT=0: if the credential is missing the push must fail
# immediately instead of blocking a Scheduled Task forever on a password
# prompt that nobody is there to answer.
$env:GIT_TERMINAL_PROMPT = '0'
$env:GIT_ASKPASS = 'echo'

$branch = (& git rev-parse --abbrev-ref HEAD 2>$null | Select-Object -First 1)
if (-not $branch) { exit 0 }
if ($branch -eq 'HEAD') { exit 0 }   # detached: no upstream to speak of

$upstream = (& git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null | Select-Object -First 1)
if (-not $upstream) {
  Write-Log "branch $branch has no upstream: nothing to push"
  exit 0
}

$pending = (& git rev-list --count '@{u}..HEAD' 2>$null | Select-Object -First 1)
if (-not $pending) { exit 0 }
$pending = [int]$pending
if ($pending -le 0) { exit 0 }

# '@{u}' is "origin/master": split it. `git push origin/master HEAD` would
# be read as a remote NAMED origin/master and fail with "does not appear
# to be a git repository" (measured 2026-11-12, first version of the
# post-commit hook had exactly that bug).
$remote    = ($upstream -split '/')[0]
$upbranch  = ($upstream -split '/', 2)[1]
if (-not $remote -or -not $upbranch) { exit 0 }

$out = & git push --quiet $remote "HEAD:refs/heads/$upbranch" 2>&1
if ($LASTEXITCODE -eq 0) {
  Write-Log "OK $branch -> $remote/$upbranch ($pending commits pushed)"
  exit 0
}

$err = ($out | Out-String).Trim()
if ($err.Length -gt 300) { $err = $err.Substring(0, 300) }
Write-Log "FALLO $branch -> $remote/$upbranch ($pending commits pendientes): $err"
exit 1

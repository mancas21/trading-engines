$cur = [Environment]::GetEnvironmentVariable('ELECTRON_EXTRA_LAUNCH_ARGS', 'User')
Write-Host "current_user_value=[$cur]"
if ($cur -ne '--remote-debugging-port=9222') {
  [Environment]::SetEnvironmentVariable('ELECTRON_EXTRA_LAUNCH_ARGS', '--remote-debugging-port=9222', 'User')
  Write-Host 'stored.'
}
Write-Host "verify=[$([Environment]::GetEnvironmentVariable('ELECTRON_EXTRA_LAUNCH_ARGS', 'User'))]"

$exe = 'C:\Program Files\WindowsApps\TradingView.Desktop_3.4.0.8149_x64__n534cwy3pjxzj\TradingView.exe'
Write-Host "exists=$([System.IO.File]::Exists($exe))"
try {
  $p = Start-Process -FilePath $exe -ArgumentList '--remote-debugging-port=9222' -PassThru
  Write-Host "spawned_pid=$($p.Id)"
} catch {
  Write-Host "spawn_failed=$($_.Exception.Message)"
}

$rundir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$startTime = Get-Date
$iterations = 30
$intervalSec = 10
$lastState = $null
$lastHealth = $null
$lastAlertContent = $null
$lastAlertExists = $false
$newDataDetected = $false
$log = @()

Write-Host "===== ATAS BRIDGE MONITOR ====="
Write-Host "Started: $($startTime.ToString('HH:mm:ss'))"
Write-Host "Polling $iterations times every ${intervalSec}s (total: $(($iterations*$intervalSec)/60) min)"
Write-Host ""

for ($i=1; $i -le $iterations; $i++) {
    $ts = Get-Date -Format 'HH:mm:ss'
    Write-Host "[$ts] Iteration $i/$iterations"

    # 1. Poll /state
    try {
        $stateRaw = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/state' -TimeoutSec 5 -ErrorAction Stop
        $stateJson = $stateRaw | ConvertTo-Json -Compress -Depth 5
        if ($lastState -ne $null -and $stateJson -ne $lastState) {
            Write-Host "  *** /state CHANGED: $stateJson" -ForegroundColor Yellow
            $newDataDetected = $true
            $log += "STATE CHANGE at $ts`: $stateJson"
        } elseif ($lastState -eq $null) {
            Write-Host "  /state baseline: $stateJson"
        }
        $lastState = $stateJson
    } catch {
        Write-Host "  /state ERROR: $_" -ForegroundColor Red
    }

    # 2. Poll /health
    try {
        $healthRaw = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 5 -ErrorAction Stop
        $healthJson = $healthRaw | ConvertTo-Json -Compress -Depth 5
        if ($lastHealth -ne $null -and $healthJson -ne $lastHealth) {
            Write-Host "  *** /health CHANGED: $healthJson" -ForegroundColor Yellow
            $newDataDetected = $true
            $log += "HEALTH CHANGE at $ts`: $healthJson"
        } elseif ($lastHealth -eq $null) {
            Write-Host "  /health baseline: $healthJson"
        }
        $lastHealth = $healthJson
    } catch {
        Write-Host "  /health ERROR: $_" -ForegroundColor Red
    }

    # 3. Check alert file
    $alertPath = 'C:\atas-bridge\_last_raw_alert.json'
    $alertExists = Test-Path $alertPath
    if ($alertExists) {
        $alertContent = Get-Content $alertPath -Raw
        if ($lastAlertExists) {
            if ($alertContent -ne $lastAlertContent) {
                Write-Host "  *** Alert FILE CHANGED!" -ForegroundColor Green
                Write-Host "  NEW CONTENT:" -ForegroundColor Green
                $alertObj = $alertContent | ConvertFrom-Json
                Write-Host "    Ticker : $($alertObj.ticker)" -ForegroundColor Green
                Write-Host "    Volumen: $($alertObj.volumen)" -ForegroundColor Green
                Write-Host "    Delta  : $($alertObj.delta)" -ForegroundColor Green
                Write-Host "    Precio : $($alertObj.precio)" -ForegroundColor Green
                Write-Host "  RAW MESSAGE:" -ForegroundColor Green
                Write-Host "    $($alertContent.Trim())" -ForegroundColor Green
                $newDataDetected = $true
                $log += "ALERT FILE CHANGE at $ts`: $($alertContent.Trim())"
            }
        } else {
            Write-Host "  Alert file EXISTS (first check):"
            $alertObj = $alertContent | ConvertFrom-Json
            Write-Host "    Ticker : $($alertObj.ticker)"
            Write-Host "    Volumen: $($alertObj.volumen)"
            Write-Host "    Delta  : $($alertObj.delta)"
            Write-Host "    Precio : $($alertObj.precio)"
        }
        $lastAlertContent = $alertContent
        $lastAlertExists = $true
    } else {
        if ($lastAlertExists) {
            Write-Host "  *** Alert FILE DELETED!" -ForegroundColor Yellow
            $log += "ALERT FILE DELETED at $ts"
        }
        $lastAlertExists = $false
        $lastAlertContent = $null
        Write-Host "  Alert file: NOT FOUND"
    }

    Write-Host ""

    if ($i -lt $iterations) {
        Start-Sleep -Seconds $intervalSec
    }
}

$endTime = Get-Date
$elapsed = ($endTime - $startTime).TotalSeconds
Write-Host "===== MONITORING COMPLETE ====="
Write-Host "Duration: ${elapsed}s"
Write-Host ""

Write-Host "========== FINAL REPORT ==========" -ForegroundColor Cyan
if ($newDataDetected) {
    Write-Host "RESULT: NEW ALERTS / DATA WERE RECEIVED during monitoring." -ForegroundColor Green
    Write-Host "Changes detected:" -ForegroundColor Cyan
    $log | ForEach-Object { Write-Host "  - $_" }
} else {
    Write-Host "RESULT: NO new alerts or data arrived in ${elapsed}s." -ForegroundColor Red
    Write-Host "ATAS X is NOT sending any webhooks to the bridge at http://127.0.0.1:8000" -ForegroundColor Red
    Write-Host ""
    Write-Host "LIKELY CAUSES:" -ForegroundColor Yellow
    Write-Host "  1. ATAS X is not configured to send webhooks to http://127.0.0.1:8000/webhook" -ForegroundColor Yellow
    Write-Host "  2. ATAS X alert conditions have not been triggered (no market activity hitting your thresholds)" -ForegroundColor Yellow
    Write-Host "  3. ATAS X is not running or the bot/alarm is disabled" -ForegroundColor Yellow
    Write-Host "  4. The webhook URL in ATAS X points to a different address/port" -ForegroundColor Yellow
    Write-Host "  5. Firewall or network issue between ATAS X and the bridge on localhost:8000" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "VERIFICATION STEPS:" -ForegroundColor Yellow
    Write-Host "  - In ATAS X: Check Bot/Alarm settings -> Webhook URL should be http://127.0.0.1:8000/webhook" -ForegroundColor Yellow
    Write-Host "  - In ATAS X: Verify the bot is ENABLED and alarms are set" -ForegroundColor Yellow
    Write-Host "  - Manually test: curl -X POST http://127.0.0.1:8000/webhook -H `"Content-Type: application/json`" -d `'{\"ticker\":\"TEST\",\"volumen\":\"1\",\"delta\":\"+1\",\"precio\":\"50000\"}`'" -ForegroundColor Yellow
    Write-Host "  - Check ATAS X logs for 'Webhook sent' or 'HTTP POST' messages" -ForegroundColor Yellow
}
Write-Host "==================================" -ForegroundColor Cyan

if ($log.Count -gt 0) {
    $log | Out-File -FilePath "$rundir\atas_monitor_log.txt" -Encoding utf8
    Write-Host "Detailed log saved to atas_monitor_log.txt"
}

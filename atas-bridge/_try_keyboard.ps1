$wshell = New-Object -ComObject WScript.Shell

# Bring ATAS X to front
$wshell.AppActivate("ATAS X")
Start-Sleep 1

# Method 1: Try Ctrl+I to add indicator
Write-Output "Method 1: Ctrl+I"
$wshell.SendKeys("^i")
Start-Sleep 3

# Check if file appeared
if (Test-Path "C:\atas-bridge\_live_indicators.json") {
    Write-Output "SUCCESS!"
    exit
}

# Method 2: Type LiveExporter (in case dialog opened)
$wshell.SendKeys("LiveExporter")
Start-Sleep 1
$wshell.SendKeys("{Enter}")
Start-Sleep 2

if (Test-Path "C:\atas-bridge\_live_indicators.json") {
    Write-Output "SUCCESS after typing!"
    exit
}

# Method 3: Try Alt+I (menu)  
Write-Output "Method 3: Alt+I"
$wshell.SendKeys("%i")
Start-Sleep 2

# Try selecting Add from menu
$wshell.SendKeys("{DOWN}")
Start-Sleep 0.5
$wshell.SendKeys("{DOWN}") 
Start-Sleep 0.5
$wshell.SendKeys("{ENTER}")
Start-Sleep 2

if (Test-Path "C:\atas-bridge\_live_indicators.json") {
    Write-Output "SUCCESS after Alt+I!"
    exit
}

# Method 4: Try clicking on chart, then Ctrl+I
Write-Output "Method 4: Focus chart + Ctrl+I"
$wshell.SendKeys("{TAB}")
Start-Sleep 0.5
$wshell.SendKeys("{TAB}")
Start-Sleep 0.5
$wshell.SendKeys("^i")
Start-Sleep 3

$wshell.SendKeys("LiveExporter")
Start-Sleep 1
$wshell.SendKeys("{Enter}")
Start-Sleep 2

if (Test-Path "C:\atas-bridge\_live_indicators.json") {
    Write-Output "SUCCESS!"
} else {
    Write-Output "All methods failed"
}

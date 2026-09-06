Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName System.Windows.Forms

$wshell = New-Object -ComObject WScript.Shell
$wshell.AppActivate("ATAS X")
Start-Sleep 0.5

# Click Indicators button
$root = [System.Windows.Automation.AutomationElement]::RootElement
$atas = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, 
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, "ATAS X")))

$indText = $atas.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, "Indicators")))

if ($indText) {
    $rect = $indText.Current.BoundingRectangle
    $clickX = [int]($rect.X + $rect.Width / 2)
    $clickY = [int]($rect.Y + $rect.Height / 2)
    
    [System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point($clickX, $clickY)
    Start-Sleep 0.3
    
    Add-Type @"
        using System.Runtime.InteropServices;
        public class M {
            [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);
        }
"@
    [M]::mouse_event(0x02, 0, 0, 0, 0)
    [M]::mouse_event(0x04, 0, 0, 0, 0)
    Write-Output "Clicked Indicators"
    Start-Sleep 1
    
    # Try pressing 'A' for Add
    Write-Output "Sending 'A'..."
    $wshell.SendKeys("a")
    Start-Sleep 2
    
    # Try typing LiveExporter
    Write-Output "Typing..."
    $wshell.SendKeys("LiveExporter")
    Start-Sleep 1
    $wshell.SendKeys("{Enter}")
    Write-Output "Sent Enter"
    Start-Sleep 2
    
} else {
    Write-Output "Indicators not found"
}

# Check if file appeared
if (Test-Path "C:\atas-bridge\_live_indicators.json") {
    Write-Output "SUCCESS! File created!"
    Get-Content "C:\atas-bridge\_live_indicators.json"
} elseif (Test-Path "C:\atas-bridge\_exporter_debug.log") {
    Write-Output "Debug log found:"
    Get-Content "C:\atas-bridge\_exporter_debug.log"
} else {
    Write-Output "No files created yet"
}

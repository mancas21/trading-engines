Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName System.Windows.Forms

$root = [System.Windows.Automation.AutomationElement]::RootElement
$atas = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, 
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, "ATAS X")))
        
# Find and click Indicators button  
$indText = $atas.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, "Indicators")))

$rect = $indText.Current.BoundingRectangle
$clickX = [int]($rect.X + $rect.Width / 2)
$clickY = [int]($rect.Y + $rect.Height / 2)

Add-Type @"
    using System.Runtime.InteropServices;
    public class M {
        [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);
        [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    }
"@

$wshell = New-Object -ComObject WScript.Shell
$wshell.AppActivate("ATAS X")
Start-Sleep 0.5

[M]::SetCursorPos($clickX, $clickY)
Start-Sleep 0.2
[M]::mouse_event(0x02, 0, 0, 0, 0)
[M]::mouse_event(0x04, 0, 0, 0, 0)
Write-Output "Clicked Indicators at ($clickX,$clickY)"
Start-Sleep 2

# Re-find ATAS X element (might have a popup)
$atas2 = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, "ATAS X")))

# List ALL visible elements to find the menu
$all = $atas2.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
Write-Output "`n=== All elements after click ==="
$all | ForEach-Object {
    $n = $_.Current.Name
    $ctrl = $_.Current.ControlType.ProgrammaticName
    $r = $_.Current.BoundingRectangle
    if ($r.Width -gt 0 -and $r.Height -gt 0 -and $n -ne "" -and
        $ctrl -ne "Pane" -and $ctrl -ne "Custom" -and $ctrl -ne "List") {
        Write-Output "  $ctrl: '$n' at ($($r.X),$($r.Y)) $($r.Width)x$($r.Height)"  
    }
}

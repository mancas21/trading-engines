Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient

$root = [System.Windows.Automation.AutomationElement]::RootElement
$atas = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, 
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, "ATAS X")))

# Find the Indicators text inside the button
$indText = $atas.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, "Indicators")))

if ($indText) {
    $rect = $indText.Current.BoundingRectangle
    Write-Output "Indicators text at: $rect"
    
    # Get parent button (the StackPanel)
    $parent = $indText
    for ($i = 0; $i -lt 5; $i++) {
        $parent = [System.Windows.Automation.AutomationElement]::FromPoint(
            New-Object System.Windows.Point($rect.X + 10, $rect.Y + 10))
        if ($parent -and $parent.Current.ControlType.ProgrammaticName -eq "Button") { break }
    }
    
    # Click at the center of the Indicators button
    $clickX = [int]($rect.X + $rect.Width / 2)
    $clickY = [int]($rect.Y + $rect.Height / 2)
    Write-Output "Clicking at ($clickX, $clickY)"
    
    [System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point($clickX, $clickY)
    Start-Sleep 0.2
    
    # Mouse down/up
    [System.Windows.Forms.MouseEvents]::mouse_event(0x0002, 0, 0, 0, 0) # down
    Start-Sleep 0.1
    [System.Windows.Forms.MouseEvents]::mouse_event(0x0004, 0, 0, 0, 0) # up
    Start-Sleep 1
    
    Write-Output "Clicked. Waiting for menu..."
    Start-Sleep 2
    
    # After clicking Indicators, look for a popup menu
    # The menu should appear near the button - try to find it
    $allElements = $atas.FindAll([System.Windows.Automation.TreeScope]::Descendants, 
        [System.Windows.Automation.Condition]::TrueCondition) | Where-Object { 
            $_.Current.Name -ne "" -and 
            $_.Current.Name -ne "ATAS X" -and
            $_.Current.IsOffscreen -eq $false
        }
    
    Write-Output "=== Visible elements after click ==="
    $allElements | ForEach-Object { 
        $r = $_.Current.BoundingRectangle
        if ($r.Width -gt 0 -and $r.Height -gt 0) {
            $name = $_.Current.Name
            $ctrlType = $_.Current.ControlType.ProgrammaticName
            if ($name -ne "" -and $name -ne "ATAS X" -and $name -ne "SOLUSDT" -and $name -ne "Settings") {
                Write-Output "  ${ctrlType}: '${name}' at ($($r.X),$($r.Y)) $($r.Width)x$($r.Height)"
            }
        }
    }
    
    # Try typing directly after clicking (maybe the menu uses keyboard)
    # Send keys still need focus
    Write-Output "`nTrying to type 'LiveExporter'..."
    $wshell = New-Object -ComObject WScript.Shell
    $wshell.AppActivate("ATAS X")
    Start-Sleep 0.5
    $wshell.SendKeys("LiveExporter")
    Start-Sleep 1
    $wshell.SendKeys("{Enter}")
    
} else {
    Write-Output "Indicators text not found"
}

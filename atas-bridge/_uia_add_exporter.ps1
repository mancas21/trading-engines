Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName System.Windows.Forms

$root = [System.Windows.Automation.AutomationElement]::RootElement
$atas = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, 
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, "ATAS X")))

# Find the Indicators text element (child of the button)
$indText = $atas.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, "Indicators")))

if ($indText) {
    $rect = $indText.Current.BoundingRectangle
    $clickX = [int]($rect.X + $rect.Width / 2)
    $clickY = [int]($rect.Y + $rect.Height / 2)
    
    Write-Output "Indicators button at: $rect"
    Write-Output "Clicking at ($clickX, $clickY)"
    
    # Bring ATAS X to front
    $wshell = New-Object -ComObject WScript.Shell
    $wshell.AppActivate("ATAS X")
    Start-Sleep 0.5
    
    # Click the Indicators button via mouse
    [System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point($clickX, $clickY)
    Start-Sleep 0.2
    Add-Type @"
        using System;
        using System.Runtime.InteropServices;
        public class MouseClick {
            [DllImport("user32.dll")]
            public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);
            public static void Click() {
                mouse_event(0x02, 0, 0, 0, 0); // down
                mouse_event(0x04, 0, 0, 0, 0); // up
            }
        }
"@
    [MouseClick]::Click()
    Write-Output "Clicked Indicators"
    Start-Sleep 2
    
    # Now the menu should appear. Look for "Add" menu item
    $addCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, "Add"
    )
    $addItem = $atas.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $addCond)
    
    if ($addItem) {
        Write-Output "Found 'Add' menu item"
        $addRect = $addItem.Current.BoundingRectangle
        $addX = [int]($addRect.X + $addRect.Width / 2)
        $addY = [int]($addRect.Y + $addRect.Height / 2)
        Write-Output "Add at ($addX, $addY)"
        
        [System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point($addX, $addY)
        Start-Sleep 0.2
        [MouseClick]::Click()
        Write-Output "Clicked Add"
        Start-Sleep 2
        
        # Now the indicator search dialog should appear. Type the name
        Write-Output "Typing LiveExporter..."
        $wshell.SendKeys("LiveExporter")
        Start-Sleep 1
        $wshell.SendKeys("{Enter}")
        Write-Output "Done!"
    } else {
        Write-Output "'Add' menu item not found"
    }
} else {
    Write-Output "Indicators text not found"
}

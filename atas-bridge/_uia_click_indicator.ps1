Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$root = [System.Windows.Automation.AutomationElement]::RootElement

# Find ATAS X
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, "ATAS X"
)
$atas = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)

if ($atas -eq $null) { Write-Output "Not found"; exit }

Write-Output "ATAS X found"

# Find "Indicators" button
$indCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, "Indicators"
)
$indBtn = $atas.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $indCond)

if ($indBtn -eq $null) {
    Write-Output "Indicators button not found"
} else {
    Write-Output "Found Indicators button: $($indBtn.Current.BoundingRectangle)"
    # Click it
    $invokePattern = $indBtn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $invokePattern.Invoke()
    Write-Output "Clicked Indicators"
}

Start-Sleep 2

# Now look for the popup menu (Add / Remove / Settings / etc)
# Search for "Add" text in descendant elements
$addCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, "Add"
)
$addItem = $atas.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $addCond)

if ($addItem -eq $null) {
    Write-Output "Add menu item not found"
    # List all visible elements to debug
    Write-Output "=== All visible elements ==="
    $all = $atas.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($el in $all) {
        $n = $el.Current.Name
        if ($n -ne "" -and $n -ne "ATAS X") {
            Write-Output "  $($el.Current.ControlType.ProgrammaticName): '$n'"
        }
    }
} else {
    Write-Output "Found Add menu item"
    $invokePattern = $addItem.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $invokePattern.Invoke()
    Write-Output "Clicked Add"
}

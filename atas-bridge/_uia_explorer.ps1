Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

# Find ATAS X window
$atasWindow = $null
$found = $false

[System.Windows.Automation.Automation]::AddAutomationEventHandler(
    [System.Windows.Automation.WindowPattern]::WindowOpenedEvent,
    [System.Windows.Automation.AutomationElement]::RootElement,
    [System.Windows.Automation.TreeScope]::Subtree,
    $null
)

$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, "ATAS X"
)

$atasWindow = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)

if ($atasWindow -eq $null) {
    Write-Output "ATAS X window not found"
    exit
}

Write-Output "ATAS X window found: $($atasWindow.Current.Name)"

# Get all children recursively
function Get-UIElement($element, $depth=0) {
    $indent = "  " * $depth
    $name = $element.Current.Name
    $ctrlType = $element.Current.ControlType.ProgrammaticName
    $className = $element.Current.ClassName
    Write-Output "${indent}${ctrlType}: '${name}' (Class='${className}')"
    
    if ($depth -ge 5) { return }
    
    $children = $element.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($child in $children) {
        Get-UIElement $child ($depth+1)
    }
}

Get-UIElement $atasWindow

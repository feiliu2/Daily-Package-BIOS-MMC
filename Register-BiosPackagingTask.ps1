[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$At = '15:00',

    [string]$TaskName = 'OakStream-BIOS-Daily-Package',

    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'package_latest_bios.py'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Packaging script not found: $scriptPath"
}
if (-not $PythonPath) {
    $PythonPath = (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source
}
$PythonPath = (Get-Item -LiteralPath $PythonPath -ErrorAction Stop).FullName
$existingTask = Get-ScheduledTask -TaskPath '\' -ErrorAction Stop |
    Where-Object { $_.TaskName -eq $TaskName }
if ($existingTask) {
    throw "Task already exists: $TaskName. Choose another -TaskName or remove it explicitly."
}
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$time = [DateTime]::ParseExact(
    $At, 'HH:mm', [System.Globalization.CultureInfo]::InvariantCulture
)
if ($time -le (Get-Date)) {
    $time = $time.AddDays(1)
}
$action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "-u `"$scriptPath`"" `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $time
$principal = New-ScheduledTaskPrincipal `
    -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)
$task = New-ScheduledTask `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Package the newest BIOS and matching MMC, retain the latest pair and clear C:\ATF after verification; ask the logged-in user before Artifactory upload.'

[pscustomobject]@{
    TaskName = $TaskName
    DailyLocalTime = $At
    FirstRun = $time
    User = $user
    LogonType = 'Interactive (user must be logged on)'
    Python = $PythonPath
    Arguments = $action.Arguments
}

if ($PSCmdlet.ShouldProcess($TaskName, "Register daily BIOS and MMC packaging at $At")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -ErrorAction Stop
}

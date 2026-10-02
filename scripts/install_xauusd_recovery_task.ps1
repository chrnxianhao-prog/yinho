param(
    [Parameter(Mandatory=$true)][string]$PythonwPath,
    [Parameter(Mandatory=$true)][string]$ServiceConfig,
    [switch]$ConfirmDemoAutoRecovery,
    [string]$TaskName = 'XAUUSD-Demo-Recovery-f8bf'
)
$ErrorActionPreference = 'Stop'
if (-not $ConfirmDemoAutoRecovery) { throw '需要明确确认 Demo 故障自动恢复。' }
$xauRoot = Split-Path -Parent $PSScriptRoot
$xauScript = Join-Path $PSScriptRoot 'xauusd_supervisor.py'
$xauPython = (Resolve-Path -LiteralPath $PythonwPath).Path
$xauConfig = (Resolve-Path -LiteralPath $ServiceConfig).Path
if ([IO.Path]::GetFileName($xauPython) -ne 'pythonw.exe') { throw '后台任务必须使用 pythonw.exe，避免弹出终端窗口。' }
$xauSettingsRecord = Get-Content -LiteralPath $xauConfig -Raw | ConvertFrom-Json
if ($xauSettingsRecord.enabled -ne $true -or $xauSettingsRecord.demo_only_confirmed -ne $true) {
    throw '配置未明确允许 Demo 自动恢复。'
}
if ($xauSettingsRecord.workspace -ne $xauRoot) { throw '恢复配置与本工作区不一致。' }
$xauDescription = 'XAUUSD Demo recovery managed by yinho; manual STOPPED intent blocks trading recovery.'
$xauExisting = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($xauExisting) { throw '同名任务已存在；先核验已有任务，不覆盖或重复部署。' }
$xauUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$xauArguments = '-X utf8 "' + $xauScript + '" --service-config "' + $xauConfig + '"'
$xauAction = New-ScheduledTaskAction -Execute $xauPython -Argument $xauArguments -WorkingDirectory $xauRoot
$xauLogin = New-ScheduledTaskTrigger -AtLogOn -User $xauUser
# 进程持续守护；每分钟触发是守护自身退出后的兜底，已有实例时忽略新触发。
$xauRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$xauPrincipal = New-ScheduledTaskPrincipal -UserId $xauUser -LogonType Interactive -RunLevel Limited
$xauTaskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -Hidden -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$xauTask = New-ScheduledTask -Action $xauAction -Trigger @($xauLogin, $xauRepeat) `
    -Principal $xauPrincipal -Settings $xauTaskSettings -Description $xauDescription
Register-ScheduledTask -TaskName $TaskName -InputObject $xauTask | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State

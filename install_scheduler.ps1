# install_scheduler.ps1
# 注册 Windows 任务计划程序：交易日（周一~周五）09:00-15:15，每 5 分钟运行 fetch_eastmoney_plates.py
# 用法：以管理员身份打开 PowerShell，cd 到本目录，执行  .\install_scheduler.ps1
$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
$Py       = "python"   # 若 python 不在 PATH，改成完整路径，如 "C:\Python311\python.exe"
$Script   = Join-Path $ScriptDir "fetch_eastmoney_plates.py"
$TaskName = "WhiteDashbord-EastmoneyPlates"

$action = New-ScheduledTaskAction -Execute $Py -Argument "`"$Script`"" -WorkingDirectory $ScriptDir

# 周一~周五 09:00 起，每 5 分钟重复，持续约 6h15m（覆盖 09:00-15:15）
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:00"
$trigger.Repetition.Interval = [TimeSpan]::FromMinutes(5)
$trigger.Repetition.Duration = [TimeSpan]::FromHours(6.25)

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -RestartCount 2

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force

Write-Host "已注册任务 '$TaskName'。可在 taskschd.msc 查看/手动试运行。"

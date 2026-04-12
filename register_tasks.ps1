$project = "C:\Users\aldair.garcia\OneDrive - Unosquare\Documents\Sports_Betting_Model"
$python  = "C:\Users\aldair.garcia\AppData\Local\Programs\Python\Python313\python.exe"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

# MORNING
$a1 = New-ScheduledTaskAction -Execute $python -Argument """$project\scripts\orchestrator.py"" --mode morning" -WorkingDirectory $project
$t1 = New-ScheduledTaskTrigger -Daily -At "06:00AM"
Register-ScheduledTask -TaskName "BettingModel_Morning" -Action $a1 -Trigger $t1 -Settings $settings -Force | Out-Null
Write-Host "[OK] BettingModel_Morning  06:00 AM diario"

# EVENING
$a2 = New-ScheduledTaskAction -Execute $python -Argument """$project\scripts\orchestrator.py"" --mode evening" -WorkingDirectory $project
$t2 = New-ScheduledTaskTrigger -Daily -At "11:00PM"
Register-ScheduledTask -TaskName "BettingModel_Evening" -Action $a2 -Trigger $t2 -Settings $settings -Force | Out-Null
Write-Host "[OK] BettingModel_Evening  11:00 PM diario"

# WEEKLY
$a3 = New-ScheduledTaskAction -Execute $python -Argument """$project\scripts\orchestrator.py"" --mode weekly" -WorkingDirectory $project
$t3 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "07:00AM"
Register-ScheduledTask -TaskName "BettingModel_Weekly" -Action $a3 -Trigger $t3 -Settings $settings -Force | Out-Null
Write-Host "[OK] BettingModel_Weekly   Lunes 07:00 AM"

Write-Host ""
Get-ScheduledTask -TaskName "BettingModel*" | Get-ScheduledTaskInfo | Select-Object TaskName, LastRunTime, NextRunTime | Format-Table -AutoSize

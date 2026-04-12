# setup_scheduler.ps1
# Registra las 3 tareas automaticas del Sports Betting Model
# Ejecutar con: powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1

$project = "C:\Users\aldair.garcia\OneDrive - Unosquare\Documents\Sports_Betting_Model"
$python  = "C:\Users\aldair.garcia\AppData\Local\Programs\Python\Python313\python.exe"

Write-Host ""
Write-Host "============================================================"
Write-Host " Registrando tareas en Windows Task Scheduler"
Write-Host "============================================================"
Write-Host ""

# TAREA 1: MORNING — 06:00 AM diario
$action1  = New-ScheduledTaskAction -Execute $python -Argument "`"$project\scripts\orchestrator.py`" --mode morning" -WorkingDirectory $project
$trigger1 = New-ScheduledTaskTrigger -Daily -At "06:00AM"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "BettingModel_Morning" -Action $action1 -Trigger $trigger1 -Settings $settings -Force | Out-Null
Write-Host "[OK] BettingModel_Morning  - 06:00 AM diario"

# TAREA 2: EVENING — 23:00 PM diario
$action2  = New-ScheduledTaskAction -Execute $python -Argument "`"$project\scripts\orchestrator.py`" --mode evening" -WorkingDirectory $project
$trigger2 = New-ScheduledTaskTrigger -Daily -At "11:00PM"
Register-ScheduledTask -TaskName "BettingModel_Evening" -Action $action2 -Trigger $trigger2 -Settings $settings -Force | Out-Null
Write-Host "[OK] BettingModel_Evening  - 11:00 PM diario"

# TAREA 3: WEEKLY — Lunes 07:00 AM
$action3  = New-ScheduledTaskAction -Execute $python -Argument "`"$project\scripts\orchestrator.py`" --mode weekly" -WorkingDirectory $project
$trigger3 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "07:00AM"
Register-ScheduledTask -TaskName "BettingModel_Weekly" -Action $action3 -Trigger $trigger3 -Settings $settings -Force | Out-Null
Write-Host "[OK] BettingModel_Weekly   - Lunes 07:00 AM"

Write-Host ""
Write-Host "============================================================"
Write-Host " Verificando tareas registradas:"
Write-Host "============================================================"
Get-ScheduledTask -TaskName "BettingModel*" | Format-Table TaskName, State -AutoSize

Write-Host ""
Write-Host "Comandos utiles:"
Write-Host "  Ver estado:     Get-ScheduledTask -TaskName 'BettingModel*'"
Write-Host "  Correr ahora:   Start-ScheduledTask -TaskName 'BettingModel_Morning'"
Write-Host "  Eliminar:       Unregister-ScheduledTask -TaskName 'BettingModel_Morning' -Confirm:`$false"
Write-Host ""

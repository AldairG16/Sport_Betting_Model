# fix_tasks.ps1
# Ejecutar como Administrador: clic derecho → "Ejecutar con PowerShell como administrador"

$tasks = @('BettingModel_Morning', 'BettingModel_Evening', 'BettingModel_Weekly')

foreach ($name in $tasks) {
    try {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
        $task.Settings.WakeToRun         = $true   # Despertar PC si está dormido
        $task.Settings.StartWhenAvailable = $true   # Ejecutar si se perdió el horario
        Set-ScheduledTask -InputObject $task
        Write-Host "OK: $name actualizado" -ForegroundColor Green
    } catch {
        Write-Host "ERROR en $name : $_" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Verificando configuracion final..." -ForegroundColor Cyan
foreach ($name in $tasks) {
    $s = (Get-ScheduledTask -TaskName $name).Settings
    $i = Get-ScheduledTask -TaskName $name | Get-ScheduledTaskInfo
    Write-Host "$name"
    Write-Host "  WakeToRun:         $($s.WakeToRun)"
    Write-Host "  StartWhenAvailable: $($s.StartWhenAvailable)"
    Write-Host "  NextRunTime:        $($i.NextRunTime)"
    Write-Host ""
}

Read-Host "Presiona Enter para cerrar"

@echo off
:: ============================================================
:: setup_scheduler.bat
:: Registra las tareas automaticas en Windows Task Scheduler
::
:: COMO USAR:
::   1. Edita PROJECT_DIR y PYTHON_EXE con tus rutas reales
::   2. Ejecuta este .bat como Administrador (click derecho > Ejecutar como administrador)
::   3. Verifica con: schtasks /query /tn "BettingModel*" /fo list
:: ============================================================

:: --- CONFIGURACION (edita estas rutas) ---
set PROJECT_DIR=C:\Users\aldair.garcia\OneDrive - Unosquare\Documents\Sports_Betting_Model
set PYTHON_EXE=python

:: Si usas un entorno virtual (venv), reemplaza la linea de arriba con:
:: set PYTHON_EXE=C:\Users\aldair.garcia\OneDrive - Unosquare\Documents\Sports_Betting_Model\venv\Scripts\python.exe

echo.
echo ============================================================
echo  Registrando tareas en Windows Task Scheduler
echo ============================================================
echo.

:: ------------------------------------------------------------
:: TAREA 1: MORNING — 06:00 AM todos los dias
:: fetch odds + predicciones + Telegram
:: ------------------------------------------------------------
schtasks /create /tn "BettingModel_Morning" ^
  /tr "\"%PYTHON_EXE%\" \"%PROJECT_DIR%\scripts\orchestrator.py\" --mode morning" ^
  /sc daily /st 06:00 ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /f

if %errorlevel%==0 (
    echo [OK] BettingModel_Morning - 06:00 AM diario
) else (
    echo [ERROR] No se pudo crear BettingModel_Morning
)

:: ------------------------------------------------------------
:: TAREA 2: EVENING — 23:00 PM todos los dias
:: closing odds + resultados (sin llamada API)
:: ------------------------------------------------------------
schtasks /create /tn "BettingModel_Evening" ^
  /tr "\"%PYTHON_EXE%\" \"%PROJECT_DIR%\scripts\orchestrator.py\" --mode evening" ^
  /sc daily /st 23:00 ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /f

if %errorlevel%==0 (
    echo [OK] BettingModel_Evening - 23:00 PM diario
) else (
    echo [ERROR] No se pudo crear BettingModel_Evening
)

:: ------------------------------------------------------------
:: TAREA 3: WEEKLY — Lunes 07:00 AM
:: Recarga datos historicos frescos
:: ------------------------------------------------------------
schtasks /create /tn "BettingModel_Weekly" ^
  /tr "\"%PYTHON_EXE%\" \"%PROJECT_DIR%\scripts\orchestrator.py\" --mode weekly" ^
  /sc weekly /d MON /st 07:00 ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /f

if %errorlevel%==0 (
    echo [OK] BettingModel_Weekly - Lunes 07:00 AM
) else (
    echo [ERROR] No se pudo crear BettingModel_Weekly
)

echo.
echo ============================================================
echo  Verificando tareas registradas:
echo ============================================================
schtasks /query /tn "BettingModel*" /fo table 2>nul

echo.
echo ============================================================
echo  LISTO. Tareas activas en Windows Task Scheduler.
echo.
echo  Para verificar:  schtasks /query /tn "BettingModel*" /fo list
echo  Para eliminar:   schtasks /delete /tn "BettingModel_Morning" /f
echo  Para ejecutar ya: schtasks /run /tn "BettingModel_Morning"
echo ============================================================
echo.
pause

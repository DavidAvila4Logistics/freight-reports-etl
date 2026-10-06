@echo off
rem Corrida diaria: procesa de una todos los agentes de `lote_diario`
rem (configs\global.yaml). Un agente que falla no frena a los demas.
rem
rem ANTES de correr: dejar los reportes del dia en data\raw\<agente>\
rem Para probar con datos ficticios: python tools\generar_datos_demo.py
rem
rem Uso: run_all.bat [opciones extra de main.py]
rem Ej.: run_all.bat --fecha-referencia 15/09/2026
rem      run_all.bat /nopause --sin-crudo-avisa     (para el Programador de tareas)
cd /d "%~dp0"
set PAUSA=1
if /i "%~1"=="/nopause" (
    set PAUSA=0
    shift
)
python src\main.py --lote %1 %2 %3 %4 %5 %6 %7 %8 %9
set RC=%ERRORLEVEL%
echo.
if "%PAUSA%"=="1" pause
exit /b %RC%

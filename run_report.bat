@echo off
rem Corre un solo agente.
rem Uso: run_report.bat <agente> [opciones extra de main.py]
rem Ej.:  run_report.bat air_forwarder
rem       run_report.bat parts_shipper --fecha-referencia 15/09/2026
rem Para correr todos los de la corrida diaria de una: run_all.bat
if "%~1"=="" (
    echo Uso: run_report.bat ^<agente^>  (air_forwarder ^| ocean_forwarder ^| transit_report ^| parts_shipper^)
    pause
    exit /b 1
)
cd /d "%~dp0"
python src\main.py --agente %*
echo.
pause

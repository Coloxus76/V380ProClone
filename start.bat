@echo off
title V380 Pro Clone - Servidor Local Sin Publicidad
cd /d "%~dp0"

echo ==========================================================
echo   V380 PRO CLONE - LOCAL STREAM & DVR (SIN PUBLICIDAD)
echo   Camara IP: 192.168.1.20
echo   Dispositivo: 102710996 (Macro-Video IPCamera)
echo ==========================================================
echo.
echo Iniciando servidor en http://localhost:8080 ...
echo Abriendo navegador en 3 segundos...

start "" http://localhost:8080

python backend.py
if %ERRORLEVEL% NEQ 0 (
    echo Error al iniciar backend con python. Presione una tecla para salir...
    pause
)

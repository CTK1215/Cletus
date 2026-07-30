@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM ------------------------------------------------------------
REM  Cletus launcher
REM  Starts voice service (minimized), waits, then opens the HUD.
REM  When the HUD closes, kills the voice service by its PID file.
REM ------------------------------------------------------------

if not exist logs mkdir logs

echo.
echo  Starting Cletus...
echo.

REM --- start voice service in a minimized console, log to file
start "Cletus Voice" /MIN cmd /c "voice\venv\Scripts\python.exe voice\main.py > logs\voice.log 2>&1"

REM --- give it a beat to load models
echo  Loading voice models, holdin' on a few seconds...
timeout /t 6 /nobreak > NUL

echo  Opening HUD...
echo.
cd hud
call npm start

REM --- HUD closed, stop the voice service
cd /d "%~dp0"
echo.
echo  Shutting down voice service...

if exist "voice\cletus.pid" (
    set /p VOICE_PID=<voice\cletus.pid
    taskkill /F /PID !VOICE_PID! > NUL 2>&1
    del voice\cletus.pid > NUL 2>&1
) else (
    REM fallback: close the minimized voice window
    taskkill /F /FI "WindowTitle eq Cletus Voice*" > NUL 2>&1
)

echo  Cletus stopped.
timeout /t 2 /nobreak > NUL
endlocal

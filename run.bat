@echo off
rem ============================================================
rem  PC Control Agent - daemon launcher (elevated)
rem  Self-elevate: if not admin, pop one UAC then relaunch.
rem  Running elevated lets SendInput cross UIPI so the touchpad
rem  can drive HIGH-integrity (admin) windows' menus too
rem  (e.g. i4Tools / Aisi Assistant tray menu).
rem  Close this window to stop the daemon.
rem ============================================================
set PY=C:/Users/zhw19/.workbuddy/binaries/python/envs/default/Scripts/python.exe
set DIR=%~dp0
cd /d "%DIR%"

rem --- privilege check: not admin -> self-elevate once ---
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem --- admin branch: free port 8765 from any older (low-priv) instance ---
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /i ":8765" ^| findstr /i "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo [%date% %time%] daemon started ELEVATED >> "%DIR%agent.log"

echo ============================================================
echo   PC Control Agent daemon (ELEVATED) running...
echo   close window to stop
echo ============================================================

:loop
echo [%date% %time%] starting agent (elevated)... >> "%DIR%agent.log"
"%PY%" -u "%DIR%agent.py" >> "%DIR%agent.log" 2>&1
echo [%date% %time%] agent exited(code=%errorlevel%) >> "%DIR%agent.log"
rem if another instance already holds 8765, stop fighting and exit
netstat -ano 2>nul | findstr /i ":8765" | findstr /i "LISTENING" >nul 2>&1
if %errorLevel% equ 0 (
    echo [%date% %time%] 8765 held by another instance, daemon exiting >> "%DIR%agent.log"
    goto :eof
)
echo [%date% %time%] restart in 3s... >> "%DIR%agent.log"
timeout /t 3 /nobreak >nul
goto loop

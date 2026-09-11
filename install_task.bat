@echo off
rem ============================================================
rem  Install PCControlAgent as an elevated scheduled task that
rem  auto-starts at logon (no UAC prompt after install).
rem  Run once (double-click -> approve UAC). Self-elevates.
rem  Also starts the elevated daemon immediately (no 2nd UAC),
rem  because we are already elevated here.
rem ============================================================
set DIR=%~dp0
cd /d "%DIR%"
set LOG="%DIR%install_task.log"

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -Wait"
    exit /b
)

echo [%date% %time%] install start (ELEVATED) >> %LOG%

schtasks /Create /TN "PCControlAgent" /TR "\"%DIR%run.bat\"" /SC ONLOGON /RL HIGHEST /F >> %LOG% 2>&1
if not errorlevel 1 (
    echo [%date% %time%] [OK] scheduled task PCControlAgent created (elevated, at logon) >> %LOG%
) else (
    echo [%date% %time%] [FAIL] schtasks /Create failed >> %LOG%
)

echo [%date% %time%] starting elevated daemon directly... >> %LOG%
start "" "%DIR%run.bat"

rem give the daemon a moment to bind, then clean up any leftover
rem low-priv run.bat / agent still fighting for the port
timeout /t 2 >nul
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /i ":8765" ^| findstr /i "LISTENING"') do set APID=%%a
if defined APID (
    for /f "skip=1" %%b in ('wmic process where "ProcessId=%APID%" get ParentProcessId 2^>nul') do set PPID=%%b
    powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*run.bat*' -and $_.ProcessId -ne %PPID%) -or ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*agent.py*' -and $_.ProcessId -ne %APID%) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >> %LOG% 2>&1
    echo [%date% %time%] [OK] cleanup done (APID=%APID% PPID=%PPID%) >> %LOG%
) else (
    echo [%date% %time%] [WARN] no listener on 8765 after start >> %LOG%
)
echo [%date% %time%] install finished >> %LOG%
timeout /t 4 >nul

@echo off
REM Khoi dong Flat Studio: server web + autoupdate git + mo trinh duyet.
cd /d "%~dp0"
call "%~dp0_env.bat"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8770"

REM Tu cap nhat code moi tu git remote moi 60s (chay autoupdate.sh qua Git Bash o nen).
where bash >nul 2>&1 && start "" /b bash autoupdate.sh

REM Mo trinh duyet sau 2s (cho server len).
start "" /b powershell -NoProfile -Command "Start-Sleep 2; Start-Process 'http://127.0.0.1:%PORT%'"

python server.py --port %PORT%

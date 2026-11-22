@echo off
setlocal
cd /d "%~dp0"
node -e "process.exit(Number(process.versions.node.split('.')[0]) < 24 ? 1 : 0)" >nul 2>&1
if errorlevel 1 goto need_node
if not exist node_modules\.bin\electron.cmd call npm ci
if errorlevel 1 goto failed
call npm start
if errorlevel 1 goto failed
exit /b 0

:need_node
echo Install Node.js 24 or later: https://nodejs.org/
:failed
pause
exit /b 1

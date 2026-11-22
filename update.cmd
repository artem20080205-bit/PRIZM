@echo off
setlocal
cd /d "%~dp0"
node -e "process.exit(Number(process.versions.node.split('.')[0]) < 24 ? 1 : 0)" >nul 2>&1
if errorlevel 1 goto need_node
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 goto need_git
git diff --quiet HEAD --
if errorlevel 1 goto local_changes
git pull --ff-only
if errorlevel 1 goto failed
call npm ci
if errorlevel 1 goto failed
echo Update complete. Launch start.cmd.
exit /b 0

:need_node
echo Install Node.js 24 or later: https://nodejs.org/
goto failed
:need_git
echo Install Git and run this script from a git clone of PRIZM.
goto failed
:local_changes
echo Save your local code changes before updating.
:failed
pause
exit /b 1

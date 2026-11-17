@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
py -3.13 -c "import sys" >nul 2>&1
if errorlevel 1 goto python312
py -3.13 update.py
goto finished
:python312
py -3.12 update.py
:finished
set result=%errorlevel%
if not "%result%"=="0" pause
exit /b %result%

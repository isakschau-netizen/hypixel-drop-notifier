@echo off
cd /d "%~dp0"
rem Use "python" if it is on PATH, otherwise the "py" launcher that the
rem python.org installer adds by default.
set "PY=python"
python --version >nul 2>&1 || set "PY=py -3"
%PY% --version >nul 2>&1 || goto nopython
%PY% hypixel_drop_notifier.py %*
pause
exit /b

:nopython
echo Python 3 was not found.
echo Install it from https://www.python.org/downloads/ then run this again.
pause
exit /b 1

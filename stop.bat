@echo off
rem Stops only the notifier's python process, nothing else.
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*hypixel_drop_notifier.py*' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; 'Stopped PID ' + $_.ProcessId } } else { 'Notifier is not running.' }"
pause

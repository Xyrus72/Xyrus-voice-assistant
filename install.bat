@echo off
rem Xyrus installer. Double-click it, or pass setup options, e.g.:  install.bat -NoStartup -ModelSource D:\arc\model
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
echo.
pause

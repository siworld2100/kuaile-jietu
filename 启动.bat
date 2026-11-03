@echo off
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw -3.12 screenshot_float.py
) else (
  start "" /min py -3.12 screenshot_float.py
)
